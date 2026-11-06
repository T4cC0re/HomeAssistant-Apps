"""Mount one user's home, restrict access, then start OpenCode."""

import argparse
import ctypes
import errno
import json
import os
from pathlib import Path
import re
import sys


HOME = Path("/home/opencode")
APP_DIR = Path(__file__).resolve().parent
MS_NOSUID, MS_NODEV, MS_BIND, MS_REC, MS_PRIVATE = 2, 4, 4096, 16384, 1 << 18
CLONE_NEWNS = 0x00020000
AT_FDCWD, AT_RECURSIVE = -100, 0x8000
MOUNT_ATTR_RDONLY, MOUNT_ATTR_NOSUID, MOUNT_ATTR_NODEV = 1, 2, 4
PR_SET_NO_NEW_PRIVS, PR_CAPBSET_DROP, PR_CAP_AMBIENT = 38, 24, 47
PR_CAP_AMBIENT_CLEAR_ALL = 4
CAP_SYS_ADMIN = 21


class SandboxOperationError(OSError):
    """Keep the failed kernel operation available for diagnostics."""

    def __init__(self, number: int, operation: str, target: Path | None = None,
                 flags: int | None = None):
        super().__init__(number, f"{operation}: {os.strerror(number)}")
        self.operation = operation
        self.target = target
        self.flags = flags


class MountAttr(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in ("attr_set", "attr_clr", "propagation", "userns_fd")]


class CapHeader(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]


class CapData(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint32) for name in ("effective", "permitted", "inheritable")]


def checked(result: int, operation: str, *, target: Path | None = None,
            flags: int | None = None) -> None:
    """Stop if a required kernel operation fails."""
    if result != 0:
        number = ctypes.get_errno()
        raise SandboxOperationError(number, operation, target, flags)


def log_failure(error: Exception) -> None:
    """Log selected process details without environment or response data."""
    def read_proc(path: str) -> str:
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            return "unavailable"

    status = {}
    for line in read_proc("/proc/self/status").splitlines():
        key, separator, value = line.partition(":")
        if separator and key in {"CapEff", "Seccomp", "NoNewPrivs"}:
            status[key] = value.strip()

    def status_number(key: str, base: int = 10) -> int | str:
        try:
            return int(status[key], base)
        except (KeyError, ValueError):
            return "unavailable"

    capabilities = status_number("CapEff", 16)
    target = getattr(error, "target", None)
    flags = getattr(error, "flags", None)
    details = {
        "operation": getattr(error, "operation", "sandbox setup"),
        "errno": getattr(error, "errno", None),
        "target": str(target) if target is not None else None,
        "flags": f"0x{flags:x}" if flags is not None else None,
        "apparmor_label": read_proc("/proc/self/attr/current"),
        "euid": os.geteuid(),
        "sys_admin": bool(capabilities & (1 << CAP_SYS_ADMIN)) if isinstance(capabilities, int) else "unavailable",
        "seccomp": status_number("Seccomp"),
        "no_new_privs": status_number("NoNewPrivs"),
    }
    print("Sandbox diagnostics: " + json.dumps(details, sort_keys=True), file=sys.stderr)


def mount(libc, source: str | None, target: Path, flags: int,
          filesystem: str | None = None, options: str | None = None) -> None:
    """Mount a path in this process's private mount namespace."""
    checked(libc.mount(
        os.fsencode(source) if source else None, os.fsencode(target),
        os.fsencode(filesystem) if filesystem else None, ctypes.c_ulong(flags),
        os.fsencode(options) if options else None,
    ), f"mount {target}", target=target, flags=flags)


def private_tmpfs(libc, target: Path, mode: int = 0o755) -> None:
    """Hide a container directory behind an empty private filesystem."""
    mount(libc, "tmpfs", target, MS_NOSUID | MS_NODEV, "tmpfs", f"mode={mode:o}")


def read_only(libc, target: Path) -> None:
    """Protect a system directory and each of its nested mounts."""
    mount(libc, str(target), target, MS_BIND | MS_REC)
    attr = MountAttr(MOUNT_ATTR_RDONLY | MOUNT_ATTR_NOSUID | MOUNT_ATTR_NODEV, 0, 0, 0)
    checked(libc.mount_setattr(AT_FDCWD, os.fsencode(target), AT_RECURSIVE,
                              ctypes.byref(attr), ctypes.sizeof(attr)), f"protect {target}",
            target=target, flags=AT_RECURSIVE)


