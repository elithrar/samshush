#!/usr/bin/env python3
"""Inspect and control the verified Frame Pro build through temporary root."""

import argparse
import asyncio
import base64
import datetime
import hashlib
import ipaddress
import json
import os
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

import device_scripts
import startup

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
ASSETS = HERE / "assets"
# Keep the pinned transport isolated from globally installed packages.
sys.path.insert(0, str(HERE / "vendor"))


def policy():
    return json.loads((HERE / "profile.json").read_text())


def verify_files():
    manifest = ASSETS / "SHA256SUMS.json"
    if not manifest.exists():
        raise ValueError(
            "Device asset manifest is missing: tools/assets/SHA256SUMS.json"
        )
    for name, expected in json.loads(manifest.read_text()).items():
        path = ASSETS / name
        if path.is_symlink() or not path.resolve().is_relative_to(ASSETS):
            raise ValueError(f"Unsafe package path: {name}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f"Package file changed: {name}")


def wrapped(script):
    # No script metacharacters pass through the transport command parser.
    encoded = base64.b64encode(script.encode()).decode()
    return f"printf %s {encoded} | base64 -d | /bin/bash"


def render(action, scope, *, certificate_journal=False, unit_group=None):
    p = policy()
    certs = "\n".join(
        "\t".join((c["path"], c["before"], c["after"], c["file"]))
        for c in p["certificates"]
    )
    selected_units = (
        p["voice_privacy"]["systemd_units"] if scope == "voice" else p["units"]
    )
    if unit_group is not None:
        selected_units = p["optional_controls"][unit_group]["systemd_units"]
        if not selected_units:
            raise ValueError("No assessed units in this group")
    units = "\n".join(selected_units)
    processes = (
        p["optional_controls"][unit_group].get("process_paths", [])
        if unit_group is not None
        else []
    )
    script = device_scripts.RUNTIME
    if certificate_journal:
        script = script.replace(
            "STATE=/run/frame-runtime-v1", "STATE=/run/frame-certificates-v1"
        )
    if unit_group is not None:
        script = script.replace(
            "STATE=/run/frame-runtime-v1", f"STATE=/run/frame-services-{unit_group}-v1"
        )
    for key, value in {
        "ACTION": action,
        "SCOPE": scope,
        "BUILD": p["build"],
        "CERTS": certs,
        "UNITS": units,
        "PROCESSES": "\n".join(processes),
    }.items():
        script = script.replace(f"@@{key}@@", value)
    if "@@" in script:
        raise ValueError("Unresolved script placeholder")
    if action == "inspect":
        return script
    encoded = base64.b64encode(script.encode()).decode()
    return f"printf %s {encoded} | base64 -d | nsenter --mount=/proc/1/ns/mnt /bin/bash"


def render_service_audit():
    p = policy()
    units = sorted(
        set(p.get("service_audit_units", []))
        | {
            unit
            for entry in p["optional_controls"].values()
            for unit in entry["systemd_units"]
        }
    )
    script = device_scripts.SERVICE_AUDIT.replace("@@BUILD@@", p["build"])
    return script.replace("@@UNITS@@", " ".join(shlex.quote(unit) for unit in units))


def render_apps(action, *, sound_sensor=False, group=None):
    """Preserve the deployed native-app journal and exact owner context."""
    if action not in ("apply", "status", "restore", "verify"):
        raise ValueError("Unknown app action")
    prefix = device_scripts.APP_PREFIX
    for token, name in (
        ("OWNER_DLL", "OwnerExec.dll"),
        ("OWNER_CONFIG", "OwnerExec.runtimeconfig.json"),
    ):
        encoded = base64.b64encode((ASSETS / "payloads" / name).read_bytes()).decode()
        prefix = prefix.replace("@@" + token + "@@", encoded)
    script = device_scripts.APP_CONTROL.replace(
        "@@MODE@@", "status" if action == "verify" else action
    )
    application_ids = (
        ["com.samsung.tv.ambient-sound"]
        if sound_sensor
        else policy()["voice_privacy"]["application_ids"]
    )
    if group is not None:
        if sound_sensor:
            raise ValueError("Sound sensor cannot share an optional-app group")
        application_ids = policy()["optional_controls"][group]["application_ids"]
        if not application_ids:
            raise ValueError("No assessed apps in this group")
    script = script.replace("@@APPS@@", "\n".join(application_ids))
    if action == "verify":
        start = script.index("if [[ $MODE == status")
        end = script.index('mkdir -m 700 "$LOCK"')
        script = script[:start] + device_scripts.APP_VERIFY + script[end:]
    if "@@" in prefix + script:
        raise ValueError("Unresolved app payload placeholder")
    boot = device_scripts.APP_BOOT_READBACK if action == "verify" else ""
    result = prefix + boot + script + "OWNER\n"
    if sound_sensor:
        result = result.replace("frame-voice-apps-v2", "frame-sound-sensor-v1")
    if group is not None:
        result = result.replace("frame-voice-apps-v2", f"frame-apps-{group}-v1")
    result = result.replace("25 persistent", f"{len(application_ids)} persistent")
    return result


def check_result(result):
    if not result.get("authenticated") or any(
        result.get(k) != 0 for k in ("uid", "euid", "gid", "egid")
    ):
        raise ValueError("Authenticated UID/GID 0 was not established")
    if not result.get("system_files_unchanged") or not result.get("staging_cleaned"):
        raise ValueError("Upstream root cleanup or system-file verification failed")
    if not result.get("commands"):
        raise ValueError("No TV command result was returned")
    for command in result["commands"]:
        if command.get("timed_out") or command.get("exit_code") != 0:
            raise ValueError(
                "TV command failed; inspect the saved report and run restore if apply started"
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tv",
        type=ipaddress.IPv4Address,
        help="Explicit TV IPv4 address; no discovery",
    )
    parser.add_argument("--sdb", type=Path, help="Samsung SDK sdb executable")
    parser.add_argument(
        "--callback-host",
        type=ipaddress.IPv4Address,
        help="This computer’s LAN IPv4 address",
    )
    parser.add_argument("--logs", type=Path, default=ROOT / ".local/reports")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser(
        "doctor", help="Local dependency and package checks; no TV connection"
    )
    sub.add_parser("plan", help="Show scope and limits; no TV connection")
    sub.add_parser(
        "inspect",
        help="Acquire temporary root; report identity, namespace and target state",
    )
    sub.add_parser(
        "voice-audit",
        help="Acquire temporary root; inspect voice processes and capture metadata without recording audio",
    )
    sub.add_parser(
        "debloat-audit", help="Read optional-app, telemetry and detector dependencies"
    )
    sub.add_parser(
        "service-audit",
        help="Read service inventory, activation dependencies, app registrations and process identities",
    )
    boot = sub.add_parser(
        "startup", help="Reapply currently selected runtime controls after boot"
    )
    boot.add_argument(
        "operation", choices=("audit", "probe", "install", "status", "run", "remove")
    )
    sub.add_parser("hdmi-audit", help="Read HDMI-CEC settings and connected devices")
    sub.add_parser(
        "privacy-audit",
        help="Inspect SmartThings sound sensing, Wi-Fi presence and ACR metadata",
    )
    sub.add_parser(
        "status", help="Acquire temporary root; check recorded overlays and masks"
    )
    apply = sub.add_parser(
        "apply", help="Apply reversible changes for the current boot"
    )
    apply.add_argument(
        "--scope", choices=("certificates", "services", "both", "voice"), required=True
    )
    sub.add_parser(
        "restore", help="Remove our overlays/masks and restart previously active units"
    )
    certificates = sub.add_parser(
        "certificates",
        help="Separate per-boot certificate overlays; preserve voice controls",
    )
    certificates.add_argument("operation", choices=("apply", "status", "restore"))
    apps = sub.add_parser(
        "apps",
        help="Persistent native app flags; restore also removes recorded startup guards",
    )
    apps.add_argument("operation", choices=("apply", "status", "verify", "restore"))
    apps.add_argument(
        "--group",
        choices=[
            k for k, v in policy()["optional_controls"].items() if v["application_ids"]
        ],
        help="Optional app group with its own recovery journal; default is voice",
    )
    services = sub.add_parser(
        "services", help="Assessed optional-service groups with separate recovery"
    )
    services.add_argument(
        "group",
        choices=[
            k for k, v in policy()["optional_controls"].items() if v["systemd_units"]
        ],
    )
    services.add_argument("operation", choices=("apply", "status", "restore"))
    updater = sub.add_parser(
        "auto-update", help="Read, disable or restore native automatic firmware updates"
    )
    updater.add_argument("operation", choices=("status", "disable", "restore"))
    sensor = sub.add_parser(
        "sound-sensor",
        help="Separately block or restore the ambient-sound capture app",
    )
    sensor.add_argument("operation", choices=("apply", "status", "verify", "restore"))
    sub.add_parser(
        "guards-apply",
        help="Install eight vendor startup guards after a completed app apply",
    )
    args = parser.parse_args()
    if args.action == "plan":
        print(json.dumps(policy(), indent=2))
        return 0
    verify_files()
    from samsung_tv_root.archive_root import probe
    from samsung_tv_root.sdb import SdbClient, find_sdb

    local_sdb = ROOT / ".local/toolchain/sdb-4.2.36/data/tools/sdb"
    if args.sdb:
        os.environ["SDB"] = str(args.sdb.expanduser().resolve())
    elif not os.environ.get("SDB") and local_sdb.is_file():
        os.environ["SDB"] = str(local_sdb)
    payloads = ASSETS / "payloads"
    if args.action == "doctor":
        print("Package checksums: OK")
        print("Python:", sys.version.split()[0])
        print("OpenSSL:", shutil.which("openssl") or "MISSING")
        try:
            executable = find_sdb()
            result = subprocess.run(
                [str(executable), "version"],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            )
            print("SDB:", executable)
            print(result.stdout.strip())
        except (RuntimeError, OSError, subprocess.SubprocessError) as error:
            print(error)
            return 1
        return 0 if shutil.which("openssl") else 1
    if args.tv is None:
        parser.error("--tv is required for TV operations")
    if not shutil.which("openssl"):
        raise ValueError("OpenSSL is required")
    client = SdbClient(find_sdb(), str(args.tv))
    args.logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    report = args.logs / (
        datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + args.action
        + "-"
        + secrets.token_hex(3)
        + ".json"
    )
    stage = PurePosixPath("/home/owner/share/tmp/sdk_tools") / (
        "frame-runtime-" + secrets.token_hex(8)
    )
    staged = False
    evidence = {
        "action": args.action,
        "operation": getattr(args, "operation", None),
        "group": getattr(args, "group", None),
        "tv": str(args.tv),
        "completed": False,
    }
    try:
        if args.action == "startup" and args.operation in ("audit", "probe"):
            template = (
                device_scripts.STARTUP_AUDIT
                if args.operation == "audit"
                else device_scripts.STARTUP_PROBE
            )
            script = template.replace("@@BUILD@@", policy()["build"])
        elif args.action == "startup":
            package = (
                startup.bundle(policy(), render, ASSETS)
                if args.operation == "install"
                else b""
            )
            script = startup.control(
                args.operation, policy()["build"], hashlib.sha256(package).hexdigest()
            )
            if args.operation == "install":
                client.connect()
                client.require_device()
                result = client.capture(f"mkdir {stage}")
                if "\n[exit:0]" not in result.output:
                    raise ValueError("Cannot create TV staging directory")
                staged = True
                with tempfile.TemporaryDirectory(
                    prefix="samshush-startup-"
                ) as directory:
                    path = Path(directory) / "startup.tar.gz"
                    path.write_bytes(package)
                    client.push(path, stage / path.name)
        elif args.action == "voice-audit":
            script = device_scripts.VOICE_AUDIT
        elif args.action == "service-audit":
            script = render_service_audit()
        elif args.action == "debloat-audit":
            script = device_scripts.DEBLOAT_AUDIT.replace(
                "@@BUILD@@", policy()["build"]
            )
        elif args.action == "hdmi-audit":
            script = device_scripts.HDMI_AUDIT.replace("@@BUILD@@", policy()["build"])
        elif args.action == "privacy-audit":
            script = device_scripts.PRIVACY_AUDIT.replace(
                "@@BUILD@@", policy()["build"]
            )
        elif args.action == "auto-update":
            script = device_scripts.AUTO_UPDATE.replace(
                "@@BUILD@@", policy()["build"]
            ).replace("@@MODE@@", args.operation)
        elif args.action == "services":
            script = render(args.operation, "services", unit_group=args.group)
        elif args.action == "certificates":
            script = render(args.operation, "certificates", certificate_journal=True)
        elif args.action in ("apps", "sound-sensor"):
            script = render_apps(
                args.operation,
                sound_sensor=args.action == "sound-sensor",
                group=getattr(args, "group", None),
            )
        elif args.action == "guards-apply":
            script = device_scripts.STARTUP_GUARDS
        else:
            script = render(args.action, getattr(args, "scope", "none"))
        if (args.action == "apply" and args.scope in ("certificates", "both")) or (
            args.action == "certificates" and args.operation == "apply"
        ):
            client.connect()
            client.require_device()
            # Staging contains public replacement CA bundles only, never private keys.
            result = client.capture(f"mkdir {stage}")
            if "\n[exit:0]" not in result.output:
                raise ValueError("Cannot create TV staging directory")
            staged = True
            for cert in policy()["certificates"]:
                client.push(
                    ASSETS / "certificates" / cert["file"], stage / cert["file"]
                )
        # The stage path is generated here and consists solely of fixed path text and hex.
        script = f"export FRAME_STAGE={shlex.quote(str(stage))}\n" + script
        print(
            "Acquiring temporary root. Even inspect/status execute the root exploit.",
            flush=True,
        )
        result = asyncio.run(
            probe(
                str(args.tv),
                callback_host=str(args.callback_host) if args.callback_host else None,
                command_timeout=180,
                commands=(wrapped(script),),
                payloads=payloads,
            )
        )
        evidence["result"] = result
        for command in result.get("commands", []):
            print(command.get("stdout", ""), end="")
            print(command.get("stderr", ""), end="", file=sys.stderr)
        check_result(result)
        evidence["completed"] = True
    except Exception as error:
        evidence["error"] = str(error)
        print(f"FAILED: {error}", file=sys.stderr)
    finally:
        if staged:
            try:
                result = client.capture(f"rm -rf {stage}; test ! -e {stage}")
                evidence["bundle_staging_cleaned"] = "\n[exit:0]" in result.output
            except Exception as error:
                evidence["bundle_staging_cleaned"] = False
                evidence["cleanup_error"] = str(error)
            if not evidence.get("bundle_staging_cleaned"):
                evidence["completed"] = False
                print(f"Bundle staging cleanup incomplete: {stage}", file=sys.stderr)
        with report.open("x") as output:
            os.chmod(report, 0o600)
            json.dump(evidence, output, indent=2)
            output.write("\n")
        print(f"Report: {report}", file=sys.stderr)
    return 0 if evidence["completed"] else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as error:
        print(f"FAILED: {error}", file=sys.stderr)
        raise SystemExit(1) from error
