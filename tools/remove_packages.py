"""Back up and natively remove the six assessed optional packages on the exact TV build.

Backups preserve installed files and registration evidence, not a guaranteed
reinstallable signed TPK. Recovery may require an original vendor package.
"""

import base64
import datetime
import hashlib
import json
import os
import re
import secrets
import shlex
import tempfile

PACKAGES = (
    "com.samsung.tv.ai-interpreter-service",
    "com.samsung.tv.contents-recognition-dbmgr",
    "com.samsung.tv.screen-analysis-dbmgr",
    "com.samsung.tv.screen-analysis-dbmgr-nb",
    "com.samsung.tizen.samsung-analytics",
    "com.samsung.tv.context-aware-service",
)
ROOT = "/opt/usr/share/samshush-removals-v1"
GROUPS = ("ai-models", "analytics-context", "presence-sensing", "optional-delivery")

COMMON = r"""set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
umask 077
BASE=/opt/usr/share/samshush-removals-v1
ROOT=@@ROOT@@
ATTEMPT=@@ATTEMPT@@
EXPECTED_PACKAGES=@@EXPECTED_PACKAGES@@
PARENT_HASH=@@PARENT_HASH@@
DB=/opt/dbspace/.pkgmgr_parser.db
LOCK=/run/frame-runtime-v1.lock
fail() { echo "ERROR: $*" >&2; exit 1; }
[[ $(id -u) == 0 && $(stat -c %u /opt/usr/home/owner) == 5001 ]] || fail 'Unexpected owner identity'
grep -Fxq 'BUILD_ID=@@BUILD@@' /etc/os-release || fail 'Firmware changed'
[[ $(readlink /proc/self/ns/mnt) == "$(readlink /proc/1/ns/mnt)" ]] || fail 'Wrong mount namespace'
[[ ! -L $ROOT && ! -L $LOCK ]] || fail 'Unsafe journal or lock'
[[ ! -e $ROOT || $(stat -c '%u:%a' "$ROOT") == 0:700 ]] || fail 'Unsafe removal journal'
all_packages() { cat <<'PACKAGES'
@@PACKAGES@@
PACKAGES
}
packages() {
 if [[ -n ${SELECTED:-} ]]; then printf '%s\n' "$SELECTED"
 elif [[ -n $ATTEMPT ]]; then cat "$ROOT/packages"
 else all_packages
 fi
}
units() { cat <<'UNITS'
@@UNITS@@
UNITS
}
hash() { sha256sum "$1" | cut -d ' ' -f 1; }
package_row() {
 sqlite3 -readonly "$DB" "SELECT package,package_type,package_version,package_removable,package_readonly,root_path FROM package_info WHERE package='$1';"
}
package_paths() {
 printf '%s\n' "/opt/usr/apps/$1" "/opt/share/packages/$1.xml" "/opt/usr/home/owner/apps_rw/$1" "/opt/usr/home/owner/apps_rw/.shared/$1" "/opt/usr/home/owner/apps_rw/.shared_tmp/$1"
}
snapshot_package_tree() {
 local pkg=$1 destination=$2 path
 : > "$destination" || return 1
 while IFS= read -r path; do
  [[ -e $path || -L $path ]] || continue
  find "$path" -print0 | while IFS= read -r -d '' entry; do
   [[ $entry != *$'\n'* && $entry != *$'\t'* ]] || fail 'Unsupported package filename'
   target=''
   if [[ -L $entry ]]; then
    kind=link
    target=$(readlink "$entry" && printf '\034') || exit 1
    target=${target%$'\034'}
    [[ $target == *$'\n' ]] || exit 1
    target=${target%$'\n'}
    [[ $target != *$'\n'* && $target != *$'\t'* ]] || fail 'Unsupported package link target'
   elif [[ -d $entry ]]; then kind=directory
   elif [[ -f $entry ]]; then kind=file
   else fail 'Unsupported special file in returned package'
   fi
   metadata=$(stat -c '%u:%g:%a' "$entry") || exit 1
   printf '%s\t%s\t%s\t%s\n' "$kind" "$entry" "$metadata" "$target"
  done >> "$destination" || return 1
 done < <(package_paths "$pkg")
 LC_ALL=C sort -o "$destination" "$destination"
}
check_package_absent() {
 local pkg=$1 path row
 row=$(package_row "$pkg") || fail "Cannot read package registration: $pkg"
 [[ -z $row ]] || fail "Package registered again: $pkg"
 [[ $(sqlite3 -readonly "$DB" "SELECT count(*) FROM package_app_info WHERE package='$pkg';") == 0 ]] || fail "App registered again: $pkg"
 while IFS= read -r path; do
  [[ ! -e $path && ! -L $path ]] || fail "Package/model/data path remains: $path"
 done < <(package_paths "$pkg")
}
original_receipt() {
 local name
 [[ -d $BASE && ! -L $BASE && $(stat -c '%u:%a' "$BASE") == 0:700 ]] || fail 'Unsafe original removal receipt'
 for name in complete build packages backup.sha256 host-verified.sha256; do
  [[ -f $BASE/$name && ! -L $BASE/$name && $(stat -c '%u:%a' "$BASE/$name") == 0:600 ]] || fail "Invalid original receipt: $name"
 done
 [[ $(cat "$BASE/build") == '@@BUILD@@' && $(cat "$BASE/packages") == "$(all_packages)" ]] || fail 'Original removal receipt scope changed'
 [[ $(cat "$BASE/backup.sha256") =~ ^[a-f0-9]{64}$ && $(cat "$BASE/host-verified.sha256") == "$(cat "$BASE/backup.sha256")" ]] || fail 'Original backup receipt changed'
 [[ -z $PARENT_HASH || $PARENT_HASH == "$(cat "$BASE/backup.sha256")" ]] || fail 'Original backup link changed'
}
validate_attempt_scope() {
 local directory=$1 pkg count expected=''
 while IFS= read -r pkg; do
  count=$(grep -Fxc "$pkg" "$directory/packages") || [[ $count == 0 ]] || return 1
  [[ $count == 0 || $count == 1 ]] || return 1
  if [[ $count == 1 ]]; then expected+="$pkg"$'\n'; fi
 done < <(all_packages)
 [[ -n $expected && $(cat "$directory/packages") == "${expected%$'\n'}" ]]
}
validate_attempts() {
 local allowed=${1:-} directory name file digest
 [[ -e $BASE/attempts || -L $BASE/attempts ]] || return 0
 [[ -d $BASE/attempts && ! -L $BASE/attempts && $(stat -c '%u:%a' "$BASE/attempts") == 0:700 ]] || fail 'Unsafe returned-package attempt directory'
 shopt -s nullglob dotglob
 for directory in "$BASE/attempts"/*; do
  name=${directory##*/}
  [[ $name =~ ^[a-f0-9]{16}$ && -d $directory && ! -L $directory && $(stat -c '%u:%a' "$directory") == 0:700 ]] || fail 'Invalid returned-package attempt'
  for file in attempt-id parent-backup.sha256 build packages; do
   [[ -f $directory/$file && ! -L $directory/$file && $(stat -c '%u:%a' "$directory/$file") == 0:600 ]] || fail "Incomplete attempt identity: $name"
  done
  [[ $(cat "$directory/attempt-id") == "$name" && $(cat "$directory/build") == '@@BUILD@@' && $(cat "$directory/parent-backup.sha256") == "$(cat "$BASE/backup.sha256")" ]] || fail "Changed attempt identity: $name"
  validate_attempt_scope "$directory" || fail "Invalid returned-package selection: $name"
  if [[ $name != "$allowed" || -e $directory/complete ]]; then
   for file in prepared complete backup.sha256 host-verified.sha256; do
    [[ -f $directory/$file && ! -L $directory/$file && $(stat -c '%u:%a' "$directory/$file") == 0:600 ]] || fail "Incomplete returned-package attempt: $name"
   done
   digest=$(cat "$directory/backup.sha256")
   [[ $digest =~ ^[a-f0-9]{64}$ && $(cat "$directory/host-verified.sha256") == "$digest" ]] || fail "Invalid returned-package backup receipt: $name"
  fi
 done
 shopt -u nullglob dotglob
}
create_receipt() {
 if [[ -n $ATTEMPT ]]; then
  [[ -d $BASE/attempts ]] || mkdir -m 700 "$BASE/attempts"
 fi
 mkdir -m 700 "$ROOT"
 if [[ -n $ATTEMPT ]]; then
  printf '%s\n' "$ATTEMPT" > "$ROOT/attempt-id"
  cat "$BASE/backup.sha256" > "$ROOT/parent-backup.sha256"
 fi
}
check_masks() {
 local unit state
 while IFS= read -r unit; do
  [[ -L /run/systemd/system/$unit && $(readlink /run/systemd/system/$unit) == /dev/null ]] || fail "Missing activation block: $unit"
  state=$(systemctl show "$unit" -p ActiveState --value)
  [[ $state == inactive || $state == failed ]] || fail "Selected unit is not stopped: $unit"
 done < <(units)
}
check_processes() {
 local proc target pkg line flags threads
 local -a fields=()
 for proc in /proc/[0-9]*; do
  if ! target=$(readlink "$proc/exe" 2>/dev/null); then
   [[ -d $proc ]] || continue
   line=$(cat "$proc/stat" 2>/dev/null) || { [[ ! -d $proc ]] && continue; fail "Unreadable process: $proc"; }
   [[ $line == *') '* ]] || fail "Invalid process identity: $proc"
   read -r -a fields <<< "${line##*) }"
   (( ${#fields[@]} >= 18 )) || fail "Invalid process fields: $proc"
   flags=${fields[6]}; threads=${fields[17]}
   [[ $flags =~ ^[0-9]+$ && $threads =~ ^[0-9]+$ ]] || fail "Invalid process state: $proc"
   (( (10#$flags & 0x00200000) != 0 )) && continue
   [[ ( ${fields[0]} == Z || ${fields[0]} == X ) && $threads == 1 ]] && continue
   fail "Unreadable userspace executable: $proc"
  fi
  while IFS= read -r pkg; do
   [[ $target != /opt/usr/apps/"$pkg"/* ]] || fail "Package process survived: $proc $target"
  done < <(all_packages)
 done
}
check_absent() {
 local pkg
 while IFS= read -r pkg; do
  check_package_absent "$pkg"
  echo "REMOVED $pkg"
 done < <(packages)
 check_processes
}
"""

