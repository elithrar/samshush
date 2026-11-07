"""Build the optional shortcut app with an existing Samsung signing profile."""

import argparse
import asyncio
import base64
import datetime
import hashlib
import json
import math
import os
import re
import secrets
import subprocess
import tempfile
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "launcher"
WIDGET_NS = "{http://www.w3.org/ns/widgets}"
TIZEN_NS = "{http://tizen.org/ns/widgets}"
DS_NS = "{http://www.w3.org/2000/09/xmldsig#}"
WEATHER_RESOURCES = (
    "config.xml",
    "index.html",
    "styles.css",
    "ui.js",
    "device.js",
    "weather.js",
    "icon.png",
    "water-lilies.jpg",
)
RESOURCES = WEATHER_RESOURCES + ("time-controls.js",)
WEATHER_CONFIG = "weather-config.json"
WEATHER_ORIGIN = "https://api.open-meteo.com"
LEGACY_RESOURCES = tuple(name for name in WEATHER_RESOURCES if name != "weather.js")
SIGNATURES = ("author-signature.xml", "signature1.xml")
PRIVILEGES = {
    "http://tizen.org/privilege/application.launch",
    "http://tizen.org/privilege/tv.window",
}


@dataclass(frozen=True)
class Identity:
    package: str
    app: str


def parse_xml(data):
    if not 0 < len(data) <= 256 * 1024 or b"<!DOCTYPE" in data.upper():
        raise ValueError("Invalid or oversized widget XML")
    try:
        return ET.fromstring(data)
    except ET.ParseError as error:
        raise ValueError("Invalid widget XML") from error


def manifest_identity(data, resources=RESOURCES):
    widget = parse_xml(data)
    for tag in (TIZEN_NS + "application", WIDGET_NS + "content", TIZEN_NS + "profile"):
        if len(widget.findall(tag)) != 1:
            raise ValueError("Missing or duplicate widget configuration element")
    application = widget.find(TIZEN_NS + "application")
    if widget.tag != WIDGET_NS + "widget" or application is None:
        raise ValueError("Missing widget application identity")
    package, app = application.get("package", ""), application.get("id", "")
    if not re.fullmatch(r"[A-Za-z0-9]{10}", package) or not re.fullmatch(
        re.escape(package) + r"\.[A-Za-z][A-Za-z0-9_-]{0,49}", app
    ):
        raise ValueError("Invalid SDK widget identity")
    content = widget.find(WIDGET_NS + "content")
    profile = widget.find(TIZEN_NS + "profile")
    if (
        content is None
        or content.get("src") != "index.html"
        or profile is None
        or profile.get("name") != "tv"
        or application.get("required_version") != "9.0"
    ):
        raise ValueError("Unexpected widget entry point or TV profile")
    nodes = widget.findall(TIZEN_NS + "privilege")
    privileges = [node.get("name") for node in nodes]
    permitted = PRIVILEGES.copy()
    if "weather.js" in resources:
        permitted.add("http://tizen.org/privilege/internet")
    if "time-controls.js" in resources:
        permitted.update(
            {
                "http://tizen.org/privilege/filesystem.read",
                "http://tizen.org/privilege/filesystem.write",
            }
        )
    if (
        len(privileges) != len(permitted)
        or set(privileges) != permitted
        or any(set(node.attrib) != {"name"} or len(node) for node in nodes)
    ):
        raise ValueError("Unexpected launcher privileges")
    access = widget.findall(WIDGET_NS + "access")
    if "weather.js" not in resources:
        valid_access = not access
    else:
        valid_access = (
            len(access) == 1
            and access[0].attrib == {"origin": WEATHER_ORIGIN, "subdomains": "false"}
            and not len(access[0])
        )
    if not valid_access:
        raise ValueError("Launcher network access must be limited to the weather API")
    if any(
        widget.findall(tag)
        for tag in (
            TIZEN_NS + "app-control",
            TIZEN_NS + "allow-navigation",
            TIZEN_NS + "service",
            TIZEN_NS + "service-application",
        )
    ):
        raise ValueError(
            "Launcher must not register navigation, services or Home on install"
        )
    return Identity(package, app)


def unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON property")
        result[key] = value
    return result


