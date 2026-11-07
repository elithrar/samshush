import base64
import contextlib
import copy
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import warnings
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import frame  # noqa: E402
import home  # noqa: E402
import launchpad  # noqa: E402
import launchpad_routing  # noqa: E402

WEATHER_MANIFEST = b"""<widget xmlns="http://www.w3.org/ns/widgets" xmlns:tizen="http://tizen.org/ns/widgets" id="https://example.invalid/shortcuts" version="0.2.0">
<tizen:application package="Gxqk7W2na5" id="Gxqk7W2na5.Shortcuts" required_version="9.0"/>
<content src="index.html"/>
<tizen:profile name="tv"/>
<tizen:privilege name="http://tizen.org/privilege/application.launch"/>
<tizen:privilege name="http://tizen.org/privilege/tv.window"/>
<tizen:privilege name="http://tizen.org/privilege/internet"/>
<access origin="https://api.open-meteo.com" subdomains="false"/>
</widget>"""
MANIFEST = WEATHER_MANIFEST.replace(
    b"</widget>",
    b"""<tizen:privilege name="http://tizen.org/privilege/filesystem.read"/>
<tizen:privilege name="http://tizen.org/privilege/filesystem.write"/>
</widget>""",
).replace(b'version="0.2.0"', b'version="0.3.0"')
LEGACY_MANIFEST = WEATHER_MANIFEST.replace(
    b'<tizen:privilege name="http://tizen.org/privilege/internet"/>\n', b""
).replace(b'<access origin="https://api.open-meteo.com" subdomains="false"/>\n', b"")
DIGESTS = {
    "sha256": "http://www.w3.org/2001/04/xmlenc#sha256",
    "sha512": "http://www.w3.org/2001/04/xmlenc#sha512",
}


def signature(resources, algorithm):
    """A structural fixture with intentionally untrusted signature/cert bytes."""
    namespace = launchpad.DS_NS
    root = ET.Element(namespace + "Signature")
    info = ET.SubElement(root, namespace + "SignedInfo")
    for name, data in resources.items():
        reference = ET.SubElement(info, namespace + "Reference", URI=name)
        ET.SubElement(
            reference, namespace + "DigestMethod", Algorithm=DIGESTS[algorithm]
        )
        ET.SubElement(reference, namespace + "DigestValue").text = base64.b64encode(
            hashlib.new(algorithm, data).digest()
        ).decode()
    ET.SubElement(info, namespace + "Reference", URI="#properties")
    ET.SubElement(root, namespace + "SignatureValue").text = base64.b64encode(
        b"fixture only - not a cryptographic signature"
    ).decode()
    key = ET.SubElement(root, namespace + "KeyInfo")
    certificates = ET.SubElement(key, namespace + "X509Data")
    ET.SubElement(certificates, namespace + "X509Certificate").text = base64.b64encode(
        b"fixture only - not an X509 certificate"
    ).decode()
    return ET.tostring(root)


def signed_members(resources, algorithm="sha256"):
    members = dict(resources)
    members["author-signature.xml"] = signature(resources, algorithm)
    members["signature1.xml"] = signature(members, algorithm)
    return members


def write_widget(path, members, duplicate=None):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in members.items():
                archive.writestr(name, data)
            if duplicate:
                archive.writestr(duplicate, members[duplicate])


class LaunchpadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="samshush-widget-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "launcher"
        self.source.mkdir()
        self.resources = {
            "config.xml": MANIFEST,
            "index.html": b"<!doctype html><title>Test launcher</title>",
            "styles.css": b"body { background: black; }",
            "ui.js": b"window.testUI = true;",
            "device.js": b"window.testDevice = true;",
            "time-controls.js": b"window.testSchedule = true;",
            "weather.js": b"window.testWeather = true;",
            "icon.png": b"\x89PNG\r\n\x1a\nfixture",
            "water-lilies.jpg": b"\xff\xd8\xff\xd9fixture",
            launchpad.WEATHER_CONFIG: b"{}\n",
        }
        for name, data in self.resources.items():
            if name in launchpad.RESOURCES:
                (self.source / name).write_bytes(data)
        self.widget = self.root / "Shortcuts.wgt"

    def verify(self, members=None, **kwargs):
        write_widget(self.widget, members or signed_members(self.resources), **kwargs)
        return launchpad.verify_widget(self.widget, self.source)

    def test_exact_resources_and_sdk_sha512_digests_are_structurally_valid(self):
        for algorithm in DIGESTS:
            with self.subTest(algorithm=algorithm):
                identity = self.verify(signed_members(self.resources, algorithm))
                self.assertEqual(identity.app, "Gxqk7W2na5.Shortcuts")
                self.assertEqual(identity.package, "Gxqk7W2na5")
        # The deliberately fake signature/certificate above establishes only
        # structure and resource digests, never Samsung certificate trust.

    def test_sdk_wrapped_sha512_digests_allow_only_xml_whitespace(self):
        members = signed_members(self.resources, "sha512")
        root = ET.fromstring(members["signature1.xml"])
        values = root.findall(".//" + launchpad.DS_NS + "DigestValue")
        for value in values:
            digest = value.text
            value.text = " \t" + digest[:76] + "\r\n" + digest[76:] + "\n"
        members["signature1.xml"] = ET.tostring(root)
        self.assertEqual(self.verify(members).app, "Gxqk7W2na5.Shortcuts")
        original = values[0].text
        for invalid in ("!", "\N{NO-BREAK SPACE}"):
            values[0].text = original + invalid
            members["signature1.xml"] = ET.tostring(root)
            with (
                self.subTest(invalid=invalid),
                self.assertRaisesRegex(ValueError, "digest mismatch"),
            ):
                self.verify(members)

    def test_rejects_wrong_identity_profile_entry_and_privileges(self):
        for before, after in (
            (b'package="Gxqk7W2na5"', b'package="short"'),
            (b'id="Gxqk7W2na5.Shortcuts"', b'id="OtherPkg12.Shortcuts"'),
            (b'required_version="9.0"', b'required_version="8.0"'),
            (b'name="tv"', b'name="mobile"'),
            (b'src="index.html"', b'src="https://example.com/"'),
            (b"privilege/tv.window", b"privilege/internet"),
            (b"privilege/tv.window", b"privilege/application.launch"),
            (b"https://api.open-meteo.com", b"https://example.com"),
            (b'subdomains="false"', b'subdomains="true"'),
        ):
            with self.subTest(after=after), self.assertRaises(ValueError):
                launchpad.manifest_identity(MANIFEST.replace(before, after))

    def test_rejects_network_access_home_registration_and_ambiguous_manifest(self):
        for entry in (
            b'<access origin="*"/>',
            b"<tizen:allow-navigation>https://example.com</tizen:allow-navigation>",
            b"<tizen:app-control/>",
            b'<tizen:application package="OtherPkg12" id="OtherPkg12.Other" required_version="9.0"/>',
            b'<content src="other.html"/>',
            b'<tizen:profile name="mobile"/>',
            b'<tizen:privilege name="http://tizen.org/privilege/internet"/>',
        ):
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                launchpad.manifest_identity(
                    MANIFEST.replace(b"</widget>", entry + b"</widget>")
                )

    def test_rejects_altered_missing_extra_duplicate_and_foreign_resources(self):
        changes = [
            {"ui.js": b"altered code"},
            {"config.xml": MANIFEST.replace(b"Gxqk7W2na5", b"OtherPkg12")},
            {"extra.js": b"unexpected"},
            {"../escape.js": b"unexpected"},
        ]
        for change in changes:
            with self.subTest(change=list(change)), self.assertRaises(ValueError):
                self.verify(signed_members(self.resources | change))
        for missing in ("icon.png", "author-signature.xml", "signature1.xml"):
            members = signed_members(self.resources)
            del members[missing]
            with self.subTest(missing=missing), self.assertRaises(ValueError):
                self.verify(members)
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.verify(duplicate="ui.js")

    def test_generated_weather_config_is_signed_but_never_a_source_resource(self):
        weather = b'{"latitude":40.0,"longitude":-70.0}\n'
        resources = self.resources | {launchpad.WEATHER_CONFIG: weather}
        self.assertEqual(
            self.verify(signed_members(resources)).app, "Gxqk7W2na5.Shortcuts"
        )
        self.assertFalse((self.source / launchpad.WEATHER_CONFIG).exists())
        members = signed_members(resources)
        members[launchpad.WEATHER_CONFIG] = b"{}"
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            self.verify(members)

    def test_rejects_invalid_weather_json_even_with_matching_signature_digests(self):
        for weather in (
            b"[]",
            b'{"latitude":0}',
            b'{"latitude":true,"longitude":0}',
            b'{"latitude":91,"longitude":0}',
            b'{"latitude":0,"longitude":-181}',
            b'{"latitude":NaN,"longitude":0}',
            b'{"latitude":0,"longitude":Infinity}',
            b'{"latitude":0,"longitude":0,"postcode":"90210"}',
            b'{"latitude":0,"longitude":0,"latitude":1}',
            b'{"latitude":"40","longitude":0}',
            b"{};alert(1)",
            b" " * 1025,
        ):
            with self.subTest(weather=weather), self.assertRaises(ValueError):
                self.verify(
                    signed_members(self.resources | {launchpad.WEATHER_CONFIG: weather})
                )

    def test_previous_widget_accepts_only_known_legacy_or_current_package_shapes(self):
        previous = {name: self.resources[name] for name in launchpad.LEGACY_RESOURCES}
        previous["config.xml"] = LEGACY_MANIFEST
        previous["ui.js"] = b"previous release"
        write_widget(self.widget, signed_members(previous))
        identity, files = launchpad.signed_widget_resources(self.widget, previous=True)
        self.assertEqual(identity.app, "Gxqk7W2na5.Shortcuts")
        self.assertEqual(files, previous)
        weather = {k: v for k, v in self.resources.items() if k != "time-controls.js"}
        weather["config.xml"] = WEATHER_MANIFEST
        write_widget(self.widget, signed_members(weather))
        self.assertEqual(
            launchpad.signed_widget_resources(self.widget, previous=True)[1], weather
        )
        with self.assertRaises(ValueError):
            launchpad.verify_widget(self.widget, self.source)
        for changed in (
            previous | {"config.xml": MANIFEST},
            previous | {launchpad.WEATHER_CONFIG: b"{}"},
            self.resources | {"config.xml": LEGACY_MANIFEST},
            self.resources | {"config.xml": WEATHER_MANIFEST},
            weather | {"config.xml": MANIFEST},
        ):
            write_widget(self.widget, signed_members(changed))
            with self.subTest(resources=list(changed)), self.assertRaises(ValueError):
                launchpad.signed_widget_resources(self.widget, previous=True)

    def test_rejects_incomplete_signatures_digests_and_resource_coverage(self):
        namespace = launchpad.DS_NS
        mutations = (
            lambda root: root.remove(root.find(namespace + "SignatureValue")),
            lambda root: root.remove(root.find(namespace + "KeyInfo")),
            lambda root: root.find(".//" + namespace + "DigestValue").__setattr__(
                "text", "wrong"
            ),
            lambda root: root.find(".//" + namespace + "DigestMethod").set(
                "Algorithm", "unsupported"
            ),
            lambda root: root.find(namespace + "SignedInfo").remove(
                root.find(namespace + "SignedInfo/" + namespace + "Reference")
            ),
            lambda root: root.find(".//" + namespace + "Reference").set(
                "URI", "missing.js"
            ),
        )
        for index, mutate in enumerate(mutations):
            members = signed_members(self.resources)
            root = ET.fromstring(members["signature1.xml"])
            mutate(root)
            members["signature1.xml"] = ET.tostring(root)
            with self.subTest(index=index), self.assertRaises(ValueError):
                self.verify(members)
        members = signed_members(self.resources)
        members["signature1.xml"] = b"<Signature/>"
        with self.assertRaisesRegex(ValueError, "XML signature"):
            self.verify(members)

    def test_rejects_oversized_archive_members_and_unsafe_xml(self):
        self.widget.write_bytes(b"x" * (8 * 1024 * 1024 + 1))
        with self.assertRaisesRegex(ValueError, "Invalid signed widget"):
            launchpad.verify_widget(self.widget, self.source)
        members = signed_members(self.resources)
        members["icon.png"] = b"x" * (1024 * 1024 + 1)
        with self.assertRaisesRegex(ValueError, "oversized"):
            self.verify(members)
        for data in (
            b"",
            b"broken",
            b"x" * (256 * 1024 + 1),
            b"<!DOCTYPE widget><widget/>",
        ):
            with self.subTest(length=len(data)), self.assertRaises(ValueError):
                launchpad.parse_xml(data)

    def test_rejects_resource_symlinks_and_oversized_source(self):
        resource = self.source / "ui.js"
        resource.unlink()
        resource.symlink_to(self.source / "device.js")
        with self.assertRaisesRegex(ValueError, "resource"):
            launchpad.source_files(self.source)
        resource.unlink()
        resource.write_bytes(b"x" * (1024 * 1024 + 1))
        with self.assertRaisesRegex(ValueError, "resource"):
            launchpad.source_files(self.source)

    def sdk(self):
        sdk = self.root / "sdk"
        cli = sdk / "tools/ide/bin/tizen"
        cli.parent.mkdir(parents=True)
        cli.write_text("#!/bin/sh\nexit 99\n")
        cli.chmod(0o700)
        return sdk

    def sdk_result(self, command, **kwargs):
        self.assertEqual(command[1:5], ["package", "-t", "wgt", "-s"])
        self.assertEqual(command[5], "existing-profile")
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        self.assertEqual(kwargs["timeout"], 90)
        with zipfile.ZipFile(command[-1]) as archive:
            unsigned = {name: archive.read(name) for name in archive.namelist()}
        self.assertEqual(unsigned, self.resources)
        target = Path(command[command.index("-o") + 1]) / "Shortcuts.wgt"
        write_widget(target, signed_members(unsigned, "sha512"))
        return subprocess.CompletedProcess(command, 0)

    def test_sdk_success_promotes_only_verified_artifact_and_private_receipt(self):
        output = self.root / "build"
        with patch.object(launchpad.subprocess, "run", side_effect=self.sdk_result):
            artifact = launchpad.build(
                self.sdk(), "existing-profile", output, self.source
            )
        self.assertEqual(artifact, output / "Shortcuts.wgt")
        self.assertEqual(
            {path.name for path in output.iterdir()}, {"Shortcuts.wgt", "build.json"}
        )
        self.assertEqual(artifact.stat().st_mode & 0o777, 0o600)
        self.assertEqual(output.stat().st_mode & 0o777, 0o700)
        receipt = json.loads((output / "build.json").read_text())
        self.assertFalse(receipt["installed"])
        self.assertEqual(
            receipt["sha256"], hashlib.sha256(artifact.read_bytes()).hexdigest()
        )
        self.assertEqual(
            receipt["source_sha256"],
            {
                name: hashlib.sha256(data).hexdigest()
                for name, data in self.resources.items()
                if name in launchpad.RESOURCES
            },
        )
        self.assertEqual(receipt["weather"], {})
        self.assertEqual((output / "build.json").stat().st_mode & 0o777, 0o600)
        self.assertNotIn("existing-profile", (output / "build.json").read_text())

    def test_executable_cli_override_preserves_argv_and_artifact_verification(self):
        cli = self.root / "local wrapper.py"
        cli.write_text("#!/usr/bin/env python3\nraise SystemExit(99)\n")
        cli.chmod(0o700)
        with patch.object(
            launchpad.subprocess, "run", side_effect=self.sdk_result
        ) as run:
            artifact = launchpad.build(
                None,
                "existing-profile",
                self.root / "wrapper-build",
                self.source,
                cli=cli,
            )
        self.assertEqual(run.call_args.args[0][0], str(cli))
        self.assertEqual(
            launchpad.verify_widget(artifact, self.source).app,
            "Gxqk7W2na5.Shortcuts",
        )

    def test_invalid_cli_stops_before_creating_output_or_running_signer(self):
        non_executable = self.root / "not-executable"
        non_executable.write_text("#!/bin/sh\nexit 0\n")
        non_executable.chmod(0o600)
        output = self.root / "unused"
        with patch.object(launchpad.subprocess, "run") as run:
            for cli in (self.root / "missing", self.source, non_executable, None):
                with self.subTest(cli=cli), self.assertRaises(ValueError):
                    launchpad.build(
                        None, "existing-profile", output, self.source, cli=cli
                    )
            run.assert_not_called()
        self.assertFalse(output.exists())

    def test_command_requires_exactly_one_sdk_or_cli(self):
        common = ["launchpad.py", "--profile", "Samshush", "--output", "build"]
        with patch.object(launchpad, "build") as build:
            for options in ([], ["--sdk", "sdk", "--cli", "wrapper"]):
                with (
                    self.subTest(options=options),
                    patch.object(sys, "argv", common + options),
                    contextlib.redirect_stderr(io.StringIO()),
                    self.assertRaises(SystemExit) as result,
                ):
                    launchpad.main()
                self.assertEqual(result.exception.code, 2)
            build.assert_not_called()
            for option in ("--sdk", "--cli"):
                with (
                    self.subTest(option=option),
                    patch.object(sys, "argv", common + [option, "tool"]),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    launchpad.main()
                path = Path("tool").resolve()
                build.assert_called_with(
                    path if option == "--sdk" else None,
                    "Samshush",
                    Path("build").resolve(),
                    cli=path if option == "--cli" else None,
                    weather_postcode=None,
                )

    def test_weather_postcode_build_generates_only_private_configuration(self):
        coordinates = {"latitude": 34.0901, "longitude": -118.4065}
        self.resources[launchpad.WEATHER_CONFIG] = (
            b'{"latitude":34.0901,"longitude":-118.4065}\n'
        )
        with (
            patch.object(
                launchpad, "resolve_weather_postcode", return_value=coordinates
            ) as resolve,
            patch.object(launchpad.subprocess, "run", side_effect=self.sdk_result),
        ):
            artifact = launchpad.build(
                self.sdk(),
                "existing-profile",
                self.root / "weather-build",
                self.source,
                weather_postcode="90210",
            )
        resolve.assert_called_once_with("90210")
        self.assertFalse((self.source / launchpad.WEATHER_CONFIG).exists())
        _, resources = launchpad.signed_widget_resources(artifact, self.source)
        self.assertEqual(
            launchpad.weather_config(resources[launchpad.WEATHER_CONFIG]), coordinates
        )
        receipt = json.loads((artifact.parent / "build.json").read_text())
        self.assertEqual(receipt["weather_postcode"], "90210")
        self.assertEqual(receipt["weather"], coordinates)

    def test_signer_cannot_substitute_valid_but_unrequested_weather_coordinates(self):
        def substitute(command, **kwargs):
            result = self.sdk_result(command, **kwargs)
            target = Path(command[command.index("-o") + 1]) / "Shortcuts.wgt"
            changed = self.resources | {
                launchpad.WEATHER_CONFIG: b'{"latitude":40,"longitude":-70}'
            }
            write_widget(target, signed_members(changed))
            return result

        output = self.root / "substituted-build"
        with (
            patch.object(launchpad.subprocess, "run", side_effect=substitute),
            self.assertRaisesRegex(ValueError, "differs from this build"),
        ):
            launchpad.build(self.sdk(), "existing-profile", output, self.source)
        self.assertEqual(list(output.iterdir()), [])

    def test_postcode_lookup_is_bounded_and_validates_the_returned_area(self):
        value = {
            "post code": "90210",
            "country abbreviation": "US",
            "places": [{"latitude": "34.0901", "longitude": "-118.4065"}],
        }
        url = "https://api.zippopotam.us/us/90210"
        response = SimpleNamespace(
            status=200,
            geturl=lambda: url,
            read=lambda limit: json.dumps(value).encode(),
        )
        with patch.object(
            launchpad.urllib.request,
            "urlopen",
            return_value=contextlib.nullcontext(response),
        ) as request:
            self.assertEqual(
                launchpad.resolve_weather_postcode("90210"),
                {"latitude": 34.0901, "longitude": -118.4065},
            )
        self.assertEqual(request.call_args.args[0].full_url, url)
        self.assertEqual(request.call_args.kwargs, {"timeout": 10})
        for changed in (
            value | {"post code": "10001"},
            value | {"country abbreviation": "CA"},
            value | {"places": []},
            value | {"places": [{"latitude": "NaN", "longitude": "0"}]},
            value
            | {
                "places": [
                    {"latitude": "0", "longitude": "0"},
                    {"latitude": "1", "longitude": "1"},
                ]
            },
        ):
            response.read = lambda limit, data=changed: json.dumps(data).encode()
            with (
                self.subTest(data=changed),
                patch.object(
                    launchpad.urllib.request,
                    "urlopen",
                    return_value=contextlib.nullcontext(response),
                ),
                self.assertRaises(ValueError),
            ):
                launchpad.resolve_weather_postcode("90210")

    def test_invalid_postcode_and_lookup_failure_never_silently_disable_weather(self):
        with patch.object(launchpad.urllib.request, "urlopen") as request:
            for postcode in ("", "9021", "90210-1234", "../../", "１２３４５"):
                with self.subTest(postcode=postcode), self.assertRaises(ValueError):
                    launchpad.resolve_weather_postcode(postcode)
            request.assert_not_called()
        for failure in (
            TimeoutError(),
            urllib.error.HTTPError(
                "https://api.zippopotam.us/us/00000", 404, "Not Found", {}, None
            ),
        ):
            with (
                self.subTest(failure=failure),
                patch.object(launchpad.urllib.request, "urlopen", side_effect=failure),
                self.assertRaises(ValueError),
            ):
                launchpad.resolve_weather_postcode("00000")

    def test_partial_sdk_failures_never_promote_artifacts_or_success_receipts(self):
        sdk = self.sdk()
        for failure in ("exit", "timeout", "invalid"):
            output = self.root / failure

            def partial(command, mode=failure, **kwargs):
                result = self.sdk_result(command, **kwargs)
                if mode == "exit":
                    return subprocess.CompletedProcess(command, 1)
                if mode == "timeout":
                    raise subprocess.TimeoutExpired(command, 90)
                target = Path(command[command.index("-o") + 1]) / "Shortcuts.wgt"
                target.write_bytes(b"partial widget")
                return result

            with (
                self.subTest(failure=failure),
                patch.object(launchpad.subprocess, "run", side_effect=partial),
                self.assertRaises(ValueError),
            ):
                launchpad.build(sdk, "existing-profile", output, self.source)
            self.assertEqual(list(output.iterdir()), [])

    def test_invalid_profile_or_existing_output_stops_before_sdk(self):
        sdk = self.sdk()
        with patch.object(launchpad.subprocess, "run") as run:
            for profile in ("", "x" * 129, "line\nbreak"):
                with self.subTest(profile=profile), self.assertRaises(ValueError):
                    launchpad.build(sdk, profile, self.root / "unused", self.source)
            output = self.root / "existing"
            output.mkdir()
            (output / "keep").write_text("preserved")
            with self.assertRaises(FileExistsError):
                launchpad.build(sdk, "existing-profile", output, self.source)
            self.assertEqual((output / "keep").read_text(), "preserved")
            run.assert_not_called()


class LaunchpadDeviceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="samshush-device-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "launcher"
        self.source.mkdir()
        self.resources = {
            name: MANIFEST if name == "config.xml" else f"fixture {name}".encode()
            for name in launchpad.RESOURCES
        }
        self.resources[launchpad.WEATHER_CONFIG] = b"{}\n"
        for name, data in self.resources.items():
            if name in launchpad.RESOURCES:
                (self.source / name).write_bytes(data)
        self.widget = self.root / "Shortcuts.wgt"
        write_widget(self.widget, signed_members(self.resources))
        self.original_widget = self.widget.read_bytes()
        self.previous_widget = self.root / "Previous.wgt"
        self.previous_resources = {
            name: self.resources[name] for name in launchpad.LEGACY_RESOURCES
        }
        self.previous_resources["config.xml"] = LEGACY_MANIFEST
        self.previous_resources["ui.js"] = b"previous installed release"
        write_widget(self.previous_widget, signed_members(self.previous_resources))
        self.assets = self.root / "assets"
        (self.assets / "payloads").mkdir(parents=True)
        for suffix in (".dll", ".runtimeconfig.json"):
            (self.assets / "payloads" / (launchpad_routing.NAME + suffix)).write_bytes(
                b"helper fixture"
            )
        self.routing_calls = []
        self.device_scripts = []
        self.sdk_calls = []
        self.pushes = []
        self.registration_count = "0"
        self.fail_resources = False
        self.fail_upload = False
        self.sdk_output = "install completed"
        self.sdk_returncode = 0
        self.model_error = None
        self.on_model = None
        self.on_sdk = None
        self.initial_phase = "absent"
        self.route_package = "Gxqk7W2na5"
        self.route_app = "Gxqk7W2na5.Shortcuts"
        self.route_autostart = 0
        self.installed_weather = b"{}\n"
        self.bad_probe = False
        self.bad_routing = False
        self.fake_frame = SimpleNamespace(
            ASSETS=self.assets,
            policy=lambda: {"build": home.BUILD, "model": home.MODEL},
            check_result=frame.check_result,
        )

    def route_output(self, operation):
        original = {
            "Operation": "com.samsung.tv.csfs",
            "Strings": {key: "" for key in launchpad_routing.KEYS},
            "AutoStart": self.route_autostart,
        }
        original["Strings"][launchpad_routing.HOME_KEY] = "com.samsung.tv.csfs"
        phase = {"apply": "applied", "restore": "restored"}.get(
            operation, self.initial_phase
        )
        current = copy.deepcopy(original)
        if phase in {"applied", "restore-failed"}:
            current["Operation"] = self.route_app
            current["Strings"][launchpad_routing.HOME_KEY] = current["Operation"]
        receipt = (
            None
            if phase == "absent"
            else {
                "Version": 1,
                "Build": home.BUILD,
                "Package": self.route_package,
                "App": self.route_app,
                "Phase": phase,
                "Original": original,
            }
        )
        value = {
            "schema": "samshush-home-routing-v1",
            "operation": operation,
            "phase": phase,
            "current": current,
            "receipt": receipt,
        }
        return (
            "LAUNCHPAD_ROUTING_JSON="
            + json.dumps(value)
            + "\nLAUNCHPAD_ROUTING_CLEANED\n"
        )

    def run_operation(self, operation, *, widget=None):
        args = SimpleNamespace(
            operation=operation,
            tv="192.0.2.1",
            callback_host="192.0.2.2",
            widget=widget
            if widget is not None
            else self.widget
            if operation in {"install", "update", "activate"}
            else None,
            previous_widget=self.previous_widget if operation == "update" else None,
            logs=self.root / "reports",
        )
        source_files = launchpad.source_files
        verify_widget = launchpad.verify_widget

        def render(operation, artifacts, **options):
            self.routing_calls.append((operation, options))
            return f"TEST_ROUTING {operation}\n"

        async def probe(*args, **kwargs):
            script = kwargs["commands"][0]
            self.device_scripts.append(script)
            rc = 0
            if "TEST_ROUTING " in script:
                requested = script.split("TEST_ROUTING ")[1].split()[0]
                if "LAUNCHPAD_RESOURCES_VERIFIED" in script:
                    rc = int(self.fail_resources)
                output = (
                    "missing routing output"
                    if self.bad_routing
                    else self.route_output(requested)
                )
                if requested in {"apply", "restore"}:
                    self.initial_phase = {"apply": "applied", "restore": "restored"}[
                        requested
                    ]
            elif "LAUNCHPAD_RESOURCES_VERIFIED" in script:
                rc = int(self.fail_resources)
                output = "LAUNCHPAD_PACKAGE_COUNT 1\nLAUNCHPAD_APP_COUNT 1\nLAUNCHPAD_RESOURCES_VERIFIED\n"
                if "LAUNCHPAD_WEATHER_CONFIG" in script:
                    output += (
                        "LAUNCHPAD_WEATHER_CONFIG "
                        + base64.b64encode(self.installed_weather).decode()
                        + "\n"
                    )
            elif "LAUNCHPAD_PACKAGE_COUNT" in script:
                output = f"LAUNCHPAD_PACKAGE_COUNT {self.registration_count}\nLAUNCHPAD_APP_COUNT {self.registration_count}\n"
            elif "LAUNCHPAD_UPLOAD_CLEANED" in script:
                output = "LAUNCHPAD_UPLOAD_CLEANED\n"
            elif "LAUNCHPAD_UPLOAD_VERIFIED" in script:
                rc = int(self.fail_upload)
                output = "LAUNCHPAD_UPLOAD_VERIFIED\n"
            else:
                raise AssertionError("Unexpected device script")
            return {
                "authenticated": True,
                "uid": 0,
                "euid": 0,
                "gid": 0,
                "egid": 0,
                "system_files_unchanged": True,
                "staging_cleaned": not self.bad_probe,
                "commands": [{"stdout": output, "stderr": "", "exit_code": rc}],
            }

        def verify_model(tv):
            if self.on_model:
                self.on_model()
            if self.model_error:
                raise self.model_error

        def push(local, remote):
            self.pushes.append((Path(local).read_bytes(), str(remote)))

        def sdk_run(command, **kwargs):
            self.sdk_calls.append(command)
            if self.on_sdk:
                self.on_sdk()
            return subprocess.CompletedProcess(
                command, self.sdk_returncode, self.sdk_output, ""
            )

        client = SimpleNamespace(
            serial="192.0.2.1:26101",
            connect=lambda: None,
            require_device=lambda: None,
            push=push,
            run=sdk_run,
        )
        with contextlib.ExitStack() as stack:
            for target, value in (
                ("launchpad.SOURCE", self.source),
                (
                    "launchpad.source_files",
                    lambda source=self.source: source_files(source),
                ),
                (
                    "launchpad.verify_widget",
                    lambda path: verify_widget(path, self.source),
                ),
                ("home_layout.verify_model", verify_model),
                ("home_layout.device_command", lambda script: script),
                ("launchpad_routing.render_script", render),
                ("samsung_tv_root.archive_root.probe", probe),
                ("samsung_tv_root.sdb.find_sdb", lambda: "fixture-sdb"),
                ("samsung_tv_root.sdb.SdbClient", lambda *args, **kwargs: client),
            ):
                stack.enter_context(patch(target, value))
            stack.enter_context(patch("builtins.print"))
            result = launchpad.run(args, self.fake_frame)
        reports = list(args.logs.glob("*.json"))
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].stat().st_mode & 0o777, 0o600)
        return result, json.loads(reports[0].read_text())

    def test_inspect_uses_read_only_routing_and_no_sdk(self):
        result, evidence = self.run_operation("inspect")
        self.assertEqual(result, 0, evidence)
        self.assertEqual(self.routing_calls, [("query", {})])
        self.assertEqual(self.sdk_calls, [])
        self.assertEqual(self.pushes, [])

    def test_status_reads_journal_without_sdk_or_installation(self):
        self.initial_phase = "applied"
        result, evidence = self.run_operation("status")
        self.assertEqual(result, 0, evidence)
        self.assertEqual(self.routing_calls, [("status", {})])
        self.assertEqual(self.sdk_calls, [])
        self.assertEqual(self.pushes, [])

    def test_install_keeps_home_assignment_and_checks_resources_and_upload_cleanup(
        self,
    ):
        result, evidence = self.run_operation("install")
        self.assertEqual(result, 0, evidence)
        self.assertTrue(evidence["completed"])
        self.assertEqual(self.routing_calls, [("query", {})])
        self.assertEqual(self.pushes[0][0], self.original_widget)
        frozen = Path(evidence["verified_widget"])
        self.assertEqual(frozen.read_bytes(), self.original_widget)
        self.assertEqual(frozen.stat().st_mode & 0o777, 0o600)
        uploaded_hash = hashlib.sha256(self.original_widget).hexdigest()
        verification = next(
            script
            for script in self.device_scripts
            if "LAUNCHPAD_UPLOAD_VERIFIED" in script
        )
        self.assertIn(uploaded_hash, verification)
        self.assertEqual(self.sdk_calls[0][3:5], ("0", "vd_appinstall"))
        self.assertEqual(self.sdk_calls[0][5], "Gxqk7W2na5.Shortcuts")
        self.assertTrue(
            any(
                "LAUNCHPAD_RESOURCES_VERIFIED" in script
                for script in self.device_scripts
            )
        )
        self.assertIn("LAUNCHPAD_UPLOAD_CLEANED", self.device_scripts[-1])

    def test_update_verifies_previous_and_new_resources_and_preserves_home(self):
        self.initial_phase = "applied"
        weather = b'{"latitude":40.0,"longitude":-70.0}\n'
        write_widget(
            self.widget,
            signed_members(self.resources | {launchpad.WEATHER_CONFIG: weather}),
        )
        new_widget = self.widget.read_bytes()
        old_widget = self.previous_widget.read_bytes()
        result, evidence = self.run_operation("update")
        self.assertEqual(result, 0, evidence)
        self.assertEqual([call[0] for call in self.routing_calls], ["status", "status"])
        self.assertEqual(evidence["before"]["receipt"], evidence["after"]["receipt"])
        self.assertEqual(evidence["before"]["current"], evidence["after"]["current"])
        self.assertEqual(self.pushes[0][0], new_widget)
        self.assertEqual(Path(evidence["previous_widget"]).read_bytes(), old_widget)
        self.assertEqual(
            Path(evidence["previous_widget"]).stat().st_mode & 0o777, 0o600
        )
        resource_checks = [
            script
            for script in self.device_scripts
            if "LAUNCHPAD_RESOURCES_VERIFIED" in script
        ]
        self.assertEqual(len(resource_checks), 2)
        self.assertIn(
            hashlib.sha256(self.previous_resources["ui.js"]).hexdigest(),
            resource_checks[0],
        )
        self.assertNotIn("weather-config.json", resource_checks[0])
        self.assertIn(hashlib.sha256(weather).hexdigest(), resource_checks[1])
        self.assertEqual(len(self.sdk_calls), 1)
        self.assertEqual(
            self.sdk_calls[0][4:6], ("vd_appinstall", "Gxqk7W2na5.Shortcuts")
        )

    def test_update_requires_an_applied_matching_home_journal_before_upload(self):
        for phase, package, app in (
            ("absent", "Gxqk7W2na5", "Gxqk7W2na5.Shortcuts"),
            ("restored", "Gxqk7W2na5", "Gxqk7W2na5.Shortcuts"),
            ("applied", "OtherPkg12", "OtherPkg12.Shortcuts"),
        ):
            with self.subTest(phase=phase, app=app):
                self.initial_phase, self.route_package, self.route_app = (
                    phase,
                    package,
                    app,
                )
                result, evidence = self.run_operation("update")
                self.assertEqual(result, 1)
                self.assertIn("matching applied Home assignment", evidence["error"])
                self.assertEqual(self.pushes, [])
                self.assertEqual(self.sdk_calls, [])
                for report in (self.root / "reports").glob("*.json"):
                    report.unlink()

    def test_previous_installed_resource_drift_blocks_update_before_upload(self):
        self.initial_phase = "applied"
        self.fail_resources = True
        result, evidence = self.run_operation("update")
        self.assertEqual(result, 1, evidence)
        self.assertEqual(self.pushes, [])
        self.assertEqual(self.sdk_calls, [])
        self.assertEqual([call[0] for call in self.routing_calls], ["status"])

    def test_update_failure_keeps_both_widgets_and_does_not_retry_or_change_home(self):
        self.initial_phase = "applied"
        self.sdk_output = "install failed"
        result, evidence = self.run_operation("update")
        self.assertEqual(result, 1, evidence)
        self.assertTrue(Path(evidence["previous_widget"]).is_file())
        self.assertTrue(Path(evidence["verified_widget"]).is_file())
        self.assertEqual(len(self.sdk_calls), 1)
        self.assertEqual([call[0] for call in self.routing_calls], ["status"])

    def test_update_fails_if_final_resources_or_home_receipt_change(self):
        for failure in ("resources", "home"):
            with self.subTest(failure=failure):
                self.initial_phase = "applied"
                self.fail_resources = False
                self.route_autostart = 0
                self.on_sdk = lambda failure=failure: setattr(
                    self,
                    "fail_resources" if failure == "resources" else "route_autostart",
                    1,
                )
                result, evidence = self.run_operation("update")
                self.assertEqual(result, 1, evidence)
                self.assertFalse(evidence["completed"])
                self.assertTrue(all(call[0] == "status" for call in self.routing_calls))
                if failure == "home":
                    self.assertIn("Home assignment changed", evidence["error"])
                for report in (self.root / "reports").glob("*.json"):
                    report.unlink()

    def test_invalid_previous_widget_stops_before_any_device_access(self):
        self.initial_phase = "applied"
        self.previous_widget.write_bytes(b"damaged recovery artifact")
        with self.assertRaises(ValueError):
            self.run_operation("update")
        self.assertEqual(self.device_scripts, [])
        self.assertEqual(self.sdk_calls, [])

    def test_changed_widget_during_preflight_never_installs_unverified_bytes(self):
        self.on_model = lambda: self.widget.write_bytes(b"changed after verification")
        result, evidence = self.run_operation("install")
        if self.sdk_calls:
            self.assertEqual(self.pushes[0][0], self.original_widget)
        else:
            self.assertEqual(result, 1, evidence)
            self.assertFalse(evidence["completed"])

    def test_existing_registration_blocks_install_before_push(self):
        self.registration_count = "1"
        result, evidence = self.run_operation("install")
        self.assertEqual(result, 1)
        self.assertIn("already registered", evidence["error"])
        self.assertEqual(self.pushes, [])
        self.assertEqual(self.sdk_calls, [])

    def test_wrong_model_stops_before_any_transport(self):
        self.model_error = ValueError("Wrong model")
        result, evidence = self.run_operation("install")
        self.assertEqual(result, 1)
        self.assertFalse(evidence["completed"])
        self.assertEqual(self.device_scripts, [])
        self.assertEqual(self.sdk_calls, [])

    def test_incomplete_installer_confirmation_keeps_failure_receipt(self):
        self.sdk_output = "installation requested"
        result, evidence = self.run_operation("install")
        self.assertEqual(result, 1)
        self.assertFalse(evidence["completed"])
        self.assertIn("not confirmed", evidence["error"])
        self.assertIn("staged_widget", evidence)
        self.assertEqual(len(self.sdk_calls), 1)

    def test_uploaded_widget_hash_failure_prevents_installer(self):
        self.fail_upload = True
        result, evidence = self.run_operation("install")
        self.assertEqual(result, 1)
        self.assertFalse(evidence["completed"])
        self.assertEqual(len(self.pushes), 1)
        self.assertEqual(self.sdk_calls, [])

    def test_sdk_failure_overrides_success_shaped_output(self):
        self.sdk_returncode = 1
        result, evidence = self.run_operation("install")
        self.assertEqual(result, 1)
        self.assertFalse(evidence["completed"])
        self.assertIn("SDK operation failed", evidence["error"])

    def test_invalid_local_widget_stops_before_device_access(self):
        self.widget.write_bytes(b"unsigned or damaged")
        with self.assertRaises(ValueError):
            self.run_operation("install")
        self.assertEqual(self.device_scripts, [])
        self.assertEqual(self.sdk_calls, [])
        self.assertEqual(self.pushes, [])

    def test_unverified_installed_resource_path_fails_before_launch(self):
        self.fail_resources = True
        self.sdk_output = "successfully launched"
        result, evidence = self.run_operation("launch")
        self.assertEqual(result, 1)
        self.assertFalse(evidence["completed"])
        self.assertEqual(self.sdk_calls, [])

    def test_launch_requires_resource_readback_and_sdk_confirmation(self):
        self.sdk_output = "successfully launched"
        result, evidence = self.run_operation("launch")
        self.assertEqual(result, 0, evidence)
        self.assertEqual(self.routing_calls, [("query", {})])
        self.assertEqual(
            self.sdk_calls[0],
            ("-s", "192.0.2.1:26101", "shell", "0", "execute", "Gxqk7W2na5.Shortcuts"),
        )
        self.assertIn("LAUNCHPAD_RESOURCES_VERIFIED", self.device_scripts[-1])
        self.assertEqual(self.pushes, [])

    def test_launch_validates_private_weather_configuration_before_sdk(self):
        self.sdk_output = "successfully launched"
        self.installed_weather = b'{"latitude":40.0,"longitude":-70.0}'
        result, evidence = self.run_operation("launch")
        self.assertEqual(result, 0, evidence)
        for report in (self.root / "reports").glob("*.json"):
            report.unlink()
        self.sdk_calls.clear()
        self.installed_weather = (
            b'{"latitude":40,"longitude":-70,"script":"unexpected"}'
        )
        result, evidence = self.run_operation("launch")
        self.assertEqual(result, 1, evidence)
        self.assertEqual(self.sdk_calls, [])

    def test_activate_checks_installed_resources_before_home_apply_and_status(self):
        result, evidence = self.run_operation("activate")
        self.assertEqual(result, 0, evidence)
        self.assertEqual(
            [call[0] for call in self.routing_calls], ["status", "apply", "status"]
        )
        self.assertEqual(
            self.routing_calls[1][1],
            {"package_id": "Gxqk7W2na5", "app_id": "Gxqk7W2na5.Shortcuts"},
        )
        self.assertIn("LAUNCHPAD_RESOURCES_VERIFIED", self.device_scripts[1])
        self.assertEqual(self.pushes, [])
        self.assertEqual(self.sdk_calls, [])

    def test_resource_failure_prevents_home_assignment_completion(self):
        self.fail_resources = True
        result, evidence = self.run_operation("activate")
        self.assertEqual(result, 1)
        self.assertFalse(evidence["completed"])
        self.assertEqual([call[0] for call in self.routing_calls], ["status", "apply"])
        self.assertNotIn("after", evidence)
        self.assertNotIn("status", evidence)
        self.assertEqual(self.sdk_calls, [])

    def test_incomplete_restore_can_query_then_recover_and_verify(self):
        self.initial_phase = "restore-failed"
        result, evidence = self.run_operation("restore")
        self.assertEqual(result, 0, evidence)
        self.assertEqual(
            [call[0] for call in self.routing_calls], ["query", "restore", "status"]
        )
        self.assertEqual(evidence["status"]["phase"], "restored")

    def test_failed_transport_cleanup_never_reports_completion(self):
        self.bad_probe = True
        result, evidence = self.run_operation("inspect")
        self.assertEqual(result, 1)
        self.assertFalse(evidence["completed"])
        self.assertEqual(self.sdk_calls, [])

    def test_missing_routing_evidence_never_reports_completion(self):
        self.bad_routing = True
        result, evidence = self.run_operation("inspect")
        self.assertEqual(result, 1)
        self.assertFalse(evidence["completed"])
        self.assertIn("routing/cleanup evidence", evidence["error"])
        self.assertEqual(self.sdk_calls, [])


if __name__ == "__main__":
    unittest.main()
