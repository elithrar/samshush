"""Exercise Home transactions against native-setting and filesystem fakes."""

import copy
import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import home  # noqa: E402


def fake_command(command, arguments):
    """Implement only the native operations used by the real rendered script."""
    root = Path(os.environ["TEST_ROOT"])
    if command == "stat":
        if arguments[:2] != ["-c", "%u:%a"]:
            return 1
        owner = os.environ.get("TEST_OWNER", "0")
        print(f"{owner}:{stat.S_IMODE(Path(arguments[2]).stat().st_mode):o}")
        return 0
    if command == "sha256sum":
        if arguments == ["-c"]:
            for line in sys.stdin:
                expected, filename = line.rstrip().split("  ", 1)
                if hashlib.sha256(Path(filename).read_bytes()).hexdigest() != expected:
                    return 1
        else:
            filename = arguments[0]
            print(
                f"{hashlib.sha256(Path(filename).read_bytes()).hexdigest()}  {filename}"
            )
        return 0
    if command != "vconftool":
        return 1
    values_path = root / "values.json"
    values = json.loads(values_path.read_text())
    if arguments[0] == "get":
        key = arguments[1]
        if key not in values:
            return 1
        entry = values[key]
        value = entry["value"]
        if entry["type"] == "Boolean":
            value = "True" if value else "False"
        print(f"{key}, value = {value} ({entry['type']})")
        return 0
    if len(arguments) != 6 or arguments[:2] != ["set", "-t"] or arguments[-1] != "-f":
        return 1
    _, _, kind, key, value, _ = arguments
    value = int(value)
    if values[key]["type"] != {"int": "Int32", "bool": "Boolean"}[kind]:
        return 1
    snapshot = root / "journal/before.tsv"
    event = {
        "key": key,
        "value": value,
        "snapshot": snapshot.read_text() if snapshot.is_file() else None,
        "snapshot_hash": (
            (root / "journal/before.sha256").read_text().strip()
            if (root / "journal/before.sha256").is_file()
            else None
        ),
    }
    with (root / "writes.jsonl").open("a") as stream:
        stream.write(json.dumps(event) + "\n")
    failures_path = root / "failures.json"
    failures = json.loads(failures_path.read_text())
    failure = None
    for rule in failures:
        if rule["key"] == key and rule["value"] == value and rule["remaining"]:
            rule["remaining"] -= 1
            failure = rule["mode"]
            failures_path.write_text(json.dumps(failures))
            break
    if failure == "before":
        return 1
    values[key]["value"] = value
    values_path.write_text(json.dumps(values))
    if failure == "after":
        return 1
    if failure == "interrupt":
        os.kill(os.getppid(), signal.SIGTERM)
    return 0


HELPERS = r"""
id() { printf 0; }
stat() { "$TEST_PYTHON" "$TEST_HELPER" fake stat "$@"; }
sha256sum() { "$TEST_PYTHON" "$TEST_HELPER" fake sha256sum "$@"; }
vconftool() { "$TEST_PYTHON" "$TEST_HELPER" fake vconftool "$@"; }
"""


