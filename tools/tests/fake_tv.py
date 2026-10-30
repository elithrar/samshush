# Executed through small command wrappers by test_runtime.py. No TV is contacted.
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

name, args = sys.argv[1], sys.argv[2:]
root = Path(os.environ["FAKE_TV"])
config = json.loads((root / "config.json").read_text())
state_path = root / "systemd.json"
state = json.loads(state_path.read_text())


def save():
    state_path.write_text(json.dumps(state))


def die(message="injected failure"):
    print(message, file=sys.stderr)
    raise SystemExit(1)


if name == "id":
    print("0" if args else "uid=0(root) gid=0(root)")
elif name == "uname":
    print("aarch64")
elif name == "readlink":
    if args[0] == "-f":
        print(Path(args[1]).resolve())
    elif args[0] in ("/proc/self/ns/mnt", "/proc/1/ns/mnt"):
        print("mnt:[123]")
    else:
        print(os.readlink(args[0]))
elif name == "sha256sum":
    p = Path(args[-1])
    print(hashlib.sha256(p.read_bytes()).hexdigest(), " ", p, sep="")
elif name == "stat":
    if args[:3] == ["-f", "-c", "%T"]:
        print(config.get("run_filesystem", "tmpfs"))
        raise SystemExit(0)
    p = Path(args[-1])
    value = p.stat()
    if args[1] == "%d:%i":
        print(f"{value.st_dev}:{value.st_ino}")
    elif args[1] == "%u:%a":
        print(f"0:{value.st_mode & 0o777:o}")
    else:
        die("unsupported stat")
elif name == "chsmack":
    print(args[0] + ' access="System"')
elif name == "cp":
    shutil.copy2(args[-2], args[-1])
elif name == "mount":
    if args[0] == "--bind":
        source, target = map(Path, args[-2:])
        count = root / "mount-count"
        number = int(count.read_text()) + 1 if count.exists() else 1
        count.write_text(str(number))
        if config.get("fail_mount") == number:
            die()
        target.rename(str(target) + ".original")
        target.symlink_to(source)
    elif args[:2] != ["-o", "remount,bind,ro"]:
        die("unsupported mount")
elif name == "umount":
    if config.get("fail_unmount"):
        die()
    p = Path(args[-1])
    p.unlink()
    Path(str(p) + ".original").rename(p)
elif name == "sleep":
    pass
elif name == "systemctl":
    with (root / "systemctl-calls.jsonl").open("a") as log:
        log.write(json.dumps(args) + "\n")
    cmd = args[0]
    if cmd == "daemon-reload":
        if config.get("fail_reload"):
            config.pop("fail_reload")
            (root / "config.json").write_text(json.dumps(config))
            die()
        raise SystemExit(0)
    units = [s for s in args[1:] if s.endswith((".service", ".path", ".busname"))]
    if cmd == "show" and config.get("reverse_show"):
        units.reverse()
    if cmd == "show" and config.get("omit_record"):
        units = units[:-1]
    if cmd == "show" and config.get("duplicate_record"):
        units += units[:1]
    for unit in units:
        entry = state.get(unit, {"active": "inactive", "load": "not-found"})
        mask = root / "units" / unit
        if cmd == "show":
            keys = [args[i + 1] for i, value in enumerate(args) if value == "-p"]
            values = {
                "Id": unit,
                "LoadState": "masked"
                if mask.is_symlink()
                else entry.get("load", "loaded"),
                "ActiveState": entry["active"],
                "MainPID": "123"
                if entry["active"] in ("active", "deactivating")
                else str(config.get("failed_main_pid", 0)),
                "UnitFileState": "masked-runtime" if mask.is_symlink() else "enabled",
            }
            for key in keys:
                if key == config.get("omit_property") or (
                    key == "MainPID" and not unit.endswith(".service")
                ):
                    continue
                value = values[key]
                if key == "MainPID" and config.get("bad_pid"):
                    value = "unknown"
                print(value if "--value" in args else key + "=" + value)
            print()
        elif cmd == "mask":
            # Assert that even a partially completed batch has all recovery rows.
            journal = (root / "state/units.tsv").read_text()
            if any(name + "\t" not in journal for name in units):
                die("incomplete recovery journal before masking")
            if config.get("fail_mask") == unit:
                die()
            mask.symlink_to("/dev/null")
        elif cmd == "unmask":
            mask.unlink(missing_ok=True)
        elif cmd == "stop":
            if config.get("fail_stop") == unit:
                die()
            if not (
                entry["active"] == "failed" and config.get("retain_failed_on_stop")
            ):
                entry["active"] = (
                    "deactivating" if config.get("hang_stop") == unit else "inactive"
                )
            state[unit] = entry
            save()
        elif cmd == "kill":
            if config.get("fail_kill") == unit:
                die()
            entry["active"] = "inactive"
            state[unit] = entry
            save()
        elif cmd == "start":
            entry["active"] = "active"
            state[unit] = entry
            save()
        elif cmd == "is-active":
            raise SystemExit(0 if entry["active"] == "active" else 3)
        elif cmd == "is-enabled":
            print("masked" if mask.is_symlink() else "enabled")
        else:
            die("unsupported systemctl: " + repr(args))

else:
    die("unsupported command: " + name)
