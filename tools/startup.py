"""Exact-build boot reapplication through the existing AT-HUB startup script."""

import hashlib
import io
import tarfile
import time

import remove_packages

ROOT = "/opt/usr/share/samshush-boot-v1"
HOOK = "/opt/etc/parseinfo.sh"
ORIGINAL_HASH = "c6369e7dec69408e176daa252d436acf6f8e265f560504d8bd831c8023c71d61"
UNIT_HASH = "b6829f0f7e95464178f92922e5fdca83bf0874df2e1bbb77d68db6892a5490b3"
WRAPPER = f'#!/usr/bin/gawk BEGIN{{exit system("cat {ROOT}/launch | /bin/bash")}}\n'

COMMON = r"""set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
umask 077
ROOT=/opt/usr/share/samshush-boot-v1
HOOK=/opt/etc/parseinfo.sh
LOCK=/run/samshush-startup.lock
CONTROL_LOCK=/run/samshush-startup-control.lock
LOG=/run/samshush-boot.log
RESULT=/run/samshush-boot.result
fail() { echo "ERROR: $*" >&2; exit 1; }
phase() {
    local uptime idle
    read -r uptime idle < /proc/uptime
    printf 'TIMING scope=startup phase=%s monotonic_s=%s\n' "$1" "$uptime"
}
hash() { sha256sum "$1" | cut -d ' ' -f 1; }
[[ $(id -u) == 0 ]] || fail 'UID 0 required'
identity() {
    grep -Fxq 'BUILD_ID=@@BUILD@@' /etc/os-release || fail 'Firmware changed; startup policy rejected'
    [[ $(hash /usr/lib/systemd/system/AT-HUB.service) == @@UNIT_HASH@@ ]] || fail 'Boot service changed'
}
verify() {
    [[ -d $ROOT && ! -L $ROOT && $(stat -c '%u:%a' "$ROOT") == 0:700 ]] || fail 'Unsafe startup directory'
    (cd "$ROOT" && sha256sum -c SHA256SUMS) || fail 'Startup files changed'
    [[ $(hash "$ROOT/original") == @@ORIGINAL_HASH@@ ]] || fail 'Original startup script changed'
    [[ -f $HOOK && ! -L $HOOK && $(hash "$HOOK") == $(hash "$ROOT/wrapper") ]] || fail 'Startup hook changed'
}
check_journal() {
    local directory=$1 name=$2 journal=$3 scope=$4
    [[ -d $journal && ! -L $journal && $(stat -c '%u:%a' "$journal") == 0:700 && -f $journal/complete && $(cat "$journal/scope") == "$scope" ]] || fail "Incomplete or incompatible journal: $journal"
    if [[ $scope == certificates ]]; then
        [[ $(hash "$journal/certs.tsv") == $(hash "$directory/$name.expected") ]] || fail 'Recorded certificate scope differs from this policy'
    else
        [[ $(cut -f 1 "$journal/units.tsv" | sha256sum | cut -d ' ' -f 1) == $(hash "$directory/$name.expected") ]] || fail "Recorded unit scope differs: $name"
    fi
}
"""

# The signed original still runs on firmware drift. The separate bounded service
# keeps reapplication failures out of AT-HUB's boot path.
LAUNCH = r"""set -uo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
umask 077
ROOT=/opt/usr/share/samshush-boot-v1
if [[ $(sha256sum "$ROOT/original" | cut -d ' ' -f 1) != @@ORIGINAL_HASH@@ ]]; then
    echo 'Original startup script changed' >&2
    exit 1
fi
exec > /run/samshush-boot-launch.log 2>&1
read -r uptime idle < /proc/uptime
printf 'TIMING scope=launch phase=begin monotonic_s=%s\n' "$uptime"
cat "$ROOT/original" | /bin/bash
original_rc=$?
read -r uptime idle < /proc/uptime
printf 'TIMING scope=launch phase=original_done monotonic_s=%s\n' "$uptime"
systemd-run --no-block --unit=samshush-boot.service --property=Type=oneshot --property=TimeoutStartSec=180 /bin/bash -c 'set -o pipefail; cat /opt/usr/share/samshush-boot-v1/worker | /bin/bash'
enqueue_rc=$?
read -r uptime idle < /proc/uptime
printf 'TIMING scope=launch phase=enqueued monotonic_s=%s rc=%s\n' "$uptime" "$enqueue_rc"
exit "$original_rc"
"""