STATUS = r"""
[[ -f $ROOT/complete && -f $ROOT/backup.sha256 ]] || fail 'No completed package removal receipt'
[[ $(cat "$ROOT/build") == '@@BUILD@@' ]] || fail 'Removal receipt belongs to another build'
[[ $(cat "$ROOT/packages") == "$(packages)" ]] || fail 'Removal scope changed'
[[ $(cat "$ROOT/backup.sha256") =~ ^[a-f0-9]{64}$ ]] || fail 'Invalid backup receipt'
original_receipt
validate_attempts
check_masks
check_absent
echo 'PACKAGE REMOVAL VERIFIED; privileged firmware reinstallation is not prevented by this receipt.'
"""

MUTATION = r"""
mkdir -m 700 "$LOCK" || fail 'Another runtime/removal operation is running or interrupted'
finish() { rc=$?; trap - EXIT; rm -rf -- "$stage"; rmdir "$LOCK" || rc=1; exit "$rc"; }
trap finish EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
check_masks
"""

PREPARE = r"""
[[ ! -e $ROOT ]] || fail 'Removal journal already exists; inspect it before retrying'
[[ ! -e @@ARCHIVE@@ && ! -L @@ARCHIVE@@ ]] || fail 'Backup staging path already exists'
rows=''
app_rows=''
while IFS= read -r pkg; do
 row=$(package_row "$pkg")
 IFS='|' read -r actual kind version removable readonly root <<< "$row"
 [[ $actual == "$pkg" && $kind == tpk && $removable == true && $readonly == false && $root == "/opt/usr/apps/$pkg" && -n $version && $version != *$'\n'* ]] || fail "Package is not a removable isolated TPK: $pkg"
 [[ -d $root && ! -L $root && -f $root/tizen-manifest.xml ]] || fail "Unsafe package path: $pkg"
 rows+="$row"$'\n'
 apps=$(sqlite3 -readonly "$DB" "SELECT app_id,app_disable,app_exec,package FROM package_app_info WHERE package='$pkg' ORDER BY app_id;")
 [[ -n $apps ]] || fail "Package has no app registrations: $pkg"
 while IFS='|' read -r app flag exe owner; do
  [[ $app =~ ^[A-Za-z0-9_.-]+$ && ( $flag == true || $flag == false ) && $exe == "$root/"* && -f $exe && $owner == "$pkg" ]] || fail "Unexpected app mapping: $pkg"
 done <<< "$apps"
 app_rows+="$apps"$'\n'
done < <(packages)
create_receipt
printf '%s\n' '@@BUILD@@' > "$ROOT/build"
packages > "$ROOT/packages"
printf '%s' "$rows" > "$ROOT/metadata.before"
printf '%s' "$app_rows" > "$ROOT/apps.before"
cat /proc/sys/kernel/random/boot_id > "$ROOT/boot-id"
# Record the original flags before changing them. A failed backup leaves these
# records intact and does not proceed to uninstall.
while IFS='|' read -r app flag exe owner; do
 if [[ $flag == false ]]; then owner_pkg -D -t app -n "$app" --global; fi
 [[ $(sqlite3 -readonly "$DB" "SELECT app_disable FROM package_app_info WHERE app_id='$app';") == true ]] || fail "App disable failed: $app"
done < "$ROOT/apps.before"
while IFS= read -r pkg; do owner_pkg -k -n "$pkg"; done < <(packages)
sleep 3
check_processes
: > "$ROOT/paths"
while IFS= read -r pkg; do
 while IFS= read -r path; do
  if [[ -e $path || -L $path ]]; then printf '%s\n' "${path#/}" >> "$ROOT/paths"; fi
 done < <(package_paths "$pkg")
done < <(packages)
: > "$ROOT/files.sha256"
: > "$ROOT/labels"
while IFS= read -r path; do
 find "/$path" -type f -exec sha256sum '{}' + >> "$ROOT/files.sha256"
 find "/$path" -exec chsmack '{}' + >> "$ROOT/labels"
done < "$ROOT/paths"
if [[ -n $ATTEMPT ]]; then
 mkdir -m 700 "$ROOT/package-files"
 while IFS= read -r pkg; do
  : > "$ROOT/package-files/$pkg.sha256"
  while IFS= read -r path; do
   if [[ -e $path ]]; then find "$path" -type f -exec sha256sum '{}' + >> "$ROOT/package-files/$pkg.sha256"; fi
  done < <(package_paths "$pkg")
  LC_ALL=C sort -o "$ROOT/package-files/$pkg.sha256" "$ROOT/package-files/$pkg.sha256"
  snapshot_package_tree "$pkg" "$ROOT/package-files/$pkg.tree" || fail "Cannot snapshot package paths: $pkg"
 done < <(packages)
fi
# Include the package metadata and labels as well as the complete installed
# files and private app data. This archive is not advertised as a signed TPK.
printf '%s\n' "${ROOT#/}" >> "$ROOT/paths"
size=$(while IFS= read -r path; do du -sk "/$path"; done < "$ROOT/paths" | awk '{total+=$1} END{print total}')
[[ $size =~ ^[0-9]+$ && $size -lt 524288 ]] || fail 'Backup exceeds the 512 MiB bound'
tar --acls -czf @@ARCHIVE@@ -C / -T "$ROOT/paths"
sha256sum -c "$ROOT/files.sha256" > "$ROOT/backup-file-check.log"
hash @@ARCHIVE@@ > "$ROOT/backup.sha256"
printf '%s\n' @@ARCHIVE@@ > "$ROOT/archive.path"
chmod 600 @@ARCHIVE@@
[[ $(id -u sdk) == 901 ]] || fail 'Unexpected SDB transfer identity'
chown 901:901 @@ARCHIVE@@
chsmack -a System @@ARCHIVE@@
touch "$ROOT/prepared"
printf 'BACKUP_SHA256 %s\n' "$(cat "$ROOT/backup.sha256")"
echo 'PREPARED; uninstall has not run.'
"""

