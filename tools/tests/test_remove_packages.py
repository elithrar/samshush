import base64
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import frame  # noqa: E402
import remove_packages  # noqa: E402


def returned_fake(command, args):
    """Small native package/settings/filesystem fake; no transport or TV access."""
    root = Path(os.environ["RETURNED_TEST_ROOT"])
    device = root / "device"
    database = root / "packages.json"
    packages = json.loads(database.read_text())
    options_path = root / "options.json"
    options = json.loads(options_path.read_text())

    def save():
        database.write_text(json.dumps(packages))
        options_path.write_text(json.dumps(options))

    def event(name, package):
        with (root / "events.jsonl").open("a") as stream:
            stream.write(json.dumps([name, package]) + "\n")

    def package_paths(package):
        return [
            device / f"opt/usr/apps/{package}",
            device / f"opt/share/packages/{package}.xml",
            device / f"opt/usr/home/owner/apps_rw/{package}",
            device / f"opt/usr/home/owner/apps_rw/.shared/{package}",
            device / f"opt/usr/home/owner/apps_rw/.shared_tmp/{package}",
        ]

    if command == "id":
        print(901 if args == ["-u", "sdk"] else 0)
    elif command == "stat":
        path = Path(args[-1])
        value = path.lstat()
        owner = 5001 if path == device / "opt/usr/home/owner" else 0
        print(
            {
                "%u": str(owner),
                "%u:%a": f"{owner}:{stat.S_IMODE(value.st_mode):o}",
                "%u:%g:%a": f"{owner}:100:{stat.S_IMODE(value.st_mode):o}",
            }[args[1]]
        )
    elif command == "readlink":
        if args[0].endswith(("/proc/self/ns/mnt", "/proc/1/ns/mnt")):
            print("mnt:[1]")
        else:
            try:
                print(os.readlink(args[0]))
            except OSError:
                return 1
    elif command == "systemctl":
        print("inactive")
    elif command == "sqlite3":
        query = args[-1]
        match = re.search(r"WHERE (?:package|app_id)='([^']+)'", query)
        if match is None:
            return 1
        package = match[1]
        if options.get("unreadable_package") == package:
            return 1
        row = packages.get(package)
        if "SELECT count(*)" in query:
            print(int(row is not None))
        elif row is not None:
            if "SELECT package," in query:
                print(
                    f"{package}|tpk|{row['version']}|{row['removable']}|false|{device}/opt/usr/apps/{package}"
                )
            elif "SELECT app_disable" in query:
                print(row["disabled"])
            elif "SELECT app_id," in query:
                print(
                    f"{package}|{row['disabled']}|{device}/opt/usr/apps/{package}/bin/native|{package}"
                )
            else:
                return 1
    elif command == "owner_pkg":
        package = args[args.index("-n") + 1]
        event(args[0], package)
        if args[0] == "-D":
            packages[package]["disabled"] = "true"
            save()
        elif args[0] == "-k":
            pass
        elif args[0] == "-u":
            if options.get("fail_before") == package:
                options.pop("fail_before")
                save()
                return 1
            if options.get("partial") == package:
                shutil.rmtree(package_paths(package)[0])
                return 1
            packages.pop(package)
            for path in package_paths(package):
                if path.is_dir():
                    shutil.rmtree(path)
                elif path.exists():
                    path.unlink()
            fail_after = options.pop("fail_after", None) == package
            save()
            if fail_after:
                return 1
        else:
            return 1
    elif command == "sha256sum":
        if args[0] == "-c":
            lines = (
                Path(args[1]).read_text().splitlines()
                if len(args) > 1
                else sys.stdin.read().splitlines()
            )
            for line in lines:
                expected, filename = line.split("  ", 1)
                if (
                    not Path(filename).is_file()
                    or hashlib.sha256(Path(filename).read_bytes()).hexdigest()
                    != expected
                ):
                    return 1
        else:
            for filename in args:
                if options.get("hash_failure") and (
                    "/stage/returned-" in filename or "/package-files/" in filename
                ):
                    return 1
                print(
                    f"{hashlib.sha256(Path(filename).read_bytes()).hexdigest()}  {filename}"
                )
    elif command == "chsmack":
        if args[:1] != ["-a"]:
            for filename in args:
                print(filename + ' access="User"')
    elif command == "chown":
        pass
    elif command == "tar":
        destination = Path(args[args.index("-czf") + 1])
        paths = Path(args[args.index("-T") + 1]).read_text().splitlines()
        with tarfile.open(destination, "w:gz") as archive:
            for path in paths:
                archive.add("/" + path, arcname=path)
    elif command in ("sleep", "sync"):
        pass
    else:
        return 1
    return 0


