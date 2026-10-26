import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import device_scripts

APP = "com.example.Selected"
DLL = "/usr/apps/example/bin/Selected.dll"
LOADERS = ("/usr/bin/dotnet-loader", "/usr/bin/dotnet-launcher-inhouse")
RUNTIMES = (*LOADERS, "/usr/bin/dotnet", "/opt/app/apphost", "/usr/lib/CoreRuntime")

# All device I/O is redirected to the fixture. In particular, kill never signals
# a host process: the stop methods only record calls and remove a fake proc entry.
HELPERS = r"""
id() { echo 0; }
stat() {
 if [[ $* == *'/opt/usr/home/owner' ]]; then echo 5001; else echo 0:700; fi
}
change_process() {
 printf '/usr/apps/Other.dll\0' > "$TEST_ROOT/proc/42/cmdline"
}
test_dotnet() {
 case "$2" in
 /usr/bin/pkginfo) echo 'Enabled: 1';;
 /usr/bin/pkgcmd)
  local value=false
  [[ $3 != -D ]] || value=true
  printf '%s' "$value" > "$TEST_ROOT/flag"
  echo "flag $value" >> "$TEST_ROOT/events"
  if [[ $value == true && $SCENARIO == change-after-disable ]]; then change_process; fi
 ;;
 *) return 1;;
 esac
}
sqlite3() {
 if [[ $* == *'SELECT app_disable'* ]]; then
  cat "$TEST_ROOT/flag"
 else
  cat "$TEST_ROOT/expected"
 fi
}
app_launcher() {
 if [[ $1 == -r ]]; then
  if [[ -d $TEST_ROOT/proc/42 ]]; then
   echo 'result: running'
  else
   echo 'result: not running'
  fi
 else
  echo "The app with ID: $2 is not available for the user 5001"
  return 1
 fi
}
aul_test() {
 if [[ $1 == get_pid_for_uid ]]; then
  echo 'ret = 42'
 else
  [[ $1 == term_pid_without_restart && $2 == 42 ]] || return 1
  echo 'aul 42' >> "$TEST_ROOT/events"
  if [[ $SCENARIO == change-after-aul ]]; then change_process; fi
 fi
}
kill() {
 [[ $1 == -TERM && $2 == 42 ]] || return 1
 echo 'term 42' >> "$TEST_ROOT/events"
 if [[ $SCENARIO != survive ]]; then rm -rf "$TEST_ROOT/proc/42"; fi
}
sleep() { :; }
sync() { :; }
"""


class AppControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def prepare(self, runtime, arguments, expected=DLL, *, completed=False):
        self.fixture = Path(tempfile.mkdtemp(dir=self.root))
        self.journal = self.fixture / "journal"
        self.process = self.fixture / "proc/42"
        self.process.mkdir(parents=True)
        (self.process / "exe").symlink_to(runtime)
        if arguments is not None:
            (self.process / "cmdline").write_bytes(
                b"".join(argument.encode() + b"\0" for argument in arguments)
            )
        boot = self.fixture / "proc/sys/kernel/random/boot_id"
        boot.parent.mkdir(parents=True)
        boot.write_text("test-boot\n")
        (self.fixture / "stage").mkdir()
        (self.fixture / "os-release").write_text(
            "BUILD_ID=T-RSMFAKUC-0090-REL-202607141953\n"
        )
        (self.fixture / "flag").write_text("true" if completed else "false")
        (self.fixture / "expected").write_text(expected)
        (self.fixture / "events").touch()
        self.original = f"{APP}\tfalse\t42\t{expected}\n"
        if completed:
            self.journal.mkdir()
            (self.journal / "before.tsv").write_text(self.original)
            (self.journal / "complete").touch()

    def run_control(self, mode, scenario="normal"):
        script = device_scripts.APP_CONTROL.replace("@@MODE@@", mode).replace(
            "@@APPS@@", APP
        )
        for original, target in (
            ("/opt/usr/home/owner/share/frame-voice-apps-v2", self.journal),
            ("/etc/os-release", self.fixture / "os-release"),
            ("/proc/", str(self.fixture / "proc") + "/"),
        ):
            script = script.replace(original, str(target))
        script = script.replace(
            '/usr/bin/dotnet "$stage/OwnerExec.dll"',
            'test_dotnet "$stage/OwnerExec.dll"',
        )
        return subprocess.run(
            ["/bin/bash"],
            input="set -euo pipefail\n" + HELPERS + script,
            text=True,
            capture_output=True,
            timeout=10,
            env=os.environ
            | {
                "TEST_ROOT": str(self.fixture),
                "stage": str(self.fixture / "stage"),
                "SCENARIO": scenario,
            },
        )

    def events(self):
        return (self.fixture / "events").read_text().splitlines()

    def test_apply_journals_and_stops_exact_native_or_managed_process(self):
        cases = [
            (runtime, [runtime, DLL, "--app-id", APP], DLL) for runtime in RUNTIMES
        ]
        cases += [(runtime, [DLL, "--option", "value"], DLL) for runtime in LOADERS]
        native = "/usr/apps/example/bin/native"
        cases.append((native, [native, "--option"], native))
        for runtime, arguments, expected in cases:
            with self.subTest(runtime=runtime, arguments=arguments):
                self.prepare(runtime, arguments, expected)
                result = self.run_control("apply")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    (self.journal / "before.tsv").read_text(), self.original
                )
                self.assertEqual((self.journal / "boot-id").read_text(), "test-boot\n")
                self.assertTrue((self.journal / "complete").exists())
                self.assertEqual(self.events(), ["flag true", "aul 42", "term 42"])
                self.assertFalse(self.process.exists())
                self.assertIn(f"LAUNCH DENIED {APP}", result.stdout)
                self.assertFalse(Path(str(self.journal) + ".lock").exists())

    def test_status_rejects_surviving_process_from_completed_journal(self):
        for runtime in RUNTIMES:
            with self.subTest(runtime=runtime):
                self.prepare(runtime, [DLL, "--option"], completed=True)
                result = self.run_control("status")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"SURVIVING MANAGED APP 42 {DLL}", result.stdout)
                self.assertEqual(self.events(), [])
                self.assertTrue((self.journal / "complete").exists())

    def test_apply_rejects_identity_mismatches_before_mutation(self):
        cases = [
            (LOADERS[0], [DLL + ".other"], DLL),
            (LOADERS[0], ["/usr/apps/Other.dll"], DLL),
            (LOADERS[0], ["prefix" + DLL], DLL),
            (LOADERS[0], ["--file=" + DLL], DLL),
            (LOADERS[0], ["/usr/apps/native"], "/usr/apps/native"),
            ("/usr/bin/unrelated", [DLL], DLL),
            ("/tmp/dotnet-loader", [DLL], DLL),
            (LOADERS[0], [], DLL),
            (LOADERS[0], None, DLL),
        ]
        for runtime, arguments, expected in cases:
            with self.subTest(runtime=runtime, arguments=arguments):
                self.prepare(runtime, arguments, expected)
                result = self.run_control("apply")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Unexpected running executable", result.stderr)
                self.assertFalse(self.journal.exists())
                self.assertEqual((self.fixture / "flag").read_text(), "false")
                self.assertEqual(self.events(), [])
                self.assertTrue(self.process.exists())

    def test_changed_identity_is_rechecked_before_each_stop(self):
        for scenario, events in (
            ("change-after-disable", ["flag true"]),
            ("change-after-aul", ["flag true", "aul 42"]),
        ):
            with self.subTest(scenario=scenario):
                self.prepare(LOADERS[0], [DLL])
                result = self.run_control("apply", scenario)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.events(), events)
                self.assertTrue(self.process.exists())
                self.assertTrue((self.journal / "complete").exists())

    def test_survivor_after_both_stop_attempts_rolls_back_original_flag(self):
        self.prepare(LOADERS[0], [DLL])
        result = self.run_control("apply", "survive")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Selected process survived", result.stderr)
        self.assertEqual((self.fixture / "flag").read_text(), "false")
        self.assertEqual(
            self.events(), ["flag true", "aul 42", "term 42", "flag false"]
        )
        self.assertTrue(self.process.exists())
        self.assertFalse((self.journal / "complete").exists())
        self.assertEqual((self.journal / "restored.tsv").read_text(), self.original)

    def test_status_ignores_shared_loader_without_selected_dll(self):
        for runtime in LOADERS:
            for arguments in ([runtime], ["/usr/apps/Other.dll"], [DLL + ".other"]):
                with self.subTest(runtime=runtime, arguments=arguments):
                    self.prepare(runtime, arguments, completed=True)
                    result = self.run_control("status")
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertNotIn("SURVIVING", result.stdout)
                    self.assertEqual(self.events(), [])