BACKUP = r"""
[[ -f $ROOT/prepared && ! -e $ROOT/complete ]] || fail 'No prepared backup'
archive=$(cat "$ROOT/archive.path")
[[ -f $archive && ! -L $archive && $(hash "$archive") == $(cat "$ROOT/backup.sha256") ]] || fail 'Prepared backup changed or missing'
[[ $(id -u sdk) == 901 ]] || fail 'Unexpected SDB transfer identity'
chmod 600 "$archive"
chown 901:901 "$archive"
chsmack -a System "$archive"
printf 'BACKUP_SHA256 %s\n' "$(cat "$ROOT/backup.sha256")"
printf 'BACKUP_PATH %s\n' "$archive"
"""

ACKNOWLEDGE = r"""
[[ -f $ROOT/prepared && ! -e $ROOT/complete ]] || fail 'No prepared backup'
[[ $(cat "$ROOT/archive.path") == @@ARCHIVE@@ && $(cat "$ROOT/backup.sha256") == '@@BACKUP_HASH@@' && $(hash @@ARCHIVE@@) == '@@BACKUP_HASH@@' ]] || fail 'Backup receipt mismatch'
printf '%s\n' '@@BACKUP_HASH@@' > "$ROOT/host-verified.sha256"
rm -- @@ARCHIVE@@
[[ ! -e @@ARCHIVE@@ ]] || fail 'Temporary TV backup remains'
echo 'BACKUP VERIFIED ON HOST; temporary TV archive removed.'
"""

