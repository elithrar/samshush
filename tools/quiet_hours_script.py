"""Reversible Art quiet-hours boot handoff and foreground worker lifecycle."""

import base64
import hashlib
import shlex

from home import BUILD
from startup import LAUNCH, ORIGINAL_HASH, UNIT_HASH, WRAPPER, substitute

ROOT = "/opt/usr/share/samshush-quiet-hours-v1"
UNIT = "samshush-art-quiet-hours.service"
FILES = ("ArtQuietHours.dll", "ArtQuietHours.runtimeconfig.json")


def _digest(data):
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


def _fill(source, **values):
    for key, value in values.items():
        source = source.replace(f"@@{key}@@", str(value))
    if "@@" in source:
        raise ValueError("Unresolved quiet-hours template")
    return source


def _minutes(start, end):
    if (
        any(type(v) is not int or not 0 <= v <= 1439 for v in (start, end))
        or start == end
    ):
        raise ValueError("Quiet hours require two different local minutes in 0..1439")


def render(operation, *, start=None, end=None, spotlight_art=None, assets):
    if operation not in {"set", "inspect", "status", "remove"}:
        raise ValueError("Unknown quiet-hours operation")
    if operation == "inspect" and start is None and end is None:
        start, end = 0, 1  # Classification only. No policy is installed by inspect.
    if operation in {"set", "inspect"}:
        _minutes(start, end)
    elif start is not None or end is not None:
        raise ValueError("Only set and inspect accept quiet-hours minutes")
    if spotlight_art is not None and (
        type(spotlight_art) is not bool or operation != "set"
    ):
        raise ValueError("Only set accepts a Spotlight Art policy")
    payloads = {}
    for name in FILES:
        data = assets.get(name)
        if not isinstance(data, bytes) or not 0 < len(data) <= 262144:
            raise ValueError(f"Invalid quiet-hours asset: {name}")
        payloads[name] = data
    legacy = substitute(LAUNCH, BUILD)
    common = _fill(COMMON, BUILD=BUILD, ROOT=ROOT, UNIT=UNIT, UNIT_HASH=UNIT_HASH)
    runner = common + RUNNER
    boot = common + _fill(
        BOOT,
        DLL_HASH=_digest(payloads[FILES[0]]),
        CFG_HASH=_digest(payloads[FILES[1]]),
        RUNNER_HASH=_digest(runner),
    )
    enqueue = (
        f'if [[ $(sha256sum {ROOT}/boot 2>/dev/null | cut -d " " -f 1) == {_digest(boot)} ]]; then\n'
        f'    cat {ROOT}/boot | /bin/bash || echo "Quiet-hours handoff failed" >&2\nfi\n'
    )
    privacy_launch = (
        legacy.removesuffix('exit "$original_rc"\n') + enqueue + 'exit "$original_rc"\n'
    )
    stock_launch = (
        f'set -uo pipefail\n[[ $(sha256sum {ROOT}/original | cut -d " " -f 1) == {ORIGINAL_HASH} ]] || exit 1\n'
        f"cat {ROOT}/original | /bin/bash\noriginal_rc=$?\n"
        + enqueue
        + 'exit "$original_rc"\n'
    )
    stock_wrapper = (
        f'#!/usr/bin/gawk BEGIN{{exit system("cat {ROOT}/handoff | /bin/bash")}}\n'
    )
    prepared = dict(
        payloads,
        runner=runner.encode(),
        boot=boot.encode(),
        handoff=stock_launch.encode(),
        **{
            "stock.wrapper": stock_wrapper.encode(),
            "privacy.launch": privacy_launch.encode(),
        },
    )
    emit = (
        "\n".join(
            f'printf %s {shlex.quote(base64.b64encode(data).decode())} | base64 -d > "$stage/{name}"\n'
            f'[[ $(hash "$stage/{name}") == {_digest(data)} ]] || fail "Staged {name} changed"'
            for name, data in prepared.items()
        )
        if operation in {"inspect", "set"}
        else ":"
    )
    immutable = "\n".join(f"{_digest(data)}  {name}" for name, data in prepared.items())
    return common + _fill(
        CONTROL,
        ACTION=operation,
        START=start or 0,
        END=end or 0,
        SPOTLIGHT={None: "keep", True: "on", False: "off"}[spotlight_art],
        ORIGINAL_HASH=ORIGINAL_HASH,
        LEGACY_HASH=_digest(legacy),
        WRAPPER_HASH=_digest(WRAPPER),
        EMIT=emit,
        IMMUTABLE=immutable,
    )