class RemovalTests(unittest.TestCase):
    def test_render_rejects_untrusted_backup_path_and_hash(self):
        for archive in (
            "/tmp/backup.tar.gz",
            "/tmp/$(false)",
            "/opt/usr/home/owner/share/tmp/samshush-package-backup-123.tar.gz",
        ):
            with self.assertRaises(ValueError):
                remove_packages.render("prepare", frame.policy(), archive=archive)
        with self.assertRaises(ValueError):
            remove_packages.render("remove", frame.policy(), backup_hash="x; false")

    def test_all_payloads_parse_and_wrong_identity_rejects_before_mutation(self):
        for action in ("prepare", "backup", "acknowledge", "remove", "status"):
            script = remove_packages.render(
                action,
                frame.policy(),
                archive="/opt/usr/home/owner/share/tmp/samshush-package-backup-0123456789abcdef.tar.gz",
                backup_hash="a" * 64,
            )
            subprocess.run(["/bin/bash", "-n"], input=script, text=True, check=True)
            result = subprocess.run(
                ["/bin/bash"],
                input="id() { echo 999; }\n" + script,
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Unexpected owner identity", result.stderr)

    def test_changed_local_backup_stops_before_device_access(self):
        with tempfile.TemporaryDirectory() as temp:
            backup = Path(temp)
            (backup / "packages.tar.gz").write_bytes(b"changed")
            (backup / "manifest.json").write_text(
                json.dumps(
                    {
                        "packages": list(remove_packages.PACKAGES),
                        "build": frame.policy()["build"],
                        "tv": "192.0.2.1",
                        "sha256": "a" * 64,
                    }
                )
            )
            from argparse import Namespace

            args = Namespace(operation="remove", backup_dir=backup, tv="192.0.2.1")
            with patch("samsung_tv_root.archive_root.probe") as probe:
                with self.assertRaisesRegex(ValueError, "checksum changed"):
                    remove_packages.run(args, frame)
                probe.assert_not_called()

    def test_backup_transfer_verifies_bytes_and_discards_truncated_copy(self):
        from types import SimpleNamespace

        data = b"private backup bytes"
        expected = hashlib.sha256(data).hexdigest()
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "packages.tar.gz"
            for payload, succeeds in ((data[:-1], False), (data, True)):
                client = Mock()
                client.capture.side_effect = [
                    SimpleNamespace(output=f"{len(data)}\n\n[exit:0]\n"),
                    SimpleNamespace(
                        output=base64.b64encode(payload).decode() + "\n[exit:0]\n"
                    ),
                ]
                if succeeds:
                    remove_packages.download_archive(
                        client, "/known/archive", destination, expected
                    )
                    self.assertEqual(destination.read_bytes(), data)
                else:
                    with self.assertRaisesRegex(ValueError, "truncated"):
                        remove_packages.download_archive(
                            client, "/known/archive", destination, expected
                        )
                    self.assertEqual(list(Path(temp).iterdir()), [])

    def test_backup_transfer_rejects_hash_mismatch_and_oversized_source(self):
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "packages.tar.gz"
            client = Mock()
            client.capture.side_effect = [
                SimpleNamespace(output="1\n[exit:0]\n"),
                SimpleNamespace(output="YQ==\n[exit:0]\n"),
            ]
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                remove_packages.download_archive(
                    client, "/known/archive", destination, "0" * 64
                )
            self.assertFalse(destination.exists())
            client.capture.side_effect = [
                SimpleNamespace(output="999999999\n[exit:0]\n")
            ]
            with self.assertRaisesRegex(ValueError, "oversized"):
                remove_packages.download_archive(
                    client, "/known/archive", destination, "0" * 64
                )

    def run_remove(
        self,
        *,
        changed_package=None,
        fail_package=None,
        missing_ack=False,
        process_survivor=False,
        file_drift=False,
    ):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        state = root / "state"
        state.mkdir()
        db = root / "db"
        db.mkdir()
        (state / "prepared").touch()
        (state / "backup.sha256").write_text("a" * 64 + "\n")
        if not missing_ack:
            (state / "host-verified.sha256").write_text("a" * 64 + "\n")
        (state / "packages").write_text("one\ntwo\n")
        metadata = "".join(
            f"{pkg}|tpk|1|true|false|/opt/usr/apps/{pkg}\n" for pkg in ("one", "two")
        )
        (state / "metadata.before").write_text(metadata)
        (state / "apps.before").write_text(
            "one|false|/opt/usr/apps/one/bin/one|one\ntwo|false|/opt/usr/apps/two/bin/two|two\n"
        )
        for pkg in ("one", "two"):
            (db / pkg).write_text(
                f"{pkg}|tpk|{2 if pkg == changed_package else 1}|true|false|/opt/usr/apps/{pkg}\n"
            )
        prelude = r"""set -euo pipefail
ROOT=$TEST_ROOT/state
DB=$TEST_ROOT/db
fail() { echo "ERROR: $*" >&2; exit 1; }
packages() { printf 'one\ntwo\n'; }
package_row() { [[ ! -f $DB/$1 ]] || cat "$DB/$1"; }
sqlite3() { echo true; }
sha256sum() { [[ $FILE_DRIFT == 0 ]]; }
check_processes() { [[ $PROCESS_SURVIVOR == 0 ]]; }
check_absent() { [[ ! -f $DB/one && ! -f $DB/two ]]; }
owner_pkg() {
 [[ $1 == -u && $2 == -n && $4 == --global ]] || return 99
 printf '%s\n' "$3" >> "$TEST_ROOT/calls"
 [[ $3 != "$FAIL_PACKAGE" ]] || return 1
 rm "$DB/$3"
}
sync() { :; }
"""
        script = prelude + remove_packages.REMOVE.replace("@@BACKUP_HASH@@", "a" * 64)
        result = subprocess.run(
            ["/bin/bash"],
            input=script,
            text=True,
            capture_output=True,
            env=os.environ
            | {
                "TEST_ROOT": str(root),
                "FAIL_PACKAGE": fail_package or "",
                "PROCESS_SURVIVOR": str(int(process_survivor)),
                "FILE_DRIFT": str(int(file_drift)),
            },
        )
        calls = (
            (root / "calls").read_text().splitlines()
            if (root / "calls").exists()
            else []
        )
        return result, calls, state, db

    def test_complete_batch_is_validated_before_first_uninstall(self):
        for kwargs in (
            {"changed_package": "two"},
            {"missing_ack": True},
            {"process_survivor": True},
            {"file_drift": True},
        ):
            with self.subTest(kwargs=kwargs):
                result, calls, state, db = self.run_remove(**kwargs)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(calls, [])
                self.assertFalse((state / "complete").exists())
                self.assertTrue((db / "one").exists())

    def test_partial_native_failure_keeps_receipt_without_success(self):
        result, calls, state, db = self.run_remove(fail_package="two")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, ["one", "two"])
        self.assertFalse((state / "complete").exists())
        self.assertFalse((db / "one").exists())
        self.assertTrue((db / "two").exists())
        self.assertIn("UNINSTALL_DONE one", (state / "removal.log").read_text())
        self.assertNotIn("UNINSTALL_DONE two", (state / "removal.log").read_text())

    def test_success_requires_native_removal_and_final_absence(self):
        result, calls, state, _ = self.run_remove()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, ["one", "two"])
        self.assertTrue((state / "complete").is_file())