REMOVE = r"""
[[ -f $ROOT/prepared && ! -e $ROOT/complete ]] || fail 'No prepared removal or already completed'
[[ $(cat "$ROOT/backup.sha256") == '@@BACKUP_HASH@@' && $(cat "$ROOT/host-verified.sha256") == '@@BACKUP_HASH@@' ]] || fail 'No matching host-verified backup'
[[ $(cat "$ROOT/packages") == "$(packages)" ]] || fail 'Removal scope changed'
# Validate the entire batch before the first uninstall. Never force-remove a
# firmware package or edit package-manager databases to simulate success.
while IFS='|' read -r pkg kind version removable readonly root; do
 [[ $(package_row "$pkg") == "$pkg|$kind|$version|$removable|$readonly|$root" ]] || fail "Package metadata drift: $pkg"
done < "$ROOT/metadata.before"
while IFS='|' read -r app flag exe pkg; do
 [[ $(sqlite3 -readonly "$DB" "SELECT app_disable FROM package_app_info WHERE app_id='$app';") == true ]] || fail "App reactivated: $app"
done < "$ROOT/apps.before"
sha256sum -c "$ROOT/files.sha256" > "$ROOT/remove-file-check.log"
check_processes
while IFS= read -r pkg; do
 printf 'UNINSTALL_BEGIN %s\n' "$pkg" | tee -a "$ROOT/removal.log"
 owner_pkg -u -n "$pkg" --global >> "$ROOT/removal.log" 2>&1 || fail "Native uninstall failed: $pkg; inspect the retained receipt"
 [[ -z $(package_row "$pkg") ]] || fail "Native uninstall left a registration: $pkg"
 printf 'UNINSTALL_DONE %s\n' "$pkg" | tee -a "$ROOT/removal.log"
done < <(packages)
check_absent
touch "$ROOT/complete"
sync
echo 'REMOVAL COMPLETE'
"""

RETURNED_CONTEXT = r"""
original_receipt
validate_attempts "$ATTEMPT"
[[ -d $ROOT && ! -L $ROOT ]] || fail 'Returned-package attempt is missing'
[[ -z $EXPECTED_PACKAGES || $(cat "$ROOT/packages") == "$EXPECTED_PACKAGES" ]] || fail 'Returned-package selection changed'
"""

PREPARE_RETURNED = r"""
original_receipt
validate_attempts
[[ ! -e /opt/usr/share/samshush-boot-v1 ]] || fail 'Remove automatic reapplication before preparing returned packages'
SELECTED=''
while IFS= read -r pkg; do
 row=$(package_row "$pkg") || fail "Cannot read package registration: $pkg"
 if [[ -z $row ]]; then
  check_package_absent "$pkg"
  continue
 fi
 # The existing preparation validates removability and all app mappings below.
 # Reject links and unexpected path kinds before recording or disabling any app.
 while IFS= read -r path; do
  [[ ! -L $path ]] || fail "Unsafe returned-package path: $path"
  if [[ $path == /opt/share/packages/* ]]; then
   [[ -f $path ]] || fail "Missing package registration file: $pkg"
  elif [[ -e $path ]]; then
   [[ -d $path ]] || fail "Unexpected package path type: $path"
  fi
 done < <(package_paths "$pkg")
 SELECTED+="$pkg"$'\n'
done < <(all_packages)
SELECTED=${SELECTED%$'\n'}
[[ -n $SELECTED ]] || fail 'No assessed packages have returned'
[[ -z $EXPECTED_PACKAGES || $SELECTED == "$EXPECTED_PACKAGES" ]] || fail 'Discovered returned-package selection changed'
"""