COMMON = r"""set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
export LC_ALL=C
umask 077
ROOT=@@ROOT@@
RUN=/run/samshush-quiet-hours-v1
UNIT=@@UNIT@@
DESC='Samshush Art quiet hours'
fail() { echo "ERROR: $*" >&2; exit 1; }
hash() { sha256sum "$1" | cut -d ' ' -f 1; }
private_dir() { [[ -d $1 && ! -L $1 && $(stat -c '%u:%a' "$1") == 0:700 ]]; }
regular() { [[ -f $1 && ! -L $1 && $(stat -c %u "$1") == 0 ]]; }
identity() {
    [[ $(id -u) == 0 ]] && grep -Fxq 'BUILD_ID=@@BUILD@@' /etc/os-release &&
        [[ $(hash /usr/lib/systemd/system/AT-HUB.service) == @@UNIT_HASH@@ ]] || fail 'Unexpected boot host or firmware'
}
read_config() {
    regular "$1" && [[ $(stat -c %a "$1") == 600 ]] || return 1
    local bytes='' pattern=$'^(0|[1-9][0-9]{0,3}) (0|[1-9][0-9]{0,3})\n$'
    # An editor may atomically replace this file. Validate one bounded snapshot,
    # requiring EOF before eleven bytes and rejecting embedded NUL.
    if IFS= read -r -d '' -n 11 bytes < "$1"; then return 1; fi
    [[ $bytes =~ $pattern ]] || return 1
    begin=${BASH_REMATCH[1]}; end=${BASH_REMATCH[2]}
    [[ $begin -lt 1440 && $end -lt 1440 && $begin != "$end" ]]
}
read_spotlight() {
    regular "$1" && [[ $(stat -c %a "$1") == 600 ]] || return 1
    local bytes=''
    if IFS= read -r -d '' -n 5 bytes < "$1"; then return 1; fi
    case "$bytes" in $'on\n') spotlight=on;; $'off\n') spotlight=off;; *) return 1;; esac
}
prop() { systemctl show "$UNIT" --property="$1" --value; }
# systemctl show may itself return 4 for a nonexistent transient unit.
load_state() {
    local state rc=0
    state=$(prop LoadState) || rc=$?
    [[ $state == not-found && ( $rc == 0 || $rc == 4 ) || $state == loaded && $rc == 0 ]] || return 1
    printf '%s' "$state"
}
unit_owned() {
    [[ $(prop Transient) == yes && $(prop Description) == "$DESC" ]] || return 1
    local cmd
    cmd=$(prop ExecStart) || return 1
    [[ $cmd == *"path=/bin/bash ; argv[]=/bin/bash -c "* && $cmd == *"samshush-art-quiet-hours"* &&
        $cmd == *"$ROOT/runner "* ]]
}
owner_pid() {
    local pid args
    pid=$(prop MainPID) && [[ $pid =~ ^[1-9][0-9]*$ ]] || return 1
    [[ $(readlink -f "/proc/$pid/exe") == $(readlink -f /bin/bash) ]] || return 1
    args=$(printf '%s\0' /bin/bash -s -- samshush-art-quiet-hours | sha256sum | cut -d ' ' -f 1)
    [[ $(hash "/proc/$pid/cmdline") == "$args" ]] || return 1
    printf '%s' "$pid"
}
"""

RUNNER = r"""
identity
private_dir "$ROOT" && private_dir "$RUN" && read_config "$ROOT/config" && read_spotlight "$ROOT/spotlight" || fail 'Invalid service configuration'
[[ $# == 1 && $1 == samshush-art-quiet-hours ]] || fail 'Quiet-hours arguments changed'
boot_id=$(cat /proc/sys/kernel/random/boot_id)
runner_pid=$$
# Keep only the current process's readiness and latest event. The managed runtime
# also writes GC diagnostics to stdout; those do not attest to worker readiness.
worker_args=(run)
[[ $spotlight == off ]] || worker_args+=(--spotlight-art)
set +e
/usr/bin/dotnet "$ROOT/ArtQuietHours.dll" "${worker_args[@]}" 2>&1 | while IFS= read -r line; do
    case "$line" in
        QUIET_HOURS_READY)
            printf '%s %s %s\n' "$boot_id" "$runner_pid" "$spotlight" > "$RUN/ready.tmp" && mv -f "$RUN/ready.tmp" "$RUN/ready" || exit 1;;
        QUIET_HOURS_*) printf '%.512s\n' "$line" > "$RUN/latest.tmp" && mv -f "$RUN/latest.tmp" "$RUN/latest" || exit 1;;
    esac
done
rc=$?
printf '%s\n' "$rc" > "$RUN/exit"
exit "$rc"
"""