class ReturnedHostTests(unittest.TestCase):
    def test_interrupted_transfer_resumes_same_attempt_and_builds_bound_manifest(self):
        from argparse import Namespace

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            backup = root / "new-backup"
            data = b"fresh returned-package archive"
            digest = hashlib.sha256(data).hexdigest()
            selected = list(remove_packages.PACKAGES[3:5])
            commands = []

            async def probe(*args, **kwargs):
                commands.extend(kwargs["commands"])
                attempt = json.loads((backup / "attempt.json").read_text())[
                    "attempt_id"
                ]
                output = f"BACKUP_SHA256 {digest}\nRETURNED_ATTEMPT {attempt}\nPARENT_BACKUP_SHA256 {'a' * 64}\n"
                output += "BACKUP_PATH /opt/usr/home/owner/share/tmp/samshush-package-backup-0123456789abcdef.tar.gz\n"
                output += "".join(
                    f"RETURNED_PACKAGE {package}\n" for package in selected
                )
                return {"commands": [{"stdout": output, "stderr": ""}]}

            args = Namespace(
                operation="prepare-returned",
                backup_dir=backup,
                tv="192.0.2.1",
                callback_host="192.0.2.2",
                logs=root / "logs",
            )
            with (
                patch("samsung_tv_root.archive_root.probe", probe),
                patch("samsung_tv_root.sdb.find_sdb", return_value="sdb"),
                patch("samsung_tv_root.sdb.SdbClient"),
                patch.object(frame, "wrapped", side_effect=lambda script: script),
                patch.object(frame, "check_result"),
                patch.object(
                    remove_packages,
                    "wrap_owner",
                    side_effect=lambda script, frame: script,
                ),
                patch.object(remove_packages, "download_archive") as download,
                patch("builtins.print"),
            ):
                download.side_effect = ValueError("transfer interrupted")
                self.assertEqual(remove_packages.run(args, frame), 1)
                pending = json.loads((backup / "attempt.json").read_text())
                self.assertEqual(pending["packages"], selected)
                self.assertFalse((backup / "manifest.json").exists())
                args.operation = "backup"
                download.side_effect = lambda client, archive, destination, expected: (
                    destination.write_bytes(data)
                )
                self.assertEqual(remove_packages.run(args, frame), 0)
                manifest = json.loads((backup / "manifest.json").read_text())
                self.assertEqual(manifest["attempt_id"], pending["attempt_id"])
                self.assertEqual(manifest["parent_backup_sha256"], "a" * 64)
                self.assertEqual(manifest["sha256"], digest)
                self.assertEqual(manifest["packages"], selected)
                self.assertTrue(
                    all(
                        f"/attempts/{pending['attempt_id']}" in command
                        for command in commands
                    )
                )
                args.operation = "remove"
                self.assertEqual(remove_packages.run(args, frame), 0)

    def test_changed_returned_backup_identity_fails_before_device_access(self):
        from argparse import Namespace

        with tempfile.TemporaryDirectory() as temporary:
            backup = Path(temporary)
            data = b"verified archive"
            (backup / "packages.tar.gz").write_bytes(data)
            original = {
                "schema": "samshush-returned-packages-v1",
                "attempt_id": "0123456789abcdef",
                "packages": list(remove_packages.PACKAGES[3:5]),
                "parent_backup_sha256": "a" * 64,
                "tv": "192.0.2.1",
                "build": frame.policy()["build"],
                "sha256": hashlib.sha256(data).hexdigest(),
            }
            args = Namespace(operation="remove", backup_dir=backup, tv="192.0.2.1")
            for key, value in (
                ("tv", "192.0.2.2"),
                ("build", "changed"),
                ("packages", ["foreign.package"]),
                ("attempt_id", "../wrong"),
                ("parent_backup_sha256", "unknown"),
                ("sha256", "b" * 64),
            ):
                with (
                    self.subTest(key=key),
                    patch("samsung_tv_root.archive_root.probe") as probe,
                ):
                    manifest = original | {key: value}
                    (backup / "manifest.json").write_text(json.dumps(manifest))
                    with self.assertRaises(ValueError):
                        remove_packages.run(args, frame)
                    probe.assert_not_called()