RETURNED_EVIDENCE = r"""
printf 'RETURNED_ATTEMPT %s\n' "$ATTEMPT"
printf 'PARENT_BACKUP_SHA256 %s\n' "$(cat "$ROOT/parent-backup.sha256")"
while IFS= read -r pkg; do printf 'RETURNED_PACKAGE %s\n' "$pkg"; done < <(packages)
"""

REMOVE_RETURNED = r"""
[[ -f $ROOT/prepared && ! -L $ROOT/prepared ]] || fail 'No prepared returned-package removal'
[[ $(cat "$ROOT/backup.sha256") == '@@BACKUP_HASH@@' && $(cat "$ROOT/host-verified.sha256") == '@@BACKUP_HASH@@' ]] || fail 'No matching host-verified returned-package backup'
if [[ -f $ROOT/complete ]]; then
 while IFS= read -r pkg; do check_package_absent "$pkg"; done < <(all_packages)
 check_processes
 echo 'RETURNED REMOVAL ALREADY COMPLETE'
 exit 0
fi
[[ -d $ROOT/package-files && ! -L $ROOT/package-files && $(stat -c '%u:%a' "$ROOT/package-files") == 0:700 ]] || fail 'Missing returned-package file snapshots'
if [[ -e $ROOT/uninstall || -L $ROOT/uninstall ]]; then
 [[ -d $ROOT/uninstall && ! -L $ROOT/uninstall && $(stat -c '%u:%a' "$ROOT/uninstall") == 0:700 ]] || fail 'Unsafe uninstall progress directory'
fi
remaining=''
# Validate every remaining package before invoking the native uninstaller.
# Previously begun packages may be skipped only when their full absence is proven.
while IFS= read -r pkg; do
 row=$(package_row "$pkg") || fail "Cannot read package registration: $pkg"
 progress=''
 if [[ -e $ROOT/uninstall/$pkg || -L $ROOT/uninstall/$pkg ]]; then
  [[ -f $ROOT/uninstall/$pkg && ! -L $ROOT/uninstall/$pkg && $(stat -c '%u:%a' "$ROOT/uninstall/$pkg") == 0:600 ]] || fail "Invalid uninstall progress: $pkg"
  progress=$(cat "$ROOT/uninstall/$pkg")
  [[ $progress == begun || $progress == done ]] || fail "Invalid uninstall phase: $pkg"
 fi
 if [[ -z $row ]]; then
  [[ -n $progress ]] || fail "Package disappeared before its recorded uninstall: $pkg"
  check_package_absent "$pkg"
  continue
 fi
 [[ $progress != done ]] || fail "Package returned after this attempt removed it: $pkg"
 expected=$(awk -F '|' -v package="$pkg" '$1 == package {print}' "$ROOT/metadata.before")
 [[ -n $expected && $expected != *$'\n'* && $row == "$expected" ]] || fail "Package metadata drift: $pkg"
 expected_apps=$(awk -F '|' -v package="$pkg" 'BEGIN {OFS="|"} $4 == package {$2="true"; print}' "$ROOT/apps.before")
 current_apps=$(sqlite3 -readonly "$DB" "SELECT app_id,app_disable,app_exec,package FROM package_app_info WHERE package='$pkg' ORDER BY app_id;") || fail "Cannot read app registrations: $pkg"
 [[ -n $expected_apps && $current_apps == "$expected_apps" ]] || fail "App registrations changed or reactivated: $pkg"
 snapshot="$ROOT/package-files/$pkg.sha256"
 [[ -s $snapshot && ! -L $snapshot && $(stat -c '%u:%a' "$snapshot") == 0:600 ]] || fail "Missing package file snapshot: $pkg"
 : > "$stage/returned-files.sha256"
 while IFS= read -r path; do
  [[ ! -L $path ]] || fail "Package path became a symlink: $path"
  if [[ -e $path ]]; then find "$path" -type f -exec sha256sum '{}' + >> "$stage/returned-files.sha256"; fi
 done < <(package_paths "$pkg")
 LC_ALL=C sort -o "$stage/returned-files.sha256" "$stage/returned-files.sha256"
 actual_hash=$(hash "$stage/returned-files.sha256") || fail "Cannot hash current package inventory: $pkg"
 saved_hash=$(hash "$snapshot") || fail "Cannot hash saved package inventory: $pkg"
 [[ $actual_hash =~ ^[a-f0-9]{64}$ && $saved_hash =~ ^[a-f0-9]{64}$ && $actual_hash == "$saved_hash" ]] || fail "Package file contents or inventory changed: $pkg"
 tree="$ROOT/package-files/$pkg.tree"
 [[ -s $tree && ! -L $tree && $(stat -c '%u:%a' "$tree") == 0:600 ]] || fail "Missing package tree snapshot: $pkg"
 snapshot_package_tree "$pkg" "$stage/returned-tree" || fail "Cannot inspect package paths: $pkg"
 actual_hash=$(hash "$stage/returned-tree") || fail "Cannot hash current package tree: $pkg"
 saved_hash=$(hash "$tree") || fail "Cannot hash saved package tree: $pkg"
 [[ $actual_hash =~ ^[a-f0-9]{64}$ && $saved_hash =~ ^[a-f0-9]{64}$ && $actual_hash == "$saved_hash" ]] || fail "Package paths, types, ownership or permissions changed: $pkg"
 printf 'FILES VERIFIED %s\n' "$pkg" >> "$ROOT/remove-file-check.log"
 remaining+="$pkg"$'\n'
done < <(packages)
check_processes
[[ -d $ROOT/uninstall ]] || mkdir -m 700 "$ROOT/uninstall"
while IFS= read -r pkg; do
 [[ -n $pkg ]] || continue
 printf 'begun\n' > "$ROOT/uninstall/$pkg"
 sync
 printf 'UNINSTALL_BEGIN %s\n' "$pkg" | tee -a "$ROOT/removal.log"
 owner_pkg -u -n "$pkg" --global >> "$ROOT/removal.log" 2>&1 || fail "Native uninstall failed: $pkg; retained attempt can be inspected and retried"
 check_package_absent "$pkg"
 printf 'done\n' > "$ROOT/uninstall/$pkg"
 printf 'UNINSTALL_DONE %s\n' "$pkg" | tee -a "$ROOT/removal.log"
done <<< "$remaining"
while IFS= read -r pkg; do check_package_absent "$pkg"; done < <(all_packages)
check_processes
touch "$ROOT/complete"
sync
echo 'RETURNED REMOVAL COMPLETE'
"""


