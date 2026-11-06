"""Exact-build native Home row transaction, rendered for the existing transport."""

import base64
import copy
import hashlib
import json
import shlex

BUILD = "T-RSMFAKUC-0090-REL-202607141953"
JOURNAL = "/opt/usr/home/owner/share/frame-home-layout-v1"
KEY = "db/eden/launcher/list"
LIMIT = 65536
COMPONENT = "s/source.layer.hdmi@HDMI1/0"
HASHES = {
    "/opt/usr/apps/com.samsung.tv.home.media/bin/HomeMedia.dll": (
        "9cae06a13d771f98aaaf4ce476b5d6ada12e99f9e4d035512bf49c600008699b"
    ),
    "/usr/share/dotnet.tizen/tvappapi/Tizen.TV.Service.CoBA.Core.dll": (
        "686716f173c501fae9c87fa9318c7ae7a0ebbcfdeabcdeaed00a1d3b5ad20561"
    ),
    "/usr/share/dotnet.tizen/tvappapi/Tizen.TV.Service.CoBA.Common.dll": (
        "9cce4e0c33fe7e6ee7c79031bb95b53c3e99cba6be67ca15088ef44ac9896705"
    ),
    "/usr/bin/app_launcher": (
        "5d6f641f564148eece10ea1e4c6da12c20f723db0892263afc138ecb144042c3"
    ),
}


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _layout(value):
    if not isinstance(value, str) or "\0" in value:
        raise ValueError("Layout must be a native string without NUL bytes")
    if not 0 < len(value.encode("utf-8")) <= LIMIT:
        raise ValueError("Layout must contain at most 64 KiB of UTF-8")
    try:
        parsed = json.loads(value, object_pairs_hook=_object)
        layer = parsed["app_layer"]
        rows = layer["list"]
        valid = (
            parsed["version"] == "1.0.0"
            and isinstance(rows, list)
            and type(layer["count"]) is int
            and type(layer["max_tile"]) is int
            and 0 <= layer["count"] <= len(rows) <= layer["max_tile"] <= 100
            and all(isinstance(row, dict) for row in rows)
        )
        ids = [row["component_id"] for row in rows]
        valid = valid and all(isinstance(item, str) and item for item in ids)
        valid = valid and len(ids) == len(set(ids))
    except (KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("Invalid native Home layout") from error
    if not valid:
        raise ValueError("Invalid native Home layout")
    return parsed


def render(operation, *, before=None, after=None):
    """Render a fixed native transaction; only install accepts layout strings.

    The host must freshly verify the Apple TV source before install. The script
    independently checks firmware, native owner, reset state and exact preimage.
    """
    if operation not in ("inspect", "install", "status", "remove"):
        raise ValueError("Unknown Home layout operation")
    if operation == "install":
        original, candidate = _layout(before), _layout(after)
        row = next(
            (
                row
                for row in original["app_layer"]["list"]
                if row["component_id"] == COMPONENT
            ),
            None,
        )
        if row is None or any(
            row.get(key) != value
            for key, value in {
                "id": "HDMI1/0",
                "p": "f",
                "t": "src",
                "src": "13",
            }.items()
        ):
            raise ValueError("Original layout lacks the verified Apple TV source row")
        expected = copy.deepcopy(original)
        expected["app_layer"]["list"] = [row]
        expected["app_layer"]["count"] = 1
        if candidate != expected:
            raise ValueError(
                "Candidate must retain only the complete Apple TV row and metadata"
            )
    elif before is not None or after is not None:
        raise ValueError("Only install accepts layout strings")
    policy = hashlib.sha256(
        json.dumps(
            {"build": BUILD, "hashes": HASHES, "key": KEY}, sort_keys=True
        ).encode()
    ).hexdigest()
    replacements = {
        "@@MODE@@": shlex.quote(operation),
        "@@STATE@@": shlex.quote(JOURNAL),
        "@@BUILD@@": shlex.quote(BUILD),
        "@@KEY@@": shlex.quote(KEY),
        "@@POLICY@@": shlex.quote(policy),
        "@@HASHES@@": "\n".join(f"{digest}  {path}" for path, digest in HASHES.items()),
        "@@BEFORE@@": shlex.quote(base64.b64encode((before or "").encode()).decode()),
        "@@AFTER@@": shlex.quote(base64.b64encode((after or "").encode()).decode()),
    }
    script = SCRIPT
    for token, value in replacements.items():
        script = script.replace(token, value)
    return script


SCRIPT = r"""#!/bin/bash
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
export LC_ALL=C
umask 077
MODE=@@MODE@@
STATE=@@STATE@@
BUILD=@@BUILD@@
KEY=@@KEY@@
POLICY=@@POLICY@@
BEFORE64=@@BEFORE@@
AFTER64=@@AFTER@@
LOCK=/run/frame-runtime-v1.lock
APP=com.samsung.tv.home.media
DLL=/opt/usr/apps/com.samsung.tv.home.media/bin/HomeMedia.dll
SIDEBAR=/opt/usr/apps/com.samsung.tv.home.sidebar/bin/HomeSidebar.dll
DATA=/home/owner/apps_rw/com.samsung.tv.home.media
phase=absent
locked=0
recover=0
interrupted=0
VALUE=''
PROCESS=''
SIDEBAR_PROCESS=''
retained_sidebar=''
APP_STATE=''
before=''
after=''
old_process=''
fail() { echo "ERROR: $*" >&2; exit 1; }

# A sentinel preserves trailing newlines in the native string and saved files.
read_native() {
 local key=$1 kind=$2 reply prefix suffix
 reply=$(vconftool get "$key" && printf '\034') || return 1
 [[ $reply == *$'\034' ]] || return 1
 reply=${reply%$'\034'}
 prefix="$key, value = "
 suffix=" ($kind)"$'\n'
 [[ $reply == "$prefix"*"$suffix" ]] || return 1
 VALUE=${reply#"$prefix"}
 VALUE=${VALUE%"$suffix"}
 [[ ${#VALUE} -le 65536 ]] || return 1
}
read_layout() { read_native "$KEY" String && [[ -n $VALUE ]]; }
read_int() {
 read_native "$1" Int32 || return 1
 [[ $VALUE =~ ^(0|-?[1-9][0-9]*)$ && ${#VALUE} -le 11 ]]
}
read_file() {
 local value
 value=$(cat "$1" && printf '\034') || return 1
 VALUE=${value%$'\034'}
 [[ ${#VALUE} -gt 0 && ${#VALUE} -le 65536 ]]
}
digest_file() {
 local digest
 digest=$(sha256sum "$1") || return 1
 printf '%s' "${digest%% *}"
}
write_phase() {
 printf '%s\n' "$1" > "$STATE/phase.tmp" || return 1
 mv "$STATE/phase.tmp" "$STATE/phase" || return 1
 phase=$1
}
guards() {
 local factory service flag line header
 read_int memory/eden/smarthub_reset_media && [[ $VALUE != 0 ]] || return 1
 read_native db/volt/make_your_smarthub String || return 1
 [[ -n $VALUE && $VALUE != reset && $VALUE != done ]] || return 1
 read_int db/factory/factory_reset_count || return 1
 factory=$VALUE
 read_int db/factory/service_reset_count || return 1
 service=$VALUE
 [[ ! -e $DATA/data/.SHResetflag && ! -L $DATA/data/.SHResetflag ]] || return 1
 for flag in .resetflag db_ready sqlite.db; do
  [[ -f $DATA/shared/trusted/$flag && ! -L $DATA/shared/trusted/$flag ]] || return 1
  [[ $(stat -c '%u:%g:%a' "$DATA/shared/trusted/$flag") == 5001:100:644 ]] || return 1
 done
 [[ $(stat -c %s "$DATA/shared/trusted/.resetflag") -le 128 ]] || return 1
 local counts=()
 while IFS= read -r line || [[ -n $line ]]; do counts+=("$line"); done < "$DATA/shared/trusted/.resetflag"
 [[ ${#counts[@]} == 2 && ${counts[0]} == "$factory" && ${counts[1]} == "$service" ]] || return 1
 header=$(head -c 16 "$DATA/shared/trusted/sqlite.db" | od -An -tx1 | tr -d ' \n') || return 1
 [[ $header == 53514c69746520666f726d6174203300 ]]
}
app_state() {
 local reply
 reply=$(app_launcher -r "$APP" -u 5001 2>&1) || true
 case "$reply" in
  'result: running') APP_STATE=running;;
  'result: not running') APP_STATE=absent;;
  *) echo "Unrecognized native app state: $reply" >&2; return 1;;
 esac
}
scan_processes() {
 local proc statline tail exe argument selected identity again uid
 local fields=()
 PROCESS=''
 SIDEBAR_PROCESS=''
 for proc in /proc/[0-9]*; do
  [[ -d $proc ]] || continue
  statline=$(cat "$proc/stat" 2>/dev/null) || { [[ ! -d $proc ]] && continue; return 1; }
  tail=${statline##*) }
  read -r -a fields <<< "$tail"
  [[ ${#fields[@]} -ge 20 && ${fields[6]} =~ ^[0-9]+$ && ${fields[19]} =~ ^[0-9]+$ ]] || return 1
  if ! exe=$(readlink "$proc/exe" 2>/dev/null); then
   [[ ! -d $proc ]] && continue
   # Only kernel threads and fully exited single-thread tasks lack a userspace exe.
   (( (fields[6] & 2097152) != 0 )) && continue
   [[ ${fields[0]} == Z && ${fields[17]} == 1 ]] && continue
   echo "Unreadable live executable: $proc" >&2; return 1
  fi
  [[ -r $proc/cmdline ]] || { [[ ! -d $proc ]] && continue; return 1; }
  selected=''
  while IFS= read -r -d '' argument; do
   [[ $argument != "$DLL" ]] || selected=home
   [[ $argument != "$SIDEBAR" ]] || selected=sidebar
  done < "$proc/cmdline"
  [[ -n $selected ]] || continue
  [[ $exe == /usr/bin/dotnet-launcher-inhouse ]] || return 1
  uid=$(sed -n 's/^Uid:[[:space:]]*//p' "$proc/status") || return 1
  local ids=()
  read -r -a ids <<< "$uid"
  [[ ${#ids[@]} == 4 && ${ids[0]} == 5001 && ${ids[1]} == 5001 ]] || return 1
  identity="${proc##*/}:${fields[19]}"
  again=$(cat "$proc/stat" 2>/dev/null) || return 1
  tail=${again##*) }
  read -r -a fields <<< "$tail"
  [[ ${#fields[@]} -ge 20 && $identity == "${proc##*/}:${fields[19]}" ]] || return 1
  if [[ $selected == home ]]; then
   [[ -z $PROCESS ]] || return 1
   PROCESS=$identity
  else
   [[ -z $SIDEBAR_PROCESS ]] || return 1
   SIDEBAR_PROCESS=$identity
  fi
 done
}
owner_state() {
 app_state && scan_processes || return 1
 case "$APP_STATE:$PROCESS" in
  absent:) return 0;;
  running:?*) return 0;;
  *) echo 'Native app state and exact HomeMedia process disagree' >&2; return 1;;
 esac
}
ready() { read_int memory/eden/home_media_done && [[ $VALUE == 1 ]]; }
sidebar_unchanged() { [[ -z $retained_sidebar || $SIDEBAR_PROCESS == "$retained_sidebar" ]]; }
stable() {
 local expected=$1 identity=$2 i
 for i in 0 1 2 3 4 5; do
  owner_state && sidebar_unchanged && [[ $APP_STATE == running && $PROCESS == "$identity" ]] && ready || return 1
  read_layout && [[ $VALUE == "$expected" ]] || return 1
  [[ $i == 5 ]] || sleep 1
 done
}
stop_owner() {
 local i
 owner_state || return 1
 old_process=$PROCESS
 [[ -n $old_process ]] || return 0
 write_phase stopping || return 1
 printf 'requested %s %s\n' "$(cat /proc/sys/kernel/random/boot_id)" "$old_process" > "$STATE/stopped-owner" || return 1
 app_launcher -t "$APP" -u 5001 || return 1
 for i in {1..20}; do
  app_state || return 1
  if [[ $APP_STATE == absent ]]; then
   scan_processes || return 1
   if [[ -z $PROCESS ]]; then
    printf 'stopped %s %s\n' "$(cat /proc/sys/kernel/random/boot_id)" "$old_process" > "$STATE/stopped-owner" || return 1
    write_phase stopped
    return $?
   fi
  fi
  sleep 1
 done
 echo 'HomeMedia did not terminate normally; no forced kill attempted' >&2
 return 1
}
start_owner() {
 local i expected=$1 previous=$2
 guards && owner_state && [[ $APP_STATE == absent && -z $PROCESS ]] || return 1
 read_layout && [[ $VALUE == "$expected" ]] || return 1
 write_phase starting || return 1
 app_launcher -s "$APP" -u 5001 || return 1
 for i in {1..20}; do
  app_state || return 1
  if [[ $APP_STATE == running ]]; then
   scan_processes || return 1
   if [[ -n $PROCESS && $PROCESS != "$previous" ]] && ready; then
    printf '%s\n' "$PROCESS" > "$STATE/started-process" || return 1
    mark_resumed || return 1
    stable "$expected" "$PROCESS" && return 0
    return 1
   fi
  fi
  sleep 1
 done
 echo 'HomeMedia did not become ready' >&2
 return 1
}
mark_resumed() {
 if [[ -e $STATE/stopped-owner ]]; then
  printf 'resumed %s %s\n' "$(cat /proc/sys/kernel/random/boot_id)" "$PROCESS" > "$STATE/stopped-owner" || return 1
 fi
}
change_layout() {
 local expected=$1 desired=$2
 guards && read_layout && [[ $VALUE == "$expected" ]] || return 1
 stop_owner || return 1
 # OnTerminate flushes queued changes. Never write over that newly changed value.
 guards && read_layout && [[ $VALUE == "$expected" ]] || return 1
 owner_state && sidebar_unchanged && [[ $APP_STATE == absent && -z $PROCESS ]] || return 1
 write_phase writing || return 1
 vconftool set -t string "$KEY" "$desired" -f || return 1
 read_layout && [[ $VALUE == "$desired" ]] || return 1
 write_phase written || return 1
 start_owner "$desired" "$old_process"
}
restart_saved_stop() {
 local expected=$1 state boot identity extra
 [[ -f $STATE/stopped-owner && ! -L $STATE/stopped-owner ]] || return 1
 read -r state boot identity extra < "$STATE/stopped-owner" || return 1
 [[ $state == stopped || $state == requested ]] || return 1
 [[ $boot == "$(cat /proc/sys/kernel/random/boot_id)" && $identity =~ ^[0-9]+:[0-9]+$ && -z $extra ]] || return 1
 owner_state || return 1
 if [[ $APP_STATE == running ]]; then mark_resumed; return 1; fi
 [[ $APP_STATE == absent && -z $PROCESS ]] || return 1
 # OnTerminate may flush a legitimate native edit. Recover the owner without
 # overwriting that edit; this remains an incomplete layout transaction.
 start_owner "$expected" "$identity"
}
restore_original() {
 local current
 guards && read_layout || return 1
 current=$VALUE
 [[ $current == "$before" || $current == "$after" ]] || {
  echo 'Layout changed outside the saved before/after values; refusing overwrite' >&2
  restart_saved_stop "$current" || true
  return 1
 }
 owner_state || return 1
 if [[ $current == "$before" && $APP_STATE == running ]]; then
  mark_resumed || return 1
  stable "$before" "$PROCESS"
 elif [[ $current == "$before" ]]; then
  start_owner "$before" ''
 else
  if change_layout "$after" "$before"; then return 0; fi
  # Removal can itself encounter a native flush during stop. Restore liveness
  # in this invocation while preserving the changed native value and receipt.
  if read_layout; then restart_saved_stop "$VALUE" || true; fi
  return 1
 fi
}
validate_journal() {
 local path name size digest
 [[ -d $STATE && ! -L $STATE && $(stat -c '%u:%a' "$STATE") == 0:700 ]] || return 1
 shopt -s nullglob dotglob
 for path in "$STATE"/*; do
  name=${path##*/}
  case "$name" in before|after|before.sha256|after.sha256|policy|phase|phase.tmp|boot-id|original-process|started-process|removed-receipt|stopped-owner|retained-sidebar) ;;
   *) return 1;;
  esac
  [[ -f $path && ! -L $path && $(stat -c '%u:%a' "$path") == 0:600 ]] || return 1
  size=$(stat -c %s "$path") || return 1
  [[ $size -le 65536 ]] || return 1
 done
 shopt -u nullglob dotglob
 for name in before after before.sha256 after.sha256 policy phase boot-id original-process; do
  [[ -s $STATE/$name && ! -L $STATE/$name ]] || return 1
 done
 [[ $(cat "$STATE/policy") == "$POLICY" ]] || return 1
 for name in before after; do
  digest=$(cat "$STATE/$name.sha256") || return 1
  [[ $digest =~ ^[0-9a-f]{64}$ && $(digest_file "$STATE/$name") == "$digest" ]] || return 1
 done
 read_file "$STATE/before" || return 1
 before=$VALUE
 read_file "$STATE/after" || return 1
 after=$VALUE
 phase=$(cat "$STATE/phase") || return 1
 case "$phase" in prepared|stopping|stopped|writing|written|starting|installed|rolling-back|rolled-back|removing|removed|recovery-required) ;;
  *) return 1;;
 esac
 [[ $(cat "$STATE/original-process") =~ ^[0-9]+:[0-9]+$ ]]
}
cleanup() {
 local result=$?
 trap - EXIT INT TERM
 set +e
 if [[ $result != 0 && $recover == 1 ]]; then
  if [[ $interrupted == 0 ]]; then
   if write_phase rolling-back && restore_original && write_phase rolled-back; then
    echo 'HOME_LAYOUT_ROLLED_BACK'
   else
    write_phase recovery-required
    echo 'Home layout recovery incomplete; saved journal retained for remove/inspection' >&2
   fi
  else
   echo 'Home layout interrupted; journal retained for remove/inspection' >&2
  fi
 fi
 if [[ $locked == 1 ]]; then
  rmdir "$LOCK" || { echo 'Could not release operation lock' >&2; result=1; }
 fi
 exit "$result"
}
trap cleanup EXIT
trap 'interrupted=1; exit 130' INT
trap 'interrupted=1; exit 143' TERM

[[ $(id -u) == 0 && $(stat -c %u /opt/usr/home/owner) == 5001 ]] || fail 'Unexpected UID or owner'
ours=$(readlink /proc/self/ns/mnt) && init=$(readlink /proc/1/ns/mnt) || fail 'Cannot inspect mount namespace'
[[ -n $ours && $ours == "$init" ]] || fail 'PID 1 mount namespace required'
grep -Fxq "BUILD_ID=$BUILD" /etc/os-release || fail 'Unexpected firmware'
sha256sum -c <<'HOME_LAYOUT_BINARIES' || fail 'Native Home implementation changed'
@@HASHES@@
HOME_LAYOUT_BINARIES
[[ ! -L $STATE && ! -L $LOCK ]] || fail 'Unsafe journal or lock'
mkdir -m 700 "$LOCK" || fail 'Another operation may be running'
locked=1
if [[ -e $STATE ]]; then validate_journal || fail 'Invalid Home layout journal; retained for inspection'; fi
read_layout || fail 'Cannot read the native Home layout string'
current=$VALUE
printf 'HOME_LAYOUT_VALUE_BASE64 '
printf '%s' "$current" | base64 | tr -d '\n'
printf '\nHOME_LAYOUT_PHASE %s\n' "$phase"
guards || fail 'Home reset/database guards failed'
owner_state || fail 'Cannot verify the native Home owner'
retained_sidebar=$SIDEBAR_PROCESS
printf 'HOME_LAYOUT_PROCESS %s\n' "${PROCESS:-absent}"
printf 'HOME_LAYOUT_SIDEBAR_PROCESS %s\n' "${SIDEBAR_PROCESS:-absent}"

case "$MODE" in
 inspect) exit 0;;
 status)
  case "$phase" in
   absent) echo 'HOME_LAYOUT_NOT_INSTALLED';;
   installed) [[ $current == "$after" && $APP_STATE == running ]] && stable "$after" "$PROCESS" || fail 'Installed layout or owner changed'; echo 'HOME_LAYOUT_INSTALLED';;
   removed) [[ $current == "$before" && $APP_STATE == running ]] && stable "$before" "$PROCESS" || fail 'Restored layout or owner changed'; echo 'HOME_LAYOUT_REMOVED';;
   *) fail "Home layout transaction incomplete ($phase)";;
  esac
  ;;
 install)
  input_before=$(printf '%s' "$BEFORE64" | base64 -d && printf '\034') || fail 'Invalid before encoding'
  input_before=${input_before%$'\034'}
  input_after=$(printf '%s' "$AFTER64" | base64 -d && printf '\034') || fail 'Invalid after encoding'
  input_after=${input_after%$'\034'}
  [[ ${#input_before} -gt 0 && ${#input_before} -le 65536 && ${#input_after} -gt 0 && ${#input_after} -le 65536 ]] || fail 'Invalid layout size'
  if [[ $phase == absent ]]; then
   [[ $current == "$input_before" && $APP_STATE == running ]] && ready || fail 'Preimage or original owner changed'
   before=$input_before; after=$input_after
   mkdir -m 700 "$STATE"
   printf '%s' "$before" > "$STATE/before"
   printf '%s' "$after" > "$STATE/after"
   digest_file "$STATE/before" > "$STATE/before.sha256"
   digest_file "$STATE/after" > "$STATE/after.sha256"
   printf '%s\n' "$POLICY" > "$STATE/policy"
   cat /proc/sys/kernel/random/boot_id > "$STATE/boot-id"
   printf '%s\n' "$PROCESS" > "$STATE/original-process"
   printf '%s\n' "${retained_sidebar:-absent}" > "$STATE/retained-sidebar"
   write_phase prepared
   validate_journal || fail 'Cannot validate saved layout'
  else
   [[ $before == "$input_before" && $after == "$input_after" ]] || fail 'Existing journal belongs to a different layout'
  fi
  case "$phase" in
   installed) [[ $current == "$after" && $APP_STATE == running ]] && stable "$after" "$PROCESS" || fail 'Installed layout changed'; echo 'HOME_LAYOUT_INSTALLED'; exit 0;;
   prepared|rolled-back|removed) [[ $current == "$before" && $APP_STATE == running ]] && ready || fail 'Original layout or owner changed';;
   *) fail "Recover interrupted layout with remove first ($phase)";;
  esac
  recover=1
  change_layout "$before" "$after" || fail 'Native Home layout installation failed'
  write_phase installed
  recover=0
  echo 'HOME_LAYOUT_INSTALLED'
  ;;
 remove)
  [[ $phase != absent ]] || fail 'No saved layout to restore'
  if [[ $current != "$before" && $current != "$after" ]]; then
   if restart_saved_stop "$current"; then
    write_phase recovery-required
   fi
   fail 'Layout changed externally; refusing overwrite'
  fi
  [[ $phase != installed || $current == "$after" ]] || fail 'Installed layout changed externally'
  [[ $phase != removed || $current == "$before" ]] || fail 'Restored layout changed externally'
  write_phase removing
  restore_original || { write_phase recovery-required; fail 'Layout removal incomplete; journal retained'; }
  printf '%s %s\n' "$(cat /proc/sys/kernel/random/boot_id)" "$(digest_file "$STATE/before")" > "$STATE/removed-receipt"
  write_phase removed
  echo 'HOME_LAYOUT_REMOVED'
  ;;
esac
"""