WORKER = r"""
mkdir -m 700 "$LOCK" || fail 'Startup operation already running or interrupted'
finish() {
    rc=$?
    trap - EXIT
    phase end
    printf '%s %s\n' "$(cat /proc/sys/kernel/random/boot_id)" "$rc" > "$RESULT.tmp"
    mv -f "$RESULT.tmp" "$RESULT"
    rmdir "$LOCK" || rc=1
    exit "$rc"
}
trap finish EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
rm -f "$RESULT"
exec > "$LOG" 2>&1
phase begin
printf 'STARTUP BOOT '; cat /proc/sys/kernel/random/boot_id
identity
verify
phase verified
[[ -f $ROOT/installed && -s $ROOT/enabled ]] || fail 'Startup installation incomplete'
export FRAME_STAGE=$ROOT/certificates
while IFS=$'\t' read -r name journal scope; do
    phase "$name.begin"
    echo "REAPPLY $name"
    if [[ ! -e $journal ]]; then
        cat "$ROOT/$name.apply" | /bin/bash
    fi
    check_journal "$ROOT" "$name" "$journal" "$scope"
    cat "$ROOT/$name.status" | /bin/bash
    phase "$name.end"
done < "$ROOT/enabled"
if [[ -e /opt/usr/share/samshush-removals-v1 ]]; then
    phase packages.begin
    cat "$ROOT/packages.status" | nsenter --mount=/proc/1/ns/mnt /bin/bash
    phase packages.end
fi
echo 'STARTUP COMPLETE'
"""