class ReturnedRemovalTests(unittest.TestCase):
    ATTEMPT = "0123456789abcdef"
    SELECTED = list(remove_packages.PACKAGES[3:5])
    ARCHIVE = (
        "/opt/usr/home/owner/share/tmp/samshush-package-backup-abcdef0123456789.tar.gz"
    )

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="samshush-returned-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.device = self.root / "device"
        self.base = self.device / remove_packages.ROOT.lstrip("/")
        self.base.mkdir(parents=True, mode=0o700)
        self.attempt = self.base / "attempts" / self.ATTEMPT
        self.archive = self.device / self.ARCHIVE.lstrip("/")
        self.archive.parent.mkdir(parents=True)
        for path in ("etc", "proc", "run/systemd/system"):
            (self.device / path).mkdir(parents=True, exist_ok=True)
        (self.device / "etc/os-release").write_text(
            f"BUILD_ID={frame.policy()['build']}\n"
        )
        (self.device / "proc/sys/kernel/random").mkdir(parents=True)
        (self.device / "proc/sys/kernel/random/boot_id").write_text("boot-one\n")
        for name, content in {
            "complete": "",
            "prepared": "",
            "build": frame.policy()["build"] + "\n",
            "packages": "\n".join(remove_packages.PACKAGES) + "\n",
            "backup.sha256": "a" * 64 + "\n",
            "host-verified.sha256": "a" * 64 + "\n",
            "original-private-evidence": "Do not rewrite this original receipt.\n",
        }.items():
            path = self.base / name
            path.write_text(content)
            path.chmod(0o600)
        self.original = {path.name: path.read_bytes() for path in self.base.iterdir()}
        (self.root / "packages.json").write_text("{}")
        (self.root / "options.json").write_text("{}")
        for package in self.SELECTED:
            self.add_package(package)
        for group in remove_packages.GROUPS:
            for unit in frame.policy()["optional_controls"][group]["systemd_units"]:
                (self.device / "run/systemd/system" / unit).symlink_to("/dev/null")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name in (
            "id",
            "stat",
            "readlink",
            "systemctl",
            "sqlite3",
            "owner_pkg",
            "sha256sum",
            "chsmack",
            "chown",
            "tar",
            "sleep",
            "sync",
        ):
            executable = self.bin / name
            executable.write_text(
                f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))} fake {name} "$@"\n'
            )
            executable.chmod(0o700)

    def add_package(self, package):
        database = self.root / "packages.json"
        values = json.loads(database.read_text())
        values[package] = {"version": "1.0", "disabled": "false", "removable": "true"}
        database.write_text(json.dumps(values))
        installed = self.device / f"opt/usr/apps/{package}"
        (installed / "bin").mkdir(parents=True)
        (installed / "bin/native").write_bytes(package.encode())
        (installed / "tizen-manifest.xml").write_text("<manifest />\n")
        registration = self.device / f"opt/share/packages/{package}.xml"
        registration.parent.mkdir(parents=True, exist_ok=True)
        registration.write_text("<package />\n")
        data = self.device / f"opt/usr/home/owner/apps_rw/{package}"
        data.mkdir(parents=True)
        (data / "private-data").write_bytes(b"fresh returned data")

    def configure(self, **options):
        (self.root / "options.json").write_text(json.dumps(options))

    def events(self):
        path = self.root / "events.jsonl"
        return (
            [json.loads(line) for line in path.read_text().splitlines()]
            if path.exists()
            else []
        )

    def run_action(self, action, *, attempt=None, selected=None, backup_hash=None):
        context = {} if action == "status" else {"attempt_id": attempt or self.ATTEMPT}
        if action in ("remove", "acknowledge"):
            context.update(
                selected_packages=selected or self.SELECTED, parent_backup_hash="a" * 64
            )
        if backup_hash is None and (self.attempt / "backup.sha256").exists():
            backup_hash = (self.attempt / "backup.sha256").read_text().strip()
        script = remove_packages.render(
            action,
            frame.policy(),
            archive=self.ARCHIVE,
            backup_hash=backup_hash or "",
            **context,
        )
        script = re.sub(
            r"/(?:opt|run|proc|etc|usr)/",
            lambda match: str(self.device) + match.group(),
            script,
        )
        script = re.sub(
            r"export PATH=.*", f"export PATH={self.bin}:/usr/bin:/bin", script
        )
        stage = self.root / "stage"
        stage.mkdir(exist_ok=True)
        result = subprocess.run(
            ["/bin/bash"],
            input=f"stage={shlex.quote(str(stage))}\n" + script,
            text=True,
            capture_output=True,
            timeout=30,
            env=os.environ | {"RETURNED_TEST_ROOT": str(self.root)},
        )
        self.assertEqual(
            {name: (self.base / name).read_bytes() for name in self.original},
            self.original,
        )
        return result

    def succeeds(self, action, **kwargs):
        result = self.run_action(action, **kwargs)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def fails(self, action, **kwargs):
        result = self.run_action(action, **kwargs)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def prepare(self):
        result = self.succeeds("prepare-returned")
        self.assertIn("RETURNED_ATTEMPT " + self.ATTEMPT, result.stdout)
        self.assertEqual(
            (self.attempt / "packages").read_text().splitlines(), self.SELECTED
        )
        self.assertTrue(self.archive.is_file())
        self.succeeds("acknowledge")
        self.assertFalse(self.archive.exists())

    def test_selective_repeat_and_status_preserve_original_receipt(self):
        self.prepare()
        self.fails("status")
        self.succeeds("remove")
        self.succeeds("status")
        self.succeeds("remove")
        self.assertEqual(
            [event[1] for event in self.events() if event[0] == "-u"], self.SELECTED
        )
        self.assertEqual(json.loads((self.root / "packages.json").read_text()), {})

    def test_backup_retry_reuses_same_attempt_and_its_selected_scope(self):
        self.succeeds("prepare-returned")
        events = self.events()
        self.fails("prepare-returned", attempt="1" * 16)
        self.assertEqual(self.events(), events)
        result = self.succeeds("backup")
        self.assertIn("BACKUP_PATH ", result.stdout)
        self.assertIn("PARENT_BACKUP_SHA256 " + "a" * 64, result.stdout)
        self.assertEqual(self.events(), events)
        self.succeeds("acknowledge")
        self.succeeds("remove")

    def test_missing_ack_or_changed_snapshot_blocks_every_uninstall(self):
        self.succeeds("prepare-returned")
        self.fails("remove")
        self.succeeds("acknowledge")
        native = self.device / f"opt/usr/apps/{self.SELECTED[1]}/bin/native"
        native.write_bytes(b"changed")
        self.fails("remove")
        self.assertFalse(any(event[0] == "-u" for event in self.events()))

    def test_new_file_after_backup_blocks_uninstall(self):
        self.prepare()
        extra = self.device / f"opt/usr/apps/{self.SELECTED[1]}/new-vendor-data"
        extra.write_bytes(b"not present in the verified backup")
        self.fails("remove")
        self.assertFalse(any(event[0] == "-u" for event in self.events()))

    def test_new_directory_or_link_after_backup_blocks_uninstall(self):
        self.prepare()
        extra = self.device / f"opt/usr/home/owner/apps_rw/{self.SELECTED[1]}/new-entry"
        extra.mkdir()
        self.fails("remove")
        extra.rmdir()
        extra.symlink_to("/unrelated/target")
        self.fails("remove")
        self.assertTrue(extra.is_symlink())
        self.assertFalse(any(event[0] == "-u" for event in self.events()))

    def test_failed_inventory_hash_reads_cannot_compare_as_empty_success(self):
        self.prepare()
        self.configure(hash_failure=True)
        self.fails("remove")
        self.assertFalse(any(event[0] == "-u" for event in self.events()))

    def test_partial_batch_retries_only_the_remaining_package(self):
        self.prepare()
        self.configure(fail_before=self.SELECTED[1])
        self.fails("remove")
        self.assertFalse((self.attempt / "complete").exists())
        self.succeeds("remove")
        self.succeeds("status")
        calls = [event[1] for event in self.events() if event[0] == "-u"]
        self.assertEqual(calls, [self.SELECTED[0], self.SELECTED[1], self.SELECTED[1]])

    def test_uncertain_uninstall_success_is_reconciled_by_full_absence(self):
        self.prepare()
        self.configure(fail_after=self.SELECTED[0])
        self.fails("remove")
        self.assertEqual(
            (self.attempt / "uninstall" / self.SELECTED[0]).read_text(), "begun\n"
        )
        self.succeeds("remove")
        self.assertEqual(
            [event[1] for event in self.events() if event[0] == "-u"], self.SELECTED
        )

    def test_partial_package_files_are_not_force_removed_on_retry(self):
        self.prepare()
        self.configure(partial=self.SELECTED[0])
        self.fails("remove")
        events = self.events()
        self.fails("remove")
        self.assertEqual(self.events(), events)
        self.assertFalse((self.attempt / "complete").exists())

    def test_incomplete_attempt_fails_status_even_when_packages_are_absent(self):
        self.prepare()
        self.succeeds("remove")
        (self.attempt / "complete").unlink()
        self.fails("status")

    def test_orphans_unsafe_paths_and_unreadable_inventory_reject_before_disable(self):
        absent = remove_packages.PACKAGES[0]
        orphan = self.device / f"opt/usr/apps/{absent}"
        orphan.mkdir()
        self.fails("prepare-returned")
        orphan.rmdir()
        owner = self.device / f"opt/usr/home/owner/apps_rw/{self.SELECTED[0]}"
        shutil.rmtree(owner)
        owner.symlink_to(self.root)
        self.fails("prepare-returned")
        owner.unlink()
        self.configure(unreadable_package=absent)
        self.fails("prepare-returned")
        self.assertEqual(self.events(), [])
        self.assertFalse(self.attempt.exists())

    def test_prepare_requires_hook_removal_and_unchanged_parent_backup(self):
        hook = self.device / "opt/usr/share/samshush-boot-v1"
        hook.mkdir()
        self.fails("prepare-returned")
        hook.rmdir()
        (self.base / "host-verified.sha256").write_text("b" * 64)
        # This intentional fixture corruption is itself the preserved preimage.
        self.original["host-verified.sha256"] = (
            self.base / "host-verified.sha256"
        ).read_bytes()
        self.fails("prepare-returned")
        self.assertEqual(self.events(), [])

    def test_returned_attempt_context_rejects_unknown_scope_and_path(self):
        for attempt in ("../bad", "a" * 15, "A" * 16):
            with self.assertRaises(ValueError):
                remove_packages.render(
                    "prepare-returned",
                    frame.policy(),
                    archive=self.ARCHIVE,
                    attempt_id=attempt,
                )
        for selected in (
            [],
            ["other.package"],
            [self.SELECTED[0]] * 2,
            self.SELECTED[::-1],
        ):
            with self.assertRaises(ValueError):
                remove_packages.render(
                    "remove",
                    frame.policy(),
                    backup_hash="b" * 64,
                    attempt_id=self.ATTEMPT,
                    selected_packages=selected,
                    parent_backup_hash="a" * 64,
                )


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "fake":
        raise SystemExit(returned_fake(sys.argv[2], sys.argv[3:]))
    unittest.main()
