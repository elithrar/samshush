"""Exercise journal/preflight/rollback with simulated mounts and systemd.

This validates shell control flow on the host, not Linux privileges, SMACK,
mount propagation, or real TV behavior. Every mutable path is a temp fixture.
"""

import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from device_scripts import RUNTIME  # noqa: E402 - test discovery adds the tools directory above.


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="frame-runtime-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        (self.root / "units").mkdir()
        self.stage = self.root / "staging"
        self.stage.mkdir()
        (self.root / "uptime").write_text("100.00 50.00\n")
        self.config = {}
        (self.root / "config.json").write_text("{}")
        (self.root / "systemd.json").write_text(
            json.dumps(
                {
                    "one.service": {"active": "active"},
                    "two.service": {"active": "inactive"},
                }
            )
        )
        (self.root / "os-release").write_text("BUILD_ID=expected-build\n")
        for name in (
            "id",
            "uname",
            "readlink",
            "sha256sum",
            "stat",
            "chsmack",
            "cp",
            "mount",
            "umount",
            "systemctl",
            "sleep",
        ):
            wrapper = self.bin / name
            wrapper.write_text(
                "#!/bin/sh\nexec "
                + shlex.quote(sys.executable)
                + " "
                + shlex.quote(str(BASE / "tests/fake_tv.py"))
                + " "
                + name
                + ' "$@"\n'
            )
            wrapper.chmod(0o755)
        self.rows = []
        for index in range(2):
            target = self.root / f"ca{index}.pem"
            target.write_bytes(b"original")
            staged = self.stage / f"cert-{index}.pem"
            staged.write_bytes(b"filtered")
            self.rows.append(
                "\t".join(
                    (
                        str(target),
                        hashlib.sha256(b"original").hexdigest(),
                        hashlib.sha256(b"filtered").hexdigest(),
                        staged.name,
                    )
                )
            )

    def run_action(self, action, scope="both", *, certificate_journal=False):
        text = RUNTIME.replace(
            "STOP_GRACE_SECONDS=0",
            f"STOP_GRACE_SECONDS={getattr(self, 'stop_grace', 0)}",
        )
        if certificate_journal:
            text = text.replace(
                "STATE=/run/frame-runtime-v1", "STATE=" + str(self.root / "cert-state")
            )
        values = {
            "ACTION": action,
            "SCOPE": scope,
            "BUILD": "expected-build",
            "CERTS": "\n".join(self.rows),
            "UNITS": getattr(self, "test_units", "one.service\ntwo.service"),
            "PROCESSES": getattr(self, "test_processes", ""),
        }
        for name, value in values.items():
            text = text.replace(f"@@{name}@@", value)
        text = text.replace(
            "export PATH=/usr/sbin:/usr/bin:/sbin:/bin",
            f"export PATH={self.bin}:/usr/bin:/bin",
        )
        text = text.replace("/run/frame-runtime-v1", str(self.root / "state"))
        text = text.replace("/run/systemd/system/", str(self.root / "units") + "/")
        text = text.replace("/proc/uptime", str(self.root / "uptime"))
        text = text.replace("/proc/[0-9]*", str(self.root / "proc/[0-9]*"))
        text = text.replace("/etc/os-release", str(self.root / "os-release"))
        (self.root / "config.json").write_text(json.dumps(self.config))
        return subprocess.run(
            ["/bin/bash"],
            input=text,
            text=True,
            capture_output=True,
            env=os.environ
            | {"FAKE_TV": str(self.root), "FRAME_STAGE": str(self.stage)},
            timeout=30,
        )

    def test_bounded_stop_kills_only_selected_pending_service(self):
        self.stop_grace = 2
        self.config["hang_stop"] = "one.service"
        result = self.run_action("apply", "services")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [
            json.loads(line)
            for line in (self.root / "systemctl-calls.jsonl").read_text().splitlines()
        ]
        kills = [call for call in calls if call[0] == "kill"]
        self.assertEqual(
            kills, [["kill", "--kill-who=all", "--signal=KILL", "one.service"]]
        )
        self.assertTrue((self.root / "state/complete").exists())

    def test_failed_bounded_stop_restores_original_services(self):
        self.stop_grace = 2
        self.config.update(hang_stop="one.service", fail_kill="one.service")
        result = self.run_action("apply", "services")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "state").exists())
        self.assertFalse((self.root / "units/one.service").is_symlink())
        state = json.loads((self.root / "systemd.json").read_text())
        self.assertEqual(state["one.service"]["active"], "active")

    def test_surviving_helper_rolls_back_even_when_manager_stopped(self):
        helper = self.root / "helper"
        helper.write_text("binary fixture")
        self.test_processes = str(helper)
        proc = self.root / "proc/123"
        proc.mkdir(parents=True)
        (proc / "exe").symlink_to(helper.resolve())
        result = self.run_action("apply", "services")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SURVIVING SERVICE PROCESS 123", result.stdout)
        self.assert_original()

    def test_status_detects_helper_started_outside_the_masked_manager(self):
        helper = self.root / "helper"
        helper.write_text("binary fixture")
        self.test_processes = str(helper)
        self.assertEqual(self.run_action("apply", "services").returncode, 0)
        proc = self.root / "proc/456"
        proc.mkdir(parents=True)
        (proc / "exe").symlink_to(str(helper.resolve()) + " (deleted)")
        result = self.run_action("status", "services")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SURVIVING SERVICE PROCESS 456", result.stdout)

    def test_processes_without_executable_require_positive_exit_or_kernel_evidence(
        self,
    ):
        helper = self.root / "helper"
        helper.write_text("binary fixture")
        self.test_processes = str(helper)
        proc = self.root / "proc/123"
        proc.mkdir(parents=True)
        # A regular fixture makes readlink fail without treating a missing
        # executable as evidence that the process has exited.
        (proc / "exe").write_text("unreadable executable link")
        cases = [
            ("kernel", "S", 0x00200040, 1, True),
            ("zombie", "Z", 0x00400100, 1, True),
            ("dead", "X", 0x00400100, 1, True),
            ("live userspace", "S", 0x00400100, 1, False),
            ("zombie leader with live threads", "Z", 0x00400100, 2, False),
        ]
        for name, state, flags, threads, succeeds in cases:
            with self.subTest(name=name):
                fields = [state] + ["0"] * 17
                fields[6], fields[17] = str(flags), str(threads)
                (proc / "stat").write_text(
                    "123 (name with ) parentheses) " + " ".join(fields) + "\n"
                )
                result = self.run_action("apply", "services")
                self.assertEqual(result.returncode == 0, succeeds, result.stderr)
                if succeeds:
                    self.assertEqual(
                        self.run_action("status", "services").returncode, 0
                    )
                    self.assertEqual(self.run_action("restore").returncode, 0)
                else:
                    self.assertIn("PROCESS IDENTITY UNAVAILABLE 123", result.stderr)
                self.assert_original()
        for metadata in [None, "malformed", "123 (name) S 0 0 0 0 0 bad"]:
            with self.subTest(metadata=metadata):
                if metadata is None:
                    (proc / "stat").unlink()
                else:
                    (proc / "stat").write_text(metadata)
                result = self.run_action("apply", "services")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("PROCESS IDENTITY UNAVAILABLE 123", result.stderr)
                self.assert_original()

    def test_unavailable_helper_path_is_not_verified_absent(self):
        self.test_processes = str(self.root / "missing")
        result = self.run_action("apply", "services")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PROCESS PATH UNAVAILABLE", result.stderr)
        self.assert_original()

    def test_starting_service_is_stopped_and_restarted_on_restore(self):
        state = json.loads((self.root / "systemd.json").read_text())
        state["one.service"]["active"] = "activating"
        (self.root / "systemd.json").write_text(json.dumps(state))
        applied = self.run_action("apply", "services")
        self.assertEqual(applied.returncode, 0, applied.stderr)
        restored = self.run_action("restore")
        self.assertEqual(restored.returncode, 0, restored.stderr)
        state = json.loads((self.root / "systemd.json").read_text())
        self.assertEqual(state["one.service"]["active"], "active")

    def assert_original(self):
        for i in range(2):
            self.assertFalse((self.root / f"ca{i}.pem").is_symlink())
            self.assertEqual((self.root / f"ca{i}.pem").read_bytes(), b"original")
        self.assertFalse((self.root / "state").exists())
        self.assertFalse((self.root / "state.lock").exists())
        state = json.loads((self.root / "systemd.json").read_text())
        self.assertEqual(state["one.service"]["active"], "active")
        self.assertEqual(state["two.service"]["active"], "inactive")

    def test_apply_status_restore_and_repeated_restore(self):
        result = self.run_action("apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.run_action("status").returncode, 0)
        self.assertNotEqual(self.run_action("apply").returncode, 0)
        result = self.run_action("restore")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_original()
        self.assertEqual(self.run_action("restore").returncode, 0)

    def test_certificate_apply_and_restore_preserve_existing_voice_controls(self):
        self.assertEqual(self.run_action("apply", "voice").returncode, 0)
        original_journal = (self.root / "state/units.tsv").read_bytes()
        result = self.run_action("apply", "certificates", certificate_journal=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / "state/units.tsv").read_bytes(), original_journal)
        self.assertEqual(self.run_action("status").returncode, 0)
        result = self.run_action("status", "certificates", certificate_journal=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_action("restore", "certificates", certificate_journal=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.run_action("status").returncode, 0)
        self.assertTrue((self.root / "units/one.service").is_symlink())
        self.assertEqual((self.root / "ca0.pem").read_bytes(), b"original")
        self.assertEqual(self.run_action("restore").returncode, 0)
        self.assert_original()

    def test_failed_certificate_apply_preserves_voice_journal_and_masks(self):
        self.assertEqual(self.run_action("apply", "voice").returncode, 0)
        original_journal = (self.root / "state/units.tsv").read_bytes()
        self.config["fail_mount"] = 2
        result = self.run_action("apply", "certificates", certificate_journal=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "cert-state").exists())
        self.assertEqual((self.root / "state/units.tsv").read_bytes(), original_journal)
        self.assertEqual(self.run_action("status").returncode, 0)
        self.assertEqual((self.root / "ca0.pem").read_bytes(), b"original")

    def test_voice_scope_masks_and_restores_without_certificate_changes(self):
        result = self.run_action("apply", "voice")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "mount-count").exists())
        self.assertFalse((self.root / "state/certs.tsv").exists())
        self.assertEqual(self.run_action("status").returncode, 0)
        self.assertIn("microphone shutdown", result.stdout)
        self.assertEqual(self.run_action("restore").returncode, 0)
        self.assert_original()

    def test_voice_service_and_activation_units_are_masked_and_restored(self):
        self.test_units = "voice.path\nvoice.busname\nvoice.service"
        original = {name: {"active": "active"} for name in self.test_units.splitlines()}
        (self.root / "systemd.json").write_text(json.dumps(original))
        result = self.run_action("apply", "voice")
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in original:
            self.assertEqual(os.readlink(self.root / "units" / name), "/dev/null")
        self.assertEqual(self.run_action("status").returncode, 0)
        self.assertEqual(self.run_action("restore").returncode, 0)
        self.assertEqual(json.loads((self.root / "systemd.json").read_text()), original)
        self.assertEqual(list((self.root / "units").iterdir()), [])

    def test_voice_stop_failure_rolls_back_masks(self):
        self.config["fail_stop"] = "two.service"
        result = self.run_action("apply", "voice")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list((self.root / "units").iterdir()), [])
        self.assert_original()

    def test_failed_unit_is_masked_without_restarting_it_on_restore(self):
        original = {
            "one.service": {"active": "failed"},
            "two.service": {"active": "inactive"},
        }
        (self.root / "systemd.json").write_text(json.dumps(original))
        result = self.run_action("apply", "services")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "one.service\tfailed", (self.root / "state/units.tsv").read_text()
        )
        self.assertEqual(self.run_action("restore").returncode, 0)
        restored = json.loads((self.root / "systemd.json").read_text())
        self.assertEqual(restored["one.service"]["active"], "inactive")

    def test_masked_failed_unit_without_process_is_verified(self):
        (self.root / "systemd.json").write_text(
            json.dumps(
                {
                    "one.service": {"active": "failed"},
                    "two.service": {"active": "inactive"},
                }
            )
        )
        self.config["retain_failed_on_stop"] = True
        result = self.run_action("apply", "services")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.run_action("status").returncode, 0)
        self.config["failed_main_pid"] = 123
        self.assertNotEqual(self.run_action("status").returncode, 0)

    def test_preimage_drift_aborts_before_first_mount(self):
        (self.root / "ca1.pem").write_bytes(b"newer CA bundle")
        result = self.run_action("apply")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "mount-count").exists())
        self.assertFalse((self.root / "state").exists())
        self.assertEqual((self.root / "ca0.pem").read_bytes(), b"original")

    def test_persistent_run_filesystem_is_rejected(self):
        self.config["run_filesystem"] = "ext4"
        self.assertNotEqual(self.run_action("apply").returncode, 0)
        self.assert_original()

    def test_wrong_build_aborts(self):
        (self.root / "os-release").write_text("BUILD_ID=unknown\n")
        self.assertNotEqual(self.run_action("apply").returncode, 0)
        self.assert_original()

    def test_second_mount_failure_restores_first(self):
        self.config["fail_mount"] = 2
        result = self.run_action("apply")
        self.assertNotEqual(result.returncode, 0)
        self.assert_original()

    def test_stop_failure_restores_certificates_masks_and_active_state(self):
        self.config["fail_stop"] = "two.service"
        result = self.run_action("apply")
        self.assertNotEqual(result.returncode, 0)
        self.assert_original()
        self.assertEqual(list((self.root / "units").iterdir()), [])

    def test_failed_unmount_retains_journal_for_retry(self):
        self.assertEqual(self.run_action("apply").returncode, 0)
        self.config["fail_unmount"] = True
        self.assertNotEqual(self.run_action("restore").returncode, 0)
        self.assertTrue((self.root / "state/certs.tsv").exists())
        self.config.clear()
        self.assertEqual(self.run_action("restore").returncode, 0)
        self.assert_original()

    def test_existing_runtime_override_is_untouched(self):
        link = self.root / "units/one.service"
        link.symlink_to("/dev/null")
        self.assertNotEqual(self.run_action("apply").returncode, 0)
        self.assertEqual(os.readlink(link), "/dev/null")
        self.assertFalse((self.root / "mount-count").exists())

    def test_batch_queries_accept_reordered_records_and_reload_once(self):
        self.config["reverse_show"] = True
        result = self.run_action("apply", "voice")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = [
            json.loads(line)
            for line in (self.root / "systemctl-calls.jsonl").read_text().splitlines()
        ]
        self.assertEqual(sum(call[0] == "daemon-reload" for call in calls), 1)
        self.assertEqual(sum(call[0] == "mask" for call in calls), 1)
        self.assertEqual(sum(call[0] == "stop" for call in calls), 1)
        self.assertEqual(sum(call[0] == "show" for call in calls), 3)
        self.assertIn("phase=stop_begin monotonic_s=100.00", result.stdout)
        self.assertEqual(self.run_action("status").returncode, 0)

    def test_incomplete_or_malformed_batch_fails_before_mutation(self):
        for config in (
            {"omit_record": True},
            {"duplicate_record": True},
            {"omit_property": "Id"},
            {"omit_property": "MainPID"},
            {"omit_property": "UnitFileState"},
            {"bad_pid": True},
        ):
            with self.subTest(config=config):
                self.config = config
                result = self.run_action("apply", "voice")
                self.assertNotEqual(result.returncode, 0)
                self.assert_original()
                self.assertEqual(list((self.root / "units").iterdir()), [])

    def test_partial_mask_and_reload_failure_restore_full_batch(self):
        for config in ({"fail_mask": "two.service"}, {"fail_reload": True}):
            with self.subTest(config=config):
                self.config = config
                result = self.run_action("apply", "voice")
                self.assertNotEqual(result.returncode, 0)
                self.assert_original()
                self.assertEqual(list((self.root / "units").iterdir()), [])

    def test_status_detects_drift(self):
        self.assertEqual(self.run_action("apply").returncode, 0)
        (self.root / "state/cert-0.pem").write_bytes(b"unexpected change")
        self.assertNotEqual(self.run_action("status").returncode, 0)
        self.assertEqual(self.run_action("restore").returncode, 0)
        self.assert_original()


if __name__ == "__main__":
    unittest.main()