def sandbox_profile() -> str:
    """Use the profile name that Supervisor assigned to this app."""
    current = Path("/proc/self/attr/current").read_text().strip()
    match = re.fullmatch(r"([A-Za-z0-9_.-]+) \(enforce\)", current)
    if match is None:
        raise RuntimeError("The app requires its enforced AppArmor profile.")
    return match[1] + "//sandbox"


def setup_mounts(libc, source: Path, uid: int) -> None:
    """Give the child a private filesystem view without changing the gateway."""
    if source.parent != Path("/data") or source.name in {"", ".", ".."}:
        raise ValueError("The user home must be directly below /data.")
    checked(libc.unshare(CLONE_NEWNS), "create the mount namespace")
    mount(libc, None, Path("/"), MS_REC | MS_PRIVATE)
    # Open the source in the new namespace so its mount can be bound there.
    descriptor = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if os.fstat(descriptor).st_uid != uid:
            raise ValueError("The user home has an unexpected owner.")
        private_tmpfs(libc, Path("/home"))
        HOME.mkdir(mode=0o700)
        mount(libc, f"/proc/self/fd/{descriptor}", HOME, MS_BIND)
    finally:
        os.close(descriptor)

    os.chdir(HOME)
    for path in ("/data", "/root", "/run", "/tmp", "/var/tmp", "/dev/shm"):
        private_tmpfs(libc, Path(path), 0o1777 if path in {"/tmp", "/var/tmp", "/dev/shm"} else 0o755)
    paths = {Path(path).resolve(strict=True) for path in ("/usr", "/etc", "/opt", "/bin", "/sbin", "/lib")}
    if Path("/lib64").exists():
        paths.add(Path("/lib64").resolve(strict=True))
    for path in sorted(paths, key=lambda item: (len(item.parts), str(item))):
        if not any(parent in paths for parent in path.parents):
            read_only(libc, path)


def drop_privileges(libc, uid: int) -> None:
    """Remove all capabilities and use the user's numeric identity."""
    checked(libc.prctl(PR_CAP_AMBIENT, PR_CAP_AMBIENT_CLEAR_ALL, 0, 0, 0), "clear ambient capabilities")
    for capability in range(64):
        result = libc.prctl(PR_CAPBSET_DROP, capability, 0, 0, 0)
        if result != 0 and ctypes.get_errno() != errno.EINVAL:
            checked(result, "drop a bounding capability")
    os.setgroups([])
    os.setresgid(0, 0, 0)
    os.setresuid(uid, uid, uid)
    header, data = CapHeader(0x20080522, 0), (CapData * 2)()
    checked(libc.capset(ctypes.byref(header), data), "clear process capabilities")


def enter_sandbox(source: Path, uid: int) -> None:
    """Apply all restrictions before any user code can run."""
    if sys.platform != "linux" or os.geteuid() != 0:
        raise RuntimeError("The sandbox requires Linux and a root launcher.")
    if not 10000 < uid < (1 << 32) - 1:
        raise ValueError("The sandbox user ID is invalid.")
    profile = sandbox_profile()
    libc = ctypes.CDLL(None, use_errno=True)
    if not hasattr(libc, "mount_setattr"):
        raise RuntimeError("The sandbox requires mount_setattr support.")
    setup_mounts(libc, source, uid)
    drop_privileges(libc, uid)
    with open("/proc/self/attr/current", "w", encoding="ascii") as output:
        output.write("changeprofile " + profile)
    if Path("/proc/self/attr/current").read_text().strip() != profile + " (enforce)":
        raise RuntimeError("The sandbox AppArmor profile is not enforced.")
    checked(libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0), "prevent new privileges")


def launch(source: Path, uid: int) -> None:
    """Start OpenCode only after every sandbox restriction is in place."""
    enter_sandbox(source, uid)
    os.execv("/usr/bin/mise", ["mise", "-C", str(APP_DIR), "run", "opencode"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--uid", type=int, required=True)
    args = parser.parse_args()
    try:
        launch(args.home, args.uid)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"Cannot start the OpenCode sandbox: {error}", file=sys.stderr)
        try:
            log_failure(error)
        except Exception:
            pass
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
