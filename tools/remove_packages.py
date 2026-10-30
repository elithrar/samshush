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
ROOT=/opt/usr/share/samshush-removals-v1
DB=/opt/dbspace/.pkgmgr_parser.db
LOCK=/run/frame-runtime-v1.lock
fail() { echo "ERROR: $*" >&2; exit 1; }
[[ $(id -u) == 0 && $(stat -c %u /opt/usr/home/owner) == 5001 ]] || fail 'Unexpected owner identity'
grep -Fxq 'BUILD_ID=@@BUILD@@' /etc/os-release || fail 'Firmware changed'
[[ $(readlink /proc/self/ns/mnt) == "$(readlink /proc/1/ns/mnt)" ]] || fail 'Wrong mount namespace'
[[ ! -L $ROOT && ! -L $LOCK ]] || fail 'Unsafe journal or lock'
[[ ! -e $ROOT || $(stat -c '%u:%a' "$ROOT") == 0:700 ]] || fail 'Unsafe removal journal'
packages() { cat <<'PACKAGES'
@@PACKAGES@@
PACKAGES
}
units() { cat <<'UNITS'
@@UNITS@@
UNITS
}
hash() { sha256sum "$1" | cut -d ' ' -f 1; }
package_row() {
 sqlite3 -readonly "$DB" "SELECT package,package_type,package_version,package_removable,package_readonly,root_path FROM package_info WHERE package='$1';"
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
  done < <(packages)
 done
}
check_absent() {
 local pkg path
 while IFS= read -r pkg; do
  [[ -z $(package_row "$pkg") ]] || fail "Package registered again: $pkg"
  [[ $(sqlite3 -readonly "$DB" "SELECT count(*) FROM package_app_info WHERE package='$pkg';") == 0 ]] || fail "App registered again: $pkg"
  for path in "/opt/usr/apps/$pkg" "/opt/share/packages/$pkg.xml" "/opt/usr/home/owner/apps_rw/$pkg" "/opt/usr/home/owner/apps_rw/.shared/$pkg" "/opt/usr/home/owner/apps_rw/.shared_tmp/$pkg"; do
   [[ ! -e $path && ! -L $path ]] || fail "Package/model/data path remains: $path"
  done
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
mkdir -m 700 "$ROOT"
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
 for path in "/opt/usr/apps/$pkg" "/opt/share/packages/$pkg.xml" "/opt/usr/home/owner/apps_rw/$pkg" "/opt/usr/home/owner/apps_rw/.shared/$pkg" "/opt/usr/home/owner/apps_rw/.shared_tmp/$pkg"; do
  if [[ -e $path || -L $path ]]; then printf '%s\n' "${path#/}" >> "$ROOT/paths"; fi
 done
done < <(packages)
: > "$ROOT/files.sha256"
: > "$ROOT/labels"
while IFS= read -r path; do
 find "/$path" -type f -exec sha256sum '{}' + >> "$ROOT/files.sha256"
 find "/$path" -exec chsmack '{}' + >> "$ROOT/labels"
done < "$ROOT/paths"
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


def render(action, policy, *, archive="", backup_hash=""):
    scripts = {
        "prepare": PREPARE,
        "acknowledge": ACKNOWLEDGE,
        "backup": BACKUP,
        "remove": REMOVE,
        "status": STATUS,
    }
    if action not in scripts:
        raise ValueError("Unknown package-removal action")
    if action in ("prepare", "acknowledge") and not re.fullmatch(
        r"/opt/usr/home/owner/share/tmp/samshush-package-backup-[a-f0-9]{16}\.tar\.gz",
        archive,
    ):
        raise ValueError("Invalid backup staging path")
    if action in ("remove", "acknowledge") and not re.fullmatch(
        r"[a-f0-9]{64}", backup_hash
    ):
        raise ValueError("Invalid verified backup hash")
    script = (
        COMMON
        + (MUTATION if action in ("prepare", "backup", "acknowledge", "remove") else "")
        + scripts[action]
    )
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
    }.items():
        script = script.replace("@@" + key + "@@", value)
    if "@@" in script:
        raise ValueError("Unresolved package-removal placeholder")
    return script


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
    if operation in ("prepare", "backup", "remove") and backup is None:
        raise ValueError("--backup-dir is required for prepare/backup/remove")
    if operation == "prepare":
        backup.mkdir(mode=0o700, parents=True, exist_ok=False)
    elif operation == "backup":
        if not backup.is_dir() or backup.is_symlink():
            raise ValueError("Backup retry requires the original private directory")
    elif operation == "remove":
        manifest = json.loads((backup / "manifest.json").read_text())
        if (
            manifest.get("packages") != list(PACKAGES)
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
        if operation in ("prepare", "backup"):
            output = "\n".join(
                command.get("stdout", "") for command in result["commands"]
            )
            matches = re.findall(
                r"^BACKUP_SHA256 ([a-f0-9]{64})$", output, re.MULTILINE
            )
            if len(matches) != 1:
                raise ValueError("Missing or ambiguous device backup checksum")
            backup_hash = matches[0]
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
                "packages": list(PACKAGES),
                "build": frame.policy()["build"],
                "tv": str(args.tv),
                "sha256": backup_hash,
                "created_utc": stamp,
                "recovery_limit": "Installed-file and metadata archive; not a guaranteed reinstallable signed TPK.",
            }
            manifest_path = backup / "manifest.json"
            if manifest_path.exists():
                saved = json.loads(manifest_path.read_text())
                if any(
                    saved.get(k) != manifest[k]
                    for k in ("sha256", "packages", "build", "tv")
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
