import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import frame  # noqa: E402
import remove_packages  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
