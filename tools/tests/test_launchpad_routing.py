import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import launchpad_routing as routing


def original():
    return {
        "Operation": "com.samsung.tv.csfs",
        "Strings": {
            routing.HOME_KEY: "com.samsung.tv.csfs",
            "db/appfw/overlay_home_appid": "",
            "db/appfw/startup_appid": "org.tizen.tv-viewer",
            "db/appfw/booting_appid_backup": "org.tizen.tv-viewer",
            "db/first_screen/type": "com.samsung.tv.csfs",
        },
        "AutoStart": 0,
    }


def evidence(operation="apply", phase="applied"):
    saved = original()
    current = copy.deepcopy(saved)
    if phase == "applied":
        current["Operation"] = "ABCDEFGHIJ.Launchpad"
        current["Strings"][routing.HOME_KEY] = current["Operation"]
    return {
        "schema": "samshush-home-routing-v1",
        "operation": operation,
        "phase": phase,
        "current": current,
        "receipt": {
            "Version": 1,
            "Build": routing.BUILD,
            "Package": "ABCDEFGHIJ",
            "App": "ABCDEFGHIJ.Launchpad",
            "Phase": phase,
            "Original": saved,
        },
    }


def output(value):
    return (
        "LAUNCHPAD_ROUTING_JSON=" + json.dumps(value) + "\nLAUNCHPAD_ROUTING_CLEANED\n"
    )


