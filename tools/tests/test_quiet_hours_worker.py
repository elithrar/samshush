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

    static void Check(bool condition, string message) {
        if (!condition) throw new Exception(message);
    }

    // The transition runs at the worker's settle point, between its observations.
    sealed class TV {
        public readonly W.Policy Policy = new W.Policy();
        public W.View View = Art;
        public int Minute = Start;
        public bool Stopped;
        public int NativeResult;
        public int Requests;
        public long ElapsedMs = 100_000, SuspendedMs;

        public string Step(Action? transition = null) {
            return Policy.Step(Start, End, () => Minute, () => View,
                () => transition?.Invoke(), () => Stopped,
                () => { Requests++; return NativeResult; }, ElapsedMs, SuspendedMs);
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
            ArtDll = "/usr/apps/com.samsung.tv.contentimageapp/bin/ContentImageApp.dll" };
        Check(W.Classify(Art with { Focus = image }) == "art", "verified artwork renderer was excluded");
        foreach (var view in new[]{
            Art with { ModeType = 0 },                     // regular TV/input
            Art with { ScreenState = 1 },                  // Art Store/navigation
            Art with { ScreenState = 3, AmbientMode = 2 }, // low power
            Art with { PowerState = 2 },                   // standby with stale screen state
            Art with { AmbientMode = 0 },                  // regular TV with stale screen state
            Art with { Focus = Artist with { App = "com.samsung.tv.csfs" } },
            Art with { Focus = Artist with { App = "org.tizen.tv-viewer" } },
            Art with { Focus = image with { ArtDll = Artist.ArtDll } },
            Art with { ScreenRc = -1 },
            Art with { Focus = null },
            Art with { Focus = Artist with { Uid = 0 } },
            Art with { Focus = Artist with { Exe = "/tmp/dotnet-launcher-inhouse" } },
            Art with { Focus = Artist with { ArtDll = "/tmp/ArtApp.dll" } }
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
            tv => tv.Stopped = true
        }) {
            var tv = new TV();
            tv.Step(() => change(tv));
            Check(tv.Requests == 0, "confirmation used stale state");
        }
    }

    static void StaleObservation() {
        foreach (int readDuration in new[]{500, 501}) {
            var policy = new W.Policy();
            long clock = 100_000;
            int requests = 0, reads = 0;
            string result = policy.Step(Start, End, () => Start,
                () => { if (++reads == 2) clock += readDuration; return Art; },
                () => {}, () => false, () => { requests++; return 0; },
                clock, 0, () => clock);
            if (readDuration > 500) {
                Check(result == "stale" && requests == 0,
                    "slow native read authorized standby with stale focus");
                result = policy.Step(Start, End, () => Start, () => Art,
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

    static void Main(string[] args) {
        switch (args[0]) {
            case "window": Window(); break;
            case "classification": Classification(); break;
            case "art-arguments": ArtArguments(); break;
            case "recheck": Recheck(); break;
            case "stale-observation": StaleObservation(); break;
            case "fresh-session": FreshSession(); break;
            case "resume": Resume(); break;
            case "failure": Failure(); break;
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
            "stale-observation",
            "fresh-session",
            "resume",
            "failure",
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
