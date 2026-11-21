"""Build and sign the on-demand TV data provider with the owner's saved profile."""

import argparse
import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree

SOURCE = Path(__file__).resolve().parent
ROOT = SOURCE.parents[1]
FRAMEWORK = (
    ROOT
    / "firmware/filesystems/T-RSMFAKUC-0090/pltf_t/root/usr/share/dotnet.tizen/framework"
)
ASSEMBLY = "MicrophoneStatus"


def build(output, dotnet, framework, cli, profile):
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    manifest = (SOURCE / "tizen-manifest.xml").read_bytes()
    identity = ElementTree.fromstring(manifest)
    filename = f"{identity.attrib['package']}-{identity.attrib['version']}.tpk"
    with tempfile.TemporaryDirectory(dir=output) as temporary:
        stage = Path(temporary)
        for name in ("Program.cs", f"{ASSEMBLY}.csproj"):
            shutil.copyfile(SOURCE / name, stage / name)
        # Everything referenced is in the SDK or the TV framework, so restore offline.
        (stage / "NuGet.Config").write_text(
            "<configuration><packageSources><clear/></packageSources></configuration>"
        )
        env = os.environ | {
            "DOTNET_CLI_HOME": str(ROOT / ".local/dotnet-home"),
            "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
            "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1",
            "DOTNET_GENERATE_ASPNET_CERTIFICATE": "false",
        }
        subprocess.run(
            [
                str(dotnet.resolve()),
                "build",
                f"{ASSEMBLY}.csproj",
                "-c",
                "Release",
                "-o",
                "out",
                "-p:UseSharedCompilation=false",
                f"-p:TizenFramework={framework.resolve()}",
                "--nologo",
                "--verbosity",
                "quiet",
            ],
            cwd=stage,
            env=env,
            check=True,
            timeout=120,
        )
        files = {"tizen-manifest.xml": manifest}
        for suffix in (".dll", ".deps.json", ".runtimeconfig.json"):
            name = ASSEMBLY + suffix
            files[f"bin/{name}"] = (stage / "out" / name).read_bytes()
        unsigned = stage / filename
        with zipfile.ZipFile(
            unsigned, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            for name, data in files.items():
                archive.writestr(name, data)
        signed = stage / "signed"
        signed.mkdir()
        subprocess.run(
            [
                str(cli.resolve()),
                "package",
                "-t",
                "tpk",
                "-s",
                profile,
                "-o",
                str(signed),
                "--",
                str(unsigned),
            ],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=90,
        )
        package = signed / filename
        if not package.is_file():
            raise ValueError(
                "The SDK produced no TPK. Check access to the saved signing profile."
            )
        with zipfile.ZipFile(package) as archive:
            expected = set(files) | {"signature1.xml", "author-signature.xml"}
            members = [
                entry.filename for entry in archive.infolist() if not entry.is_dir()
            ]
            if sorted(members) != sorted(expected) or any(
                archive.read(name) != data for name, data in files.items()
            ):
                raise ValueError("Signed provider package differs from the build")
        package.chmod(0o600)
        return package.replace(output / filename)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, required=True, help="Fresh private directory"
    )
    parser.add_argument(
        "--profile", required=True, help="Saved Samsung TV signing profile"
    )
    parser.add_argument(
        "--cli", type=Path, required=True, help="Official Tizen CLI or wrapper"
    )
    parser.add_argument("--framework", type=Path, default=FRAMEWORK)
    parser.add_argument(
        "--dotnet",
        type=Path,
        default=ROOT / ".local/toolchain/dotnet-sdk-6.0.428/dotnet",
    )
    args = parser.parse_args()
    try:
        print(build(args.output, args.dotnet, args.framework, args.cli, args.profile))
    except (OSError, ValueError, subprocess.SubprocessError, zipfile.BadZipFile) as exc:
        parser.exit(1, f"Provider build failed: {exc}\n")


if __name__ == "__main__":
    main()