def weather_config(data):
    """Accept only disabled weather or a coarse coordinate pair, never code."""
    if not isinstance(data, bytes) or not 0 < len(data) <= 1024:
        raise ValueError("Invalid weather configuration size")
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=unique_json_object)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Invalid weather configuration JSON") from error
    if not isinstance(value, dict) or set(value) not in (
        set(),
        {"latitude", "longitude"},
    ):
        raise ValueError(
            "Weather configuration must contain only latitude and longitude"
        )
    for key, limit in (("latitude", 90), ("longitude", 180)):
        if key in value and (
            type(value[key]) not in (int, float)
            or not -limit <= value[key] <= limit
            or not math.isfinite(value[key])
        ):
            raise ValueError("Invalid weather coordinates")
    return value


def resolve_weather_postcode(postcode):
    """Resolve one US postal area's centroid using the public Zippopotam.us API."""
    if not isinstance(postcode, str) or re.fullmatch(r"[0-9]{5}", postcode) is None:
        raise ValueError("Weather postcode must be five US ZIP-code digits")
    request = urllib.request.Request(
        f"https://api.zippopotam.us/us/{postcode}",
        headers={"Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status != 200 or response.geturl() != request.full_url:
                raise ValueError("Unexpected postcode lookup response")
            data = response.read(64 * 1024 + 1)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise ValueError("Weather postcode was not found") from error
        raise ValueError("Weather postcode lookup failed") from error
    except (OSError, urllib.error.URLError) as error:
        raise ValueError("Weather postcode lookup failed") from error
    if len(data) > 64 * 1024:
        raise ValueError("Postcode lookup response exceeds the size limit")
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=unique_json_object)
        if (
            not isinstance(value, dict)
            or value.get("country abbreviation") != "US"
            or value.get("post code") != postcode
            or not isinstance(value.get("places"), list)
            or not 0 < len(value["places"]) <= 100
        ):
            raise ValueError("Unexpected postcode lookup data")
        coordinates = set()
        for place in value["places"]:
            if not isinstance(place, dict) or any(
                not isinstance(place.get(key), str) or len(place[key]) > 64
                for key in ("latitude", "longitude")
            ):
                raise ValueError("Invalid postcode coordinates")
            pair = {key: float(place[key]) for key in ("latitude", "longitude")}
            weather_config(json.dumps(pair, allow_nan=False).encode())
            coordinates.add((pair["latitude"], pair["longitude"]))
        if len(coordinates) != 1:
            raise ValueError("Weather postcode has ambiguous coordinates")
    except (UnicodeError, json.JSONDecodeError, TypeError, OverflowError) as error:
        raise ValueError("Invalid postcode lookup data") from error
    latitude, longitude = coordinates.pop()
    return {"latitude": latitude, "longitude": longitude}


def source_files(source=SOURCE):
    files = {}
    for name in RESOURCES:
        path = source / name
        limit = (8 if name == "water-lilies.jpg" else 1) * 1024 * 1024
        if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
            raise ValueError(f"Missing or invalid launcher resource: {name}")
        files[name] = path.read_bytes()
    manifest_identity(files["config.xml"])
    return files


def signed_widget_resources(path, source=SOURCE, *, previous=False):
    """Validate signed resources; previous releases need not match this checkout."""
    expected = None if previous else source_files(source)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("Invalid signed widget file")
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            names = [item.filename for item in members]
            resources = RESOURCES + (WEATHER_CONFIG,)
            if previous:
                for old in (LEGACY_RESOURCES, WEATHER_RESOURCES + (WEATHER_CONFIG,)):
                    if set(names) == set(old + SIGNATURES):
                        resources = old
                        break
            if len(names) != len(set(names)) or set(names) != set(
                resources + SIGNATURES
            ):
                raise ValueError(
                    "Widget has missing, duplicate or unexpected resources"
                )
            if any(
                item.file_size
                > (
                    1024
                    if item.filename == WEATHER_CONFIG
                    else (8 if item.filename == "water-lilies.jpg" else 1) * 1024 * 1024
                )
                or item.flag_bits & 1
                for item in members
            ):
                raise ValueError("Widget member is oversized or encrypted")
            actual = {name: archive.read(name) for name in names}
    except (zipfile.BadZipFile, RuntimeError) as error:
        raise ValueError("Cannot read signed widget") from error
    if expected is not None and any(
        actual[name] != data for name, data in expected.items()
    ):
        raise ValueError("Signed widget differs from current launcher source")
    identity = manifest_identity(actual["config.xml"], resources)
    if WEATHER_CONFIG in resources:
        weather_config(actual[WEATHER_CONFIG])
    for name in SIGNATURES:
        signature = parse_xml(actual[name])
        if signature.tag != DS_NS + "Signature":
            raise ValueError("Missing XML signature")
        value = signature.findtext(DS_NS + "SignatureValue", "").strip()
        certificates = signature.findall(".//" + DS_NS + "X509Certificate")
        if not value or not certificates or any(not node.text for node in certificates):
            raise ValueError("Incomplete widget signature")
        references = signature.findall(DS_NS + "SignedInfo/" + DS_NS + "Reference")
        covered = set()
        for reference in references:
            uri = reference.get("URI", "")
            if uri.startswith("#"):
                continue  # SDK signature-properties reference, not a resource.
            if uri not in actual or uri == name or uri in covered:
                raise ValueError("Unexpected or duplicate signature resource reference")
            method = reference.find(DS_NS + "DigestMethod")
            # The SDK's XML-security encoder wraps Base64 at 76 columns.
            digest = re.sub(
                r"[ \t\r\n]", "", reference.findtext(DS_NS + "DigestValue", "")
            )
            algorithms = {
                "http://www.w3.org/2001/04/xmlenc#sha256": hashlib.sha256,
                "http://www.w3.org/2001/04/xmlenc#sha512": hashlib.sha512,
            }
            algorithm = (
                algorithms.get(method.get("Algorithm")) if method is not None else None
            )
            if algorithm is None:
                raise ValueError("Unsupported widget digest algorithm")
            expected_digest = base64.b64encode(algorithm(actual[uri]).digest()).decode()
            if digest != expected_digest:
                raise ValueError("Widget signature resource digest mismatch")
            covered.add(uri)
        required = set(resources)
        if name == "signature1.xml":
            required.add("author-signature.xml")
        if not required <= covered:
            raise ValueError("Widget signature omits launcher resources")
    return identity, {name: actual[name] for name in resources}


def verify_widget(path, source=SOURCE):
    """Match static resources to this checkout and validate signed weather data.

    XML signature presence and resource digests are checked here. This is not
    cryptographic validation of Samsung's certificate chain; TV installation
    validates trust.
    """
    return signed_widget_resources(path, source)[0]


def build(sdk, profile, output, source=SOURCE, *, cli=None, weather_postcode=None):
    """Use the official packager without logging profile credentials."""
    if cli is None:
        if sdk is None:
            raise ValueError("A Tizen SDK or executable CLI path is required")
        cli = sdk / "tools/ide/bin/tizen"
    else:
        cli = Path(cli)
    if not cli.is_file() or not os.access(cli, os.X_OK):
        raise ValueError("Official Tizen CLI is unavailable")
    if not profile or len(profile) > 128 or any(ord(char) < 32 for char in profile):
        raise ValueError("Invalid signing profile name")
    files = source_files(source)
    coordinates = (
        resolve_weather_postcode(weather_postcode)
        if weather_postcode is not None
        else {}
    )
    generated = (
        json.dumps(coordinates, allow_nan=False, separators=(",", ":")).encode() + b"\n"
    )
    weather_config(generated)
    files[WEATHER_CONFIG] = generated
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    with tempfile.TemporaryDirectory(prefix="stage-", dir=output) as directory:
        stage = Path(directory)
        unsigned = stage / "Shortcuts.wgt"
        with zipfile.ZipFile(
            unsigned, "w", compression=zipfile.ZIP_DEFLATED
        ) as archive:
            for name, data in files.items():
                archive.writestr(name, data)
        signed = stage / "signed"
        signed.mkdir(mode=0o700)
        try:
            result = subprocess.run(
                [
                    str(cli),
                    "package",
                    "-t",
                    "wgt",
                    "-s",
                    profile,
                    "-o",
                    str(signed),
                    "--",
                    str(unsigned),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=90,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise ValueError(
                "SDK signing timed out; check the saved signing profile"
            ) from error
        if result.returncode:
            raise ValueError(
                "SDK signing failed; unlock the saved profile in Certificate Manager"
            )
        artifact = signed / "Shortcuts.wgt"
        identity, signed_resources = signed_widget_resources(artifact, source)
        if signed_resources[WEATHER_CONFIG] != generated:
            raise ValueError("Signed weather configuration differs from this build")
        target = output / "Shortcuts.wgt"
        artifact.replace(target)
        target.chmod(0o600)
    receipt = {
        "app_id": identity.app,
        "package_id": identity.package,
        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "source_sha256": {
            name: hashlib.sha256(files[name]).hexdigest() for name in RESOURCES
        },
        "weather": coordinates,
        "weather_config_sha256": hashlib.sha256(generated).hexdigest(),
        "installed": False,
    }
    if weather_postcode is not None:
        receipt["weather_postcode"] = weather_postcode
    receipt_path = output / "build.json"
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
    receipt_path.chmod(0o600)
    return target


def registration_script(identity, *, verify_resources=False, resources=None):
    """Read the one app's registration, optionally checking its installed resources."""
    files = source_files() if resources is None else resources
    if manifest_identity(files["config.xml"], files) != identity:
        raise ValueError("Launcher identity differs from this checkout")
    from home import BUILD

    script = f"""set -euo pipefail
[[ $(id -u) == 0 ]]
grep -Fxq 'BUILD_ID={BUILD}' /etc/os-release
db=/opt/dbspace/.pkgmgr_parser.db
packages=$(sqlite3 -readonly "$db" "SELECT count(*) FROM package_info WHERE package='{identity.package}';")
apps=$(sqlite3 -readonly "$db" "SELECT count(*) FROM package_app_info WHERE app_id='{identity.app}' AND package='{identity.package}' AND app_disable='false';")
printf 'LAUNCHPAD_PACKAGE_COUNT %s\\n' "$packages"
printf 'LAUNCHPAD_APP_COUNT %s\\n' "$apps"
"""
    if verify_resources:
        script += f"""[[ $packages == 1 && $apps == 1 ]]
root=/opt/usr/apps/{identity.package}/res/wgt
[[ -d /opt/usr/apps/{identity.package} && ! -L /opt/usr/apps/{identity.package} ]]
[[ -d /opt/usr/apps/{identity.package}/res && ! -L /opt/usr/apps/{identity.package}/res ]]
[[ -d $root && ! -L $root ]]
"""
        for name, data in files.items():
            digest = hashlib.sha256(data).hexdigest()
            script += f'[[ -f "$root/{name}" && ! -L "$root/{name}" ]]\n'
            script += (
                f'[[ $(sha256sum "$root/{name}" | cut -d " " -f 1) == {digest} ]]\n'
            )
        if WEATHER_CONFIG not in files and "weather.js" in files:
            script += f"""[[ -f "$root/{WEATHER_CONFIG}" && ! -L "$root/{WEATHER_CONFIG}" && $(stat -c %s "$root/{WEATHER_CONFIG}") -le 1024 ]]
printf 'LAUNCHPAD_WEATHER_CONFIG %s\\n' "$(base64 < "$root/{WEATHER_CONFIG}" | tr -d '\\n')"
"""
        script += "echo LAUNCHPAD_RESOURCES_VERIFIED\n"
    return script


def run(args, frame):
    """Device operations stay separate from packaging and from privacy controls."""
    from samsung_tv_root.archive_root import probe
    from samsung_tv_root.sdb import SdbClient, find_sdb

    import home_layout
    import launchpad_routing
    from home import BUILD, MODEL

    identity = manifest_identity((SOURCE / "config.xml").read_bytes())
    needs_widget = args.operation in {"install", "update", "activate"}
    previous_path = getattr(args, "previous_widget", None)
    if needs_widget:
        if args.widget is None or verify_widget(args.widget) != identity:
            raise ValueError("A matching signed --widget is required")
    elif args.widget is not None:
        raise ValueError("--widget is only used by install, update or activate")
    if args.operation == "update":
        if (
            previous_path is None
            or signed_widget_resources(previous_path, previous=True)[0] != identity
        ):
            raise ValueError("Update requires a matching signed --previous-widget")
    elif previous_path is not None:
        raise ValueError("--previous-widget is only used by update")
    if frame.policy().get("build") != BUILD or frame.policy().get("model") != MODEL:
        raise ValueError("Launcher requires the verified model and build")
    args.logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    report = args.logs / (
        datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
        + f"-launchpad-{args.operation}-{secrets.token_hex(3)}.json"
    )
    evidence = {
        "operation": args.operation,
        "app_id": identity.app,
        "completed": False,
        "results": [],
    }

    def freeze(path, *, previous=False):
        with path.open("rb") as original:
            widget_bytes = original.read(8 * 1024 * 1024 + 1)
        if len(widget_bytes) > 8 * 1024 * 1024:
            raise ValueError("Signed widget exceeds the upload bound")
        with tempfile.NamedTemporaryFile(
            prefix="launchpad-previous-" if previous else "launchpad-verified-",
            suffix=".wgt",
            dir=args.logs,
            delete=False,
        ) as frozen:
            frozen.write(widget_bytes)
            frozen_path = Path(frozen.name)
        frozen_identity, resources = signed_widget_resources(
            frozen_path, SOURCE, previous=previous
        )
        if frozen_identity != identity:
            raise ValueError("Frozen widget identity differs from the launcher")
        return frozen_path, resources, hashlib.sha256(widget_bytes).hexdigest()

    frozen_widget = None
    widget_resources = previous_resources = None
    if needs_widget:
        frozen_widget, widget_resources, widget_digest = freeze(args.widget)
        evidence["verified_widget"] = str(frozen_widget)
        evidence["widget_sha256"] = widget_digest
    if args.operation == "update":
        frozen_previous, previous_resources, previous_digest = freeze(
            previous_path, previous=True
        )
        evidence["previous_widget"] = str(frozen_previous)
        evidence["previous_widget_sha256"] = previous_digest

    def save():
        temporary = report.with_suffix(".tmp")
        with temporary.open("w") as output:
            os.chmod(temporary, 0o600)
            json.dump(evidence, output, indent=2)
            output.write("\n")
        temporary.replace(report)

    def execute(script):
        result = asyncio.run(
            probe(
                str(args.tv),
                model=MODEL,
                callback_host=str(args.callback_host) if args.callback_host else None,
                accept_timeout=180,
                command_timeout=90,
                commands=(home_layout.device_command(script),),
                payloads=frame.ASSETS / "payloads",
            )
        )
        evidence["results"].append(result)
        save()
        frame.check_result(result)
        commands = result.get("commands", [])
        if len(commands) != 1:
            raise ValueError("Incomplete launcher operation evidence")
        return commands[0]["stdout"]

    artifacts = {
        suffix: (
            frame.ASSETS / "payloads" / (launchpad_routing.NAME + suffix)
        ).read_bytes()
        for suffix in (".dll", ".runtimeconfig.json")
    }

    def routing(operation):
        options = (
            {"package_id": identity.package, "app_id": identity.app}
            if operation == "apply"
            else {}
        )
        script = launchpad_routing.render_script(operation, artifacts, **options)
        if operation == "apply":
            script = (
                registration_script(
                    identity, verify_resources=True, resources=widget_resources
                )
                + script
            )
        return launchpad_routing.parse_output(execute(script))

    def check_resources(resources=None):
        output = execute(
            registration_script(identity, verify_resources=True, resources=resources)
        )
        if output.splitlines().count("LAUNCHPAD_RESOURCES_VERIFIED") != 1:
            raise ValueError("Missing launcher resource verification")
        if resources is None:
            encoded = home_layout.marker(output, "LAUNCHPAD_WEATHER_CONFIG")
            weather_config(base64.b64decode(encoded, validate=True))

    def checked_upload(script, marker):
        if execute(script).splitlines().count(marker) != 1:
            raise ValueError("Missing launcher upload verification")

    def require_owned_home(state):
        receipt = state["receipt"]
        if (
            state["phase"] != "applied"
            or receipt is None
            or receipt["Package"] != identity.package
            or receipt["App"] != identity.app
        ):
            raise ValueError("Update requires the matching applied Home assignment")

    def sdk(client, command):
        result = client.run(
            ("-s", client.serial, "shell", "0", *command), check=False, timeout=90
        )
        evidence["results"].append(
            {
                "sdk_command": command,
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        )
        save()
        if result.returncode:
            raise ValueError("SDK operation failed; inspect its report before retrying")
        return result.stdout + result.stderr

    save()
    try:
        home_layout.verify_model(args.tv)
        before = routing(
            "query"
            if args.operation in {"inspect", "install", "launch", "restore"}
            else "status"
        )
        evidence["before"] = before
        if args.operation in {"install", "update"}:
            if args.operation == "update":
                require_owned_home(before)
                check_resources(previous_resources)
            else:
                output = execute(registration_script(identity))
                if (
                    home_layout.marker(output, "LAUNCHPAD_PACKAGE_COUNT") != "0"
                    or home_layout.marker(output, "LAUNCHPAD_APP_COUNT") != "0"
                ):
                    raise ValueError(
                        "Launcher is already registered; inspect it before replacing the package"
                    )
            client = SdbClient(find_sdb(), str(args.tv))
            client.connect()
            client.require_device()
            digest = evidence["widget_sha256"]
            remote = (
                PurePosixPath("/home/owner/share/tmp/sdk_tools")
                / f"Samshush-{digest[:16]}-{secrets.token_hex(4)}.wgt"
            )
            evidence.update(widget_sha256=digest, staged_widget=str(remote))
            save()
            client.push(frozen_widget, remote)
            checked_upload(
                f"""set -euo pipefail
[[ $(id -u) == 0 ]]
grep -Fxq 'BUILD_ID={BUILD}' /etc/os-release
path={remote}
[[ -f $path && ! -L $path && $(sha256sum "$path" | cut -d " " -f 1) == {digest} ]]
echo LAUNCHPAD_UPLOAD_VERIFIED
""",
                "LAUNCHPAD_UPLOAD_VERIFIED",
            )
            output = sdk(client, ("vd_appinstall", identity.app, str(remote)))
            if "install completed" not in output or "install failed" in output.lower():
                raise ValueError(
                    "Installation was not confirmed; inspect before retrying"
                )
            check_resources(widget_resources)
            if args.operation == "update":
                after = routing("status")
                evidence["after"] = after
                require_owned_home(after)
                if any(after[key] != before[key] for key in ("receipt", "current")):
                    raise ValueError("Home assignment changed during the app update")
            # The installer may remove its upload. Remove only our exact verified bytes.
            checked_upload(
                f"""set -euo pipefail
[[ $(id -u) == 0 ]]
grep -Fxq 'BUILD_ID={BUILD}' /etc/os-release
path={remote}
if [[ -e $path || -L $path ]]; then
 [[ -f $path && ! -L $path && $(sha256sum "$path" | cut -d " " -f 1) == {digest} ]]
 rm -- "$path"
fi
[[ ! -e $path && ! -L $path ]]
echo LAUNCHPAD_UPLOAD_CLEANED
""",
                "LAUNCHPAD_UPLOAD_CLEANED",
            )
            print(
                "Shortcut app updated; Home assignment is unchanged. Previous signed widget retained."
                if args.operation == "update"
                else "Shortcut app installed. Home assignment is unchanged; launch and check its controls before activation."
            )
        elif args.operation == "launch":
            check_resources()
            client = SdbClient(find_sdb(), str(args.tv))
            client.connect()
            client.require_device()
            output = sdk(client, ("execute", identity.app))
            if "successfully launched" not in output:
                raise ValueError("App launch was not confirmed")
            print(
                "Shortcut app launched. Check HDMI, Settings, Art Mode and Back on the TV."
            )
        elif args.operation == "activate":
            evidence["after"] = routing("apply")
            evidence["status"] = routing("status")
            print(
                "Home assigned to Shortcuts. Physical Home-button and restart checks remain separate."
            )
        elif args.operation == "restore":
            evidence["after"] = routing("restore")
            evidence["status"] = routing("status")
            print(
                "Original Home assignment restored; the shortcut app remains installed."
            )
        else:
            print("Home routing journal:", before["phase"])
        evidence["completed"] = True
    except Exception as error:
        evidence["error"] = str(error)
        print(f"FAILED: {error}. Inspect the saved report before retrying.")
    finally:
        save()
        print(f"Report: {report}")
    return 0 if evidence["completed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    command = parser.add_mutually_exclusive_group(required=True)
    command.add_argument(
        "--sdk", type=Path, help="SDK root containing tools/ide/bin/tizen"
    )
    command.add_argument(
        "--cli", type=Path, help="Executable Tizen CLI or local wrapper"
    )
    parser.add_argument("--profile", required=True)
    parser.add_argument(
        "--weather-postcode", help="US ZIP code for weather; omitted disables weather"
    )
    parser.add_argument(
        "--output", required=True, type=Path, help="Fresh private output directory"
    )
    args = parser.parse_args()
    try:
        target = build(
            args.sdk.expanduser().resolve() if args.sdk else None,
            args.profile,
            args.output.resolve(),
            cli=args.cli.expanduser().resolve() if args.cli else None,
            weather_postcode=args.weather_postcode,
        )
    except (OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")
    print(f"Signed launcher: {target}")


if __name__ == "__main__":
    main()
