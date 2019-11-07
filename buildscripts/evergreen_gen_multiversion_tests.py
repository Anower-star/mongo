#!/usr/bin/env python3
"""Generate multiversion tests to run in evergreen in parallel."""

import datetime
from datetime import timedelta
import logging
import os

from collections import namedtuple

import click
import structlog
import yaml

from evergreen.api import RetryingEvergreenApi
from shrub.config import Configuration
from shrub.command import CommandDefinition
from shrub.task import TaskDependency
from shrub.variant import DisplayTaskDefinition
from shrub.variant import TaskSpec

import buildscripts.util.read_config as read_config
import buildscripts.util.taskname as taskname
import buildscripts.evergreen_generate_resmoke_tasks as generate_resmoke

LOGGER = structlog.getLogger(__name__)

REQUIRED_CONFIG_KEYS = {
    "build_variant", "fallback_num_sub_suites", "project", "task_id", "task_name",
    "use_multiversion"
}

DEFAULT_CONFIG_VALUES = generate_resmoke.DEFAULT_CONFIG_VALUES
CONFIG_DIR = DEFAULT_CONFIG_VALUES["generated_config_dir"]
DEFAULT_CONFIG_VALUES["is_sharded"] = False
TEST_SUITE_DIR = DEFAULT_CONFIG_VALUES["test_suites_dir"]
CONFIG_FILE = generate_resmoke.CONFIG_FILE
CONFIG_FORMAT_FN = generate_resmoke.CONFIG_FORMAT_FN
REPL_MIXED_VERSION_CONFIGS = ["new-old-new", "new-new-old", "old-new-new"]
SHARDED_MIXED_VERSION_CONFIGS = ["new-old-old-new"]

BURN_IN_TASK = "burn_in_tests_multiversion"
BURN_IN_CONFIG_KEY = "use_in_multiversion_burn_in_tests"
PASSTHROUGH_TAG = "multiversion_passthrough"


def prepare_directory_for_suite(directory):
    """Ensure that directory exists."""
    if not os.path.exists(directory):
        os.makedirs(directory)


def is_suite_sharded(suite_dir, suite_name):
    """Return true if a suite uses ShardedClusterFixture."""
    source_config = generate_resmoke.read_yaml(suite_dir, suite_name + ".yml")
    return source_config["executor"]["fixture"]["class"] == "ShardedClusterFixture"


def update_suite_config_for_multiversion_replset(suite_config):
    """Update suite_config with arguments for multiversion tests using ReplicaSetFixture."""
    suite_config["executor"]["fixture"]["num_nodes"] = 3
    suite_config["executor"]["fixture"]["linear_chain"] = True


def update_suite_config_for_multiversion_sharded(suite_config):
    """Update suite_config with arguments for multiversion tests using ShardedClusterFixture."""

    fixture_config = suite_config["executor"]["fixture"]
    default_shards = "default_shards"
    default_num_nodes = "default_nodes"
    base_num_shards = (default_shards if "num_shards" not in fixture_config
                       or not fixture_config["num_shards"] else fixture_config["num_shards"])
    base_num_rs_nodes_per_shard = (default_num_nodes
                                   if "num_rs_nodes_per_shard" not in fixture_config
                                   or not fixture_config["num_rs_nodes_per_shard"] else
                                   fixture_config["num_rs_nodes_per_shard"])

    if base_num_shards is not default_shards or base_num_rs_nodes_per_shard is not default_num_nodes:
        num_shard_num_nodes_pair = "{}-{}".format(base_num_shards, base_num_rs_nodes_per_shard)
        assert num_shard_num_nodes_pair in {"default_shards-2", "2-default_nodes", "2-3"}, \
               "The multiversion suite runs sharded clusters with 2 shards and 2 nodes per shard. "\
               " acceptable, please add '{}' to this assert.".format(num_shard_num_nodes_pair)

    suite_config["executor"]["fixture"]["num_shards"] = 2
    suite_config["executor"]["fixture"]["num_rs_nodes_per_shard"] = 2


