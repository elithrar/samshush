#!/usr/bin/env python3
"""Rebuild the TV-only .NET helpers into a fresh local directory; never deploy."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import launchpad_routing
import quiet_hours_worker
from payload_sources import SOURCES

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dotnet", type=Path, help="Path to the .NET 6 SDK executable")
    parser.add_argument("--output", type=Path, default=ROOT / ".local/payload-build")
    args = parser.parse_args()
    cached = ROOT / ".local/toolchain/dotnet-sdk-6.0.428/dotnet"
    dotnet = args.dotnet or (cached if cached.is_file() else shutil.which("dotnet"))
    if not dotnet:
        parser.error(
            "Install the .NET 6 SDK or supply --dotnet; prebuilt assets do not need rebuilding"
        )
    env = os.environ | {
        "DOTNET_CLI_HOME": str(ROOT / ".local/dotnet-home"),
        "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
        "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1",
        "DOTNET_GENERATE_ASPNET_CERTIFICATE": "false",
        "DOTNET_NOLOGO": "1",
    }
    version = subprocess.check_output(
        [str(dotnet), "--version"], text=True, timeout=15, env=env
    ).strip()
    if version != "6.0.428":
        parser.error(f"Expected SDK 6.0.428, got {version}")
    args.output.mkdir(parents=True, exist_ok=False)
    result = {"sdk": version, "deployed": False, "helpers": []}
    sources = SOURCES | {
        launchpad_routing.NAME + ".cs": launchpad_routing.SOURCE,
        launchpad_routing.NAME + ".csproj": launchpad_routing.PROJECT,
        quiet_hours_worker.NAME + ".cs": quiet_hours_worker.SOURCE,
        quiet_hours_worker.NAME + ".csproj": quiet_hours_worker.PROJECT,
    }
    for name in (
        "SamsungTvArchiveRoot",
        "SamsungTvRootAgent",
        "OwnerExec",
        "HomeSourceReadout",
        launchpad_routing.NAME,
        quiet_hours_worker.NAME,
    ):
        project = args.output / name
        project.mkdir()
        for suffix in (".cs", ".csproj"):
            (project / (name + suffix)).write_text(sources[name + suffix])
        subprocess.run(
            [
                str(dotnet),
                "build",
                str(project / (name + ".csproj")),
                "--configuration",
                "Release",
                "--output",
                str(project / "out"),
                "--nologo",
                "--verbosity",
                "quiet",
            ],
            check=True,
            timeout=180,
            env=env,
        )
        data = (project / "out" / (name + ".dll")).read_bytes()
        baseline = ROOT / "tools/assets/payloads" / (name + ".dll")
        helper = {
            "name": name,
            "sha256": hashlib.sha256(data).hexdigest(),
            "matches_checked_in_asset": data == baseline.read_bytes(),
        }
        if name in (
            "OwnerExec",
            "HomeSourceReadout",
            launchpad_routing.NAME,
            quiet_hours_worker.NAME,
        ):
            runtimeconfig = name + ".runtimeconfig.json"
            config_data = (project / "out" / runtimeconfig).read_bytes()
            config_baseline = ROOT / "tools/assets/payloads" / runtimeconfig
            helper["runtimeconfig_sha256"] = hashlib.sha256(config_data).hexdigest()
            helper["runtimeconfig_matches_checked_in_asset"] = (
                config_data == config_baseline.read_bytes()
            )
        result["helpers"].append(helper)
    (args.output / "manifest.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