CONTROL = r"""
ACTION=@@ACTION@@
if [[ $ACTION == status ]]; then
    if [[ ! -e $ROOT ]]; then echo 'Automatic reapplication is not installed'; exit 0; fi
    identity
    verify
    echo 'Automatic reapplication installed for:'
    cut -f 1 "$ROOT/enabled"
    active=$(systemctl show samshush-boot.service -p ActiveState --value)
    job=$(systemctl show samshush-boot.service -p Job --value)
    if [[ -n $job || -e $LOCK || -e $CONTROL_LOCK || $active == active || $active == activating || $active == deactivating ]]; then
        echo 'Startup operation is still running. Completion is not yet verified.'
        exit 1
    fi
    if [[ -f $RESULT ]]; then
        cat "$RESULT"
        cat "$LOG"
        [[ $(cat "$RESULT") == "$(cat /proc/sys/kernel/random/boot_id) 0" ]] || fail 'Startup reapplication failed or belongs to another boot'
    else
        fail 'No completed startup run recorded this boot'
    fi
    exit 0
fi
mkdir -m 700 "$CONTROL_LOCK" || fail 'Another startup command is running or interrupted'
operation_locked=0
TEMP=
finish() {
    rc=$?
    trap - EXIT
    if [[ -n $TEMP ]]; then rm -rf -- "$TEMP"; fi
    if [[ $operation_locked == 1 ]]; then rmdir "$LOCK" || rc=1; fi
    rmdir "$CONTROL_LOCK" || rc=1
    exit "$rc"
}
trap finish EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
if [[ $ACTION == run ]]; then
    identity
    verify
    active=$(systemctl show samshush-boot.service -p ActiveState --value)
    job=$(systemctl show samshush-boot.service -p Job --value)
    [[ -z $job && ! -e $LOCK && $active != active && $active != activating && $active != deactivating ]] || fail 'Startup worker already running'
    rm -f "$RESULT"
    if [[ $active == failed ]]; then systemctl reset-failed samshush-boot.service; fi
    systemctl start AT-HUB.service
    for ((i=0; i<170; i++)); do
        active=$(systemctl show samshush-boot.service -p ActiveState --value)
        job=$(systemctl show samshush-boot.service -p Job --value)
        if [[ -z $job && ! -e $LOCK && ( $active == inactive || $active == failed ) ]]; then
            if [[ -f $RESULT ]]; then
                cat "$LOG"
                [[ $(cat "$RESULT") == "$(cat /proc/sys/kernel/random/boot_id) 0" ]] || fail 'Startup reapplication failed'
                # Type=simple can still be finishing its short launch script.
                hub_active=$(systemctl show AT-HUB.service -p ActiveState --value)
                if [[ $hub_active == active || $hub_active == activating ]]; then sleep 1; continue; fi
                [[ $(systemctl show AT-HUB.service -p ExecMainStatus --value) == 0 ]] || fail 'Original startup script failed'
                exit 0
            fi
        fi
        sleep 1
    done
    fail 'Startup run did not finish; inspect its service and log before retrying'
fi
mkdir -m 700 "$LOCK" || fail 'Startup operation already running or interrupted'
operation_locked=1
if [[ $ACTION == remove ]]; then
    [[ -d $ROOT && ! -L $ROOT ]] || fail 'No startup installation to remove'
    [[ $(stat -c '%u:%a' "$ROOT") == 0:700 ]] || fail 'Unsafe startup directory'
    [[ $(hash "$ROOT/original") == @@ORIGINAL_HASH@@ ]] || fail 'Original backup changed; refusing restore'
    [[ ! -L $HOOK && -f $HOOK ]] || fail 'Unexpected startup hook type'
    current=$(hash "$HOOK")
    [[ $current == $(hash "$ROOT/wrapper") || $current == @@ORIGINAL_HASH@@ ]] || fail 'Startup hook changed; refusing overwrite'
    TEMP=$(mktemp -d /opt/etc/.samshush-restore.XXXXXXXX)
    cp --preserve=all "$ROOT/original" "$TEMP/parseinfo.sh"
    [[ $(hash "$TEMP/parseinfo.sh") == @@ORIGINAL_HASH@@ ]] || fail 'Restore copy mismatch'
    mv -f "$TEMP/parseinfo.sh" "$HOOK"
    [[ $(hash "$HOOK") == @@ORIGINAL_HASH@@ && $(stat -c '%u:%g:%a' "$HOOK") == $(stat -c '%u:%g:%a' "$ROOT/original") ]] || fail 'Restore readback failed'
    [[ $(chsmack "$HOOK" | cut -d ' ' -f 2-) == $(chsmack "$ROOT/original" | cut -d ' ' -f 2-) ]] || fail 'Restore labels changed'
    rm -rf -- "$ROOT"
    echo 'Automatic reapplication removed. Current controls remain until separately restored or rebooted.'
    exit 0
fi
[[ $ACTION == install ]] || fail 'Unknown startup action'
identity
[[ ! -e $ROOT && ! -L $ROOT ]] || fail 'Startup directory exists; inspect status or remove before installing'
[[ -f $HOOK && ! -L $HOOK && $(hash "$HOOK") == @@ORIGINAL_HASH@@ ]] || fail 'Original startup script changed'
[[ $(stat -c '%u:%g:%a' "$HOOK") == 0:0:777 ]] || fail 'Original startup metadata changed'
[[ $(hash "$FRAME_STAGE/startup.tar.gz") == @@ARCHIVE_HASH@@ ]] || fail 'Staged startup package changed'
TEMP=$(mktemp -d /opt/usr/share/.samshush-boot.XXXXXXXX)
chmod 700 "$TEMP"
chsmack -a _ "$TEMP"
tar -xzf "$FRAME_STAGE/startup.tar.gz" -C "$TEMP"
(cd "$TEMP" && sha256sum -c SHA256SUMS) || fail 'Startup package verification failed'
cp --preserve=all "$HOOK" "$TEMP/original"
: > "$TEMP/enabled"
while IFS=$'\t' read -r name journal scope; do
    [[ -e $journal ]] || continue
    check_journal "$TEMP" "$name" "$journal" "$scope"
    cat "$TEMP/$name.status" | /bin/bash
    printf '%s\t%s\t%s\n' "$name" "$journal" "$scope" >> "$TEMP/enabled"
done < "$TEMP/candidates"
[[ -s $TEMP/enabled ]] || fail 'No completed runtime controls to reapply'
if [[ -e /opt/usr/share/samshush-removals-v1 ]]; then
    cat "$TEMP/packages.status" | nsenter --mount=/proc/1/ns/mnt /bin/bash
fi
# Add the selected policy to the integrity manifest before the atomic directory move.
(cd "$TEMP" && sha256sum enabled >> SHA256SUMS)
chown -R 0:0 "$TEMP"
# This directory is private. Keep the original metadata for exact restoration.
find "$TEMP" -type f ! -name original -exec chmod 600 '{}' +
find "$TEMP" -type f ! -name original -exec chsmack -a _ '{}' +
chmod 755 "$TEMP/wrapper"
sync
mv "$TEMP" "$ROOT"
TEMP=$(mktemp -d /opt/etc/.samshush-install.XXXXXXXX)
cp --preserve=all "$ROOT/wrapper" "$TEMP/parseinfo.sh"
mv -f "$TEMP/parseinfo.sh" "$HOOK"
verify
touch "$ROOT/installed"
rm -f "$RESULT"
sync
echo 'Automatic reapplication installed for:'
cut -f 1 "$ROOT/enabled"
"""