BOOT = r"""
identity
private_dir "$ROOT" && read_config "$ROOT/config" && read_spotlight "$ROOT/spotlight" || fail 'Invalid quiet-hours installation'
phase=$(cat "$ROOT/phase")
[[ $phase == active || ${1:-} == control && ( $phase == prepared || $phase == updating ) ]] || fail 'Quiet-hours transaction is incomplete'
for path in "$ROOT/runner" "$ROOT/ArtQuietHours.dll" "$ROOT/ArtQuietHours.runtimeconfig.json"; do
    regular "$path" && [[ $(stat -c %a "$path") == 600 ]] || fail 'Unsafe worker files'
done
[[ $(hash "$ROOT/runner") == @@RUNNER_HASH@@ && $(hash "$ROOT/ArtQuietHours.dll") == @@DLL_HASH@@ &&
   $(hash "$ROOT/ArtQuietHours.runtimeconfig.json") == @@CFG_HASH@@ ]] || fail 'Worker hash drift'
state=$(load_state) || fail 'Unknown service state'
if [[ $state == loaded ]]; then
    unit_owned && [[ $(prop ActiveState) == active ]] && owner_pid >/dev/null || fail 'Quiet-hours unit already exists with unexpected state'
    exit 0
fi
if [[ ! -e $RUN && ! -L $RUN ]]; then mkdir -m 700 "$RUN"; fi
private_dir "$RUN" || fail 'Unsafe runtime directory'
rm -f -- "$RUN/ready" "$RUN/ready.tmp" "$RUN/latest" "$RUN/latest.tmp" "$RUN/exit"
systemd-run --no-block --collect --unit="$UNIT" --description="$DESC" --property=Type=simple \
    --property=Restart=on-failure --property=RestartSec=60 --property=StartLimitIntervalSec=900 \
    --property=StartLimitBurst=3 --property=TimeoutStopSec=8 --property=KillMode=control-group \
    /bin/bash -c 'exec /bin/bash -s -- samshush-art-quiet-hours < <(cat "$0")' "$ROOT/runner"
"""

