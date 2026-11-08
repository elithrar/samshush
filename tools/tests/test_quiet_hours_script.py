"""Run quiet-hours device transactions with local files and a fake service manager."""

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import quiet_hours_script as qh

ASSETS = {
    "ArtQuietHours.dll": b"fake worker",
    "ArtQuietHours.runtimeconfig.json": b"{}",
}
VENDOR = b"#!/bin/bash\nprintf 'vendor script ran\\n'\n"
LEGACY = 'set -u\nprintf "privacy handoff ran\\n"\noriginal_rc=0\nexit "$original_rc"\n'


def digest(data):
    return hashlib.sha256(data).hexdigest()


MOCKS = r"""
id() { printf '0\n'; }
chsmack() { if [[ $1 != -a ]]; then printf '%s access="_"\n' "$1"; fi; }
stat() {
    local mode size
    if [[ $QH_OS == Darwin ]]; then mode=$(command stat -f %OLp "$3") || return 1; size=$(command stat -f %z "$3") || return 1
    else mode=$(command stat -c %a "$3") || return 1; size=$(command stat -c %s "$3") || return 1; fi
    case "$2" in %u) echo 0;; %a) echo "$mode";; '%u:%a') echo "0:$mode";; '%u:%g:%a') echo "0:0:$mode";; %s) echo "$size";; *) return 1;; esac
}
cp() { if [[ $1 == --preserve=all ]]; then shift; command cp -p "$@"; else command cp "$@"; fi; }
mv() {
    local dest=${@: -1}
    command mv "$@" || return 1
    if [[ $dest == "$QH_TEST/share/privacy/launch" || $dest == "$QH_TEST/share/privacy/SHA256SUMS" ]]; then
        if (cd "$QH_TEST/share/privacy" && sha256sum -c SHA256SUMS >/dev/null); then
            echo valid >> "$QH_TEST/transitions"
        else echo invalid >> "$QH_TEST/transitions"; fi
    fi
}
sync() { :; }
sleep() { :; }
systemctl() {
    if [[ $1 == stop ]]; then
        printf 'stop\n' >> "$QH_TEST/events"
        if [[ -f $QH_TEST/edit-on-stop ]]; then
            mv "$QH_TEST/edit-on-stop" "$ROOT/config"
        fi
        rm -f "$QH_TEST/unit"; return
    fi
    if [[ $1 == show && $2 != "$UNIT" ]]; then
        if [[ -e $QH_TEST/privacy-queued ]]; then printf 'LoadState=loaded\nActiveState=activating\nJob=4\n'
        else printf 'LoadState=loaded\nActiveState=failed\nJob=\n'; fi; return
    fi
    [[ $1 == show && $2 == "$UNIT" ]] || return 1
    local key=${3#--property=}
    if [[ ! -f $QH_TEST/unit ]]; then if [[ $key == LoadState ]]; then echo not-found; return 4; fi; return 1; fi
    case "$key" in
        LoadState) echo loaded;; ActiveState) echo active;; Transient) echo yes;;
        MainPID)
            if [[ -f $QH_TEST/edit-during-ready ]]; then mv "$QH_TEST/edit-during-ready" "$ROOT/config"; fi
            echo 321;;
        Description) if [[ -f $QH_TEST/foreign ]]; then echo Other; else echo "$DESC"; fi;;
        ExecStart) printf '{ path=/bin/bash ; argv[]=/bin/bash -c exec /bin/bash -s -- samshush-art-quiet-hours < <(cat "$0") %s/runner ; ignore_errors=no ; }\n' "$ROOT";;
        *) return 1;;
    esac
}
systemd-run() {
    local a b; read -r a b < "$ROOT/config" || return 1
    printf 'start %s %s\n' "$a" "$b" >> "$QH_TEST/events"
    [[ ! -f $QH_TEST/reject || $(cat "$QH_TEST/reject") != "$a $b" ]] || return 1
    [[ ! -f $QH_TEST/unit ]] || return 1
    until [[ $# == 0 || $1 == /bin/bash ]]; do shift; done
    [[ $# -gt 0 ]] || return 1
    "$@" || return 1
    [[ -s $RUN/ready ]] || return 1
    printf '%s %s\n' "$a" "$b" > "$QH_TEST/unit"
    mkdir -p "$QH_TEST/proc/321"
    ln -sf /bin/bash "$QH_TEST/proc/321/exe"
    printf '%s\0' /bin/bash -s -- samshush-art-quiet-hours > "$QH_TEST/proc/321/cmdline"
    local boot pid spotlight
    read -r boot pid spotlight < "$RUN/ready"
    printf '%s 321 %s\n' "$boot" "$spotlight" > "$RUN/ready"
}
"""


class QuietHoursLifecycleTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="samshush-quiet-test-")
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.share = self.base / "share"
        self.share.mkdir()
        self.state = self.share / "quiet"
        self.privacy = self.share / "privacy"
        self.hook = self.base / "parseinfo.sh"
        self.hook.write_bytes(VENDOR)
        self.hook.chmod(0o777)
        self.run = self.base / "run"
        self.run.mkdir()
        self.env = os.environ | {"QH_TEST": str(self.base), "QH_OS": os.uname().sysname}
        guard = self.base / "shell-guard"
        guard.write_text(
            # macOS Bash 3.2 sets the positional script name after BASH_ENV.
            """trap 'if [[ $0 == "$QH_TEST/share/quiet/"* && -z ${BASH_EXECUTION_STRING:-} ]]; then echo "UEP: unsigned script" >&2; exit 126; fi' DEBUG\n"""
        )
        self.env["BASH_ENV"] = str(guard)
        (self.base / "os-release").write_text(f"BUILD_ID={qh.BUILD}\n")
        (self.base / "hub").write_bytes(b"test boot host")
        (self.base / "boot-id").write_text("boot-a\n")
        self.dotnet = self.base / "dotnet"
        self.dotnet.write_text(
            """#!/bin/bash
case $2 in
    check) echo 'QUIET_HOURS_CHECK={"schema":1}';;
    run)
        case ${3:-off} in
            off) [[ $# == 2 ]] || exit 1;;
            --spotlight-art) [[ $# == 3 && ! -e $QH_TEST/reject-spotlight ]] || exit 1;;
            *) exit 1;;
        esac
        echo "${3:-off}" >> "$QH_TEST/worker-modes"
        echo QUIET_HOURS_READY;;
    *) exit 1;;
esac
"""
        )
        self.dotnet.chmod(0o755)
        substitutions = {
            "/opt/etc/parseinfo.sh": self.hook,
            "/opt/usr/share/samshush-boot-v1": self.privacy,
            "/opt/usr/share/.samshush": str(self.share / ".samshush"),
            "/run/": str(self.run) + "/",
            "/etc/os-release": self.base / "os-release",
            "/usr/lib/systemd/system/AT-HUB.service": self.base / "hub",
            "/proc/$pid/": str(self.base / "proc") + "/$pid/",
            "/proc/sys/kernel/random/boot_id": self.base / "boot-id",
            "/usr/bin/dotnet": self.dotnet,
        }
        for name in ("COMMON", "CONTROL", "RUNNER", "BOOT"):
            script = getattr(qh, name)
            for original, replacement in substitutions.items():
                script = script.replace(original, str(replacement))
            if name == "COMMON":
                script += MOCKS
            self.enterContext(patch.object(qh, name, script))
        for key, value in dict(
            ROOT=str(self.state),
            ORIGINAL_HASH=digest(VENDOR),
            UNIT_HASH=digest(b"test boot host"),
            LAUNCH=LEGACY,
        ).items():
            self.enterContext(patch.object(qh, key, value))

    def privacy_install(self):
        self.privacy.mkdir(mode=0o700)
        (self.run / "samshush-boot.result").write_text("boot-a 1\n")
        for name, data in {
            "original": VENDOR,
            "wrapper": qh.WRAPPER.encode(),
            "launch": LEGACY.encode(),
            "enabled": b"voice /run/existing privacy\n",
            "worker": b"existing privacy worker",
            "installed": b"",
        }.items():
            (self.privacy / name).write_bytes(data)
            (self.privacy / name).chmod(0o600)
        self.hook.write_text(qh.WRAPPER)
        self.hook.chmod(0o755)
        names = ("wrapper", "launch", "worker", "enabled")
        manifest = "".join(
            f"{digest((self.privacy / name).read_bytes())}  {name}\n" for name in names
        )
        (self.privacy / "SHA256SUMS").write_text(manifest)
        (self.privacy / "SHA256SUMS").chmod(0o600)
        return {
            name: (self.privacy / name).read_bytes() for name in (*names, "SHA256SUMS")
        }

    def action(self, name, start=None, end=None, *, spotlight_art=None, okay=True):
        script = qh.render(
            name, start=start, end=end, spotlight_art=spotlight_art, assets=ASSETS
        )
        p = subprocess.run(
            ["/bin/bash"],
            input=script,
            text=True,
            capture_output=True,
            env=self.env,
            timeout=40,
        )
        self.assertEqual(p.returncode == 0, okay, f"{p.stdout}\n{p.stderr}")
        self.assertFalse((self.run / "samshush-startup.lock").exists())
        self.assertFalse((self.run / "samshush-startup-control.lock").exists())
        return p.stdout + p.stderr

    def test_read_only_inspection_never_steals_privacy_boot_lock(self):
        self.privacy_install()
        (self.run / "samshush-boot.result").write_text("earlier-boot 0\n")
        # Prove ordinary readers succeed even while the actual startup worker owns its lock.
        lock = self.run / "samshush-startup.lock"
        lock.mkdir()
        for action in ("inspect", "status"):
            script = qh.render(action, assets=ASSETS)
            p = subprocess.run(
                ["/bin/bash"],
                input=script,
                text=True,
                capture_output=True,
                env=self.env,
            )
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertTrue(lock.is_dir())
        lock.rmdir()
        self.assertIn(
            "not completed on this boot", self.action("set", 1200, 450, okay=False)
        )
        (self.run / "samshush-boot.result").write_text("boot-a 1\n")
        (self.base / "privacy-queued").touch()
        self.assertIn("active job", self.action("set", 1200, 450, okay=False))
        self.assertFalse(self.state.exists())

    def test_invalid_intervals_and_unsupported_operations(self):
        for start, end in ((True, 2), (0, 0), (-1, 7), (1440, 7), (4, 4.1)):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                qh.render("set", start=start, end=end, assets=ASSETS)
        with self.assertRaises(ValueError):
            qh.render("remove", start=1, end=4, assets=ASSETS)
        with self.assertRaises(ValueError):
            qh.render("shell", assets=ASSETS)

    def test_stock_install_reboot_and_exact_restore(self):
        self.assertIn("QUIET_HOURS_STATE=absent", self.action("status"))
        self.assertIn("QUIET_HOURS_CHECK=", self.action("inspect"))
        self.assertFalse(self.state.exists())
        output = self.action("set", 22 * 60, 7 * 60)
        self.assertIn("22:00–07:00 (TV local time)", output)
        self.assertIn("QUIET_HOURS_STATE=active", output)
        self.assertIn("QUIET_HOURS_BOOT_MODE=stock", output)
        self.assertIn("QUIET_HOURS_READY=boot-a 321 1320 420", self.action("status"))
        rejected = subprocess.run(
            ["/bin/bash", str(self.state / "boot")],
            env=self.env,
            text=True,
            capture_output=True,
        )
        self.assertEqual(rejected.returncode, 126, rejected.stderr)
        self.assertIn("UEP: unsigned script", rejected.stderr)
        (self.base / "unit").unlink()
        shutil.rmtree(self.run / "samshush-quiet-hours-v1")
        (self.base / "boot-id").write_text("boot-b\n")
        p = subprocess.run(
            ["/bin/bash"],
            input=(self.state / "handoff").read_text(),
            env=self.env,
            text=True,
            capture_output=True,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("vendor script ran", p.stdout)
        self.assertIn("QUIET_HOURS_READY=boot-b 321 1320 420", self.action("status"))
        self.assertIn("QUIET_HOURS_STATE=removed", self.action("remove"))
        self.assertEqual(self.hook.read_bytes(), VENDOR)
        self.assertEqual(self.hook.stat().st_mode & 0o777, 0o777)
        self.assertFalse(self.state.exists())

    def test_privacy_handoff_preserves_enrollment_and_recovers_mixed_manifest(self):
        before = self.privacy_install()
        self.action("set", 1110, 405)
        for name in ("enabled", "worker", "wrapper"):
            self.assertEqual((self.privacy / name).read_bytes(), before[name])
        self.assertEqual(self.hook.read_text(), qh.WRAPPER)
        self.assertNotEqual((self.privacy / "launch").read_bytes(), before["launch"])
        # Exercise the composed handoff as AT-HUB actually invokes it, through stdin.
        (self.base / "unit").unlink()
        p = subprocess.run(
            ["/bin/bash"],
            input=(self.privacy / "launch").read_text(),
            env=self.env,
            text=True,
            capture_output=True,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("privacy handoff ran", p.stdout)
        self.assertIn("QUIET_HOURS_READY=boot-a 321 1110 405", self.action("status"))
        self.assertEqual(
            (self.base / "transitions").read_text().splitlines(), ["valid"] * 3
        )
        # Power loss during removal: the temporary manifest covers every unrelated
        # file and both preimages while either launch file can occupy the live path.
        (self.privacy / "SHA256SUMS").write_bytes(
            (self.state / "bridge.manifest").read_bytes()
        )
        (self.privacy / "launch").write_bytes(before["launch"])
        (self.state / "phase").write_text("removing\n")
        subprocess.run(
            ["sha256sum", "-c", "SHA256SUMS"],
            cwd=self.privacy,
            check=True,
            capture_output=True,
        )
        self.action("remove")
        self.assertEqual(
            (self.base / "transitions").read_text().splitlines(), ["valid"] * 6
        )
        self.assertEqual(
            {name: (self.privacy / name).read_bytes() for name in before}, before
        )

    def test_rejected_update_restores_prior_times_and_running_service(self):
        self.action("set", 1100, 450)
        (self.base / "reject").write_text("1200 360\n")
        self.assertIn(
            "QUIET_HOURS_ROLLBACK=verified", self.action("set", 1200, 360, okay=False)
        )
        self.assertEqual((self.state / "config").read_text(), "1100 450\n")
        self.assertIn("QUIET_HOURS_READY=boot-a 321 1100 450", self.action("status"))
        events = (self.base / "events").read_bytes()
        self.action("set", 1100, 450)
        self.assertEqual(
            (self.base / "events").read_bytes(),
            events,
            "Repeated set restarted the worker",
        )

    def test_spotlight_opt_in_survives_edits_and_both_policies_rollback(self):
        self.assertIn("QUIET_HOURS_SPOTLIGHT_ART=off", self.action("set", 1320, 420))
        self.assertEqual((self.base / "worker-modes").read_text(), "off\n")
        (self.base / "reject-spotlight").touch()
        rejected = self.action("set", 1380, 430, spotlight_art=True, okay=False)
        self.assertIn("QUIET_HOURS_ROLLBACK=verified", rejected)
        readback = self.action("status")
        self.assertIn("QUIET_HOURS_WINDOW=1320 420", readback)
        self.assertIn("QUIET_HOURS_SPOTLIGHT_ART=off", readback)
        (self.base / "reject-spotlight").unlink()

        enabled = self.action("set", 1320, 420, spotlight_art=True)
        self.assertIn("QUIET_HOURS_SPOTLIGHT_ART=on", enabled)
        self.assertEqual(
            (self.base / "worker-modes").read_text().splitlines()[-1], "--spotlight-art"
        )
        self.assertIn("QUIET_HOURS_SPOTLIGHT_ART=on", self.action("set", 1380, 480))
        # A Home edit only changes the minute pair.
        (self.state / "config").write_text("1380 390\n")
        readback = self.action("status")
        self.assertIn("QUIET_HOURS_WINDOW=1380 390", readback)
        self.assertIn("QUIET_HOURS_SPOTLIGHT_ART=on", readback)
        before = (self.base / "events").read_text()
        self.action("set", 1380, 390, spotlight_art=True)
        self.assertEqual((self.base / "events").read_text(), before)
        self.assertIn(
            "QUIET_HOURS_SPOTLIGHT_ART=off",
            self.action("set", 1380, 390, spotlight_art=False),
        )
        self.assertEqual(
            (self.base / "worker-modes").read_text().splitlines()[-1], "off"
        )

    def test_spotlight_policy_is_attested_by_running_worker(self):
        self.action("set", 1320, 420, spotlight_art=True)
        saved = self.state / "spotlight"
        saved.write_bytes(b"off\n")
        self.assertIn("not ready", self.action("status", okay=False))
        for malformed in (b"on", b"on\n\x00", b"off\n\n", b"ON\n"):
            with self.subTest(malformed=malformed):
                saved.write_bytes(malformed)
                self.assertIn(
                    "Invalid quiet-hours receipt", self.action("status", okay=False)
                )

    def test_failed_first_start_restores_original_boot_path(self):
        (self.base / "reject").write_text("1110 405\n")
        self.assertIn(
            "QUIET_HOURS_ROLLBACK=verified", self.action("set", 1110, 405, okay=False)
        )
        self.assertEqual(self.hook.read_bytes(), VENDOR)
        self.assertEqual((self.state / "phase").read_text(), "rolled-back\n")
        self.action("remove")
        self.assertFalse(self.state.exists())

    def test_home_edit_is_immediately_visible_to_status_and_survives_failed_cli_update(
        self,
    ):
        self.action("set", 1320, 420)
        config = self.state / "config"
        config.write_text("1200 360\n")
        events = (self.base / "events").read_bytes()
        self.assertIn("QUIET_HOURS_WINDOW=1200 360", self.action("status"))
        self.action("set", 1200, 360)
        self.assertEqual((self.base / "events").read_bytes(), events)
        # Simulate a second accepted Home edit immediately before SIGTERM completes.
        edit = self.base / "edit-on-stop"
        edit.write_text("1230 375\n")
        edit.chmod(0o600)
        (self.base / "reject").write_text("1290 435\n")
        self.assertIn(
            "QUIET_HOURS_ROLLBACK=verified", self.action("set", 1290, 435, okay=False)
        )
        self.assertIn("QUIET_HOURS_WINDOW=1230 375", self.action("status"))
        self.assertEqual(config.read_bytes(), b"1230 375\n")

    def test_status_rejects_malformed_and_unterminated_effective_schedule(self):
        self.action("set", 1320, 420)
        for invalid in (b"1320 420", b"013 42\n", b"3 3\n", b"3 42\n\n", b"3\x00 42\n"):
            with self.subTest(invalid=invalid):
                (self.state / "config").write_bytes(invalid)
                self.assertIn(
                    "Invalid quiet-hours receipt", self.action("status", okay=False)
                )

    def test_same_window_set_reports_one_snapshot_during_a_home_edit(self):
        self.action("set", 1320, 420)
        edit = self.base / "edit-during-ready"
        edit.write_text("1380 420\n")
        edit.chmod(0o600)
        receipt = self.action("set", 1320, 420)
        self.assertIn("QUIET_HOURS_WINDOW=1320 420", receipt)
        self.assertIn("QUIET_HOURS_READY=boot-a 321 1320 420", receipt)
        self.assertIn("QUIET_HOURS_WINDOW=1380 420", self.action("status"))

    def test_drift_refuses_replacement_and_unrelated_service_stop(self):
        self.privacy_install()
        self.action("set", 1200, 450)
        (self.privacy / "worker").write_text("altered unrelated file")
        self.assertIn("Unrelated privacy file drift", self.action("remove", okay=False))
        self.assertEqual((self.base / "unit").read_text(), "1200 450\n")
        (self.privacy / "worker").write_bytes(b"existing privacy worker")
        (self.base / "foreign").touch()
        self.assertIn("unrelated service", self.action("remove", okay=False))
        self.assertTrue((self.base / "unit").exists())