def render(
    action,
    policy,
    *,
    archive="",
    backup_hash="",
    attempt_id="",
    selected_packages=None,
    parent_backup_hash="",
):
    scripts = {
        "prepare": PREPARE,
        "acknowledge": ACKNOWLEDGE,
        "backup": BACKUP,
        "remove": REMOVE,
        "status": STATUS,
        "prepare-returned": PREPARE_RETURNED + PREPARE + RETURNED_EVIDENCE,
    }
    if action not in scripts:
        raise ValueError("Unknown package-removal action")
    if action in ("prepare", "prepare-returned", "acknowledge") and not re.fullmatch(
        r"/opt/usr/home/owner/share/tmp/samshush-package-backup-[a-f0-9]{16}\.tar\.gz",
        archive,
    ):
        raise ValueError("Invalid backup staging path")
    if action in ("remove", "acknowledge") and not re.fullmatch(
        r"[a-f0-9]{64}", backup_hash
    ):
        raise ValueError("Invalid verified backup hash")
    if attempt_id:
        if not re.fullmatch(r"[a-f0-9]{16}", attempt_id):
            raise ValueError("Invalid returned-package attempt ID")
        if action in ("prepare", "status"):
            raise ValueError("This action does not accept a returned-package attempt")
        if parent_backup_hash and not re.fullmatch(r"[a-f0-9]{64}", parent_backup_hash):
            raise ValueError("Invalid original backup link")
        if selected_packages is not None:
            validate_selection(selected_packages)
        if action in ("remove", "acknowledge") and (
            selected_packages is None or not parent_backup_hash
        ):
            raise ValueError(
                "Returned removal requires its selection and original backup link"
            )
    elif (
        action == "prepare-returned"
        or selected_packages is not None
        or parent_backup_hash
    ):
        raise ValueError("Returned-package context requires an attempt ID")
    context = ""
    body = scripts[action]
    if attempt_id and action != "prepare-returned":
        context = RETURNED_CONTEXT
        if action == "remove":
            body = REMOVE_RETURNED
        elif action == "backup":
            body += RETURNED_EVIDENCE
    script = COMMON + (MUTATION if action != "status" else "") + context + body
    units = [
        unit
        for group in GROUPS
        for unit in policy["optional_controls"][group]["systemd_units"]
    ]
    for key, value in {
        "BUILD": policy["build"],
        "PACKAGES": "\n".join(PACKAGES),
        "UNITS": "\n".join(units),
        "ARCHIVE": archive,
        "BACKUP_HASH": backup_hash,
        "ROOT": shlex.quote(f"{ROOT}/attempts/{attempt_id}" if attempt_id else ROOT),
        "ATTEMPT": shlex.quote(attempt_id),
        "EXPECTED_PACKAGES": shlex.quote("\n".join(selected_packages or ())),
        "PARENT_HASH": shlex.quote(parent_backup_hash),
    }.items():
        script = script.replace("@@" + key + "@@", value)
    if "@@" in script:
        raise ValueError("Unresolved package-removal placeholder")
    return script


def validate_selection(packages):
    if (
        not isinstance(packages, (list, tuple))
        or not packages
        or any(package not in PACKAGES for package in packages)
        or list(packages) != [package for package in PACKAGES if package in packages]
    ):
        raise ValueError("Invalid returned-package selection")


