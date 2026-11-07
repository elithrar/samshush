"""Optional Apple TV shortcut row using Samsung's native Home setting."""

import asyncio
import base64
import copy
import datetime
import gzip
import hashlib
import http.client
import json
import os
import re
import secrets
import shlex

import home_layout_script
from home import BUILD, MODEL

MAX_LAYOUT_BYTES = 65536
COMPONENT = re.compile(r"s/source\.layer\.hdmi@HDMI1(?:/[0-9]+)?\Z")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON property: {key}")
        result[key] = value
    return result


def parse_layout(value):
    if not isinstance(value, str) or not 0 < len(value.encode()) <= MAX_LAYOUT_BYTES:
        raise ValueError("Home layout is empty or exceeds the supported size")

    def invalid_constant(value):
        raise ValueError(f"Invalid JSON constant: {value}")

    try:
        layout = json.loads(
            value, object_pairs_hook=unique_object, parse_constant=invalid_constant
        )
    except RecursionError as error:
        raise ValueError("Home JSON nesting exceeds bounds") from error
    pending = [(layout, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > 32:
            raise ValueError("Home JSON nesting exceeds bounds")
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
    if not isinstance(layout, dict) or layout.get("version") != "1.0.0":
        raise ValueError("Unsupported Home layout version")
    layer = layout.get("app_layer")
    if not isinstance(layer, dict):
        raise ValueError("Missing Home app layer")
    maximum, count, rows = layer.get("max_tile"), layer.get("count"), layer.get("list")
    if (
        type(maximum) is not int
        or not 1 <= maximum <= 100
        or type(count) is not int
        or not 0 <= count <= maximum
        or not isinstance(rows, list)
        or not 1 <= len(rows) <= maximum
    ):
        raise ValueError("Invalid Home row bounds")
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Invalid Home row")
        component = row.get("component_id")
        if not isinstance(component, str) or not component or component in seen:
            raise ValueError("Missing or duplicate Home component")
        seen.add(component)
    return layout


def apple_tv_component(sources):
    if (
        not isinstance(sources, dict)
        or sources.get("schema") != "samshush-home-source-readout-v1"
        or sources.get("complete") is not True
    ):
        raise ValueError("Incomplete native source readout")
    rows = sources.get("hdmiSources")
    if not isinstance(rows, list):
        raise ValueError("Missing HDMI inventory")
    matches = [
        row for row in rows if isinstance(row, dict) and row.get("sourceType") == 13
    ]
    if len(matches) != 1:
        raise ValueError("HDMI 1 source is missing or ambiguous")
    source = matches[0]
    component = source.get("componentId")
    if (
        source.get("connectionState") is not True
        or str(source.get("detectedName", "")).casefold() != "apple tv"
        or not isinstance(component, str)
        or len(component) > 64
        or COMPONENT.fullmatch(component) is None
        or source.get("sourceId") != component.split("@", 1)[1]
    ):
        raise ValueError("Connected Apple TV on HDMI 1 was not verified")
    return component


def plan(value, sources):
    """Keep the complete existing Apple TV row and all surrounding metadata."""
    layout = parse_layout(value)
    component = apple_tv_component(sources)
    rows = [
        row for row in layout["app_layer"]["list"] if row["component_id"] == component
    ]
    if len(rows) != 1:
        raise ValueError("Add Apple TV to Home using the TV menu before installation")
    row = rows[0]
    if (
        row.get("id") != component.split("@", 1)[1]
        or row.get("t") != "src"
        or row.get("src") != "13"
        or row.get("p") != "f"
    ):
        raise ValueError("Apple TV shortcut differs from the verified native schema")
    result = copy.deepcopy(layout)
    result["app_layer"]["list"] = [copy.deepcopy(row)]
    # HDMI entries count; Samsung's LiveTV/preview exclusions are no longer present.
    result["app_layer"]["count"] = 1
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    parse_layout(encoded)
    return component, encoded


def marker(output, name):
    values = [
        line.removeprefix(name + " ")
        for line in output.splitlines()
        if line.startswith(name + " ")
    ]
    if len(values) != 1:
        raise ValueError(f"Missing or ambiguous TV evidence: {name}")
    return values[0]


def read_layout(output):
    encoded = marker(output, "HOME_LAYOUT_VALUE_BASE64")
    if len(encoded) > 4 * ((MAX_LAYOUT_BYTES + 2) // 3):
        raise ValueError("Encoded Home layout exceeds bounds")
    value = base64.b64decode(encoded, validate=True).decode("utf-8")
    parse_layout(value)
    return value


def verify_model(tv):
    connection = http.client.HTTPConnection(str(tv), 8001, timeout=5)
    try:
        connection.request("GET", "/api/v2/", headers={"Cache-Control": "no-cache"})
        response = connection.getresponse()
        data = response.read(65537)
        if response.status != 200 or len(data) > 65536:
            raise ValueError("Cannot verify TV model")
        identity = json.loads(data)
        if (
            not isinstance(identity, dict)
            or not isinstance(identity.get("device"), dict)
            or identity["device"].get("modelName") != MODEL
        ):
            raise ValueError(f"Expected Samsung TV model {MODEL}")
    finally:
        connection.close()


def source_prefix(assets, component=None):
    """Stage a bounded getter, verify its bytes, and remove it before mutation."""
    files = []
    checks = []
    for name in ("HomeSourceReadout.dll", "HomeSourceReadout.runtimeconfig.json"):
        data = (assets / "payloads" / name).read_bytes()
        files.append(
            f'printf %s {base64.b64encode(data).decode()} | base64 -d > "$stage/{name}"'
        )
        checks.append(f"{hashlib.sha256(data).hexdigest()}  {name}")
    argument = ""
    if component is not None:
        if len(component) > 64 or COMPONENT.fullmatch(component) is None:
            raise ValueError("Invalid Apple TV component")
        argument = " --verify-apple-tv " + shlex.quote(component)
    return (
        r"""set -euo pipefail
(
umask 077
stage=$(mktemp -d /run/frame-home-source.XXXXXXXX)
cleanup_source() {
 result=$?
 trap - EXIT
 for cmdline in /proc/[0-9]*/cmdline; do
  [[ -r $cmdline ]] || continue
  while IFS= read -r -d '' arg; do
   if [[ $arg == "$stage/HomeSourceReadout.dll" ]]; then
    echo "Source helper remains; retaining $stage" >&2; exit 1
   fi
  done < "$cmdline" || true
 done
 rm -f "$stage/HomeSourceReadout.dll" "$stage/HomeSourceReadout.runtimeconfig.json"
 rmdir "$stage" || result=1
 echo HOME_SOURCE_CLEANED
 exit "$result"
}
trap cleanup_source EXIT
"""
        + "\n".join(files)
        + "\n(cd \"$stage\" && sha256sum -c <<'SOURCE_HASHES'\n"
        + "\n".join(checks)
        + "\nSOURCE_HASHES\n)\n"
        + '/usr/bin/dotnet "$stage/HomeSourceReadout.dll"'
        + argument
        + "\n)\n"
    )


def device_command(script):
    encoded = base64.b64encode(gzip.compress(script.encode(), mtime=0)).decode()
    pipeline = f"printf %s {encoded} | base64 -d | gzip -dc | nsenter --mount=/proc/1/ns/mnt /bin/bash"
    command = "/bin/bash -o pipefail -c " + shlex.quote(pipeline)
    if len(command.encode()) > 65536:
        raise ValueError("TV command exceeds the transport command bound")
    return command


def run(args, frame):
    from samsung_tv_root.archive_root import probe

    args.logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    report = args.logs / (
        datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
        + f"-home-layout-{args.operation}-{secrets.token_hex(3)}.json"
    )
    evidence = {
        "action": "home-layout",
        "operation": args.operation,
        "tv": str(args.tv),
        "completed": False,
        "results": [],
    }

    def save():
        temporary = report.with_suffix(".tmp")
        with temporary.open("w") as output:
            os.chmod(temporary, 0o600)
            json.dump(evidence, output, indent=2)
            output.write("\n")
        os.replace(temporary, report)

    def execute(operation, *, before=None, after=None, sources=False, component=None):
        script = home_layout_script.render(operation, before=before, after=after)
        if sources or component is not None:
            script = source_prefix(frame.ASSETS, component) + script
        command = device_command(script)
        result = asyncio.run(
            probe(
                str(args.tv),
                model=frame.policy()["model"],
                callback_host=str(args.callback_host) if args.callback_host else None,
                accept_timeout=180,
                command_timeout=150,
                commands=(command,),
                payloads=frame.ASSETS / "payloads",
            )
        )
        evidence["results"].append(result)
        save()
        for entry in result.get("commands", []):
            if entry.get("stderr"):
                print(entry["stderr"], end="")
        frame.check_result(result)
        return result["commands"][0]["stdout"]

    save()
    try:
        if frame.policy().get("build") != BUILD or frame.policy().get("model") != MODEL:
            raise ValueError("Home layout policy differs from the verified TV")
        expected_policy = {
            "journal": home_layout_script.JOURNAL,
            "component": home_layout_script.COMPONENT,
            "binary_hashes": home_layout_script.HASHES,
        }
        if frame.policy().get("home", {}).get("layout") != expected_policy:
            raise ValueError(
                "Home layout policy differs from the verified layout scope"
            )
        verify_model(args.tv)
        if args.operation == "install":
            output = execute("inspect", sources=True)
            source_rows = [
                json.loads(line)
                for line in output.splitlines()
                if line.startswith('{"schema":"samshush-home-source-readout-v1"')
            ]
            if len(source_rows) != 1:
                raise ValueError("Missing or ambiguous native source readout")
            before = read_layout(output)
            component, after = plan(before, source_rows[0])
            evidence["plan"] = {
                "component": component,
                "before": before,
                "after": after,
            }
            save()
            if marker(output, "HOME_LAYOUT_PHASE") != "installed":
                print(
                    "Installing Apple TV shortcut row; native Settings and Art components are retained.",
                    flush=True,
                )
                execute("install", before=before, after=after, component=component)
            output = execute("status")
            if parse_layout(read_layout(output)) != parse_layout(after):
                raise ValueError(
                    "Installed row differs from the verified Apple TV plan"
                )
        else:
            output = execute(args.operation)
            if args.operation == "remove":
                output = execute("status")
        value = read_layout(output)
        phase = marker(output, "HOME_LAYOUT_PHASE")
        print(f"Home layout journal: {phase}")
        print("Home shortcut components:")
        for row in parse_layout(value)["app_layer"]["list"]:
            print(" ", row["component_id"])
        evidence["completed"] = True
    except Exception as error:
        evidence["error"] = str(error)
        print(f"FAILED: {error}. Recovery evidence retained; inspect before retrying.")
    finally:
        save()
        print(f"Report: {report}")
    return 0 if evidence["completed"] else 1