class RoutingEvidenceTests(unittest.TestCase):
    def test_complete_apply_restore_and_query(self):
        for operation, phase in (
            ("apply", "applied"),
            ("restore", "restored"),
            ("status", "rolled-back"),
            ("query", "recovery-needed"),
        ):
            with self.subTest(operation=operation):
                value = evidence(operation, phase)
                self.assertEqual(routing.parse_output(output(value)), value)

    def test_absent_journal_is_explicit_and_only_read_only(self):
        for operation in ("query", "status", "apply", "restore"):
            value = evidence(operation, "absent") | {"receipt": None}
            if operation in ("query", "status"):
                self.assertEqual(routing.parse_output(output(value)), value)
            else:
                with self.assertRaises(ValueError):
                    routing.parse_output(output(value))

    def test_incomplete_ambiguous_or_inconsistent_evidence_fails(self):
        valid = output(evidence())
        for raw in (
            valid.replace("LAUNCHPAD_ROUTING_CLEANED\n", ""),
            valid + "LAUNCHPAD_ROUTING_CLEANED\n",
            valid + valid.splitlines()[0] + "\n",
            valid.replace('"schema":', '"schema":"duplicate", "schema":', 1),
        ):
            with self.subTest(raw=raw[:60]), self.assertRaises(ValueError):
                routing.parse_output(raw)
        for mutation in (
            lambda v: v.update(phase="prepared"),
            lambda v: v["current"].update(AutoStart=True),
            lambda v: v["current"]["Strings"].pop("db/first_screen/type"),
            lambda v: v["current"]["Strings"].update(
                {"db/appfw/overlay_home_appid": "drift"}
            ),
            lambda v: v["receipt"].update(Build="other"),
            lambda v: v["receipt"].update(Version=True),
            lambda v: v["receipt"].update(App="OTHERPKG00.Launchpad"),
            lambda v: v["receipt"]["Original"].update(Operation="other.home"),
        ):
            value = evidence()
            mutation(value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                routing.parse_output(output(value))

    def test_identity_and_artifacts_reject_untrusted_values(self):
        artifacts = {".dll": b"dll", ".runtimeconfig.json": b"{}"}
        for package, app in (
            ("abc", "abc.Launchpad"),
            ("ABCDEFGHIJ", "OTHERPKG00.X"),
            ("ABCDEFGHIJ", "ABCDEFGHIJ.X'; DROP TABLE x;--"),
        ):
            with self.subTest(app=app), self.assertRaises(ValueError):
                routing.render_script(
                    "apply", artifacts, package_id=package, app_id=app
                )
        with self.assertRaises(ValueError):
            routing.render_script("exec", artifacts)
        with self.assertRaises(ValueError):
            routing.render_script("status", artifacts, package_id="ABCDEFGHIJ")
        for value in (b"", b"x" * 262145, "dll"):
            with self.subTest(value=type(value)), self.assertRaises(ValueError):
                routing.render_script("query", artifacts | {".dll": value})

    def test_all_rendered_operations_have_valid_bash_syntax(self):
        for operation in ("query", "apply", "status", "restore"):
            args = (
                {"package_id": "ABCDEFGHIJ", "app_id": "ABCDEFGHIJ.Launchpad"}
                if operation == "apply"
                else {}
            )
            script = routing.render_script(
                operation, {".dll": b"dll", ".runtimeconfig.json": b"{}"}, **args
            )
            with self.subTest(operation=operation):
                subprocess.run(
                    ["bash", "-n"],
                    input=script,
                    text=True,
                    check=True,
                    capture_output=True,
                )
                self.assertNotIn("@@", script)
                self.assertNotIn("a.app_exec=", script)


HARNESS = r"""using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;
using R = LaunchpadRouting;

internal static class RoutingTests {
    const string Home = "db/appfw/home_appid";
    const string App = "ABCDEFGHIJ.Launchpad";
    static R.State Original() => new R.State("com.samsung.tv.csfs", new Dictionary<string,string> {
        [Home] = "com.samsung.tv.csfs", ["db/appfw/overlay_home_appid"] = "",
        ["db/appfw/startup_appid"] = "org.tizen.tv-viewer",
        ["db/appfw/booting_appid_backup"] = "org.tizen.tv-viewer",
        ["db/first_screen/type"] = "com.samsung.tv.csfs" }, 0);
    static R.State Clone(R.State state) => state with { Strings = new Dictionary<string,string>(state.Strings) };
    static void Check(bool ok, string message) { if (!ok) throw new Exception(message); }
    static string Fails(Action run) {
        try { run(); } catch (Exception e) { return e.Message; }
        throw new Exception("Expected failure");
    }
    static R.Receipt Receipt(string phase="prepared") => new R.Receipt(1,
        "T-RSMFAKUC-0090-REL-202607141953", "ABCDEFGHIJ", App, phase, Original());
    static void Transaction(string scenario) {
        var original = Original(); var target = R.Target(original, App); var state = Clone(original);
        var phases = new List<string>(); int operations = 0, homes = 0; bool fail = true;
        bool restoring = scenario == "restore-retry";
        if (restoring) state = Clone(target);
        var before = Clone(state); var after = restoring ? original : target;
        Action<string> operation = value => {
            operations++;
            if (scenario == "operation-failure" && fail) { fail=false; throw new Exception("operation failed"); }
            if (scenario == "rollback-failure" && operations == 2) throw new Exception("rollback failed");
            if (scenario != "silent-operation") state = state with { Operation = value };
            if (scenario == "setter-side-effect") state.Strings[Home] = value;
            if (scenario == "retained-drift") state.Strings["db/appfw/overlay_home_appid"] = "other.app";
        };
        Action<string> home = value => {
            homes++;
            if ((scenario == "home-failure" || scenario == "rollback-failure" || scenario == "restore-retry") && fail) {
                fail=false; throw new Exception("home failed");
            }
            if (scenario != "silent-home") state.Strings[Home] = value;
            if (scenario == "home-failure-after-write" && fail) { fail=false; throw new Exception("late home failure"); }
        };
        Action<string> phase = value => {
            phases.Add(value);
            if (scenario == "journal-full" && (value == "applied" || value == "rolling-back")) throw new Exception("journal full");
            if (scenario == "initial-journal-failure") throw new Exception("journal unavailable");
        };
        Action change = () => R.Change(before, after, () => Clone(state), operation, home, phase, restoring);
        if (scenario == "success" || scenario == "setter-side-effect") {
            change(); Check(R.Equal(state,target), "target not reached");
            Check(phases.SequenceEqual(new[]{"applying","applied"}), "wrong success phases");
            if (scenario == "setter-side-effect") Check(homes == 0, "stale state caused extra setting write");
            Check(original.Strings[Home] == "com.samsung.tv.csfs", "original mutated"); return;
        }
        if (scenario == "preflight-drift") state = state with { AutoStart = 1 };
        string error = Fails(change);
        if (scenario == "preflight-drift" || scenario == "initial-journal-failure") {
            Check(operations == 0 && homes == 0, "mutation before journal/preflight"); return;
        }
        if (scenario == "retained-drift") {
            Check(state.Strings["db/appfw/overlay_home_appid"] == "other.app", "external drift overwritten");
            Check(operations == 1 && homes == 0 && phases.Last() == "recovery-needed", "unsafe drift rollback"); return;
        }
        if (scenario == "rollback-failure") {
            Check(!R.Equal(state,original) && phases.Last() == "recovery-needed", "partial failure lost");
            R.Change(Clone(state), original, () => Clone(state), value => state = state with {Operation=value},
                value => state.Strings[Home]=value, phases.Add, true);
            Check(R.Equal(state,original) && phases.Last() == "restored", "partial recovery failed"); return;
        }
        Check(R.Equal(state,before), "rollback did not restore preimage: " + error);
        if (scenario == "journal-full") Check(error.Contains("rollback verified"), "journal failure hid recovery result");
        Check(phases.Last() == (restoring ? "restore-failed" : "rolled-back"), "wrong rollback phase");
        if (restoring) {
            R.Change(Clone(state), original, () => Clone(state), operation, home, phases.Add, true);
            Check(R.Equal(state,original) && phases.Last() == "restored", "restore retry failed");
        }
    }
    static void Store(string scenario) {
        string directory = Path.Combine(Path.GetTempPath(), "routing-test-"+Guid.NewGuid());
        Directory.CreateDirectory(directory);
        try {
            int syncs=0; Action<string> sync = path => { Check(path==directory,"wrong sync target"); syncs++; };
            var receipt = Receipt();
            if (scenario == "interrupted-initial") {
                File.WriteAllText(directory+"/state.json.tmp",JsonSerializer.Serialize(receipt));
                Check(R.ReadReceipt(directory).Phase == "prepared", "prepared temp not recoverable");
            } else if (scenario == "invalid-initial") {
                File.WriteAllText(directory+"/state.json.tmp",JsonSerializer.Serialize(Receipt("applied")));
                Fails(() => R.ReadReceipt(directory)); return;
            } else if (scenario == "truncated-initial") {
                File.WriteAllText(directory+"/state.json.tmp","{\"Version\":1,");
                Fails(() => R.ReadReceipt(directory)); return;
            } else {
                R.WriteReceipt(directory,receipt,sync);
                Check(syncs==1, "rename was not synced");
            }
            if (scenario == "stale-temp") {
                File.WriteAllText(directory+"/state.json.tmp","truncated future write");
                Check(R.ReadReceipt(directory).Phase == "prepared", "usable canonical receipt ignored");
            }
            if (scenario == "sync-failure") {
                Fails(() => R.WriteReceipt(directory,Receipt("applying"), _ => throw new Exception("sync failed")));
                Check(R.ReadReceipt(directory).Phase == "applying", "sync failure destroyed receipt");
            }
            if (scenario == "duplicate-receipt") {
                string json=JsonSerializer.Serialize(receipt).Replace("\"Version\":1", "\"Version\":1,\"Version\":1");
                File.WriteAllText(directory+"/state.json",json); Fails(() => R.ReadReceipt(directory)); return;
            }
            if (scenario == "malformed-receipt") {
                File.WriteAllText(directory+"/state.json",JsonSerializer.Serialize(receipt with {Original=null!}));
                Fails(() => R.ReadReceipt(directory)); return;
            }
            R.WriteReceipt(directory,Receipt("restored"),sync);
            Check(R.ReadReceipt(directory).Phase=="restored" && !File.Exists(directory+"/state.json.tmp"), "restore journal failed");
        } finally { Directory.Delete(directory,true); }
    }
    static int Main(string[] args) {
        try {
            if (args[0].StartsWith("store:")) Store(args[0].Substring(6)); else Transaction(args[0]);
            Console.WriteLine("PASS " + args[0]); return 0;
        } catch(Exception e) { Console.Error.WriteLine(e); return 1; }
    }
}
"""


class NativeRoutingTransactionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dotnet = (
            Path(__file__).resolve().parents[2]
            / ".local/toolchain/dotnet-sdk-6.0.428/dotnet"
        )
        if not cls.dotnet.is_file():
            raise unittest.SkipTest("Pinned .NET SDK 6.0.428 unavailable")
        cls.temp = tempfile.TemporaryDirectory(prefix="samshush-routing-tests-")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.project = Path(cls.temp.name)
        (cls.project / "LaunchpadRouting.cs").write_text(routing.SOURCE)
        (cls.project / "RoutingTests.cs").write_text(HARNESS)
        (cls.project / "RoutingTests.csproj").write_text(
            routing.PROJECT.replace(
                "<OutputType>",
                "<StartupObject>RoutingTests</StartupObject><OutputType>",
                1,
            )
        )
        (cls.project / "NuGet.Config").write_text(
            "<configuration><packageSources><clear/></packageSources></configuration>"
        )
        cls.env = os.environ | {
            "DOTNET_CLI_HOME": str(cls.project),
            "DOTNET_NOLOGO": "1",
            "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
            "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1",
        }
        result = subprocess.run(
            [
                str(cls.dotnet),
                "build",
                "RoutingTests.csproj",
                "-c",
                "Release",
                "-o",
                "out",
                "--nologo",
                "--verbosity",
                "quiet",
            ],
            cwd=cls.project,
            env=cls.env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode:
            raise AssertionError(result.stdout + result.stderr)

    def run_scenario(self, scenario):
        result = subprocess.run(
            [str(self.dotnet), str(self.project / "out/RoutingTests.dll"), scenario],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), "PASS " + scenario)

    def test_transaction_failure_and_recovery_contracts(self):
        for scenario in (
            "success",
            "setter-side-effect",
            "operation-failure",
            "home-failure",
            "home-failure-after-write",
            "silent-operation",
            "silent-home",
            "preflight-drift",
            "retained-drift",
            "rollback-failure",
            "journal-full",
            "initial-journal-failure",
            "restore-retry",
        ):
            with self.subTest(scenario=scenario):
                self.run_scenario(scenario)

    def test_interrupted_journal_and_sync_failure_recovery(self):
        for scenario in (
            "normal",
            "stale-temp",
            "interrupted-initial",
            "invalid-initial",
            "truncated-initial",
            "sync-failure",
            "duplicate-receipt",
            "malformed-receipt",
        ):
            with self.subTest(scenario=scenario):
                self.run_scenario("store:" + scenario)


if __name__ == "__main__":
    unittest.main()