def attempt_context(record, tv, build, *, pending=False):
    """Validate a local attempt/backup receipt before selecting a TV journal."""
    if (
        not isinstance(record, dict)
        or record.get("schema") != "samshush-returned-packages-v1"
        or record.get("tv") != tv
        or record.get("build") != build
        or not isinstance(record.get("attempt_id"), str)
        or not re.fullmatch(r"[a-f0-9]{16}", record["attempt_id"])
    ):
        raise ValueError(
            "Returned-package receipt belongs to another TV/build or is invalid"
        )
    selected = record.get("packages")
    parent_hash = record.get("parent_backup_sha256", "")
    if selected is None and not parent_hash and pending:
        return record["attempt_id"], None, ""
    validate_selection(selected)
    if not isinstance(parent_hash, str) or not re.fullmatch(
        r"[a-f0-9]{64}", parent_hash
    ):
        raise ValueError("Returned-package receipt lacks a valid original backup link")
    return record["attempt_id"], selected, parent_hash


def save_attempt(directory, record):
    """Durably record the attempt identity before transfer or device access."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=directory, delete=False
        ) as output:
            temporary = output.name
            os.chmod(output.name, 0o600)
            json.dump(record, output, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, directory / "attempt.json")
        temporary = None
    finally:
        if temporary is not None:
            os.unlink(temporary)


def wrap_owner(script, frame):
    prefix = frame.device_scripts.APP_PREFIX
    for key, name in (
        ("OWNER_DLL", "OwnerExec.dll"),
        ("OWNER_CONFIG", "OwnerExec.runtimeconfig.json"),
    ):
        prefix = prefix.replace(
            "@@" + key + "@@",
            base64.b64encode((frame.ASSETS / "payloads" / name).read_bytes()).decode(),
        )
    return (
        prefix
        + 'owner_pkg() { /usr/bin/dotnet "$stage/OwnerExec.dll" /usr/bin/pkgcmd "$@"; }\n'
        + script
        + "\nOWNER\n"
    )


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def download_archive(client, archive, destination, expected_hash):
    """Use the existing callback transport; TV SDB rejects ordinary file pulls."""
    size_result = client.capture(f"stat -c %s {shlex.quote(archive)}")
    match = re.fullmatch(r"([0-9]+)\s*\[exit:0\]\s*", size_result.output)
    if not match or not 0 < int(match[1]) <= 512 * 1024 * 1024:
        raise ValueError("Invalid or oversized remote backup")
    size = int(match[1])
    if destination.exists():
        if destination.is_symlink() or digest(destination) != expected_hash:
            raise ValueError("Existing local backup differs; refusing to overwrite it")
        return
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent, prefix=".packages-", delete=False
        ) as output:
            temporary = output.name
            for offset in range(0, size, 8 * 1024 * 1024):
                command = f"set -o pipefail; dd if={shlex.quote(archive)} bs=1048576 skip={offset // 1048576} count=8 2>/dev/null | base64"
                result = client.capture(command, timeout=180)
                encoded, separator, trailer = result.output.rpartition("\n[exit:0]")
                if not separator or trailer.strip():
                    raise ValueError("Backup chunk transfer failed")
                data = base64.b64decode("".join(encoded.split()), validate=True)
                if len(data) != min(8 * 1024 * 1024, size - offset):
                    raise ValueError("Backup chunk was truncated or oversized")
                output.write(data)
            output.flush()
            os.fsync(output.fileno())
        from pathlib import Path

        if digest(Path(temporary)) != expected_hash:
            raise ValueError("Downloaded package backup checksum mismatch")
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None:
            os.unlink(temporary)


def run(args, frame):
    import asyncio

    from samsung_tv_root.archive_root import probe
    from samsung_tv_root.sdb import SdbClient, find_sdb

    operation = args.operation
    backup = args.backup_dir
    archive = (
        "/opt/usr/home/owner/share/tmp/samshush-package-backup-"
        + secrets.token_hex(8)
        + ".tar.gz"
    )
    backup_hash = ""
    attempt_id = ""
    selected_packages = None
    parent_backup_hash = ""
    attempt_record = None
    if (
        operation in ("prepare", "prepare-returned", "backup", "remove")
        and backup is None
    ):
        raise ValueError(
            "--backup-dir is required for prepare/prepare-returned/backup/remove"
        )
    if operation in ("prepare", "prepare-returned"):
        backup.mkdir(mode=0o700, parents=True, exist_ok=False)
        if operation == "prepare-returned":
            attempt_id = secrets.token_hex(8)
            attempt_record = {
                "schema": "samshush-returned-packages-v1",
                "attempt_id": attempt_id,
                "tv": str(args.tv),
                "build": frame.policy()["build"],
            }
            save_attempt(backup, attempt_record)
    elif operation in ("backup", "remove"):
        if not backup.is_dir() or backup.is_symlink():
            raise ValueError("Backup/removal requires the original private directory")
    if operation == "backup":
        descriptor = backup / "attempt.json"
        if not descriptor.exists():
            descriptor = backup / "manifest.json"
        if descriptor.exists():
            saved = json.loads(descriptor.read_text())
            if descriptor.name == "attempt.json" or "attempt_id" in saved:
                attempt_id, selected_packages, parent_backup_hash = attempt_context(
                    saved, str(args.tv), frame.policy()["build"], pending=True
                )
                attempt_record = saved
    elif operation == "remove":
        manifest = json.loads((backup / "manifest.json").read_text())
        if "attempt_id" in manifest:
            attempt_id, selected_packages, parent_backup_hash = attempt_context(
                manifest, str(args.tv), frame.policy()["build"]
            )
        if (
            manifest.get("packages") != list(selected_packages or PACKAGES)
            or manifest.get("build") != frame.policy()["build"]
            or manifest.get("tv") != str(args.tv)
        ):
            raise ValueError("Backup belongs to another TV, build or package selection")
        backup_hash = digest(backup / "packages.tar.gz")
        if backup_hash != manifest["sha256"]:
            raise ValueError("Local backup checksum changed")

    args.logs.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    report = args.logs / f"{stamp}-packages-{operation}-{secrets.token_hex(3)}.json"
    evidence = {
        "action": "packages",
        "operation": operation,
        "completed": False,
        "results": [],
    }

    def execute(action):
        result = asyncio.run(
            probe(
                str(args.tv),
                callback_host=str(args.callback_host) if args.callback_host else None,
                accept_timeout=180,
                command_timeout=180,
                commands=(
                    frame.wrapped(
                        wrap_owner(
                            render(
                                action,
                                frame.policy(),
                                archive=archive,
                                backup_hash=backup_hash,
                                attempt_id=attempt_id,
                                selected_packages=selected_packages,
                                parent_backup_hash=parent_backup_hash,
                            ),
                            frame,
                        )
                    ),
                ),
                payloads=frame.ASSETS / "payloads",
            )
        )
        evidence["results"].append(result)
        for command in result.get("commands", []):
            print(command.get("stdout", ""), end="", flush=True)
            print(command.get("stderr", ""), end="", flush=True)
        frame.check_result(result)
        return result

    try:
        result = execute(operation)
        if operation in ("prepare", "prepare-returned", "backup"):
            output = "\n".join(
                command.get("stdout", "") for command in result["commands"]
            )
            matches = re.findall(
                r"^BACKUP_SHA256 ([a-f0-9]{64})$", output, re.MULTILINE
            )
            if len(matches) != 1:
                raise ValueError("Missing or ambiguous device backup checksum")
            backup_hash = matches[0]
            if attempt_id:
                ids = re.findall(
                    r"^RETURNED_ATTEMPT ([a-f0-9]{16})$", output, re.MULTILINE
                )
                parents = re.findall(
                    r"^PARENT_BACKUP_SHA256 ([a-f0-9]{64})$", output, re.MULTILINE
                )
                selected = re.findall(
                    r"^RETURNED_PACKAGE ([A-Za-z0-9_.-]+)$", output, re.MULTILINE
                )
                validate_selection(selected)
                if (
                    ids != [attempt_id]
                    or len(parents) != 1
                    or (selected_packages is not None and selected != selected_packages)
                    or (parent_backup_hash and parents[0] != parent_backup_hash)
                ):
                    raise ValueError("Returned-package backup identity changed")
                selected_packages = selected
                parent_backup_hash = parents[0]
                attempt_record = {
                    "schema": "samshush-returned-packages-v1",
                    "attempt_id": attempt_id,
                    "packages": selected_packages,
                    "parent_backup_sha256": parent_backup_hash,
                    "tv": str(args.tv),
                    "build": frame.policy()["build"],
                }
                save_attempt(backup, attempt_record)
            if operation == "backup":
                paths = re.findall(
                    r"^BACKUP_PATH (/opt/usr/home/owner/share/tmp/samshush-package-backup-[a-f0-9]{16}\.tar\.gz)$",
                    output,
                    re.MULTILINE,
                )
                if len(paths) != 1:
                    raise ValueError("Invalid prepared backup path")
                archive = paths[0]
            client = SdbClient(find_sdb(), str(args.tv), timeout=180)
            client.connect()
            client.require_device()
            local_archive = backup / "packages.tar.gz"
            download_archive(client, archive, local_archive, backup_hash)
            manifest = {
                "packages": list(selected_packages or PACKAGES),
                "build": frame.policy()["build"],
                "tv": str(args.tv),
                "sha256": backup_hash,
                "created_utc": stamp,
                "recovery_limit": "Installed-file and metadata archive; not a guaranteed reinstallable signed TPK.",
            }
            if attempt_record is not None:
                manifest.update(attempt_record)
            manifest_path = backup / "manifest.json"
            if manifest_path.exists():
                saved = json.loads(manifest_path.read_text())
                if any(
                    saved.get(k) != manifest.get(k)
                    for k in (
                        "sha256",
                        "packages",
                        "build",
                        "tv",
                        "attempt_id",
                        "parent_backup_sha256",
                        "schema",
                    )
                ):
                    raise ValueError("Existing backup manifest differs")
            with manifest_path.open("w") as output_file:
                os.chmod(output_file.name, 0o600)
                json.dump(manifest, output_file, indent=2)
                output_file.flush()
                os.fsync(output_file.fileno())
            with local_archive.open("rb") as archive_file:
                os.fsync(archive_file.fileno())
            execute("acknowledge")
        evidence["completed"] = True
    except Exception as error:
        evidence["error"] = str(error)
        print(
            f"FAILED: {error}. Inspect the retained receipt before retrying.",
            flush=True,
        )
    finally:
        with report.open("x") as output_file:
            os.chmod(output_file.name, 0o600)
            json.dump(evidence, output_file, indent=2)
        print(f"Report: {report}", flush=True)
    return 0 if evidence["completed"] else 1
