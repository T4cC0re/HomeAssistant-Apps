"""Check configuration merge and proxy startup behavior."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

import yaml

import run


ROOT = Path(__file__).resolve().parents[1]


class DeepMergeTests(unittest.TestCase):
    def test_nested_values_keep_default_siblings(self):
        defaults = {
            "general_settings": {"master_key": "default", "disable_spend_logs": True},
            "router_settings": {"retry_policy": {"TimeoutErrorRetries": 2, "RateLimitErrorRetries": 3}},
        }
        supplied = {
            "general_settings": {"master_key": "override"},
            "router_settings": {"retry_policy": {"TimeoutErrorRetries": 0}},
        }

        self.assertEqual(
            run.deep_merge(defaults, supplied),
            {
                "general_settings": {"master_key": "override", "disable_spend_logs": True},
                "router_settings": {"retry_policy": {"TimeoutErrorRetries": 0, "RateLimitErrorRetries": 3}},
            },
        )

    def test_lists_replace_defaults(self):
        defaults = {"model_list": [{"model_name": "default"}], "nested": {"values": [1, 2]}}
        supplied = {"model_list": [{"model_name": "supplied"}], "nested": {"values": []}}

        self.assertEqual(run.deep_merge(defaults, supplied), supplied)

    def test_false_zero_and_null_replace_defaults(self):
        self.assertEqual(
            run.deep_merge({"enabled": True, "retries": 3, "limit": 10}, {"enabled": False, "retries": 0, "limit": None}),
            {"enabled": False, "retries": 0, "limit": None},
        )

    def test_mappings_and_other_values_can_replace_each_other(self):
        for default, supplied in [({"key": 1}, None), (1, {"key": 2}), ({"key": 1}, []), ([], {"key": 2})]:
            with self.subTest(default=default, supplied=supplied):
                self.assertEqual(run.deep_merge({"setting": default}, {"setting": supplied}), {"setting": supplied})

    def test_root_values_follow_supplied_type_and_value(self):
        for default, supplied in [
            ({"nested": [1]}, None), (None, {"nested": [2]}),
            ({"nested": [1]}, []), ([1], {"nested": [2]}),
            ([1, 2], [3]), (True, False), (3, 0), (None, None),
        ]:
            with self.subTest(default=default, supplied=supplied):
                self.assertEqual(run.deep_merge(default, supplied), supplied)

    def test_unilateral_nested_keys_are_preserved(self):
        defaults = {"only_default": {"values": [1]}, "shared": {"default_child": {"value": 2}}}
        supplied = {"only_supplied": {"values": [3]}, "shared": {"supplied_child": {"value": 4}}}

        self.assertEqual(run.deep_merge(defaults, supplied), {
            "only_default": {"values": [1]},
            "only_supplied": {"values": [3]},
            "shared": {"default_child": {"value": 2}, "supplied_child": {"value": 4}},
        })

    def test_missing_keys_and_explicit_null_have_different_results(self):
        self.assertEqual(
            run.deep_merge({"keep": {"value": 1}, "clear": {"value": 2}}, {"clear": None, "new": None}),
            {"keep": {"value": 1}, "clear": None, "new": None},
        )

    def test_merge_does_not_change_or_share_input_values(self):
        defaults = {"nested": {"keep": [1], "replace": [2]}}
        supplied = {"nested": {"replace": [3]}, "extra": {"values": [4]}}
        original_defaults = deepcopy(defaults)
        original_supplied = deepcopy(supplied)

        result = run.deep_merge(defaults, supplied)
        result["nested"]["keep"].append(5)
        result["nested"]["replace"].append(6)
        result["extra"]["values"].append(7)

        self.assertEqual(defaults, original_defaults)
        self.assertEqual(supplied, original_supplied)


class ParseConfigTests(unittest.TestCase):
    def test_empty_text_is_an_empty_mapping(self):
        self.assertEqual(run.parse_config(" \n\t", "config"), {})

    def test_rejects_non_mapping_roots(self):
        for text in ["null", "false", "1", "text", "[1, 2]", "# Comment only\n"]:
            with self.subTest(text=text):
                with self.assertRaisesRegex(run.ConfigurationError, "must contain a YAML mapping"):
                    run.parse_config(text, "config")

    def test_invalid_yaml_error_does_not_expose_values(self):
        secret = "private-api-key-123"
        with self.assertRaises(run.ConfigurationError) as caught:
            run.parse_config(f"api_key: {secret}\nmodels: [\n", "config")

        self.assertIn("invalid YAML", str(caught.exception))
        self.assertIn("line", str(caught.exception))
        self.assertNotIn(secret, str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)

    def test_rejects_multiple_yaml_documents(self):
        with self.assertRaisesRegex(run.ConfigurationError, "invalid YAML"):
            run.parse_config("one: 1\n---\ntwo: 2\n", "config")

    def test_rejects_python_yaml_tags(self):
        with self.assertRaisesRegex(run.ConfigurationError, "invalid YAML"):
            run.parse_config("value: !!python/name:os.system", "config")


class PrepareStartupTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.options_path = self.path / "options.json"

    def prepare(self, options):
        if isinstance(options, dict):
            options = {"master_key": "option-key", "server_root_path": "/v1", **options}
        self.options_path.write_text(json.dumps(options), encoding="utf-8")
        self.startup = run.prepare_startup(self.options_path, self.path)
        return self.startup[0]

    def read_config(self, command):
        return yaml.safe_load(Path(command[command.index("--config") + 1]).read_text(encoding="utf-8"))

    def test_command_uses_container_port_and_private_config_file(self):
        command = self.prepare({"litellm_config": "general_settings:\n  master_key: user-key\n"})
        config_path = Path(command[-1])

        self.assertEqual(command[:-1], ["litellm", "--host", "0.0.0.0", "--port", "4000", "--config"])
        self.assertEqual(config_path.parent, self.path)
        self.assertEqual(stat.S_IMODE(config_path.stat().st_mode), 0o600)
        self.assertEqual(
            self.read_config(command),
            {
                "general_settings": {"store_model_in_db": False, "disable_spend_logs": True, "master_key": "option-key"},
                "model_list": [],
            },
        )
        self.assertNotIn("user-key", " ".join(command))
        self.assertNotIn("option-key", " ".join(command))
        self.assertEqual(self.startup[1:], ("option-key", "/v1"))

    def test_master_key_option_is_required(self):
        self.options_path.write_text('{"server_root_path": "/v1"}', encoding="utf-8")

        with self.assertRaisesRegex(run.ConfigurationError, "master_key"):
            run.prepare_startup(self.options_path, self.path)

    def test_server_root_path_option_is_required(self):
        self.options_path.write_text('{"master_key": "selected-key"}', encoding="utf-8")

        with self.assertRaisesRegex(run.ConfigurationError, "server_root_path"):
            run.prepare_startup(self.options_path, self.path)

    def test_master_key_must_be_nonempty_literal_text(self):
        for key in [None, "", " \t", 0, False, [], {}, "os.environ/PRIVATE_KEY"]:
            with self.subTest(key=key):
                with self.assertRaises(run.ConfigurationError) as caught:
                    self.prepare({"master_key": key})
                self.assertIn("master_key", str(caught.exception))
                self.assertNotIn("PRIVATE_KEY", str(caught.exception))

    def test_master_key_option_overrides_yaml_values_and_preserves_siblings(self):
        for yaml_key in ["yaml-key", None, "os.environ/OTHER_KEY"]:
            with self.subTest(yaml_key=yaml_key):
                command = self.prepare({
                    "master_key": "selected-key",
                    "litellm_config": yaml.safe_dump({"general_settings": {"master_key": yaml_key, "custom_setting": 42}}),
                })
                self.assertEqual(self.read_config(command)["general_settings"], {
                    "master_key": "selected-key", "custom_setting": 42,
                    "store_model_in_db": False, "disable_spend_logs": True,
                })
                self.assertEqual(self.startup[1], "selected-key")

    def test_master_key_ignores_process_and_yaml_environment(self):
        os.environ["LITELLM_MASTER_KEY"] = "process-key"
        command = self.prepare({
            "master_key": "selected-key",
            "litellm_config": "environment_variables:\n  LITELLM_MASTER_KEY: yaml-key\n",
        })

        self.assertEqual(self.read_config(command)["general_settings"]["master_key"], "selected-key")
        self.assertEqual(self.startup[1], "selected-key")

    def test_non_mapping_general_settings_is_replaced_by_key_override(self):
        for settings in [None, [], False, 0, "", "invalid"]:
            with self.subTest(settings=settings):
                command = self.prepare({"litellm_config": yaml.safe_dump({"general_settings": settings})})
                self.assertEqual(self.read_config(command)["general_settings"], {"master_key": "option-key"})

    def test_default_options_prepare_a_config_without_a_database(self):
        command = self.prepare({})
        config = self.read_config(command)

        self.assertEqual(command[command.index("--port") + 1], "4000")
        self.assertFalse(config["general_settings"]["store_model_in_db"])
        self.assertTrue(config["general_settings"]["disable_spend_logs"])
        self.assertNotIn("database_url", config["general_settings"])
        self.assertNotIn("environment_variables", config)
        self.assertEqual(os.environ["SERVER_ROOT_PATH"], "/v1")

    def test_custom_root_path_is_exported_and_returned(self):
        command = self.prepare({"server_root_path": "/proxy/v2"})

        self.assertNotIn("environment_variables", self.read_config(command))
        self.assertEqual(os.environ["SERVER_ROOT_PATH"], "/proxy/v2")
        self.assertEqual(self.startup[2], "/proxy/v2")

    def test_yaml_environment_is_preserved_without_overriding_root_option(self):
        command = self.prepare({
            "server_root_path": "/option",
            "litellm_config": "environment_variables:\n  SERVER_ROOT_PATH: /yaml\n  BACKEND_KEY: private-key\n",
        })

        self.assertEqual(
            self.read_config(command)["environment_variables"],
            {"SERVER_ROOT_PATH": "/yaml", "BACKEND_KEY": "private-key"},
        )
        self.assertEqual(os.environ["SERVER_ROOT_PATH"], "/option")
        self.assertEqual(self.startup[2], "/option")

    def test_root_path_can_be_empty_or_root(self):
        for root_path in ["", "/"]:
            with self.subTest(root_path=root_path):
                command = self.prepare({"server_root_path": root_path})
                self.assertNotIn("environment_variables", self.read_config(command))
                self.assertEqual(os.environ["SERVER_ROOT_PATH"], root_path)
                self.assertEqual(self.startup[2], root_path)

    def test_invalid_root_path_options_are_rejected_without_values(self):
        invalid_paths = [None, 0, False, [], {}, "v1", "/v1/", "/v1?key=private", "/v1#private", "/a b", "//v1", "/a//b", "/a/../b", "/./v1"]
        for root_path in invalid_paths:
            with self.subTest(root_path=root_path):
                with self.assertRaises(run.ConfigurationError) as caught:
                    self.prepare({"server_root_path": root_path})
                self.assertNotIn("private", str(caught.exception))
                self.assertNotIn("SERVER_ROOT_PATH", os.environ)

    def test_yaml_environment_values_are_preserved_without_affecting_root_option(self):
        for environment in [None, [], False, 0, "", "invalid", {"SERVER_ROOT_PATH": "/not/a/valid/path?value"}]:
            with self.subTest(environment=environment):
                command = self.prepare({"litellm_config": yaml.safe_dump({"environment_variables": environment})})
                self.assertEqual(self.read_config(command)["environment_variables"], environment)
                self.assertEqual(os.environ["SERVER_ROOT_PATH"], "/v1")
                self.assertEqual(self.startup[2], "/v1")

    def test_old_port_option_cannot_change_container_port(self):
        command = self.prepare({"port": 8124})

        self.assertEqual(command[command.index("--port") + 1], "4000")

    def test_manifest_matches_listener_and_leaves_host_mapping_disabled(self):
        manifest = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
        self.options_path.write_text(json.dumps(manifest["options"]), encoding="utf-8")
        command, master_key, root_path = run.prepare_startup(self.options_path, self.path)
        port = command[command.index("--port") + 1]

        self.assertEqual(manifest["ports"], {f"{port}/tcp": None})
        self.assertFalse(manifest.get("host_network", False))
        self.assertNotIn("port", manifest["options"])
        self.assertNotIn("port", manifest["schema"])
        self.assertEqual(master_key, "changeme1")
        self.assertEqual(self.read_config(command)["general_settings"]["master_key"], master_key)
        self.assertEqual(root_path, manifest["options"]["server_root_path"])
        self.assertEqual(os.environ["SERVER_ROOT_PATH"], root_path)

    def test_config_must_be_yaml_text(self):
        for config in [{}, [], None, 42, True]:
            with self.subTest(config=config):
                with self.assertRaisesRegex(run.ConfigurationError, "litellm_config must be YAML text"):
                    self.prepare({"litellm_config": config})

    def test_options_must_be_a_mapping(self):
        for options in [[], None, "text", 4000, True]:
            with self.subTest(options=options):
                with self.assertRaisesRegex(run.ConfigurationError, "options must contain a mapping"):
                    self.prepare(options)

    def test_invalid_json_error_does_not_expose_values(self):
        secret = "private-json-key-123"
        self.options_path.write_text('{"api_key": "' + secret + '",}', encoding="utf-8")

        with self.assertRaises(run.ConfigurationError) as caught:
            run.prepare_startup(self.options_path, self.path)

        self.assertEqual(str(caught.exception), "The app options contain invalid JSON.")
        self.assertNotIn(secret, str(caught.exception))

    def test_invalid_supplied_config_does_not_create_runtime_config(self):
        with self.assertRaisesRegex(run.ConfigurationError, "litellm_config contains invalid YAML"):
            self.prepare({"litellm_config": "api_key: private-key\nmodel_list: ["})

        self.assertEqual(list(self.path.glob("litellm-*.yaml")), [])

    def test_sample_config_keeps_credentials_and_model_options(self):
        supplied_text = (Path(__file__).parent / "fixtures" / "litellm.yaml").read_text(encoding="utf-8")
        supplied = yaml.safe_load(supplied_text)
        command = self.prepare({"litellm_config": supplied_text})
        config = self.read_config(command)

        self.assertEqual(config["model_list"], supplied["model_list"])
        self.assertEqual(config["credential_list"], supplied["credential_list"])
        self.assertEqual(config["general_settings"]["master_key"], "option-key")
        self.assertFalse(config["general_settings"]["store_model_in_db"])
        self.assertTrue(config["general_settings"]["disable_spend_logs"])


class MainTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def test_discovery_uses_selected_key_and_finishes_before_exec(self):
        os.environ["SUPERVISOR_TOKEN"] = "private-token"
        command = ["litellm", "--port", "4000", "--config", "/tmp/config.yaml"]
        startup = (command, "selected-key", "/proxy")
        calls = []
        output = io.StringIO()

        def publish_with_token(*args):
            self.assertEqual(os.environ["SUPERVISOR_TOKEN"], "private-token")
            calls.append("discovery")

        def execute_without_token(*args):
            self.assertNotIn("SUPERVISOR_TOKEN", os.environ)
            calls.append("exec")

        with (
            patch.object(run, "prepare_startup", return_value=startup),
            patch.object(run, "publish_discovery", side_effect=publish_with_token) as publish,
            patch.object(run.os, "execvp", side_effect=execute_without_token) as execute,
            redirect_stdout(output),
            redirect_stderr(output),
        ):
            run.main()

        publish.assert_called_once_with("selected-key", "/proxy", 4000)
        execute.assert_called_once_with("litellm", command)
        self.assertEqual(calls, ["discovery", "exec"])
        self.assertNotIn("selected-key", output.getvalue())
        self.assertNotIn("private-token", output.getvalue())

    def test_missing_token_prevents_proxy_start(self):
        command = ["litellm", "--port", "4000", "--config", "/tmp/config.yaml"]
        with (
            patch.object(run, "prepare_startup", return_value=(command, "selected-key", "/v1")),
            patch.object(run, "publish_discovery", wraps=run.publish_discovery) as publish,
            patch.object(run.HTTP, "open") as request,
            patch.object(run.os, "execvp") as execute,
        ):
            with self.assertRaisesRegex(KeyError, "SUPERVISOR_TOKEN"):
                run.main()

        publish.assert_called_once_with("selected-key", "/v1", 4000)
        request.assert_not_called()
        execute.assert_not_called()

    def test_discovery_failure_is_sanitized_and_does_not_stop_proxy(self):
        os.environ["SUPERVISOR_TOKEN"] = "private-token"
        command = ["litellm", "--port", "4000", "--config", "/tmp/config.yaml"]
        output = io.StringIO()

        def fail_with_token(*args):
            self.assertEqual(os.environ["SUPERVISOR_TOKEN"], "private-token")
            raise run.DiscoveryError("private-server-value")

        def execute_without_token(*args):
            self.assertNotIn("SUPERVISOR_TOKEN", os.environ)

        with (
            patch.object(run, "prepare_startup", return_value=(command, "private-key", "/v1")),
            patch.object(run, "publish_discovery", side_effect=fail_with_token) as publish,
            patch.object(run.os, "execvp", side_effect=execute_without_token) as execute,
            redirect_stdout(output),
            redirect_stderr(output),
        ):
            run.main()

        publish.assert_called_once_with("private-key", "/v1", 4000)
        execute.assert_called_once_with("litellm", command)
        self.assertIn("Discovery", output.getvalue())
        self.assertNotIn("private", output.getvalue())
        self.assertNotIn("Traceback", output.getvalue())

    def test_io_error_output_does_not_expose_values(self):
        output = io.StringIO()
        with patch.object(run, "prepare_startup", side_effect=OSError("private-value-123")), redirect_stderr(output):
            with self.assertRaises(SystemExit) as caught:
                run.main()

        self.assertEqual(caught.exception.code, 1)
        self.assertIn("Cannot read the options", output.getvalue())
        self.assertNotIn("private-value-123", output.getvalue())
        self.assertNotIn("Traceback", output.getvalue())

    def test_configuration_error_exits_without_a_traceback(self):
        output = io.StringIO()
        with patch.object(run, "prepare_startup", side_effect=run.ConfigurationError("Invalid options.")), redirect_stderr(output):
            with self.assertRaises(SystemExit) as caught:
                run.main()

        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(output.getvalue(), "LiteLLM: Invalid options.\n")


if __name__ == "__main__":
    unittest.main()
