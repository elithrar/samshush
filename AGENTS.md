# Samshush agent guide

## Goal and scope

Help the owner disable microphone capture and always-on listening using the
strongest reversible controls supported by the exact TV. Implement only the
requested scope; an audit or research request does not authorize device changes.
Advertising, telemetry, unwanted apps and certificates are separate opt-in scopes.

Preserve Settings, normal remote navigation, HDMI/CEC, playback, audio output,
networking, DRM, manual updates, One Connect, Netflix, browser/shared runtimes and manual
Art Mode. Microphone shutdown takes priority over microphone-based automatic Art
presence sensing; explain that loss. Do not flash firmware, downgrade, develop a
launcher or install a persistent root hook as part of ordinary service shutdown.
Automatic boot reapplication is a separate, explicit scope.

The [reusable prompt](prompts/samsung-tv-privacy.md) describes the general method.
This file owns the repository layout and concrete commands. Reuse these tools
rather than generating parallel installers. README recommendations are guidance,
not authorization to apply every candidate in the profile. The verified default
controls cover voice apps/services and the separate ambient-sound app. Optional
app/service groups, certificate removal and the native Auto Update setting have
separate commands and journals. Apply them only within the owner's requested scope.

## Establish compatibility

1. Read `tools/profile.json`, `tools/provenance.json` and, when available,
   `.local/reports/current-state.json`. Historical evidence is not current state.
2. Discover the TV through existing records, ARP, mDNS or SSDP, then verify the full
   model, region, installed build and companion hardware. Determine the host LAN
   address that routes to the TV. Keep the TV awake during setup.