class MultiversionConfig(object):
    """An object containing the configurations to generate and run the multiversion tests with."""

    def __init__(self, update_yaml, version_configs):
        """Create new MultiversionConfig object."""
        self.update_yaml = update_yaml
        self.version_configs = version_configs


class EvergreenConfigGenerator(object):
    """Generate evergreen configurations for multiversion tests."""

    def __init__(self, evg_api, evg_config, options):
        """Create new EvergreenConfigGenerator object."""
        self.evg_api = evg_api
        self.evg_config = evg_config
        self.options = options
        self.task_names = []
        self.task_specs = []
        # Strip the "_gen" suffix appended to the name of tasks generated by evergreen.
        self.task = generate_resmoke.remove_gen_suffix(self.options.task)

    def _generate_sub_task(self, mixed_version_config, task, task_index, suite, num_suites,
                           burn_in_test=None):
        # pylint: disable=too-many-arguments
        """Generate a sub task to be run with the provided suite and  mixed version config."""

        # Create a sub task name appended with the task_index and build variant name.
        task_name = "{0}_{1}".format(task, mixed_version_config)
        sub_task_name = taskname.name_generated_task(task_name, task_index, num_suites,
                                                     self.options.variant)
        self.task_names.append(sub_task_name)
        self.task_specs.append(TaskSpec(sub_task_name))
        task = self.evg_config.task(sub_task_name)

        gen_task_name = BURN_IN_TASK if burn_in_test is not None else self.task

        commands = [
            CommandDefinition().function("do setup"),
            # Fetch and download the proper mongod binaries before running multiversion tests.
            CommandDefinition().function("do multiversion setup")
        ]
        exclude_tags = "requires_fcv_44,multiversion_incompatible"
        # TODO(SERVER-43306): Remove --dryRun command line option once we start turning on
        #  multiversion tests.
        run_tests_vars = {
            "resmoke_args":
                "{0} --suite={1} --mixedBinVersions={2} --excludeWithAnyTags={3} --dryRun=tests ".
                format(self.options.resmoke_args, suite, mixed_version_config, exclude_tags),
            "task":
                gen_task_name,
        }
        if burn_in_test is not None:
            run_tests_vars["resmoke_args"] += burn_in_test

        commands.append(CommandDefinition().function("run generated tests").vars(run_tests_vars))
        task.dependency(TaskDependency("compile")).commands(commands)

    def _write_evergreen_config_to_file(self, task_name):
        """Save evergreen config to file."""
        if not os.path.exists(CONFIG_DIR):
            os.makedirs(CONFIG_DIR)

        with open(os.path.join(CONFIG_DIR, task_name + ".json"), "w") as file_handle:
            file_handle.write(self.evg_config.to_json())

    def create_display_task(self, task_name, task_specs, task_list):
        """Create the display task definition for the MultiversionConfig object."""
        dt = DisplayTaskDefinition(task_name).execution_tasks(task_list)\
            .execution_task("{0}_gen".format(task_name))
        self.evg_config.variant(self.options.variant).tasks(task_specs).display_task(dt)

    def _generate_burn_in_execution_tasks(self, config, suites, burn_in_test, burn_in_idx):
        burn_in_prefix = "burn_in_multiversion"
        task = "{0}:{1}".format(burn_in_prefix, self.task)

        for version_config in config.version_configs:
            # For burn in tasks, it doesn't matter which generated suite yml to use as all the
            # yaml configurations are the same.
            source_suite = os.path.join(CONFIG_DIR, suites[0].name + ".yml")
            self._generate_sub_task(version_config, task, burn_in_idx, source_suite, 1,
                                    burn_in_test)
        return self.evg_config

    def generate_evg_tasks(self, burn_in_test=None, burn_in_idx=0):
        """
        Generate evergreen tasks for multiversion tests.

        The number of tasks generated equals
        (the number of version configs) * (the number of generated suites).

        :param burn_in_test: The test to be run as part of the burn in multiversion suite.
        """
        idx = 0
        # Divide tests into suites based on run-time statistics for the last
        # LOOKBACK_DURATION_DAYS. Tests without enough run-time statistics will be placed
        # in the misc suite.
        gen_suites = generate_resmoke.GenerateSubSuites(self.evg_api, self.options)
        end_date = datetime.datetime.utcnow().replace(microsecond=0)
        start_date = end_date - datetime.timedelta(days=generate_resmoke.LOOKBACK_DURATION_DAYS)
        suites = gen_suites.calculate_suites(start_date, end_date)
        # Render the given suites into yml files that can be used by resmoke.py.
        if is_suite_sharded(TEST_SUITE_DIR, self.options.suite):
            config = MultiversionConfig(update_suite_config_for_multiversion_sharded,
                                        SHARDED_MIXED_VERSION_CONFIGS)
        else:
            config = MultiversionConfig(update_suite_config_for_multiversion_replset,
                                        REPL_MIXED_VERSION_CONFIGS)
        config_file_dict = generate_resmoke.render_suite_files(
            suites, self.options.suite, gen_suites.test_list, TEST_SUITE_DIR, config.update_yaml)
        generate_resmoke.write_file_dict(CONFIG_DIR, config_file_dict)

        if burn_in_test is not None:
            # Generate the subtasks to run burn_in_test against the appropriate mixed version
            # configurations. The display task is defined later as part of generating the burn
            # in tests.
            self._generate_burn_in_execution_tasks(config, suites, burn_in_test, burn_in_idx)
            return self.evg_config

        for version_config in config.version_configs:
            for suite in suites:
                # Generate the newly divided test suites
                source_suite = os.path.join(CONFIG_DIR, suite.name + ".yml")
                self._generate_sub_task(version_config, self.task, idx, source_suite, len(suites))
                idx += 1

            # Also generate the misc task.
            misc_suite_name = "{0}_misc".format(self.options.suite)
            source_suite = os.path.join(CONFIG_DIR, misc_suite_name + ".yml")
            self._generate_sub_task(version_config, self.task, idx, source_suite, 1)
            idx += 1
        self.create_display_task(self.task, self.task_specs, self.task_names)
        return self.evg_config

    def run(self):
        """Generate and run multiversion suites that run within a specified target execution time."""
        if not generate_resmoke.should_tasks_be_generated(self.evg_api, self.options.task_id):
            LOGGER.info("Not generating configuration due to previous successful generation.")
            return
        self.generate_evg_tasks()
        self._write_evergreen_config_to_file(self.task)


