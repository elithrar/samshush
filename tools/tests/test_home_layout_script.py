"""Exercise the native layout transaction against isolated app/settings fakes."""

import copy
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import home_layout_script as layout  # noqa: E402


def process(root, pid, start, *, selected=True, argv_zero=True):
    path = root / "proc" / str(pid)
    path.mkdir(parents=True, exist_ok=True)
    executable = path / "exe"
    if executable.is_symlink():
        executable.unlink()
    executable.symlink_to("/usr/bin/dotnet-launcher-inhouse")
    dll = str(root / "opt/usr/apps/com.samsung.tv.home.media/bin/HomeMedia.dll")
    arguments = (
        [dll]
        if selected
        else [
            str(root / "opt/usr/apps/com.samsung.tv.home.sidebar/bin/HomeSidebar.dll")
        ]
    )
    if not argv_zero:
        arguments.insert(0, "dotnet-launcher-inhouse")
    (path / "cmdline").write_bytes(
        b"\0".join(arg.encode() for arg in arguments) + b"\0"
    )
    fields = ["S"] + ["0"] * 49
    fields[17] = "3"
    fields[19] = str(start)
    (path / "stat").write_text(f"{pid} (Home Media) {' '.join(fields)}\n")
    (path / "status").write_text("Uid:\t5001\t5001\t5001\t5001\n")


def fake_command(command, arguments):
    root = Path(os.environ["LAYOUT_TEST_ROOT"])
    config_path = root / "config.json"
    config = json.loads(config_path.read_text())
    values_path = root / "values.json"
    values = json.loads(values_path.read_text())

    def save():
        config_path.write_text(json.dumps(config))
        values_path.write_text(json.dumps(values))

    def consume(name):
        remaining = config.get(name, 0)
        if remaining:
            config[name] = remaining - 1
            save()
            return True
        return False

    def event(name):
        journal = root / "journal"
        item = {
            "name": name,
            "args": arguments,
            "phase": (journal / "phase").read_text().strip()
            if (journal / "phase").exists()
            else None,
            "before": (journal / "before").read_text()
            if (journal / "before").exists()
            else None,
            "after": (journal / "after").read_text()
            if (journal / "after").exists()
            else None,
            "app_running": config["running"],
            "current": values[layout.KEY]["value"],
        }
        with (root / "events.jsonl").open("a") as stream:
            stream.write(json.dumps(item) + "\n")

    if command == "id":
        print(config.get("uid", 0))
    elif command == "stat":
        path = Path(arguments[-1])
        info = path.stat()
        owner = 5001 if str(path).startswith(str(root / "home")) else 0
        if path == root / "owner":
            owner = config.get("owner_uid", 5001)
        pattern = arguments[1]
        print(
            {
                "%u": str(owner),
                "%s": str(info.st_size),
                "%u:%a": f"{owner}:{stat.S_IMODE(info.st_mode):o}",
                "%u:%g:%a": f"{owner}:100:{stat.S_IMODE(info.st_mode):o}",
            }[pattern]
        )
    elif command == "sha256sum":
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
    elif command == "readlink":
        if arguments[0] == str(root / "proc/self/ns/mnt"):
            print(config.get("namespace", "mnt:[1]"))
        elif arguments[0] == str(root / "proc/1/ns/mnt"):
            print("mnt:[1]")
        else:
            try:
                print(os.readlink(arguments[0]))
            except OSError:
                return 1
    elif command == "vconftool":
        if arguments[0] == "get":
            key = arguments[1]
            if key == config.get("unreadable_key") or key not in values:
                return 1
            value = values[key]
            print(f"{key}, value = {value['value']} ({value['type']})")
        else:
            if arguments[:3] != ["set", "-t", "string"] or arguments[-1] != "-f":
                return 1
            event("write")
            if consume("fail_write_before"):
                return 1
            values[arguments[3]]["value"] = arguments[4]
            save()
            if consume("interrupt_write"):
                os.kill(os.getppid(), signal.SIGTERM)
            if consume("fail_write_after"):
                return 1
    elif command == "app_launcher":
        if arguments[1:] != ["com.samsung.tv.home.media", "-u", "5001"]:
            return 1
        operation = arguments[0]
        if operation == "-r":
            if config.get("unknown_app_state"):
                print("state unavailable")
                return 1
            print("result: running" if config["running"] else "result: not running")
        elif operation == "-t":
            event("stop")
            if consume("fail_stop"):
                return 1
            config["running"] = False
            values["memory/eden/home_media_done"]["value"] = 0
            if not config.get("surviving_process"):
                shutil.rmtree(root / "proc" / str(config["pid"]))
            if consume("drift_at_stop"):
                values[layout.KEY]["value"] = "external edit during shutdown"
            if consume("change_sidebar_at_stop"):
                process(root, 6789, 200, selected=False)
            save()
            if consume("interrupt_stop"):
                os.kill(os.getppid(), signal.SIGTERM)
            if consume("fail_stop_after"):
                return 1
        elif operation == "-s":
            event("start")
            if consume("fail_start"):
                return 1
            if not config.get("reuse_identity"):
                config["pid"] += 1
                config["start"] += 100
            config["running"] = True
            process(root, config["pid"], config["start"])
            values["memory/eden/home_media_done"]["value"] = 1
            if consume("drift_at_start"):
                values[layout.KEY]["value"] = "external edit after relaunch"
            save()
        else:
            return 1
    elif command == "sleep":
        pass
    else:
        return 1
    return 0