3. Follow [Samsung's device setup guide](https://developer.samsung.com/smarttv/develop/getting-started/using-sdk/tv-device.html).
   The documented Developer Mode flow uses Apps → App Settings → `12345`, the
   computer's LAN address and a reboot; menus vary. Request physical interaction
   only when needed. Developer Mode/SDB connectivity does not establish root.
4. Use existing official SDB and OpenSSL installations before installing duplicates.
   Run `uv sync --project tools --locked`, then `frame.py doctor`. SDB resolves from
   `--sdb`, `SDB`, the local cached SDK, or `PATH`. `doctor` never contacts the TV.
5. Inspect the access method and side effects before invoking it. The supplied
   profile and payloads have evidence only for QN65LS03FWFXZA, Tizen 9, build
   `T-RSMFAKUC-0090-REL-202607141953`. Do not bypass build checks or merely replace a
   build string to support another TV. The app path also requires owner UID 5001.
   All device commands acquire temporary root, including inspect/status/audit.
6. Keep official downloads immutable. Record source URL, retrieval date, size and
   SHA-256. Downloaded firmware may differ from the installed build. Extracted or
   decrypted images are not signed, flashable updates. Preserve paired families.

## Commands and state

Run from the repository root. Replace the example addresses with discovered ones.
Global connection options precede the command:

```sh
uv run --project tools tools/frame.py doctor
uv run --project tools tools/frame.py plan
uv run --project tools tools/frame.py --tv 192.0.2.10 --callback-host 192.0.2.20 inspect
uv run --project tools tools/frame.py --tv 192.0.2.10 --callback-host 192.0.2.20 voice-audit
```

`frame.py --help` is the command reference. `--logs` defaults to `.local/reports`;
reports include completion, command results and cleanup evidence. Protect raw logs
and device identifiers; do not publish them in a Gist or commit them.

| Command | Effect and limits |
| --- | --- |
| `apps apply` | Save native app flags/processes, disable 25 selected apps, verify launch denial. Persistent journal; refuses an existing journal. |
| `sound-sensor apply/status/verify/restore` | Separately block/read/verify/restore `ambient-sound`; uses `/opt/usr/home/owner/share/frame-sound-sensor-v1`, leaving the voice journal and shared sensor mediator alone. |
| `guards-apply` | Add eight verified vendor startup markers after a completed app apply; journal the markers. |
| `apply --scope voice` | Journal, mask and stop 24 voice units in PID 1's mount namespace for this boot. |
| `apps status` | Read back app flags, recorded guards and process absence. |
| `apps verify` | Also report boot evidence and explicitly attempt launches; unexpected success is a failure and may start an app. |
| `status` | Verify this boot's recorded masks/overlays. No journal means no recorded changes, not verified protection. |
| `hdmi-audit` | Read Anynet+/CEC settings and connected-device metadata; sends no power or input-switch commands. |
| `privacy-audit` | Read SmartThings sensor app flags, two known sound settings, Wi-Fi/ACR units and dependencies; does not change settings or launch apps. |
| `voice-audit` | Inspect processes and capture metadata; does not record audio. |
| `service-audit` | Read all system service/unit-file listings, profile and investigation dependencies, app registrations, native/managed process identities and selected presence settings. No app launch, media/packet capture or settings write. |
| `debloat-audit` | Read optional-app registrations, dependencies, process identities and game-model/config metadata without capturing screen frames. |
| `apps apply/status/verify/restore --group GROUP` | Control a profile app group with its own journal; CLI help lists available groups. |
| `services GROUP apply/status/restore` | Control a profile runtime group with its own journal; CLI help lists available groups. |
| `certificates apply/status/restore` | Overlay 11 verified certificate files in a separate journal, removing four emSign roots without disturbing voice state. Current boot only. |
| `auto-update status/disable/restore` | Read or set the native Auto Update option; preserve its original value and leave manual updater services intact. |
| `startup install/status/run/remove` | Save currently applied runtime scopes for automatic boot reapplication, inspect it, test the installed startup path, or restore the original startup script. |
| `restore` | Remove recorded runtime masks/overlays and restart units that were previously active. Requires startup removal first when installed. |
| `apps restore` | Remove recorded startup guards, restore app flags and restart previously running apps. |

For an authorized, compatible voice shutdown: inspect → audit → save pre-state →
`apps apply` → `guards-apply` → `apply --scope voice` → `apps verify` → `status` →
`voice-audit`. On another build, test native disable/readback/restore on one suitable
component before extending a batch. Preserve exact original flags, permissions,
labels and activation paths. Never delete vendor units or edit opaque caches.

Prefer the named groups for optional controls. The legacy `apply --scope services`
and `both` also include shared broadcast units that have not been applied; they
are not shorthand for the assessed groups. Do not use them as a blanket debloat.
For certificates use the separate `certificates` command, preserving the original
voice runtime journal. Verify certificate preimages and replacements; embedded
Chromium roots, cert-svc databases and app TLS caches are outside its coverage.

The native Auto Update key is `db/menu/support/softwareupdate/autoupdate` (Int32).
The stock menu maps Off to 0 and On to 1. The control saves the original value,
uses `vconftool set -t int KEY VALUE -f`, and verifies readback. Its journal is
persistent; a failed write retains recovery evidence. Do not stop a running
firmware installation or mask the general updater to change this preference.

## Automatic boot reapplication

When explicitly requested, use `startup install` after applying and verifying the
selected runtime controls. It enrolls only completed, matching journals. It does
not enable unapplied groups or change native app flags and update preferences.

The exact-build integration replaces `/opt/etc/parseinfo.sh`, invoked by the stock
`AT-HUB.service`. It saves the original bytes and metadata under
`/opt/usr/share/samshush-boot-v1`, runs the original script first, then starts a
separate root job without waiting for it. The job has a 180-second timeout and
checks the firmware, boot service and saved file hashes before reapplying the
selected scopes. It uses the existing runtime journals and operation lock. Each
service group snapshots unit state in a batch, journals every selected unit before
masking, then masks without
implicit reloads, reloads once, stops the batch and verifies it. Partial failure
uses the same journaled rollback. Groups remain sequential. It opens no network
listener and does not alter Developer Mode, firmware images or the vendor service cache.

Use `startup audit` for boot metadata and `startup probe` for a temporary root
interpreter check without installation. Run `startup run` to test the installed
path. After a reboot, use `startup status` and the ordinary scope status commands
before any host-side apply. Require a new boot ID and successful `/run/samshush-boot.result` and `.log` evidence. Installation
alone does not establish reboot persistence. Failed or interrupted jobs retain
recovery evidence and do not retry automatically. When installed, `startup status`
returns failure until the current run completes successfully, including when a
job is queued. An absent installation is reported separately.
Missing or duplicate unit records, missing properties and malformed service PIDs
fail validation before mutation. The host tests cover partial masking, reload and
stop failures, rollback, queued jobs, stale results and command overlap.
Startup control commands have a separate lock so `run` cannot overlap removal or
installation. Logs record monotonic phase timestamps for the launch handoff, each
scope, and service preflight, masking, reload, stop and readback. Compare phase
differences within a boot, not wall-clock timestamps.

Run `startup remove` before restoring runtime controls or changing the enrolled
scopes. It restores the original script and metadata while leaving current
controls applied. Then use the relevant restore commands, or reinstall after
verifying a changed selection. On changed hook or backup bytes, stop and inspect
rather than overwriting them. Keep scripts, profile, AGENTS.md and the concise,
user-facing README synchronized when behavior changes.

## Additional service groups

The offline review covers both TV and One Connect source families, product and
isolated-zone partitions, D-Bus entries, native service apps and bundled unit
files. These declarations are not a runtime inventory or a safe-to-disable list.
Use `service-audit` and save its private report before applying a new group.

New independent groups preserve the deployed group lists and recovery journals:

- `visual-analysis`: screen-analysis and contents-cognition managers; verifies
  the discovery, UI-detector and subtitle-recognition helpers are absent. Loss:
  food/text discovery and subtitle/accessibility recognition. Check ordinary
  subtitles, HDMI, manual Art and accessibility. A mask does not prevent another
  process reading a model, and these controls do not remove model packages.
- `gaming-backend`: ghdaemon/ghservice native IDs and the ghdaemon service/path.
  Preserve Game Mode, Game Bar, ALLM/VRR and controller support.
- `remote-support`: Remote Management, Easy Support and Shop Care daemons. Keep
  Maintenance Mode backup/restore, owner SDB and normal remote navigation.
- `remote-pc`: Remote PC native ID and unit. Keep zone isolation and shared
  browser/runtime services. No exploit on this firmware is established.
- `voice-launchers`: Alexa's separate jump-starter native ID. Do not rewrite the
  completed 25-app voice journal to add this registration.

The first four groups passed live apply and selected-process checks on the supported
TV. Visual-analysis rollback also restored its previously running managers. The
Alexa jump-starter registration was absent, so voice-launchers was not applied.
Full restore, retained-feature and fresh reboot validation remain separate. Apply
each app group before its runtime group. Native helper checks reject missing executable paths
before mutation and reject surviving helpers after stopping the manager, invoking
the existing journaled rollback. Runtime status checks selected native helpers as
well as masks. Unreadable executable links are accepted only for vanished tasks,
confirmed kernel threads or fully exited single-thread processes; unreadable live
userspace identities fail verification. Managed app verification stays in the
native-app command.

Before extending automatic boot reapplication, verify its current status, remove
the installed hook, apply/verify the new groups, reinstall and run the startup
path. Keep existing controls enrolled. An installed/run-tested hook is not a fresh
reboot test; require a changed boot ID for that claim.

Wi-Fi motion/CSI, user-awareness, context collection, generic app/model installers
and additional AI/model apps remain audit-only. Preserve shared sensors, soundbar
coordination (`ssc-service`/`ssdm-service`), input analysis/iACR, consent services
and One Connect. Do not infer privacy behavior from names: content-grab-service
has key-grabbing code, and route-webproxy is tied to ATSC3. Do not apply TV masks
to the companion processor based on duplicated filenames.

## Recovery and repeat runs

- Inspect before retrying after a timeout: the outcome is unknown. Do not start a
  second root operation while the first may still be cleaning up.
- Runtime state is `/run/frame-runtime-v1`; native state is
  `/opt/usr/home/owner/share/frame-voice-apps-v2`. These are recovery journals, not
  disposable caches. Optional app journals are `frame-apps-GROUP-v1` under the
  same owner share; Auto Update uses `frame-auto-update-v1`. Optional runtime
  journals are `/run/frame-services-GROUP-v1`; certificates use
  `/run/frame-certificates-v1`. Do not delete journals or stale locks to force an apply.
- Remove automatic reapplication first with `startup remove`, if installed.
  For voice recovery, run `restore` to remove runtime masks, then `apps restore` to
  remove guards and restore flags. A runtime restart may fail while native blocks
  remain: retain its journal and retry `restore` after the app restore succeeds.
  Verify both layers and their original processes before claiming recovery.
- Restore optional app flags with `apps restore --group GROUP`, then restore
  their runtime units with `services GROUP restore`. Use `certificates restore`
  and `auto-update restore` for those independent scopes; the plain `restore`
  command does not remove them. If an app restart fails while its units are still
  masked, retain the app journal and retry its restore after the runtime restore.
  Verify each restored layer.
- `apps restore` retains an audit journal. A new apply deliberately refuses it;
  archive it only after verifying restoration and saving the evidence locally.
- A full reboot clears `/run` controls. The optional startup hook reapplies its
  enrolled scopes. Native app flags/guards and the update
  preference use persistent storage; survival across a firmware update is not
  guaranteed. Do not use reboot as a substitute for checking a failed persistent restore.
- On preimage/build drift or an unhealthy service manager, stop mutations and
  prioritize recovery. Never change the opaque vendor systemd cache: the previous
  temporary cache experiment stalled PID 1 and was reverted.

## Prove the outcome

Check native flags, exact executable identity/process absence, service/path/socket/
bus/timer activation, direct app launches and applicable capture controls separately.
A stopped unit is insufficient when the app manager can start the executable.
For built-in .NET apps, process checks require a recognized launcher and an exact
selected DLL argument, including when Tizen places it in argument zero. Never
identify a selected app by its shared launcher alone. Host tests cover apply,
surviving-process status, changed PID identity and rollback; these checks do not
establish live capture denial or support for other TV builds.

Audit TV hardware microphones, AOV/standby, far-field engines, Smart Remote,
Bluetooth/HFP, soundbar passthrough, USB, browser, virtual inputs and HAL/DSP routes.
Also audit SmartThings TV Sound Sensor/Pet Care and `tvsensor-iot-service`.
The separate `sound-sensor` command blocks `ambient-sound`; it is deliberately
independent of the original voice app list. The stock
update script names `db/tvsensor/EnabledSoundDetection` and `db/tvsensor/sensor_onoff`;
these are discovery leads, not validated write instructions. The live audit found
Boolean false and `{ "light": "off", "sound": "off", "camera": "off" }` respectively.
Do not alter them when already off. `tvsensor-iot-service` also advertises a
light-sync mediator; preserve that shared app while blocking `ambient-sound`.
Prefer supported settings; do not guess integer values. Verify physical microphone-off controls.
Do not record audio or capture network packets. A quiet audit, an ACR-owned capture
endpoint or a failed wake word does not establish microphone capture denial.

For separately requested telemetry reduction, investigate Viewing Information
Services/ACR consent, `canalysis-service`, `wifi-presence` and Remote Management.
Their presence does not prove current upload or consent bypass. Check dependencies
before disabling them, and preserve core networking, shared sensors and playback.

Test retained features in the running interface where possible and label user
observations separately. For persistence: save the boot ID, finish cleanup, reboot
using a supported method, require a changed boot ID, and inspect `startup status`
plus the ordinary scope status commands before any host-side reapplication. Run
`apps verify` to check native launch denial. Test standby/wake separately. Do not
equate a software reboot with a power cycle, or either with survival across a
firmware update.

The 2026-10-25 live deployment verified 25 voice app blocks, eight startup guards,
24 voice units, ambient-sound and 17 optional app blocks. All selected native apps
rejected direct launches. Another 19 runtime units are masked, and all 11
certificate overlays matched their replacement hashes. Native Auto Update reads
Off. Startup removal also restored the original script's bytes, metadata and
security label on the TV.

The owner reported a temporary failure to leave Art Mode around the earlier
low-level reboot. Art, ambient-support, remoteapp and TV-viewer remain enabled.
No stop-propagation dependency to those components was found. After the startup
revision, the owner confirmed picture, Power/Art Mode switching and soundbar
volume worked. The earlier symptom's cause remains unproven. Microphone-based
automatic presence wake is intentionally lost with ambient-sound/far-field blocking.

The installed revision batches masks, status queries and stops, with seven
service-manager reloads across the selected groups instead of 43. It queues the
worker without waiting. A warm run returned from AT-HUB in 83 ms and checked the
existing controls in 1.09 seconds. This was a status check, not fresh application.

On the owner's subsequent physical power cycle, the kernel boot ID changed.
Before any host apply, the startup job had reapplied every enrolled scope, all
native flags and guards remained intact, and Auto Update still read Off. AT-HUB
ran for 3.237 seconds and exited 8.368 seconds after boot. The background worker
ran from 13.07 to 107.26 seconds, taking 94.19 seconds. Voice mask readback finished
at 59.04 seconds. The logs expose the interval before runtime controls are ready.
Native launch denial was tested before this reboot, while the post-boot check
read flags and process absence without launching apps.

The previous wrapper ran for about 130 seconds, with `starter.target` active at
about 11 seconds. On the physical power cycle, that target became active at
14.129 seconds. These are different boots, not a controlled whole-TV speed
comparison. The verified improvement is fewer manager operations and a handoff
that finishes before the worker. Do not describe the worker's duration as added
boot delay. Prefer an owner-controlled restart. The earlier single-force systemd
reboot skips normal unit shutdown and must not be the default test. On this TV,
the remote power procedures retained the kernel boot ID. If the empty local SDB
server cannot reconnect after a power cycle despite open TV ports, restarting
that local server recovered the connection without changing TV settings.

The owner confirmed Apple TV picture, sidebar/input selection, soundbar volume
and manual Art Mode. Netflix, browser, full restore, firmware-update persistence
and complete microphone capture denial are not fully verified. Update
`.local/reports/current-state.json` after live work with timestamps, per-control
evidence, recovery commands and explicit gaps.

Screen recognition is separate from Gaming Hub and from the retained
`canalysis-service`/iACR input/playback stack. The installed model has a FLOAT32
[1,64,64,3] input with INT8 internal tensors. Capture code requests 960×540 frames;
the installed analysis interval is 2500 ms. The `screen-recognition` group masks
both the recognition launcher and UI-image detector; verify all three recognition
processes are absent, including `contents-ott-detector-service`. This is not a
kernel-level prohibition on direct execution. Exact viewing-consent gating,
external uploads, recognition accuracy and CPU cost remain unproven.

PISA remains enabled: its manifests and code show account-consent/customization
clients as well as advertising integration. Its separate adagent is blocked.
Further PISA work needs a bounded test of consent/settings behavior. Shared slive/BMS
broadcast backends and `tvsensor-iot-service` light-sync integration remain enabled.
Do not describe selected SmartThings app blocks as removal of every widget or
streaming/sensor component. Keep unsupported coverage claims out of README.

## Files and offline tools

- `tools/`: all maintained Python entry points, uv lock/config, tests and profile.
- `tools/vendor/`: pinned root transport; preserve upstream license and provenance.
- `tools/assets/`: checksummed public certificate replacements, TV helper binaries
  and extractor patch. `device_scripts.py`/`payload_sources.py` contain TV Bash/C#
  source data; host entry points are Python. The TV does not need Python.
- `firmware/` (ignored): original ZIP, extracted/decrypted images, filesystems,
  metadata and retrieved device binaries. Historical metadata can contain old paths.
- `.local/` (ignored): toolchain, private reports, candidates and helper build output.

All tools support `--help`; invoke with `uv run --project tools tools/<name>.py`.

| Tool | Inputs and output |
| --- | --- |
| `inspect_firmware.py` | Official ZIP + pinned unixtract `keys.ukf`; requires fresh extraction output; writes inventory/header metadata. OpenSSL required. |
| `verify_images.py` | Decrypted sections + extractor logs; verifies CRCs and GPT, writes a local report. `--carve` explicitly writes partitions into a fresh directory. |
| `check_msd_signatures.py` | Original MSDs + extraction logs + updater public key; prints signature results. OpenSSL required. Does not prove modified firmware acceptance. |
| `inventory_components.py` | One extracted family, or `--firmware-root firmware/filesystems` for both. Scans all provided partitions, systemd/D-Bus/bundled definitions and package manifests; preserves family/source context in `.local/reports/components`. |
| `prepare_emudhra_removal.py RUN_NAME` | Exact baseline filesystem trees; writes non-installable certificate candidates under `.local/candidates/RUN_NAME`. Requires `rg` and OpenSSL. |
| `build_payloads.py` | Optional .NET SDK 6.0.428; rebuilds TV helpers into fresh `.local/payload-build`, compares hashes, never deploys. Prebuilt assets need no host .NET. |

Offline defaults target the paired 1301.0/1301.8 package, not arbitrary Samsung
formats. Decryption/filesystem extraction use external tools pinned in
`tools/provenance.json`; their local source/builds live under `.local/toolchain`.
They are not silently downloaded or bundled as independent project entry points.
Preserve file metadata and document reconstruction limits before proposing writes.

## Keep the public findings current

Update README’s **Findings & Services** table whenever a confirmed finding or
recommendation changes. Keep it user-facing: name the feature, give a concrete
recommendation and explain its benefit or tradeoff in one or two sentences.
Do not turn the table into a research backlog, device log or testing dashboard.
Keep the short supported-controls paragraph accurate about implementation and
persistence. Explain discoveries, decisions and unfinished work directly to the
user; writing a file is not a substitute for communicating the result.

Keep forensic evidence and open investigations in ignored local reports. The
Apple TV warning is reported on TV wake-up; Anynet+ is enabled and the device is
on HDMI 1. Its Power On handler calls `mbr_send_power_ir(0)`. No wake-up fix has
been implemented, and that call alone does not prove what went over the CEC bus.
Preserve CEC/ARC and diagnose the actual wake transition before changing behavior.

## Development and handoff

Use Python 3.12–3.13 and uv. Keep dependencies deliberate; runtime uses the standard
library plus pinned vendor code. Run Ruff lint/format and unittest commands from
README. Host tests use a fake TV; do not contact a TV to validate formatting/docs.
Test drift rejection, partial failure, recovery, transport quoting and empty/missing
evidence as failures. Keep vendored bytes unchanged unless deliberately updating
provenance. Never add generic arbitrary-shell device execution to this CLI.

Finish with actual changes, evidence, persistence scope and remaining gaps. Separate
prepared artifacts from applied controls. Keep useful firmware/evidence locally;
remove obsolete interim documentation instead of duplicating the current status.