def substitute(text, build, **values):
    for key, value in {
        "BUILD": build,
        "ORIGINAL_HASH": ORIGINAL_HASH,
        "UNIT_HASH": UNIT_HASH,
        **values,
    }.items():
        text = text.replace(f"@@{key}@@", value)
    if "@@" in text:
        raise ValueError("Unresolved startup placeholder")
    return text


def bundle(policy, render, assets):
    files = {
        "wrapper": WRAPPER.encode(),
        "launch": substitute(LAUNCH, policy["build"]).encode(),
        "worker": substitute(COMMON + WORKER, policy["build"]).encode(),
    }
    candidates = [
        ("voice", "/run/frame-runtime-v1", "voice", {}),
        (
            "certificates",
            "/run/frame-certificates-v1",
            "certificates",
            {"certificate_journal": True},
        ),
    ]
    candidates += [
        (name, f"/run/frame-services-{name}-v1", "services", {"unit_group": name})
        for name, entry in policy["optional_controls"].items()
        if entry["systemd_units"]
    ]
    for name, _, scope, options in candidates:
        for action in ("apply", "status"):
            files[f"{name}.{action}"] = (
                render(action, scope, **options) + "\n"
            ).encode()
        if scope == "certificates":
            expected = [
                "\t".join((c["path"], c["before"], c["after"], c["file"]))
                for c in policy["certificates"]
            ]
        elif name == "voice":
            expected = policy["voice_privacy"]["systemd_units"]
        else:
            expected = policy["optional_controls"][name]["systemd_units"]
        files[f"{name}.expected"] = ("\n".join(expected) + "\n").encode()
    files["packages.status"] = remove_packages.render("status", policy).encode()
    files["candidates"] = "".join(
        f"{name}\t{journal}\t{scope}\n" for name, journal, scope, _ in candidates
    ).encode()
    for cert in policy["certificates"]:
        files["certificates/" + cert["file"]] = (
            assets / "certificates" / cert["file"]
        ).read_bytes()
    files["SHA256SUMS"] = "".join(
        f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in files.items()
    ).encode()
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = int(time.time())
            info.mode = 0o600
            archive.addfile(info, io.BytesIO(data))
    return output.getvalue()


def control(action, build, archive_hash="unused"):
    return substitute(COMMON + CONTROL, build, ACTION=action, ARCHIVE_HASH=archive_hash)