@click.command()
@click.option("--expansion-file", type=str, required=True,
              help="Location of expansions file generated by evergreen.")
@click.option("--evergreen-config", type=str, default=CONFIG_FILE,
              help="Location of evergreen configuration file.")
def main(expansion_file, evergreen_config=None):
    """
    Create a configuration for generate tasks to create sub suites for the specified resmoke suite.

    Tests using ReplicaSetFixture will be generated to use 3 nodes and linear_chain=True.
    Tests using ShardedClusterFixture will be generated to use 2 shards with 2 nodes each.
    The different binary version configurations tested are stored in REPL_MIXED_VERSION_CONFIGS
    and SHARDED_MIXED_VERSION_CONFIGS.

    The `--expansion-file` should contain all the configuration needed to generate the tasks.
    \f
    :param expansion_file: Configuration file.
    :param evergreen_config: Evergreen configuration file.
    """
    evg_api = RetryingEvergreenApi.get_api(config_file=evergreen_config)
    prepare_directory_for_suite(CONFIG_DIR)
    evg_config = Configuration()
    config_options = generate_resmoke.ConfigOptions.from_file(
        expansion_file, REQUIRED_CONFIG_KEYS, DEFAULT_CONFIG_VALUES, CONFIG_FORMAT_FN)
    config_generator = EvergreenConfigGenerator(evg_api, evg_config, config_options)
    config_generator.run()


if __name__ == '__main__':
    main()  # pylint: disable=no-value-for-parameter
