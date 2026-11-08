import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import quiet_hours_worker as worker

HARNESS = r"""using System;
using W = ArtQuietHours;

internal static class QuietHoursTests {
    const int Start = 22 * 60, End = 7 * 60;
    static readonly W.Focus Artist = new W.Focus(
        123, 840UL, 5001, "org.tizen.art-app", "/usr/bin/dotnet-launcher-inhouse",
        "/opt/usr/apps/org.tizen.art-app/bin/ArtApp.dll");
    static readonly W.View Art = new W.View(4, 0, 2, 0, 1, Artist);
    static readonly W.Focus Saver = new(456, 910, 5001, "com.samsung.tv.spotlight-ss",
        "/usr/bin/dotnet-launcher-inhouse", "/usr/apps/com.samsung.tv.spotlight-ss/bin/ScreenSaverContent.dll");
    static readonly W.View Spotlight = new(0, 0, 0, 0, 0, Saver, true);

    static void Check(bool condition, string message) {
        if (!condition) throw new Exception(message);
    }

    // The transition runs at the worker's settle point, between its observations.
    sealed class TV {
        public readonly W.Policy Policy = new W.Policy();
        public W.View View = Art;
        public int Minute = Start;
        public W.Hours Hours = new(Start, End);
        public bool Stopped;
        public int NativeResult;
        public int Requests, Handoffs, Reads;
        public bool AllowHandoff;
        public long ElapsedMs = 100_000, SuspendedMs;

        public string Step(Action? transition = null) {
            return Policy.Step(() => W.InWindow(Minute, Hours.Start, Hours.End), () => { Reads++; return View; },
                () => transition?.Invoke(), () => Stopped,
                () => { Requests++; return NativeResult; }, ElapsedMs, SuspendedMs, launchArt: AllowHandoff
                    ? focus => { Check(focus == Saver, "wrong handoff identity"); Handoffs++; return NativeResult; } : null,
                saverVisible: () => View.ScreenSaverOn == true);
        }
    }

    static void Window() {
        foreach (int minute in new[]{Start, 1439, 0, End - 1})
            Check(W.InWindow(minute, Start, End), "missing overnight minute " + minute);
        foreach (int minute in new[]{End, Start - 1})
            Check(!W.InWindow(minute, Start, End), "extra overnight minute " + minute);
        Check(W.InWindow(600, 600, 720) && W.InWindow(719, 600, 720), "daytime window");
        Check(!W.InWindow(599, 600, 720) && !W.InWindow(720, 600, 720), "daytime edges");
        var tv = new TV { Minute = End };
        Check(tv.Step() == "outside" && tv.Requests == 0, "end must release quiet hours");
    }

    static void Classification() {
        Check(W.Classify(Art) == "art", "confirmed fullscreen Art was excluded");
        var image = Artist with { App = "com.samsung.tv.contentimageapp",
            Assembly = "/usr/apps/com.samsung.tv.contentimageapp/bin/ContentImageApp.dll" };
        Check(W.Classify(Art with { Focus = image }) == "art", "verified artwork renderer was excluded");
        foreach (var view in new[]{
            Art with { ModeType = 0 },                     // regular TV/input
            Art with { ScreenState = 1 },                  // Art Store/navigation
            Art with { ScreenState = 3, AmbientMode = 2 }, // low power
            Art with { PowerState = 2 },                   // standby with stale screen state
            Art with { AmbientMode = 0 },                  // regular TV with stale screen state
            Art with { Focus = Artist with { App = "com.samsung.tv.csfs" } },
            Art with { Focus = Artist with { App = "org.tizen.tv-viewer" } },
            Art with { Focus = image with { Assembly = Artist.ArtDll } },
            Art with { ScreenRc = -1 },
            Art with { Focus = null },
            Art with { Focus = Artist with { Uid = 0 } },
            Art with { Focus = Artist with { Exe = "/tmp/dotnet-launcher-inhouse" } },
            Art with { Focus = Artist with { Assembly = "/tmp/ArtApp.dll" } }
        }) {
            var tv = new TV { View = view };
            string state = W.Classify(view);
            Check(state == "other" || state == "unknown", "accepted unsafe observation " + view);
            tv.Step();
            Check(tv.Requests == 0, "requested standby on " + view);
        }
    }

    static void ArtArguments() {
        string path = Artist.ArtDll;
        Check(W.AssemblyArgument(Artist.App, new[]{path, "--start", path, ""}) == path,
            "legitimate argv[0] plus repeated assembly path was rejected");
        Check(W.AssemblyArgument(Artist.App, new[]{path}) == path,
            "assembly identity in argv[0] alone was ignored");
        foreach (string nearMatch in new[]{path + ".bak", "/tmp" + path, "ArtApp.dll"}) {
            Check(W.AssemblyArgument(Artist.App, new[]{Artist.Exe, nearMatch}) == "",
                "accepted a partial assembly match: " + nearMatch);
        }
        Check(W.AssemblyArgument("org.tizen.tv-viewer", new[]{path}) == "",
            "accepted the Art assembly for an unrelated application");
    }

    static void Recheck() {
        foreach (Action<TV> change in new Action<TV>[]{
            tv => tv.View = Art with { Focus = Artist with { Pid = Artist.Pid + 1 } },
            tv => tv.View = Art with { Focus = Artist with { StartTicks = Artist.StartTicks + 1 } },
            tv => tv.View = Art with { ScreenState = 1 },
            tv => tv.View = Art with { Focus = null },
            tv => tv.Minute = End,
            tv => tv.Hours = new W.Hours(60, 120),
            tv => tv.Stopped = true
        }) {
            var tv = new TV();
            tv.Step(() => change(tv));
            Check(tv.Requests == 0, "confirmation used stale state");
        }
    }

    static void SpotlightPolicy() {
        string dll = Saver.Assembly;
        Check(W.AssemblyArgument(Saver.App, new[]{dll, "--", dll}) == dll &&
            W.AssemblyArgument(Saver.App, new[]{"/tmp/" + dll}) == "", "exact saver assembly");
        var disabled = new TV { View = Spotlight };
        disabled.Step();
        Check(disabled.Requests == 0 && disabled.Handoffs == 0, "handoff requires opt-in");
        disabled.View = Art;
        disabled.Step();
        disabled.View = Spotlight with { ScreenSaverOn = null }; // bare worker never reads the flag
        disabled.Step();
        disabled.View = Art;
        Check(disabled.Step() == "requested" && disabled.Requests == 2,
            "unchecked Spotlight retained the opt-out worker's old Art latch");
        foreach (int minute in new[]{Start, End}) {
            var tv = new TV { View = Spotlight, Minute = minute, AllowHandoff = true };
            Check(tv.Step() == "handoff-requested" && tv.Handoffs == 1 && tv.Requests == 0, "handoff at both times");
            Check(tv.Step() == "cooldown" && tv.Handoffs == 1, "repeated an accepted handoff");
            tv.View = Art;
            tv.Step();
            Check(tv.Requests == (minute == Start ? 1 : 0), "Art standby scope or latch crossed with handoff");
        }
        var overlay = new TV { View = Art with { ScreenSaverOn = true }, AllowHandoff = true };
        overlay.Step();
        Check(overlay.Requests == 0 && overlay.Handoffs == 0, "standby with the screensaver still active");
        overlay.View = Art with { ScreenSaverReadFailed = true };
        overlay.Step();
        Check(overlay.Requests == 0, "standby without a valid compositor reading");
        foreach (var wrong in new[]{Spotlight with { ScreenSaverOn = null }, Spotlight with { ScreenSaverOn = false },
            Spotlight with { PowerState = 1 }, Spotlight with { AmbientMode = 1 }, Spotlight with { ModeType = 4 },
            Spotlight with { ScreenState = 2 }, Spotlight with { ScreenRc = -1 },
            Spotlight with { Focus = Saver with { Assembly = "/tmp/ScreenSaverContent.dll" } },
            Spotlight with { Focus = Saver with { Exe = "/usr/bin/dotnet" } },
            Spotlight with { Focus = Saver with { Uid = 0 } },
            Spotlight with { Focus = Saver with { App = "org.tizen.tv-viewer" } }}) {
            var tv = new TV { View = wrong, AllowHandoff = true };
            tv.Step();
            Check(tv.Handoffs == 0 && tv.Requests == 0, "unverified saver caused action");
        }
        foreach (var changed in new[]{Art, Spotlight with { ScreenSaverOn = false },
            Spotlight with { Focus = Saver with { StartTicks = 9000 } }}) {
            var tv = new TV { View = Spotlight, AllowHandoff = true };
            tv.Step(() => tv.View = changed);
            Check(tv.Handoffs == 0 && tv.Requests == 0, "stale handoff on dismissal or PID reuse");
        }
        var failed = new TV { View = Spotlight, Minute = End, AllowHandoff = true, NativeResult = 1 };
        Check(failed.Step() == "handoff-failed" && failed.Step() == "cooldown", "unbounded failed retries");
        failed.ElapsedMs += 60000; failed.NativeResult = 0;
        Check(failed.Step() == "handoff-requested" && failed.Handoffs == 2, "retry never re-enabled");
        var ordinary = new TV { View = Spotlight with { ScreenSaverOn = false }, Minute = End, AllowHandoff = true };
        ordinary.Step();
        Check(ordinary.Reads == 0, "unnecessary daytime foreground query");
    }

    static void StaleObservation() {
        foreach (int readDuration in new[]{500, 501}) {
            var policy = new W.Policy();
            long clock = 100_000;
            int requests = 0, reads = 0;
            string result = policy.Step(() => true,
                () => { if (++reads == 2) clock += readDuration; return Art; },
                () => {}, () => false, () => { requests++; return 0; },
                clock, 0, () => clock);
            if (readDuration > 500) {
                Check(result == "stale" && requests == 0,
                    "slow native read authorized standby with stale focus");
                result = policy.Step(() => true, () => Art,
                    () => {}, () => false, () => { requests++; return 0; },
                    clock, 0, () => clock);
            }
            Check(result == "requested" && requests == 1,
                "a fresh confirmed observation could not request standby");
        }
    }

    static void FreshSession() {
        var tv = new TV();
        Check(tv.Step() == "requested" && tv.Requests == 1, "missed entry into Art");
        tv.ElapsedMs += 3_600_000;
        Check(tv.Step() == "latched" && tv.Requests == 1, "repeated a successful request");
        tv.View = Art with { Focus = null };
        Check(tv.Step() == "unknown", "missing focus misclassified as leaving Art");
        tv.View = Art;
        Check(tv.Step() == "latched" && tv.Requests == 1, "missing focus reset the same-session latch");
        tv.View = Art with { ModeType = 0, ScreenState = 0, AmbientMode = 0 };
        tv.Step();
        tv.View = Art;
        Check(tv.Step() == "requested" && tv.Requests == 2, "fresh entry into Art was suppressed");
    }

    static void Resume() {
        var tv = new TV();
        Check(tv.Step() == "requested", "first standby request");
        tv.SuspendedMs += 3_600_000;
        tv.ElapsedMs += 3_600_000;
        tv.Step();
        Check(tv.Requests == 2, "fresh post-suspend Art would be latched all night");
    }

    static void Failure() {
        var tv = new TV { NativeResult = -1 };
        Check(tv.Step() == "failed" && tv.Requests == 1, "failed standby was reported as accepted");
        tv.NativeResult = 0;
        tv.ElapsedMs += 1;
        Check(tv.Step() == "cooldown" && tv.Requests == 1, "retried immediately after failure");
        tv.ElapsedMs += 3_600_000;
        Check(tv.Step() == "requested" && tv.Requests == 2, "failed request suppressed later Art");
    }

    static void HomeSchedule() {
        var original = new W.Hours(Start, End);
        var updated = new W.Hours(1380, 0);
        var schedule = new W.Schedule(original);
        string token = new string('a', 32), session = schedule.Session;
        string get = $"get {token}\n";
        string set = $"set {token} {session} 1320 420 1380 0\n";
        W.Hours saved = original;
        Action<W.Hours> write = hours => saved = hours;
        Func<W.Hours> recover = () => saved;
        Check(schedule.Answer(get, write, recover) == $"{token} {session} ok 1320 420\n", "GET readback");
        foreach (var bad in new[]{set.TrimEnd(), set + "\n", set.Replace("420", "0420"),
            set.Replace("1380", "1440"), set.Replace("1380", "0"), set.Replace(token, "abc"),
            set.Replace("set ", "set  "), set.Replace("1320", "-1"), get.Replace("get", "run")}) {
            try { schedule.Answer(bad, write, recover); throw new Exception("accepted malformed request"); }
            catch (ArgumentException) {}
        }
        Check(saved == original && schedule.Current == original, "invalid request changed times");
        Check(schedule.Answer(set, _ => throw new System.IO.IOException("disk full"), recover) ==
            $"{token} {session} conflict 1320 420\n", "failed persistence was acknowledged");
        Check(schedule.Current == original, "failed persistence changed effective hours");
        Check(schedule.Answer(set, write, recover) == $"{token} {session} ok 1380 0\n" && saved == updated,
            "save and read effective schedule");
        Check(schedule.Answer(set, _ => throw new Exception("unexpected save"), recover)
            == $"{token} {session} conflict 1380 0\n", "stale edit overwrote schedule");
        var restarted = new W.Schedule(original);
        Check(restarted.Answer(set, _ => throw new Exception("replayed old edit"), recover) ==
            $"{token} {restarted.Session} conflict 1320 420\n", "old request replayed after restart");
        string newSet = set.Replace(session, restarted.Session);
        Check(restarted.Answer(newSet, _ => throw new System.IO.IOException("directory sync failed"), recover) ==
            $"{token} {restarted.Session} conflict 1380 0\n" && restarted.Current == updated,
            "lost a committed update after late I/O failure");
    }

    static void Main(string[] args) {
        switch (args[0]) {
            case "window": Window(); break;
            case "classification": Classification(); break;
            case "art-arguments": ArtArguments(); break;
            case "recheck": Recheck(); break;
            case "spotlight": SpotlightPolicy(); break;
            case "stale-observation": StaleObservation(); break;
            case "fresh-session": FreshSession(); break;
            case "resume": Resume(); break;
            case "failure": Failure(); break;
            case "home-schedule": HomeSchedule(); break;
            default: throw new ArgumentException("Unknown scenario");
        }
    }
}
"""


class QuietHoursWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dotnet = (
            Path(__file__).resolve().parents[2]
            / ".local/toolchain/dotnet-sdk-6.0.428/dotnet"
        )
        if not cls.dotnet.is_file():
            raise unittest.SkipTest("Pinned .NET SDK 6.0.428 unavailable")
        temp = tempfile.TemporaryDirectory(prefix="samshush-quiet-worker-tests-")
        cls.addClassCleanup(temp.cleanup)
        cls.project = Path(temp.name)
        (cls.project / f"{worker.NAME}.cs").write_text(worker.SOURCE)
        (cls.project / "QuietHoursTests.cs").write_text(HARNESS)
        (cls.project / "QuietHoursTests.csproj").write_text(
            worker.PROJECT.replace(
                "<OutputType>",
                "<StartupObject>QuietHoursTests</StartupObject><OutputType>",
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
                "QuietHoursTests.csproj",
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

    def test_art_quiet_hours_policy(self):
        for scenario in (
            "window",
            "classification",
            "art-arguments",
            "recheck",
            "spotlight",
            "stale-observation",
            "fresh-session",
            "resume",
            "failure",
            "home-schedule",
        ):
            with self.subTest(scenario=scenario):
                result = subprocess.run(
                    [
                        str(self.dotnet),
                        str(self.project / "out/QuietHoursTests.dll"),
                        scenario,
                    ],
                    env=self.env,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