HELPERS = r"""
id() { "$LAYOUT_TEST_PYTHON" "$LAYOUT_TEST_HELPER" fake id "$@"; }
stat() { "$LAYOUT_TEST_PYTHON" "$LAYOUT_TEST_HELPER" fake stat "$@"; }
sha256sum() { "$LAYOUT_TEST_PYTHON" "$LAYOUT_TEST_HELPER" fake sha256sum "$@"; }
readlink() { "$LAYOUT_TEST_PYTHON" "$LAYOUT_TEST_HELPER" fake readlink "$@"; }
vconftool() { "$LAYOUT_TEST_PYTHON" "$LAYOUT_TEST_HELPER" fake vconftool "$@"; }
app_launcher() { "$LAYOUT_TEST_PYTHON" "$LAYOUT_TEST_HELPER" fake app_launcher "$@"; }
sleep() { :; }
"""


class HomeLayoutTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="samshush-layout-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.journal = self.root / "journal"
        self.data = self.root / "home/owner/apps_rw/com.samsung.tv.home.media"
        for path in ("shared/trusted", "data"):
            (self.data / path).mkdir(parents=True)
        (self.root / "owner").mkdir()
        (self.root / "os-release").write_text(f"BUILD_ID={layout.BUILD}\n")
        (self.root / "boot-id").write_text("boot-one\n")
        trusted = self.data / "shared/trusted"
        (trusted / ".resetflag").write_text("1\n0\n")
        (trusted / "db_ready").write_bytes(b"")
        (trusted / "sqlite.db").write_bytes(b"SQLite format 3\0" + bytes(20))
        for path in trusted.iterdir():
            path.chmod(0o644)
        self.row = {
            "id": "HDMI1/0",
            "p": "f",
            "component_id": layout.COMPONENT,
            "t": "src",
            "src": "13",
        }
        self.original = {
            "version": "1.0.0",
            "other": {"unknown": "keep"},
            "app_layer": {
                "max_tile": 40,
                "list": [{"component_id": "s/livetv@TVPLUS"}, self.row],
                "count": 1,
                "another": "keep this too",
            },
        }
        candidate = copy.deepcopy(self.original)
        candidate["app_layer"]["list"] = [self.row]
        self.before = json.dumps(self.original, separators=(",", ":")) + "\n"
        self.after = json.dumps(candidate, separators=(",", ":"))
        self.values = {
            layout.KEY: {"type": "String", "value": self.before},
            "db/volt/make_your_smarthub": {"type": "String", "value": "finish"},
            "memory/eden/smarthub_reset_media": {"type": "Int32", "value": 1},
            "memory/eden/home_media_done": {"type": "Int32", "value": 1},
            "db/factory/factory_reset_count": {"type": "Int32", "value": 1},
            "db/factory/service_reset_count": {"type": "Int32", "value": 0},
        }
        self.write_values()
        self.configure(running=True, pid=4321, start=100)
        process(self.root, 4321, 100)
        process(self.root, 6789, 100, selected=False)
        self.binary_files = {}
        for index, (name, digest) in enumerate(layout.HASHES.items()):
            path = self.root / name.lstrip("/")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"native implementation {index}".encode())
            self.binary_files[digest] = hashlib.sha256(path.read_bytes()).hexdigest()

    def configure(self, **changes):
        path = self.root / "config.json"
        config = json.loads(path.read_text()) if path.exists() else {}
        config.update(changes)
        path.write_text(json.dumps(config))

    def write_values(self):
        (self.root / "values.json").write_text(json.dumps(self.values))

    def current(self):
        return json.loads((self.root / "values.json").read_text())[layout.KEY]["value"]

    def events(self):
        path = self.root / "events.jsonl"
        return (
            [json.loads(line) for line in path.read_text().splitlines()]
            if path.exists()
            else []
        )

    def run_control(self, operation, *, before=None, after=None):
        script = layout.render(
            operation,
            before=(self.before if before is None else before)
            if operation == "install"
            else None,
            after=(self.after if after is None else after)
            if operation == "install"
            else None,
        )
        for original, replacement in self.binary_files.items():
            script = script.replace(original, replacement)
        # Preserve the exact expected launcher identity while moving all filesystem reads.
        script = script.replace("/usr/bin/dotnet-launcher-inhouse", "@@RUNTIME@@")
        script = script.replace(layout.JOURNAL, str(self.journal))
        script = script.replace("/opt/usr/home/owner", str(self.root / "owner"))
        script = script.replace("/run/frame-runtime-v1.lock", str(self.root / "lock"))
        script = script.replace("/etc/os-release", str(self.root / "os-release"))
        script = script.replace(
            "/proc/sys/kernel/random/boot_id", str(self.root / "boot-id")
        )
        script = re.sub(
            r"/(?:proc|home|usr|opt)/",
            lambda match: str(self.root) + match.group(),
            script,
        )
        script = script.replace("@@RUNTIME@@", "/usr/bin/dotnet-launcher-inhouse")
        # The fixed PATH is not a device path in this host fixture.
        script = re.sub(r"export PATH=.*", "export PATH=/usr/bin:/bin", script)
        result = subprocess.run(
            ["/bin/bash"],
            input=HELPERS + script,
            text=True,
            capture_output=True,
            timeout=30,
            env=os.environ
            | {
                "LAYOUT_TEST_ROOT": str(self.root),
                "LAYOUT_TEST_PYTHON": sys.executable,
                "LAYOUT_TEST_HELPER": str(Path(__file__).resolve()),
            },
        )
        self.assertTrue((self.root / "proc/6789/cmdline").is_file())
        return result

    def succeeds(self, operation, **kwargs):
        result = self.run_control(operation, **kwargs)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def fails(self, operation, **kwargs):
        result = self.run_control(operation, **kwargs)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_install_status_remove_reinstall_preserves_exact_original_and_other_apps(
        self,
    ):
        self.succeeds("install")
        self.assertEqual(self.current(), self.after)
        self.assertEqual((self.journal / "before").read_text(), self.before)
        self.assertEqual(
            (self.journal / "before.sha256").read_text(),
            hashlib.sha256(self.before.encode()).hexdigest(),
        )
        self.succeeds("status")
        self.succeeds("install")
        self.assertEqual(
            [event["name"] for event in self.events()], ["stop", "write", "start"]
        )
        first_stop = self.events()[0]
        self.assertEqual(first_stop["before"], self.before)
        self.assertEqual(first_stop["after"], self.after)
        self.assertFalse(self.events()[1]["app_running"])
        self.succeeds("remove")
        self.assertEqual(self.current(), self.before)
        self.assertTrue((self.journal / "removed-receipt").is_file())
        self.succeeds("status")
        self.succeeds("install")
        self.assertTrue((self.journal / "removed-receipt").is_file())
        self.assertFalse((self.root / "lock").exists())

    def test_inspect_status_absent_are_read_only_and_report_exact_raw_string(self):
        import base64

        for operation in ("inspect", "status"):
            result = self.succeeds(operation)
            self.assertIn("HOME_LAYOUT_PHASE absent", result.stdout)
            encoded = re.search(r"HOME_LAYOUT_VALUE_BASE64 (.+)", result.stdout)[1]
            self.assertEqual(base64.b64decode(encoded).decode(), self.before)
        self.assertEqual(self.events(), [])
        self.assertFalse(self.journal.exists())

    def test_identity_build_and_binary_drift_fail_before_mutation(self):
        for options in ({"uid": 9}, {"owner_uid": 9}, {"namespace": "mnt:[2]"}):
            with self.subTest(options=options):
                self.configure(
                    **({"uid": 0, "owner_uid": 5001, "namespace": "mnt:[1]"} | options)
                )
                self.fails("install")
                self.assertEqual(self.events(), [])
        self.configure(uid=0, owner_uid=5001, namespace="mnt:[1]")
        (self.root / "os-release").write_text("BUILD_ID=changed\n")
        self.fails("install")
        (self.root / "os-release").write_text(f"BUILD_ID={layout.BUILD}\n")
        (self.root / next(iter(layout.HASHES)).lstrip("/")).write_bytes(b"different")
        self.fails("install")
        self.assertFalse(self.journal.exists())
        self.assertEqual(self.events(), [])

    def test_reset_and_database_guards_prevent_owner_stop(self):
        changes = (
            ("memory/eden/smarthub_reset_media", 0),
            ("db/factory/factory_reset_count", 2),
            ("db/volt/make_your_smarthub", "reset"),
            ("db/volt/make_your_smarthub", "done"),
        )
        for key, value in changes:
            with self.subTest(key=key, value=value):
                saved = self.values[key]["value"]
                self.values[key]["value"] = value
                self.write_values()
                self.fails("install")
                self.values[key]["value"] = saved
        self.write_values()
        flag = self.data / "data/.SHResetflag"
        flag.touch()
        self.fails("install")
        flag.unlink()
        database = self.data / "shared/trusted/sqlite.db"
        database.write_bytes(b"corrupt")
        self.fails("install")
        self.assertEqual(self.events(), [])

    def test_native_type_and_external_preimage_edits_fail(self):
        self.values[layout.KEY]["type"] = "Int32"
        self.write_values()
        self.fails("install")
        self.values[layout.KEY]["type"] = "String"
        self.values[layout.KEY]["value"] = self.before + " "
        self.write_values()
        self.fails("install")
        self.assertEqual(self.events(), [])

    def test_all_dll_argument_positions_and_exact_runtime_are_checked(self):
        process(self.root, 4321, 100, argv_zero=False)
        self.succeeds("install")
        self.succeeds("remove")
        config = json.loads((self.root / "config.json").read_text())
        exe = self.root / "proc" / str(config["pid"]) / "exe"
        exe.unlink()
        exe.symlink_to("/unrecognized/launcher")
        count = len(self.events())
        self.fails("install")
        self.assertEqual(len(self.events()), count)

    def test_surviving_selected_process_prevents_write(self):
        self.configure(surviving_process=True)
        self.fails("install")
        self.assertEqual(self.current(), self.before)
        self.assertEqual([event["name"] for event in self.events()], ["stop"])
        self.assertEqual(
            (self.journal / "phase").read_text().strip(), "recovery-required"
        )

    def test_stop_failure_keeps_original_and_never_kills(self):
        self.configure(fail_stop=1)
        self.fails("install")
        self.assertEqual(self.current(), self.before)
        self.assertEqual([event["name"] for event in self.events()], ["stop"])
        self.assertEqual((self.journal / "phase").read_text().strip(), "rolled-back")

    def test_shutdown_drift_is_not_overwritten(self):
        self.configure(drift_at_stop=1)
        self.fails("install")
        self.assertEqual(self.current(), "external edit during shutdown")
        self.assertEqual([event["name"] for event in self.events()], ["stop", "start"])
        self.assertTrue(json.loads((self.root / "config.json").read_text())["running"])
        self.assertEqual(
            (self.journal / "phase").read_text().strip(), "recovery-required"
        )
        self.fails("status")
        self.fails("remove")
        self.assertEqual([event["name"] for event in self.events()], ["stop", "start"])

    def test_write_failure_after_mutation_restores_original_through_native_lifecycle(
        self,
    ):
        self.configure(fail_write_after=1)
        self.fails("install")
        self.assertEqual(self.current(), self.before)
        self.assertEqual(
            [event["name"] for event in self.events()],
            ["stop", "write", "write", "start"],
        )
        self.assertEqual((self.journal / "phase").read_text().strip(), "rolled-back")

    def test_stop_failure_after_native_flush_recovers_only_owner_liveness(self):
        self.configure(drift_at_stop=1, fail_stop_after=1)
        self.fails("install")
        self.assertEqual(self.current(), "external edit during shutdown")
        self.assertTrue(json.loads((self.root / "config.json").read_text())["running"])
        self.assertEqual([event["name"] for event in self.events()], ["stop", "start"])
        self.assertEqual(
            (self.journal / "phase").read_text().strip(), "recovery-required"
        )

    def test_interrupted_stop_with_native_flush_is_resumable_without_overwrite(self):
        self.configure(drift_at_stop=1, interrupt_stop=1)
        self.fails("install")
        self.assertFalse(json.loads((self.root / "config.json").read_text())["running"])
        self.fails("remove")
        self.assertEqual(self.current(), "external edit during shutdown")
        self.assertTrue(json.loads((self.root / "config.json").read_text())["running"])
        self.assertEqual([event["name"] for event in self.events()], ["stop", "start"])

    def test_sidebar_identity_change_prevents_layout_write(self):
        self.configure(change_sidebar_at_stop=1)
        self.fails("install")
        self.assertEqual(self.current(), self.before)
        self.assertEqual([event["name"] for event in self.events()], ["stop", "start"])
        self.assertEqual(
            (self.journal / "phase").read_text().strip(), "recovery-required"
        )

    def test_removal_native_flush_recovers_owner_without_another_remove(self):
        self.succeeds("install")
        self.configure(drift_at_stop=1)
        self.fails("remove")
        self.assertEqual(self.current(), "external edit during shutdown")
        self.assertTrue(json.loads((self.root / "config.json").read_text())["running"])
        self.assertEqual(
            [event["name"] for event in self.events()],
            ["stop", "write", "start", "stop", "start"],
        )
        self.assertEqual(
            (self.journal / "phase").read_text().strip(), "recovery-required"
        )

    def test_relaunch_failure_rolls_back_and_restores_running_owner(self):
        self.configure(fail_start=1)
        self.fails("install")
        self.assertEqual(self.current(), self.before)
        self.assertTrue(json.loads((self.root / "config.json").read_text())["running"])
        self.assertEqual((self.journal / "phase").read_text().strip(), "rolled-back")

    def test_relaunch_drift_is_detected_without_clobbering_the_new_value(self):
        self.configure(drift_at_start=1)
        self.fails("install")
        self.assertEqual(self.current(), "external edit after relaunch")
        self.assertEqual(
            len([event for event in self.events() if event["name"] == "write"]), 1
        )

    def test_interruption_is_read_only_in_status_and_resumable_by_remove(self):
        self.configure(interrupt_write=1)
        self.fails("install")
        self.assertEqual(self.current(), self.after)
        self.assertEqual((self.journal / "phase").read_text().strip(), "writing")
        events = self.events()
        self.fails("status")
        self.assertEqual(self.events(), events)
        self.succeeds("remove")
        self.assertEqual(self.current(), self.before)
        self.assertEqual((self.journal / "phase").read_text().strip(), "removed")

    def test_external_edits_after_install_are_never_overwritten(self):
        self.succeeds("install")
        values = json.loads((self.root / "values.json").read_text())
        values[layout.KEY]["value"] = self.after + " "
        (self.root / "values.json").write_text(json.dumps(values))
        count = len(self.events())
        self.fails("remove")
        self.fails("status")
        self.assertEqual(len(self.events()), count)

    def test_existing_lock_and_changed_journal_fail_closed(self):
        (self.root / "lock").mkdir()
        self.fails("install")
        self.assertTrue((self.root / "lock").is_dir())
        (self.root / "lock").rmdir()
        self.succeeds("install")
        (self.journal / "before").write_text("tampered")
        count = len(self.events())
        self.fails("remove")
        self.assertEqual(len(self.events()), count)

    def test_render_rejects_scope_changes_and_malformed_or_oversized_strings(self):
        with self.assertRaises(ValueError):
            layout.render("anything")
        with self.assertRaises(ValueError):
            layout.render("inspect", before=self.before)
        for before in (None, "", "{", self.before + "\0", " " * 65537):
            with self.subTest(before=str(before)[:30]), self.assertRaises(ValueError):
                layout.render("install", before=before, after=self.after)
        for field, value in (("count", 2), ("list", []), ("another", "changed")):
            changed = json.loads(self.after)
            changed["app_layer"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                layout.render("install", before=self.before, after=json.dumps(changed))
        changed = json.loads(self.after)
        changed["app_layer"]["list"][0]["extra"] = "new"
        with self.assertRaises(ValueError):
            layout.render("install", before=self.before, after=json.dumps(changed))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "fake":
        raise SystemExit(fake_command(sys.argv[2], sys.argv[3:]))
    unittest.main()
