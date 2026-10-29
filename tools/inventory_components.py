#!/usr/bin/env python3
"""Inventory inert firmware extraction; never contacts or modifies a TV."""

import argparse
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "firmware/filesystems/T-RSMFAKUC-0090"
PLATFORM = BASE / "pltf_t/root"
DATA = BASE / "data_backup/root"


def relative(p):
    return str(p.resolve())


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def directives(text):
    result, section, pending, start = [], "", "", 0
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if not pending:
            start = number
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        line, pending = pending + line, ""
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
        elif line and not line.startswith(("#", ";")) and "=" in line:
            key, value = line.split("=", 1)
            result.append(
                {
                    "section": section,
                    "key": key.strip(),
                    "value": value.strip(),
                    "line": start,
                }
            )
    if pending:
        raise ValueError("Unterminated unit continuation")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--firmware-root", type=Path, default=BASE)
    parser.add_argument(
        "--output", type=Path, default=ROOT / ".local/reports/components"
    )
    args = parser.parse_args()
    base_path = args.firmware_root.resolve()
    if not base_path.is_dir():
        parser.error("Firmware root must be an existing directory")
    families = (
        [base_path]
        if (base_path / "pltf_t/root").is_dir()
        else sorted(
            p
            for p in base_path.iterdir()
            if p.is_dir() and (p / "pltf_t/root").is_dir()
        )
    )
    if not families:
        parser.error(
            "An extracted firmware family or directory of families is required"
        )
    primary = next((p for p in families if p.name == "T-RSMFAKUC-0090"), families[0])
    platform, data = primary / "pltf_t/root", primary / "data_backup/root"
    args.output.mkdir(parents=True, exist_ok=True)
    units = []
    manifests = []
    for family in families:
        for p in sorted(family.rglob("*")):
            if not p.is_file():
                continue
            name = p.relative_to(family).as_posix()
            if p.suffix == ".xml" and "/share/packages/" in name:
                manifests.append((family.name, p))
            unit_suffixes = {
                ".service",
                ".socket",
                ".path",
                ".timer",
                ".target",
                ".mount",
                ".automount",
                ".swap",
                ".slice",
                ".scope",
                ".busname",
                ".conf",
                ".preset",
            }
            if not (
                p.suffix == ".service"
                or (
                    "/systemd/" in name
                    and (
                        p.suffix in unit_suffixes or p.name.endswith(".symlink-target")
                    )
                )
            ):
                continue
            row = {
                "path": relative(p),
                "name": p.name,
                "sha256": sha(p),
                "family": family.name,
            }
            text = p.read_text(errors="replace")
            if p.name.endswith(".symlink-target"):
                row["link_target"] = text
            else:
                row["directives"] = directives(text)
                row["activation_kind"] = (
                    "dbus"
                    if any(d["section"] == "D-BUS Service" for d in row["directives"])
                    else "systemd"
                    if "/systemd/" in name
                    else "bundled-definition"
                )
            units.append(row)

    packages = []
    for family, p in manifests:
        try:
            root = ET.parse(p).getroot()
        except ET.ParseError as error:
            raise ValueError(f"Invalid package XML: {p}") from error
        apps = []
        dependencies = []
        for node in root.iter():
            kind = node.tag.split("}")[-1]
            if kind in ("ui-application", "service-application"):
                apps.append({"kind": kind, **node.attrib})
            if kind == "dependency":
                dependencies.append(
                    {
                        "kind": "dependency",
                        **node.attrib,
                        "value": (node.text or "").strip(),
                    }
                )
            if kind == "requires":
                dependencies.extend({"kind": "requires", **n.attrib} for n in node)
        packages.append(
            {
                "family": family,
                "path": relative(p),
                "sha256": sha(p),
                **root.attrib,
                "applications": apps,
                "dependencies": dependencies,
            }
        )

    inventory = {
        "firmware": primary.name,
        "families": [p.name for p in families],
        "scope": "Offline source declarations, including product partitions, zones, D-Bus and bundled definitions. Duplicate contexts remain separate; presence does not prove installation or activity. References are not resolved runtime dependencies.",
        "units_and_links": units,
        "packages": packages,
    }
    (args.output / "component-inventory.json").write_text(
        json.dumps(inventory, indent=2) + "\n"
    )

    selected = [
        "adagent-service.service",
        "pisa-service.service",
        "pisa-service.socket",
        "pisa-service.path",
        "media-analytics-service.service",
        "org.tizen.tv.media.analytics.service.busname",
        "issue_report_agent.service",
        "devicetracker.service",
        "abnormal_user_behavior.service",
        "emergency-log-transmitter.service",
        "data-service.service",
        "com.samsung.tv.context-aware-service.service",
        "reporterCA.service",
        "reporterCA.timer",
        "scs-agent.service",
        "ambient-ua-agent.service",
        "com.samsung.tv.farfieldvoice.service",
        "slive-provider-daemon.service",
        "slive-provider-daemon.path",
        "com.samsung.FCCallListener.busname",
        "bms.service",
    ]
    lines = [
        "# Firmware component evidence",
        "",
        "Generated from extracted files. Line numbers refer to original extracted files.",
        "",
    ]
    for name in selected:
        p = platform / "usr/lib/systemd/system" / name
        if not p.is_file():
            continue
        lines += ["## " + name, "", f"`{relative(p)}`", "", "```ini"]
        lines += [
            f"{i}: {line}"
            for i, line in enumerate(p.read_text().splitlines(), 1)
            if not line.startswith("#RPAC=")
        ]
        lines += ["```", "", "References in inventoried systemd files:", ""]
        for u in units:
            if u["name"] == name:
                continue
            if name in u.get("link_target", ""):
                lines.append(f"- `{u['path']}` → `{u['link_target']}`")
            for d in u.get("directives", []):
                if name in d["value"]:
                    lines.append(
                        f"- `{u['path']}:{d['line']}` — `{d['key']}={d['value']}`"
                    )
        lines += [""]

    for p in [
        data / "usr/apps/com.samsung.tv.pisa-service/bin/adagent-service",
        platform / "usr/bin/media-analytics-service",
        platform / "usr/bin/emergencyLogTransmitter",
        platform / "usr/bin/devicetracker",
    ]:
        if not p.exists():
            continue
        strings = re.findall(rb"[\x20-\x7e]{8,}", p.read_bytes())
        hits = [
            s.decode()
            for s in strings
            if not s.startswith(b"_Z")
            and re.search(
                rb"https?://|ClickTracking|AdEventTracking|uploadPeriod|upload request|loggingpolicy|AnalyticsServer",
                s,
            )
        ]
        lines += (
            [
                "## Binary evidence: " + p.name,
                "",
                f"`{relative(p)}`",
                "",
                f"SHA-256: `{sha(p)}`",
                "",
                "```text",
            ]
            + hits[:35]
            + ["```", ""]
        )
    (args.output / "component-evidence.md").write_text("\n".join(lines))
    print(
        json.dumps(
            {
                "unit_entries": len(units),
                "package_manifests": len(packages),
                "evidence_file": str(args.output / "component-evidence.md"),
            }
        )
    )


if __name__ == "__main__":
    main()
