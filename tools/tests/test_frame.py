import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
spec = importlib.util.spec_from_file_location("frame", BASE / "frame.py")
frame = importlib.util.module_from_spec(spec)
spec.loader.exec_module(frame)


class HostTests(unittest.TestCase):
    def test_voice_scope_excludes_certificates_and_unrelated_units(self):
        policy = {
            "build": "test",
            "certificates": [],
            "units": ["ads.service", "voice.path", "voice.service"],
            "voice_privacy": {"systemd_units": ["voice.path", "voice.service"]},
        }
        with patch.object(frame, "policy", return_value=policy):
            encoded = frame.render("apply", "voice").split()[2]
            script = frame.base64.b64decode(encoded).decode()
            self.assertIn("voice.path\nvoice.service", script)
            self.assertNotIn("ads.service", script)
            self.assertIn("SCOPE='voice'", script)

    def test_certificate_commands_isolate_recovery_but_share_operation_lock(self):
        for action in ("apply", "status", "restore"):
            payload = frame.render(action, "certificates", certificate_journal=True)
            script = frame.base64.b64decode(payload.split()[2]).decode()
            subprocess.run(["/bin/bash", "-n"], input=script, text=True, check=True)
            self.assertIn("STATE=/run/frame-certificates-v1", script)
            self.assertIn("LOCK=/run/frame-runtime-v1.lock", script)
            self.assertNotIn("STATE=/run/frame-runtime-v1", script)

    def test_optional_groups_have_independent_state_and_valid_shell(self):
        for group, entries in frame.policy()["optional_controls"].items():
            for action in ("apply", "status", "restore"):
                if entries["application_ids"]:
                    script = frame.render_apps(action, group=group)
                    self.assertIn(
                        f"STATE=/opt/usr/home/owner/share/frame-apps-{group}-v1", script
                    )
                    self.assertNotIn(
                        "STATE=/opt/usr/home/owner/share/frame-voice-apps-v2", script
                    )
                    subprocess.run(
                        ["/bin/bash", "-n"], input=script, text=True, check=True
                    )
                if entries["systemd_units"]:
                    script = frame.base64.b64decode(
                        frame.render(action, "services", unit_group=group).split()[2]
                    ).decode()
                    self.assertIn(f"STATE=/run/frame-services-{group}-v1", script)
                    self.assertIn("LOCK=/run/frame-runtime-v1.lock", script)
                    subprocess.run(
                        ["/bin/bash", "-n"], input=script, text=True, check=True
                    )

    def test_service_audit_is_complete_and_rejects_wrong_identity(self):
        script = frame.render_service_audit()
        subprocess.run(["/bin/bash", "-n"], input=script, text=True, check=True)
        self.assertNotIn("@@", script)
        self.assertIn("screen-analysis-service-manager.service", script)
        self.assertIn("com.samsung.tv.wifi-motion-mgr.service", script)
        self.assertIn("/usr/bin/dotnet-launcher-inhouse", script)
        result = subprocess.run(
            ["/bin/bash"],
            input="id() { printf 999; }\n" + script,
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("SERVICE AUDIT COMPLETE", result.stdout)

    def test_transport_preserves_script_literals(self):
        text = "printf '%s' 'spaces $HOME `false` ; \"quotes\"'"
        result = subprocess.run(
            ["/bin/bash", "-c", frame.wrapped(text)],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(result.stdout, 'spaces $HOME `false` ; "quotes"')

    def test_global_transport_executes_decoded_script(self):
        policy = {"build": "test", "certificates": [], "units": []}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = "printf '%s' 'literal $HOME ; payload'\n"
            nsenter = root / "nsenter"
            nsenter.write_text(
                '#!/bin/bash\ntest "$1" = --mount=/proc/1/ns/mnt || exit 99\nshift\nexec "$@"\n'
            )
            nsenter.chmod(0o700)
            with (
                patch.object(frame.device_scripts, "RUNTIME", payload),
                patch.object(frame, "policy", return_value=policy),
            ):
                for action in ("apply", "status", "restore"):
                    with self.subTest(action=action):
                        result = subprocess.run(
                            [
                                "/bin/bash",
                                "-c",
                                frame.wrapped(frame.render(action, "both")),
                            ],
                            env=os.environ
                            | {"PATH": str(root) + ":" + os.environ["PATH"]},
                            capture_output=True,
                            text=True,
                            check=True,
                        )
                        self.assertEqual(result.stdout, "literal $HOME ; payload")

    def test_native_app_scripts_and_reboot_readback(self):
        for action in ("apply", "status", "verify", "restore"):
            with self.subTest(action=action):
                script = frame.render_apps(action)
                subprocess.run(["/bin/bash", "-n"], input=script, text=True, check=True)
                self.assertNotIn("@@", script)
                if action == "verify":
                    self.assertIn("/proc/sys/kernel/random/boot_id", script)
                    self.assertLess(
                        script.index("BOOT READBACK"),
                        script.index("LAUNCH DENIED"),
                    )
        subprocess.run(
            ["/bin/bash", "-n"],
            input=frame.device_scripts.STARTUP_GUARDS,
            text=True,
            check=True,
        )

    def test_privacy_audit_has_valid_shell_and_requires_matching_build(self):
        script = frame.device_scripts.PRIVACY_AUDIT.replace(
            "@@BUILD@@", frame.policy()["build"]
        )
        subprocess.run(["/bin/bash", "-n"], input=script, text=True, check=True)
        # Identity failure must stop before any device query.
        result = subprocess.run(
            ["/bin/bash"],
            input="id() { printf 999; }\n" + script,
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("PRIVACY AUDIT COMPLETE", result.stdout)

    def test_hdmi_device_count_is_bounded_and_checked(self):
        script = frame.device_scripts.HDMI_AUDIT.replace("@@BUILD@@", "test")
        subprocess.run(["/bin/bash", "-n"], input=script, text=True, check=True)
        parser = script[
            script.index("values=()") : script.index("query GetActiveDeviceName")
        ]
        for reply, expected in (
            ("   int32 0\n   int32 2", 0),
            ("   int32 -1\n   int32 2", 1),
            ("   int32 0\n   int32 99", 1),
            ("unavailable", 1),
        ):
            with self.subTest(reply=reply):
                result = subprocess.run(
                    [
                        "/bin/bash",
                        "-c",
                        "set -eu; count_reply=$1\n" + parser,
                        "test",
                        reply,
                    ],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, expected, result.stderr)

    def test_sound_sensor_uses_separate_recovery_state(self):
        for action in ("apply", "status", "verify", "restore"):
            script = frame.render_apps(action, sound_sensor=True)
            subprocess.run(["/bin/bash", "-n"], input=script, text=True, check=True)
            self.assertIn(
                "STATE=/opt/usr/home/owner/share/frame-sound-sensor-v1", script
            )
            self.assertNotIn("frame-voice-apps-v2", script)
            self.assertIn("com.samsung.tv.ambient-sound", script)
            self.assertNotIn("com.samsung.tv.tvsensor-iot-service", script)
            self.assertNotIn("com.samsung.tv.alexa-client", script)
            self.assertNotIn("25 persistent", script)

    def test_failed_remote_command_is_not_success(self):
        good = dict(
            authenticated=True,
            uid=0,
            euid=0,
            gid=0,
            egid=0,
            system_files_unchanged=True,
            staging_cleaned=True,
            commands=[{"exit_code": 0, "timed_out": False}],
        )
        frame.check_result(good)
        for bad in (
            {"commands": []},
            {"uid": 901},
            {"staging_cleaned": False},
            {"commands": [{"exit_code": 1, "timed_out": False}]},
            {"commands": [{"exit_code": 0, "timed_out": True}]},
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                frame.check_result(good | bad)

    def test_checksum_rejects_tampering_and_path_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "file").write_text("original")
            expected = frame.hashlib.sha256(b"original").hexdigest()
            (root / "SHA256SUMS.json").write_text(json.dumps({"file": expected}))
            with patch.object(frame, "ASSETS", root):
                frame.verify_files()
                (root / "file").write_text("changed")
                with self.assertRaises(ValueError):
                    frame.verify_files()
                (root / "SHA256SUMS.json").write_text(
                    json.dumps({"../escape": expected})
                )
                with self.assertRaises(ValueError):
                    frame.verify_files()

    def test_doctor_rejects_unrunnable_sdb(self):
        from samsung_tv_root import sdb

        with (
            patch.object(frame.sys, "argv", ["frame.py", "doctor"]),
            patch.object(frame, "verify_files"),
            patch("sys.stdout", new_callable=io.StringIO),
            patch.object(sdb, "find_sdb", return_value=Path("/fake/sdb")),
            patch.object(frame.subprocess, "run", side_effect=OSError("bad CPU type")),
        ):
            self.assertEqual(frame.main(), 1)

    def test_global_operations_enter_namespace_and_inspection_does_not(self):
        p = {"build": "test", "certificates": [], "units": []}
        with patch.object(frame, "policy", return_value=p):
            self.assertTrue(
                frame.render("apply", "both").endswith(
                    "nsenter --mount=/proc/1/ns/mnt /bin/bash"
                )
            )
            self.assertTrue(frame.render("inspect", "none").startswith("# Generated"))
            for action in ("inspect", "status", "apply", "restore"):
                subprocess.run(
                    ["/bin/bash", "-n"],
                    input=frame.render(action, "both"),
                    text=True,
                    check=True,
                )


if __name__ == "__main__":
    unittest.main()
