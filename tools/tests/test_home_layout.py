import base64
import copy
import gzip
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import frame
import home_layout


class LayoutPlanTests(unittest.TestCase):
    def setUp(self):
        self.apple = {
            "id": "HDMI1/0",
            "component_id": "s/source.layer.hdmi@HDMI1/0",
            "t": "src",
            "src": "13",
            "p": "f",
            "future_native_metadata": {"keep": True},
        }
        self.original = {
            "version": "1.0.0",
            "app_layer": {
                "max_tile": 40,
                "count": 2,
                "list": [
                    {"id": "Netflix", "component_id": "d/webapp@Netflix", "t": "app"},
                    {"id": "TVPLUS", "component_id": "s/livetv@TVPLUS", "t": "tv"},
                    self.apple,
                ],
                "future_layer_field": "keep",
            },
            "future_root_field": [1, 2],
        }
        self.sources = {
            "schema": "samshush-home-source-readout-v1",
            "complete": True,
            "hdmiSources": [
                {
                    "sourceType": 13,
                    "connectionState": True,
                    "detectedName": "Apple TV",
                    "editName": "Living Room",
                    "sourceId": "HDMI1/0",
                    "componentId": "s/source.layer.hdmi@HDMI1/0",
                },
                {
                    "sourceType": 15,
                    "connectionState": True,
                    "detectedName": "Arc Ultra",
                    "sourceId": "HDMI3",
                    "componentId": "s/source.layer.hdmi@HDMI3",
                },
            ],
        }

    def make_plan(self):
        return home_layout.plan(json.dumps(self.original), self.sources)

    def test_keeps_existing_apple_row_and_metadata_without_mutating_input(self):
        before = copy.deepcopy(self.original)
        component, encoded = self.make_plan()
        result = json.loads(encoded)
        self.assertEqual(component, self.apple["component_id"])
        self.assertEqual(result["app_layer"]["list"], [self.apple])
        self.assertEqual(result["app_layer"]["count"], 1)
        self.assertEqual(result["app_layer"]["max_tile"], 40)
        self.assertEqual(result["future_root_field"], [1, 2])
        self.assertEqual(result["app_layer"]["future_layer_field"], "keep")
        self.assertEqual(self.original, before)

    def test_missing_or_ambiguous_apple_source_is_rejected(self):
        for change in (
            "disconnected",
            "other_device",
            "missing",
            "duplicate",
            "incomplete",
        ):
            with self.subTest(change=change):
                source = copy.deepcopy(self.sources)
                if change == "disconnected":
                    source["hdmiSources"][0]["connectionState"] = False
                elif change == "other_device":
                    source["hdmiSources"][0]["detectedName"] = "Console"
                elif change == "missing":
                    source["hdmiSources"].pop(0)
                elif change == "duplicate":
                    source["hdmiSources"].append(source["hdmiSources"][0])
                else:
                    source["complete"] = False
                with self.assertRaises(ValueError):
                    home_layout.plan(json.dumps(self.original), source)

    def test_activity_identity_must_match_existing_row(self):
        self.sources["hdmiSources"][0].update(
            sourceId="HDMI1/1", componentId="s/source.layer.hdmi@HDMI1/1"
        )
        with self.assertRaisesRegex(ValueError, "Add Apple TV"):
            self.make_plan()

    def test_unknown_selected_row_schema_is_rejected(self):
        for key, value in (("src", "15"), ("p", "t"), ("t", "app")):
            with self.subTest(key=key):
                original = copy.deepcopy(self.original)
                original["app_layer"]["list"][-1][key] = value
                with self.assertRaises(ValueError):
                    home_layout.plan(json.dumps(original), self.sources)

    def test_duplicate_properties_or_components_are_rejected(self):
        raw = json.dumps(self.original).replace(
            '"version": "1.0.0"', '"version": "1.0.0", "version": "1.0.0"'
        )
        with self.assertRaises(ValueError):
            home_layout.plan(raw, self.sources)
        self.original["app_layer"]["list"].append(copy.deepcopy(self.apple))
        with self.assertRaises(ValueError):
            self.make_plan()

    def test_bad_bounds_fail_before_candidate(self):
        for value in (True, -1, 101, "40"):
            with self.subTest(value=value):
                self.original["app_layer"]["max_tile"] = value
                with self.assertRaises(ValueError):
                    self.make_plan()

    def test_repeat_install_checks_saved_install_without_replacing_original(self):
        _, after = self.make_plan()
        calls = []
        outputs = {
            "inspect": json.dumps(self.sources, separators=(",", ":"))
            + "\n"
            + self.output(after, "installed"),
            "status": self.output(after, "installed"),
        }
        result, console = self.workflow("install", outputs, calls)
        self.assertEqual(result, 0, console)
        self.assertEqual(calls, ["inspect", "status"])

    def test_remove_reports_fresh_restored_layout(self):
        _, after = self.make_plan()
        calls = []
        outputs = {
            "remove": self.output(after, "installed"),
            "status": self.output(json.dumps(self.original), "removed"),
        }
        result, console = self.workflow("remove", outputs, calls)
        self.assertEqual(result, 0, console)
        self.assertEqual(calls, ["remove", "status"])
        self.assertIn("Home layout journal: removed", console)
        self.assertIn("d/webapp@Netflix", console)

    @staticmethod
    def output(value, phase):
        return (
            "HOME_LAYOUT_VALUE_BASE64 "
            + base64.b64encode(value.encode()).decode()
            + f"\nHOME_LAYOUT_PHASE {phase}\n"
        )

    def workflow(self, operation, outputs, calls):
        async def probe(*args, **kwargs):
            encoded = re.search(
                r"printf %s ([A-Za-z0-9+/=]+) \|", kwargs["commands"][0]
            ).group(1)
            script = gzip.decompress(base64.b64decode(encoded)).decode()
            name = re.search(r"^MODE=(\w+)$", script, re.MULTILINE).group(1)
            calls.append(name)
            return {
                "authenticated": True,
                "uid": 0,
                "euid": 0,
                "gid": 0,
                "egid": 0,
                "system_files_unchanged": True,
                "staging_cleaned": True,
                "commands": [
                    {
                        "exit_code": 0,
                        "timed_out": False,
                        "stdout": outputs[name],
                        "stderr": "",
                    }
                ],
            }

        with tempfile.TemporaryDirectory() as directory:
            arguments = SimpleNamespace(
                logs=Path(directory),
                operation=operation,
                tv="192.0.2.1",
                callback_host="192.0.2.2",
            )
            with (
                patch.object(home_layout, "verify_model"),
                patch.object(home_layout, "source_prefix", return_value=""),
                patch("samsung_tv_root.archive_root.probe", side_effect=probe),
                patch("sys.stdout", new_callable=io.StringIO) as output,
            ):
                result = home_layout.run(arguments, frame)
            return result, output.getvalue()


if __name__ == "__main__":
    unittest.main()
