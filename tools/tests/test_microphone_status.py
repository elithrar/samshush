import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


class MicrophoneStatusTests(unittest.TestCase):
    def test_named_panel_inputs(self):
        root = Path(__file__).resolve().parents[2]
        sdk = root / ".local/toolchain/dotnet-sdk-6.0.428/dotnet"
        framework = (
            root
            / "firmware/filesystems/T-RSMFAKUC-0090/pltf_t/root/usr/share/dotnet.tizen/framework"
        )
        if not (sdk.is_file() and (framework / "Tizen.Multimedia.dll").is_file()):
            self.skipTest("Pinned TV framework and .NET 6 SDK required")

        with tempfile.TemporaryDirectory() as temp:
            project = Path(temp)
            for name in ("MicrophoneStatus.csproj", "Program.cs"):
                shutil.copyfile(root / "tools/microphone_status" / name, project / name)
            (project / "NuGet.Config").write_text(
                "<configuration><packageSources><clear/></packageSources></configuration>"
            )
            (project / "Tests.cs").write_text(r"""
using System;
using static PanelMicrophones;

internal static class Tests
{
    private static void Check(bool ok, string scenario)
    {
        if (!ok) throw new Exception(scenario);
    }

    private static void Main()
    {
        const string mic = "mic0 cap mic0 capture codec-6";
        const string valid = "02-06: mic0 cap mic0 capture codec-6 :  : capture 1\n";
        const string decoder = "00-07: decoder :  : capture 1\n";
        Check(NamedInput(decoder + valid, "02-06", mic), "verified mic next to decoder");
        Check(!NamedInput(decoder, "02-06", mic), "decoder alone");
        Check(!NamedInput(valid + valid, "02-06", mic), "duplicate address");
        Check(!NamedInput(valid.Replace("mic0", "mic1"), "02-06", mic), "card remapped");
        Check(!NamedInput(valid.Replace("capture 1", "playback 1"), "02-06", mic), "playback");
        Check(!NamedInput(valid.Replace("capture 1", "capture 2"), "02-06", mic), "extra substream");
        Check(!NamedInput(null, "02-06", mic), "inventory inaccessible");
        Check(ClassifyStatus("state: RUNNING\nowner_pid: 321\n") == Active, "running input");
        Check(ClassifyStatus("closed\n") == Idle, "closed input");
        foreach (var malformed in new[] { null, "", "closed\nstate: RUNNING", "state: XRUN",
            "state: RUNNINGLY", "state: PREPARED", "owner_pid: 1\nstate: RUNNING" })
            Check(ClassifyStatus(malformed) == Unknown, "unverified stream state");
        Check(Combine(Idle, Idle) == Idle, "both closed");
        Check(Combine(Unknown, Idle) == Unknown, "one unavailable");
        Check(Combine(Unknown, Active) == Active, "one verified running");
        Check(Read(Idle, () => Active) == Active, "hardware client bypasses the framework mixer");
        Check(Read(Unknown, () => Idle) == Idle, "both panel streams verified closed");
        Check(Read(Idle, () => Unknown) == Idle, "framework reports idle");
        Check(Read(Unknown, () => Unknown) == Unknown, "neither source observable");
        Check(Read(Active, () => throw new Exception("unneeded read")) == Active, "framework active");
    }
}
""")
            env = os.environ | {
                "DOTNET_CLI_HOME": temp,
                "DOTNET_NOLOGO": "1",
                "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
            }
            for command in (
                [
                    sdk,
                    "build",
                    "-p:StartupObject=Tests",
                    f"-p:TizenFramework={framework}",
                    "-o",
                    "out",
                ],
                [sdk, "out/MicrophoneStatus.dll"],
            ):
                result = subprocess.run(
                    command,
                    cwd=project,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