CONTROL = r"""
ACTION=@@ACTION@@
START=@@START@@
END=@@END@@
requested_spotlight=@@SPOTLIGHT@@
HOOK=/opt/etc/parseinfo.sh
PRIV=/opt/usr/share/samshush-boot-v1
CTRL=/run/samshush-startup-control.lock
LOCK=/run/samshush-startup.lock
QLOCK=/run/samshush-quiet-hours-control.lock
stage=''
scratch=''
rollback=''
locked=0
control_locked=0
quiet_locked=0
old_active=0
identity
# All lifecycle writes serialize with existing AT-HUB startup control and worker operations.
cleanup() {
    local rc=$?
    trap - EXIT INT TERM
    set +e
    if [[ $rc != 0 && -n $rollback ]]; then
        (set -e; recover_failure "$rollback")
        if [[ $? == 0 ]]; then echo 'QUIET_HOURS_ROLLBACK=verified'
        else echo 'Quiet-hours rollback incomplete; journal retained, inspect before retrying' >&2; fi
    fi
    [[ -z $scratch ]] || rm -rf -- "$scratch" || rc=1
    [[ -z $stage ]] || rm -rf -- "$stage" || rc=1
    if [[ $locked == 1 ]]; then rmdir "$LOCK" || rc=1; fi
    if [[ $control_locked == 1 ]]; then rmdir "$CTRL" || rc=1; fi
    if [[ $quiet_locked == 1 ]]; then rmdir "$QLOCK" || rc=1; fi
    exit "$rc"
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
mkdir -m 700 "$QLOCK" || fail 'Quiet-hours control already running or interrupted'
quiet_locked=1
if [[ $ACTION == set || $ACTION == remove ]]; then
    mkdir -m 700 "$CTRL" || fail 'Startup control already running or interrupted'
    control_locked=1
    if [[ -e $PRIV || -L $PRIV ]]; then
        # The boot worker claims LOCK after its asynchronous handoff. Taking it
        # during that gap would abort privacy reapplication for the entire boot.
        result=/run/samshush-boot.result
        regular "$result" && read -r boot rc extra < "$result" &&
          [[ $boot == "$(cat /proc/sys/kernel/random/boot_id)" && -z $extra && $rc =~ ^(0|[1-9][0-9]{0,2})$ && $rc -le 255 ]] ||
            fail 'Privacy startup has not completed on this boot'
        for service in AT-HUB.service samshush-boot.service; do
            rc=0
            info=$(systemctl show "$service" --all -p LoadState -p ActiveState -p Job) || rc=$?
            [[ $rc == 0 || $rc == 4 ]] && grep -Eq '^LoadState=(loaded|not-found)$' <<< "$info" &&
              grep -Eq '^ActiveState=(inactive|failed)$' <<< "$info" && grep -Fxq 'Job=' <<< "$info" ||
                fail 'Privacy startup or AT-HUB still has an active job'
        done
    fi
    mkdir -m 700 "$LOCK" || fail 'Startup operation already running or interrupted'
    locked=1
fi
phase_set() { printf '%s\n' "$1" > "$ROOT/phase.tmp" && mv -f "$ROOT/phase.tmp" "$ROOT/phase" && sync; }
config_set() { printf '%s %s\n' "$1" "$2" > "$ROOT/config.tmp" && mv -f "$ROOT/config.tmp" "$ROOT/config" && sync; }
spotlight_set() { printf '%s\n' "$1" > "$ROOT/spotlight.tmp" && mv -f "$ROOT/spotlight.tmp" "$ROOT/spotlight" && sync; }
window_report() {
    echo "QUIET_HOURS_SPOTLIGHT_ART=$spotlight"
    echo "QUIET_HOURS_WINDOW=$1 $2"
    printf 'Art Mode quiet hours: %02d:%02d–%02d:%02d (TV local time)\n' "$(($1 / 60))" "$(($1 % 60))" "$(($2 / 60))" "$(($2 % 60))"
}
write_copy() {
    local src=$1 dest=$2 temp
    temp=$(mktemp -d "$(dirname "$dest")/.samshush-quiet-copy.XXXXXXXX") || return 1
    scratch=$temp
    cp --preserve=all "$src" "$temp/file" && sync && mv -f "$temp/file" "$dest" && sync &&
        matches "$src" "$dest" || fail "Copy readback failed: $dest"
    rmdir "$temp" && scratch=''
}
legacy_tree() {
    private_dir "$PRIV" && regular "$PRIV/installed" && regular "$PRIV/enabled" && [[ -s $PRIV/enabled ]] &&
    regular "$PRIV/original" && [[ $(hash "$PRIV/original") == @@ORIGINAL_HASH@@ ]] &&
    regular "$PRIV/wrapper" && [[ $(hash "$PRIV/wrapper") == @@WRAPPER_HASH@@ ]] &&
    regular "$HOOK" && [[ $(hash "$HOOK") == @@WRAPPER_HASH@@ ]] || fail 'Privacy startup hook drift'
}
preflight() {
    regular "$HOOK" || fail 'Unsafe boot hook'
    if [[ -e $PRIV || -L $PRIV ]]; then
        mode=privacy
        legacy_tree
        for file in launch SHA256SUMS; do regular "$PRIV/$file" && [[ $(stat -c %a "$PRIV/$file") == 600 ]] || fail 'Unsafe privacy handoff'; done
        [[ $(hash "$PRIV/launch") == @@LEGACY_HASH@@ ]] || fail 'Unrecognized privacy handoff'
        (cd "$PRIV" && sha256sum -c SHA256SUMS >/dev/null) || fail 'Privacy manifest drift'
    else
        mode=stock
        [[ $(hash "$HOOK") == @@ORIGINAL_HASH@@ && $(stat -c '%u:%g:%a' "$HOOK") == 0:0:777 ]] || fail 'Unrecognized stock hook'
    fi
}
immutable() {
    private_dir "$ROOT" || fail 'Unsafe quiet-hours directory'
    local path name
    shopt -s nullglob dotglob
    for path in "$ROOT"/*; do
        name=${path##*/}
        case "$name" in
            ArtQuietHours.dll|ArtQuietHours.runtimeconfig.json|runner|boot|handoff|stock.wrapper|privacy.launch|original|original.manifest|bridge.manifest|managed.manifest|preimages.sha256|config|config.tmp|spotlight|spotlight.tmp|mode|phase|phase.tmp|previous|previous.tmp) ;;
            *) fail 'Unexpected quiet-hours recovery entry';;
        esac
        regular "$path" || [[ $name == config.tmp && ! -e $path && ! -L $path ]] || fail 'Unsafe quiet-hours recovery entry'
    done
    shopt -u nullglob dotglob
    (cd "$ROOT" && sha256sum -c - >/dev/null <<'QH_FILES'
@@IMMUTABLE@@
QH_FILES
    ) || fail 'Quiet-hours code drift'
    regular "$ROOT/mode" && regular "$ROOT/phase" && read_config "$ROOT/config" && read_spotlight "$ROOT/spotlight" || fail 'Invalid quiet-hours receipt'
    mode=$(cat "$ROOT/mode")
    case "$mode" in
        stock) [[ ! -e $PRIV && ! -L $PRIV ]] && regular "$ROOT/original" && [[ $(hash "$ROOT/original") == @@ORIGINAL_HASH@@ ]] || fail 'Stock recovery preimage drift';;
        privacy) legacy_tree; regular "$ROOT/original" && [[ $(hash "$ROOT/original") == @@LEGACY_HASH@@ ]] || fail 'Original privacy launch drift'
            for file in original.manifest bridge.manifest managed.manifest; do regular "$ROOT/$file" || fail 'Missing manifest backup'; done;;
        *) fail 'Unknown quiet-hours boot mode';;
    esac
    (cd "$ROOT" && sha256sum -c preimages.sha256 >/dev/null) || fail 'Quiet-hours recovery receipt drift'
}
matches() {
    local sum_a sum_b stat_a stat_b label_a label_b
    regular "$1" && regular "$2" &&
    sum_a=$(hash "$1") && sum_b=$(hash "$2") &&
    stat_a=$(stat -c '%u:%g:%a' "$1") && stat_b=$(stat -c '%u:%g:%a' "$2") &&
    label_a=$(chsmack "$1" | cut -s -d ' ' -f 2-) && label_b=$(chsmack "$2" | cut -s -d ' ' -f 2-) &&
    [[ $sum_a == "$sum_b" && $stat_a == "$stat_b" && $label_a == "$label_b" ]]
}
known_file() {
    matches "$1" "$2" || matches "$1" "$3" || fail "Unrecognized handoff: $1"
}
verify_point() {
    local expected=$1
    immutable
    if [[ $mode == stock ]]; then
        known_file "$HOOK" "$ROOT/original" "$ROOT/stock.wrapper"
        [[ $expected == either ]] && return 0
        local version=original; [[ $expected != managed ]] || version=stock.wrapper
        matches "$HOOK" "$ROOT/$version" || fail 'Unexpected stock handoff state'
    else
        known_file "$PRIV/launch" "$ROOT/original" "$ROOT/privacy.launch"
        matches "$PRIV/SHA256SUMS" "$ROOT/bridge.manifest" || known_file "$PRIV/SHA256SUMS" "$ROOT/original.manifest" "$ROOT/managed.manifest"
        (cd "$PRIV" && awk '$2 != "launch"' "$ROOT/original.manifest" | sha256sum -c - >/dev/null) || fail 'Unrelated privacy file drift'
        [[ $expected == either ]] && return 0
        local launch=original manifest=original.manifest
        if [[ $expected == managed ]]; then launch=privacy.launch; manifest=managed.manifest; fi
        matches "$PRIV/launch" "$ROOT/$launch" && matches "$PRIV/SHA256SUMS" "$ROOT/$manifest" || fail 'Unexpected privacy handoff state'
    fi
}
switch_to() {
    local target=$1
    verify_point either
    if [[ $mode == stock ]]; then
        if [[ $target == managed ]]; then write_copy "$ROOT/stock.wrapper" "$HOOK"; else write_copy "$ROOT/original" "$HOOK"; fi
    else
        local launch=original manifest=original.manifest
        if [[ $target == managed ]]; then launch=privacy.launch; manifest=managed.manifest; fi
        # The boot worker can run after any power loss. Its manifest must pass
        # at each rename, including the switch between two valid handoff files.
        write_copy "$ROOT/bridge.manifest" "$PRIV/SHA256SUMS"
        (cd "$PRIV" && sha256sum -c SHA256SUMS >/dev/null) || fail 'Transition manifest verification failed'
        write_copy "$ROOT/$launch" "$PRIV/launch"
        (cd "$PRIV" && sha256sum -c SHA256SUMS >/dev/null) || fail 'Intermediate handoff verification failed'
        write_copy "$ROOT/$manifest" "$PRIV/SHA256SUMS"
        (cd "$PRIV" && sha256sum -c SHA256SUMS >/dev/null) || fail 'Final privacy manifest verification failed'
    fi
    sync
    verify_point "$target"
}
stop_worker() {
    local state
    state=$(load_state) || fail 'Unknown quiet-hours unit state'
    [[ $state != not-found ]] || return 0
    unit_owned || fail 'Refusing to stop an unrelated service'
    systemctl stop "$UNIT" || fail 'Could not stop quiet-hours service'
    for ((i=0;i<8;i++)); do
        state=$(load_state) || fail 'Unknown stopped service state'
        [[ $state != not-found ]] || return 0
        unit_owned || fail 'Unexpected service after stop'
        sleep 1
    done
    fail 'Transient quiet-hours unit did not disappear'
}
ready() {
    local pid stamp
    read_config "$ROOT/config" && read_spotlight "$ROOT/spotlight" && [[ $(load_state) == loaded && $(prop ActiveState) == active ]] && unit_owned &&
        pid=$(owner_pid) && private_dir "$RUN" && regular "$RUN/ready" || return 1
    stamp="$(cat /proc/sys/kernel/random/boot_id) $pid"
    [[ $(cat "$RUN/ready") == "$stamp $spotlight" ]] || return 1
    ready_receipt="QUIET_HOURS_READY=$stamp $begin $end"
    echo "$ready_receipt"
}
start_worker() {
    cat "$ROOT/boot" | /bin/bash -s -- control || fail 'Could not queue quiet-hours service'
    for ((i=0;i<15;i++)); do if ready; then return 0; fi; sleep 1; done
    if regular "$RUN/latest"; then cat "$RUN/latest"; fi
    fail 'Quiet-hours worker did not confirm readiness'
}
recover_failure() {
    local kind=$1
    verify_point either
    if [[ $kind == install ]]; then
        stop_worker
        switch_to original
        phase_set rolled-back
    else
        if [[ $kind == update ]]; then
            stop_worker
            read_config "$ROOT/previous" || fail 'Prior quiet-hours config missing'
            config_set "$begin" "$end"
            spotlight_set "$old_spotlight"
        fi
        if [[ $old_active == 1 ]]; then start_worker; fi
        phase_set active
        rm -f -- "$ROOT/previous"
    fi
}
if [[ -e $ROOT || -L $ROOT ]]; then
    verify_point either
    phase=$(cat "$ROOT/phase")
    case "$phase" in active|prepared|updating|rolled-back|removing) ;; *) fail 'Unknown quiet-hours transaction phase';; esac
    echo "QUIET_HOURS_BOOT_MODE=$mode"
    echo "QUIET_HOURS_PHASE=$phase"
else
    phase=absent
    preflight
fi
if [[ $ACTION == status ]]; then
    if [[ $phase == absent ]]; then [[ $(load_state) == not-found ]] || fail 'Orphaned service'; echo QUIET_HOURS_STATE=absent; exit 0; fi
    [[ $phase == active ]] || fail 'Quiet-hours transaction is incomplete; recover with remove'
    verify_point managed && ready || fail 'Configured quiet-hours service is not ready'
    window_report "$begin" "$end"
    if regular "$RUN/latest"; then cat "$RUN/latest"; fi
    echo QUIET_HOURS_STATE=active
    exit 0
fi
if [[ $ACTION == remove ]]; then
    if [[ $phase == absent ]]; then [[ $(load_state) == not-found ]] || fail 'Orphaned service'; echo QUIET_HOURS_STATE=absent; exit 0; fi
    phase_set removing
    stop_worker
    switch_to original
    # Keep the receipt reachable until both restore checks pass. Removal after a
    # crash can safely retry from any recognized intermediate handoff state.
    stage=$(mktemp -d /opt/usr/share/.samshush-quiet-removed.XXXXXXXX)
    mv "$ROOT" "$stage/receipt"
    if [[ -e $RUN || -L $RUN ]]; then private_dir "$RUN" || fail 'Unsafe runtime state'; rm -rf -- "$RUN"; fi
    sync
    echo QUIET_HOURS_STATE=removed
    exit 0
fi
if [[ $phase == absent || $ACTION == inspect ]]; then
    if [[ $ACTION == inspect ]]; then stage=$(mktemp -d /run/samshush-quiet-check.XXXXXXXX)
    else stage=$(mktemp -d /opt/usr/share/.samshush-quiet-stage.XXXXXXXX); fi
    chsmack -a _ "$stage"
@@EMIT@@
    for f in "$stage"/*; do chmod 600 "$f"; chsmack -a _ "$f"; done
    check_dll=$stage/ArtQuietHours.dll
else
    check_dll=$ROOT/ArtQuietHours.dll
fi
check_output=$(/usr/bin/dotnet "$check_dll" check "$START" "$END") || { printf '%s\n' "$check_output"; fail 'Native classification failed'; }
printf '%s\n' "$check_output"
[[ $(printf '%s\n' "$check_output" | grep -c '^QUIET_HOURS_CHECK={') == 1 ]] || fail 'Missing or ambiguous native classification evidence'
if [[ $ACTION == inspect ]]; then echo "QUIET_HOURS_BOOT_MODE=$mode"; echo "QUIET_HOURS_PHASE=$phase"; exit 0; fi
if [[ $phase == absent ]]; then
    [[ $(load_state) == not-found ]] || fail 'A service with the quiet-hours name already exists'
    printf '%s\n' "$mode" > "$stage/mode"
    printf '%s %s\n' "$START" "$END" > "$stage/config"
    [[ $requested_spotlight != keep ]] || requested_spotlight=off
    printf '%s\n' "$requested_spotlight" > "$stage/spotlight"
    printf 'prepared\n' > "$stage/phase"
    if [[ $mode == stock ]]; then
        cp --preserve=all "$HOOK" "$stage/original"
        chmod 755 "$stage/stock.wrapper"
        (cd "$stage" && sha256sum original mode > preimages.sha256)
    else
        cp --preserve=all "$PRIV/launch" "$stage/original"
        cp --preserve=all "$PRIV/SHA256SUMS" "$stage/original.manifest"
        cp --preserve=all "$PRIV/SHA256SUMS" "$stage/managed.manifest"
        cp --preserve=all "$PRIV/SHA256SUMS" "$stage/bridge.manifest"
        awk -v h="$(hash "$stage/privacy.launch")" '$2 == "launch" {n++; print h "  launch"; next} {print} END {if (n != 1) exit 1}' \
            "$stage/original.manifest" > "$stage/managed.manifest" || fail 'Invalid privacy launch entry'
        awk -v root="$ROOT" -v old="$(hash "$stage/original")" -v new="$(hash "$stage/privacy.launch")" \
            '$2 == "launch" {n++; print old "  " root "/original"; print new "  " root "/privacy.launch"; next}
             {print} END {if (n != 1) exit 1}' "$stage/original.manifest" > "$stage/bridge.manifest" || fail 'Invalid transition manifest'
        (cd "$stage" && sha256sum original original.manifest bridge.manifest managed.manifest mode > preimages.sha256)
    fi
    sync
    mv "$stage" "$ROOT"
    stage=''
    rollback=install
    switch_to managed
else
    [[ $phase == active ]] || fail 'Incomplete installation; remove it before setting a new policy'
    verify_point managed
    old_spotlight=$spotlight
    [[ $requested_spotlight != keep ]] || requested_spotlight=$old_spotlight
    if ready >/dev/null; then old_active=1; fi
    if [[ $old_active == 1 && $begin == "$START" && $end == "$END" && $old_spotlight == "$requested_spotlight" ]]; then
        window_report "$begin" "$end"; echo "$ready_receipt"; echo QUIET_HOURS_STATE=active; exit 0
    fi
    phase_set updating
    # A Home edit may finish while the worker is stopping. Snapshot only after
    # systemd has collected the unit, so rollback retains the last accepted edit.
    rollback=resume
    stop_worker
    cp --preserve=all "$ROOT/config" "$ROOT/previous.tmp" && mv -f "$ROOT/previous.tmp" "$ROOT/previous" || fail 'Could not save prior quiet-hours config'
    rollback=update
    config_set "$START" "$END"
    spotlight_set "$requested_spotlight"
fi
start_worker
phase_set active
rollback=''
rm -f -- "$ROOT/previous"
window_report "$START" "$END"
echo "QUIET_HOURS_BOOT_MODE=$mode"
echo QUIET_HOURS_STATE=active
"""
