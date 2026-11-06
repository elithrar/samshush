"""Optional, exact-build native Home startup preferences.

This module does not edit source shortcuts or replace Samsung Home. The host
uses the ordinary Samshush transport to run the rendered TV Bash script.
"""

import hashlib
import json
import shlex

BUILD = "T-RSMFAKUC-0090-REL-202607141953"
MODEL = "QN65LS03FWFXZA"
JOURNAL = "/opt/usr/home/owner/share/frame-home-v1"
SETTINGS = (
    ("auto_start", "db/menu/smart_hub/ontvsetting/auto_start", "int", 0),
    ("app_booting", "db/menu/app_booting", "int", 0),
    ("autorun_tv_plus", "db/menu/autorun_tv_plus", "bool", 0),
)
PROVIDERS = {
    "/usr/share/menu/providers/libsmarthub-data-provider.so": (
        "79c08b012324b56e3245a9289166c2adaa7a0fddac74cc37e14b987203a1fbf6"
    ),
    "/usr/share/menu/providers/libsystem-provider.so": (
        "0960462781f515cf882398ef599f76b4b27ee30c1d3a53e7a772ce9b1eca2fb7"
    ),
}


def render(operation, profile):
    """Render a bounded TV script; reject changed policy before transport use."""
    if operation not in ("inspect", "install", "status", "remove"):
        raise ValueError("Unknown Home operation")
    if profile.get("build") != BUILD or profile.get("model") != MODEL:
        raise ValueError("Home preferences require the verified model and build")
    home = profile.get("home")
    if not isinstance(home, dict):
        raise ValueError("Missing Home policy")
    if home.get("journal", JOURNAL) != JOURNAL:
        raise ValueError("Unexpected Home recovery journal")
    expected = [
        {"name": name, "key": key, "type": kind, "value": value}
        for name, key, kind, value in SETTINGS
    ]
    settings = home.get("settings")
    if settings != expected or any(type(item["value"]) is not int for item in settings):
        raise ValueError("Home setting selection or value differs from verified policy")
    if home.get("provider_hashes") != PROVIDERS:
        raise ValueError("Home provider hashes differ from verified policy")
    policy = {"build": BUILD, "settings": expected, "provider_hashes": PROVIDERS}
    fingerprint = hashlib.sha256(
        json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    replacements = {
        "@@OPERATION@@": shlex.quote(operation),
        "@@BUILD@@": shlex.quote(BUILD),
        "@@JOURNAL@@": shlex.quote(JOURNAL),
        "@@POLICY@@": shlex.quote(fingerprint),
        "@@NAMES@@": " ".join(shlex.quote(row[0]) for row in SETTINGS),
        "@@KEYS@@": " ".join(shlex.quote(row[1]) for row in SETTINGS),
        "@@TYPES@@": " ".join(shlex.quote(row[2]) for row in SETTINGS),
        "@@PROVIDERS@@": "\n".join(
            f"{digest}  {path}" for path, digest in PROVIDERS.items()
        ),
    }
    script = SCRIPT
    for token, replacement in replacements.items():
        script = script.replace(token, replacement)
    return script


SCRIPT = r"""#!/bin/bash
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
umask 077
MODE=@@OPERATION@@
BUILD=@@BUILD@@
STATE=@@JOURNAL@@
POLICY=@@POLICY@@
LOCK=/run/frame-runtime-v1.lock
names=(@@NAMES@@)
keys=(@@KEYS@@)
types=(@@TYPES@@)
original=()
current=()
phase=absent
locked=0
fail() { echo "ERROR: $*" >&2; exit 1; }
[[ $(id -u) == 0 ]] || fail 'UID 0 required'
grep -Fxq "BUILD_ID=$BUILD" /etc/os-release || fail 'Unexpected firmware'
sha256sum -c <<'HOME_PROVIDER_HASHES' || fail 'Native Home provider changed'
@@PROVIDERS@@
HOME_PROVIDER_HASHES

read_value() {
 local i=$1 reply
 reply=$(vconftool get "${keys[i]}") || return 1
 case "${types[i]}:$reply" in
  "int:${keys[i]}, value = 0 (Int32)"|"bool:${keys[i]}, value = False (Boolean)") printf 0;;
  "int:${keys[i]}, value = 1 (Int32)"|"bool:${keys[i]}, value = True (Boolean)") printf 1;;
  *) echo "Unexpected type or value for ${keys[i]}: $reply" >&2; return 1;;
 esac
}
read_all() {
 local i value
 current=()
 for i in "${!keys[@]}"; do
  value=$(read_value "$i") || return 1
  current+=("$value")
 done
}
write_value() {
 local i=$1 value=$2
 vconftool set -t "${types[i]}" "${keys[i]}" "$value" -f || return 1
 [[ $(read_value "$i") == "$value" ]]
}
write_phase() {
 printf '%s\n' "$1" > "$STATE/phase.tmp" || return 1
 mv "$STATE/phase.tmp" "$STATE/phase" || return 1
 phase=$1
}
validate_journal() {
 local path file name key kind desired saved extra i=0 digest
 [[ -d $STATE && ! -L $STATE && $(stat -c '%u:%a' "$STATE") == 0:700 ]] || return 1
 shopt -s nullglob dotglob
 for path in "$STATE"/*; do
  file=${path##*/}
  case "$file" in policy|before.tsv|before.sha256|boot-id|phase|phase.tmp) ;;
   *) echo "Unexpected Home journal entry: $file" >&2; return 1;;
  esac
  [[ -f $path && ! -L $path && $(stat -c '%u:%a' "$path") == 0:600 ]] || return 1
 done
 shopt -u nullglob dotglob
 for file in policy before.tsv before.sha256 boot-id phase; do
  [[ -f $STATE/$file && -s $STATE/$file && ! -L $STATE/$file ]] || return 1
 done
 [[ $(cat "$STATE/policy") == "$POLICY" ]] || return 1
 digest=$(sha256sum "$STATE/before.tsv") || return 1
 [[ ${digest%% *} == "$(cat "$STATE/before.sha256")" ]] || return 1
 original=()
 while IFS=$'\t' read -r name key kind desired saved extra; do
  (( i < ${#keys[@]} )) || return 1
  [[ $name == "${names[i]}" && $key == "${keys[i]}" && $kind == "${types[i]}" && $desired == 0 && -z $extra ]] || return 1
  [[ $saved == 0 || $saved == 1 ]] || return 1
  original+=("$saved")
  i=$((i + 1))
 done < "$STATE/before.tsv"
 [[ $i == ${#keys[@]} ]] || return 1
 phase=$(cat "$STATE/phase") || return 1
 case "$phase" in prepared|applying|installed|rolling-back|rolled-back|removing|removed) ;;
  *) return 1;;
 esac
}
matches_original() {
 local i
 for i in "${!keys[@]}"; do [[ ${current[i]} == "${original[i]}" ]] || return 1; done
}
matches_desired() {
 local value
 for value in "${current[@]}"; do [[ $value == 0 ]] || return 1; done
}
recovery_values_valid() {
 local i
 for i in "${!keys[@]}"; do
  [[ ${current[i]} == 0 || ${current[i]} == "${original[i]}" ]] || return 1
 done
}
restore_values() {
 local i failed=0
 read_all && recovery_values_valid || return 1
 for i in "${!keys[@]}"; do
  if [[ ${current[i]} != "${original[i]}" ]]; then
   write_value "$i" "${original[i]}" || failed=1
  fi
 done
 read_all && matches_original || failed=1
 [[ $failed == 0 ]]
}
rollback_install() {
 write_phase rolling-back || return 1
 restore_values || return 1
 write_phase rolled-back
}
cleanup() {
 local result=$?
 trap - EXIT INT TERM
 if [[ $MODE == install && $phase == applying && $result != 0 ]]; then
  echo 'Install did not finish; restoring saved Home startup preferences.' >&2
  rollback_install || echo 'Rollback incomplete; Home recovery journal retained.' >&2
 fi
 if [[ $locked == 1 ]]; then
  rmdir "$LOCK" || { echo 'Could not release Home operation lock.' >&2; result=1; }
 fi
 exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

[[ ! -L $STATE && ! -L $LOCK ]] || fail 'Unsafe Home journal or lock'
# Serialize inspection with writes too, so a partial transaction is not reported
# as a stable set of preferences.
mkdir -m 700 "$LOCK" || fail 'Another operation may be running'
locked=1
read_all || fail 'Cannot read all native Home startup preferences'
echo 'HOME SCOPE: startup preferences only; source shortcut layout is not managed or verified'
for i in "${!keys[@]}"; do printf '%s=%s (%s)\n' "${names[i]}" "${current[i]}" "${types[i]}"; done
if [[ -e $STATE ]]; then
 validate_journal || fail 'Invalid or incomplete Home recovery journal; retained for inspection'
 echo "HOME JOURNAL: $phase"
else
 echo 'HOME JOURNAL: NOT_INSTALLED'
 if [[ $MODE == inspect || $MODE == status ]]; then exit 0; fi
 [[ $MODE == install ]] || fail 'No saved Home preferences to restore'
 mkdir -m 700 "$STATE"
 printf '%s\n' "$POLICY" > "$STATE/policy"
 cat /proc/sys/kernel/random/boot_id > "$STATE/boot-id"
 original=("${current[@]}")
 for i in "${!keys[@]}"; do
  printf '%s\t%s\t%s\t0\t%s\n' "${names[i]}" "${keys[i]}" "${types[i]}" "${original[i]}"
 done > "$STATE/before.tsv"
 digest=$(sha256sum "$STATE/before.tsv")
 printf '%s\n' "${digest%% *}" > "$STATE/before.sha256"
 write_phase prepared
 validate_journal || fail 'Cannot validate saved Home preferences'
fi

case "$MODE" in
 inspect) exit 0;;
 status)
  case "$phase" in
   installed) matches_desired || fail 'Home startup preferences changed'; echo 'HOME STARTUP PREFERENCES: VERIFIED';;
   removed) matches_original || fail 'Restored Home preferences changed'; echo 'HOME STARTUP PREFERENCES: REMOVED';;
   *) fail "Home transaction is incomplete ($phase)";;
  esac
  ;;
 install)
  case "$phase" in
   installed) matches_desired || fail 'Installed Home preferences changed'; echo 'HOME STARTUP PREFERENCES: already installed'; exit 0;;
   applying|rolling-back)
    rollback_install || fail 'Cannot recover interrupted Home installation'
    fail 'Interrupted Home installation restored; inspect before retrying'
    ;;
   prepared|rolled-back) matches_original || fail 'Original Home preferences changed';;
   *) fail "Cannot install over Home journal in phase $phase";;
  esac
  write_phase applying
  for i in "${!keys[@]}"; do
   if [[ ${current[i]} != 0 ]]; then
    write_value "$i" 0 || fail "Failed to apply ${names[i]}"
   fi
  done
  read_all && matches_desired || fail 'Home startup readback failed'
  write_phase installed
  echo 'HOME STARTUP PREFERENCES: INSTALLED; source shortcut layout still requires separate setup'
  ;;
 remove)
  case "$phase" in
   installed) matches_desired || fail 'Home preferences changed externally; refusing to overwrite';;
   prepared|rolled-back|removed) matches_original || fail 'Original Home preferences changed';;
   applying|rolling-back|removing) recovery_values_valid || fail 'Home recovery values changed externally';;
  esac
  write_phase removing
  restore_values || fail 'Home removal incomplete; journal retained for retry'
  write_phase removed
  echo 'HOME STARTUP PREFERENCES: REMOVED; original values verified and journal retained'
  ;;
esac
"""