class HomeTests(unittest.TestCase):
    def setUp(self):
        self.profile = {
            "model": home.MODEL,
            "build": home.BUILD,
            "home": {
                "journal": home.JOURNAL,
                "settings": [
                    dict(zip(("name", "key", "type", "value"), setting, strict=True))
                    for setting in home.SETTINGS
                ],
                "provider_hashes": dict(home.PROVIDERS),
            },
        }
        self.new_device()

    def new_device(self):
        temporary = tempfile.TemporaryDirectory(prefix="samshush-home-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.journal = self.root / "journal"
        (self.root / "os-release").write_text(f"BUILD_ID={home.BUILD}\n")
        (self.root / "boot-id").write_text("test-boot\n")
        (self.root / "failures.json").write_text("[]")
        self.initial = {
            key: {"type": {"int": "Int32", "bool": "Boolean"}[kind], "value": 1}
            for _, key, kind, _ in home.SETTINGS
        }
        self.set_values(self.initial)
        self.providers = {}
        for index, path in enumerate(home.PROVIDERS):
            fixture = self.root / f"provider-{index}.so"
            fixture.write_bytes(f"unchanged provider {index}".encode())
            self.providers[path] = (
                fixture,
                hashlib.sha256(fixture.read_bytes()).hexdigest(),
            )
        self.privacy = self.root / "privacy-journal"
        self.privacy.mkdir()
        (self.privacy / "original").write_bytes(b"existing privacy recovery data\n")

    def set_values(self, values):
        (self.root / "values.json").write_text(json.dumps(values))

    def values(self):
        return json.loads((self.root / "values.json").read_text())

    def writes(self):
        path = self.root / "writes.jsonl"
        return (
            [json.loads(line) for line in path.read_text().splitlines()]
            if path.exists()
            else []
        )

    def fail_write(self, index, value, mode="before"):
        path = self.root / "failures.json"
        rules = json.loads(path.read_text())
        rules.append(
            {
                "key": home.SETTINGS[index][1],
                "value": value,
                "mode": mode,
                "remaining": 1,
            }
        )
        path.write_text(json.dumps(rules))

    def run_control(self, operation, **environment):
        script = home.render(operation, self.profile)
        replacements = {
            home.JOURNAL: self.journal,
            "/run/frame-runtime-v1.lock": self.root / "lock",
            "/etc/os-release": self.root / "os-release",
            "/proc/sys/kernel/random/boot_id": self.root / "boot-id",
        }
        for path, (fixture, digest) in self.providers.items():
            replacements[path] = fixture
            replacements[home.PROVIDERS[path]] = digest
        for source, target in replacements.items():
            script = script.replace(source, str(target))
        result = subprocess.run(
            ["/bin/bash"],
            input=HELPERS + script,
            text=True,
            capture_output=True,
            timeout=30,
            env=os.environ
            | {
                "TEST_ROOT": str(self.root),
                "TEST_PYTHON": sys.executable,
                "TEST_HELPER": str(Path(__file__).resolve()),
            }
            | environment,
        )
        self.assertEqual(
            (self.privacy / "original").read_bytes(),
            b"existing privacy recovery data\n",
        )
        return result

    def succeeds(self, operation, **environment):
        result = self.run_control(operation, **environment)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def fails(self, operation, **environment):
        result = self.run_control(operation, **environment)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_policy_rejects_unverified_settings_providers_and_identity(self):
        for field, value in (("model", "another TV"), ("build", "another build")):
            with self.subTest(field=field):
                profile = copy.deepcopy(self.profile)
                profile[field] = value
                with self.assertRaises(ValueError):
                    home.render("install", profile)
        for field, value in (
            ("name", "other"),
            ("key", "db/another/key"),
            ("type", "bool"),
            ("value", 1),
            ("value", False),
        ):
            with self.subTest(field=field, value=value):
                profile = copy.deepcopy(self.profile)
                profile["home"]["settings"][0][field] = value
                with self.assertRaises(ValueError):
                    home.render("install", profile)
        for field, value in (
            ("settings", []),
            ("provider_hashes", {}),
            ("journal", "/tmp/another-journal"),
        ):
            profile = copy.deepcopy(self.profile)
            profile["home"][field] = value
            with self.assertRaises(ValueError):
                home.render("install", profile)

    def test_runtime_build_and_provider_drift_fail_before_snapshot_or_write(self):
        (self.root / "os-release").write_text("BUILD_ID=other\n")
        self.fails("install")
        self.assertFalse(self.journal.exists())
        self.assertEqual(self.writes(), [])
        (self.root / "os-release").write_text(f"BUILD_ID={home.BUILD}\n")
        next(iter(self.providers.values()))[0].write_bytes(b"changed provider")
        self.fails("install")
        self.assertFalse(self.journal.exists())
        self.assertEqual(self.writes(), [])

    def test_missing_and_malformed_native_values_fail_before_mutation(self):
        key = home.SETTINGS[0][1]
        for entry in (
            None,
            {"type": "Int32", "value": 2},
            {"type": "String", "value": 1},
        ):
            with self.subTest(entry=entry):
                values = copy.deepcopy(self.initial)
                if entry is None:
                    del values[key]
                else:
                    values[key] = entry
                self.set_values(values)
                self.fails("install")
                self.assertFalse(self.journal.exists())
                self.assertEqual(self.writes(), [])

    def test_status_without_journal_reports_not_installed_without_writes(self):
        for operation in ("inspect", "status"):
            result = self.succeeds(operation)
            self.assertIn("NOT_INSTALLED", result.stdout)
            self.assertIn(
                "source shortcut layout is not managed or verified", result.stdout
            )
        self.assertFalse(self.journal.exists())
        self.assertEqual(self.writes(), [])

    def test_complete_snapshot_precedes_writes_and_originals_survive_repeat_install(
        self,
    ):
        self.initial[home.SETTINGS[1][1]]["value"] = 0
        self.set_values(self.initial)
        self.succeeds("install")
        snapshot = (self.journal / "before.tsv").read_text()
        self.assertEqual(len(snapshot.splitlines()), len(home.SETTINGS))
        for row in snapshot.splitlines():
            _, key, _, _, saved = row.split("\t")
            self.assertEqual(int(saved), self.initial[key]["value"])
        for write in self.writes():
            self.assertEqual(write["snapshot"], snapshot)
            self.assertEqual(
                write["snapshot_hash"], hashlib.sha256(snapshot.encode()).hexdigest()
            )
        count = len(self.writes())
        self.succeeds("install")
        self.succeeds("status")
        self.assertEqual(len(self.writes()), count)
        self.succeeds("remove")
        self.succeeds("remove")
        self.succeeds("status")
        self.assertEqual(self.values(), self.initial)
        self.assertEqual((self.journal / "before.tsv").read_text(), snapshot)
        self.fails("install")

    def test_failed_write_that_mutates_is_rolled_back_before_retry(self):
        self.fail_write(1, 0, "after")
        self.fails("install")
        self.assertEqual(self.values(), self.initial)
        self.assertEqual((self.journal / "phase").read_text().strip(), "rolled-back")
        self.fails("status")
        self.succeeds("install")
        self.succeeds("status")

    def test_failed_rollback_retains_snapshot_and_remove_recovers(self):
        self.fail_write(0, 0, "after")
        self.fail_write(0, 1)
        self.fails("install")
        self.assertEqual((self.journal / "phase").read_text().strip(), "rolling-back")
        self.assertEqual(self.values()[home.SETTINGS[0][1]]["value"], 0)
        self.assertTrue((self.journal / "before.tsv").is_file())
        self.succeeds("remove")
        self.assertEqual(self.values(), self.initial)

    def test_partial_remove_can_resume(self):
        self.succeeds("install")
        self.fail_write(1, 1)
        self.fails("remove")
        self.assertEqual((self.journal / "phase").read_text().strip(), "removing")
        self.assertEqual(self.values()[home.SETTINGS[0][1]]["value"], 1)
        self.assertEqual(self.values()[home.SETTINGS[1][1]]["value"], 0)
        self.succeeds("remove")
        self.assertEqual(self.values(), self.initial)

    def test_owner_drift_blocks_remove_repeat_install_and_verified_status(self):
        self.succeeds("install")
        changed = self.values()
        changed[home.SETTINGS[1][1]]["value"] = 1
        self.set_values(changed)
        count = len(self.writes())
        for operation in ("remove", "install", "status"):
            self.fails(operation)
        self.assertEqual(self.values(), changed)
        self.assertEqual(len(self.writes()), count)

    def test_corrupt_or_unsafe_journals_prevent_mutation(self):
        for corruption in (
            "changed",
            "missing",
            "empty",
            "symlink",
            "mode",
            "owner",
            "unknown",
        ):
            with self.subTest(corruption=corruption):
                self.new_device()
                self.succeeds("install")
                count = len(self.writes())
                arguments = {}
                if corruption == "changed":
                    (self.journal / "before.tsv").write_text("changed\n")
                elif corruption == "missing":
                    (self.journal / "phase").unlink()
                elif corruption == "empty":
                    (self.journal / "boot-id").write_text("")
                elif corruption == "symlink":
                    (self.journal / "policy").rename(self.root / "saved-policy")
                    (self.journal / "policy").symlink_to(self.root / "saved-policy")
                elif corruption == "mode":
                    (self.journal / "before.tsv").chmod(0o666)
                elif corruption == "owner":
                    arguments["TEST_OWNER"] = "5001"
                else:
                    (self.journal / "unexpected").write_text("unknown entry")
                self.fails("remove", **arguments)
                self.assertEqual(len(self.writes()), count)

    def test_journal_symlink_and_existing_operation_lock_are_not_followed_or_removed(
        self,
    ):
        self.journal.symlink_to(self.privacy, target_is_directory=True)
        self.fails("install")
        self.assertEqual(self.writes(), [])
        self.journal.unlink()
        (self.root / "lock").mkdir()
        self.fails("install")
        self.assertTrue((self.root / "lock").is_dir())
        self.assertEqual(self.writes(), [])

    def test_interrupt_rolls_back_and_retains_original_snapshot(self):
        self.fail_write(0, 0, "interrupt")
        self.fails("install")
        self.assertEqual(self.values(), self.initial)
        self.assertEqual((self.journal / "phase").read_text().strip(), "rolled-back")
        self.assertFalse((self.root / "lock").exists())
        self.succeeds("remove")

    def test_interrupted_applying_checkpoint_requires_recovery_before_reinstall(self):
        self.succeeds("install")
        (self.journal / "phase").write_text("applying\n")
        values = self.values()
        values[home.SETTINGS[1][1]]["value"] = 1
        self.set_values(values)
        self.fails("install")
        self.assertEqual(self.values(), self.initial)
        self.assertEqual((self.journal / "phase").read_text().strip(), "rolled-back")
        self.succeeds("install")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "fake":
        raise SystemExit(fake_command(sys.argv[2], sys.argv[3:]))
    unittest.main()
