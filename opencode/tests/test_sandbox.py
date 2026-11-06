"""Check sandbox setup, failure handling, and optional Linux enforcement."""

import ctypes
import errno
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import sandbox


class SandboxTests(unittest.TestCase):
    def test_profile_uses_supervisor_name_and_requires_enforcement(self):
        with patch.object(Path, "read_text", return_value="local_opencode (enforce)\n"):
            self.assertEqual(sandbox.sandbox_profile(), "local_opencode//sandbox")
        for value in ("unconfined", "local_opencode (complain)", "local_opencode//sandbox (enforce)"):
            with self.subTest(value=value), patch.object(Path, "read_text", return_value=value):
                with self.assertRaises(RuntimeError):
                    sandbox.sandbox_profile()

    def test_mount_setup_binds_home_before_hiding_data(self):
        events = []
        libc = MagicMock()
        libc.unshare.side_effect = lambda *_: events.append("unshare") or 0
        with patch.object(sandbox.os, "open", side_effect=lambda *a: events.append("open") or 9), \
                patch.object(sandbox.os, "close", side_effect=lambda *_: events.append("close")), \
                patch.object(sandbox.os, "fstat", side_effect=lambda *_: events.append("owner") or MagicMock(st_uid=10001)), \
                patch.object(sandbox.os, "chdir", side_effect=lambda p: events.append(("cwd", str(p)))), \
                patch.object(Path, "mkdir"), \
                patch.object(Path, "resolve", autospec=True, side_effect=lambda p, **kw: p), \
                patch.object(sandbox, "mount", side_effect=lambda api, source, target, *args: events.append((source, str(target)))), \
                patch.object(sandbox, "private_tmpfs", side_effect=lambda _, target, *a: events.append(("hide", str(target)))), \
                patch.object(sandbox, "read_only", side_effect=lambda _, target: events.append(("readonly", str(target)))):
            sandbox.setup_mounts(libc, Path("/data/Alice"), 10001)
        self.assertEqual(events[:7], ["unshare", (None, "/"), "open", "owner", ("hide", "/home"),
                                     ("/proc/self/fd/9", "/home/opencode"), "close"])
        self.assertLess(events.index(("cwd", "/home/opencode")), events.index(("hide", "/data")))
        self.assertIn(("hide", "/dev/shm"), events)
        self.assertIn(("readonly", "/etc"), events)

    def test_setup_rejects_wrong_source_and_owner(self):
        libc = MagicMock()
        libc.unshare.return_value = 0
        for path in ("/root/Alice", "/data/Alice/child", "/data/.."):
            with self.subTest(path=path), self.assertRaises(ValueError):
                sandbox.setup_mounts(libc, Path(path), 10001)
        libc.unshare.assert_not_called()
        with patch.object(sandbox.os, "open", return_value=9), patch.object(sandbox.os, "close") as close, \
                patch.object(sandbox.os, "fstat", return_value=MagicMock(st_uid=0)), \
                patch.object(sandbox, "mount"), patch.object(sandbox, "private_tmpfs") as hide:
            with self.assertRaises(ValueError):
                sandbox.setup_mounts(libc, Path("/data/Alice"), 10001)
            close.assert_called_once_with(9)
            hide.assert_not_called()

    def test_namespace_failure_stops_before_mounting(self):
        libc = MagicMock()
        libc.unshare.return_value = -1
        ctypes.set_errno(errno.EPERM)
        with patch.object(sandbox.os, "open") as source_open, \
                patch.object(sandbox, "mount") as mount:
            with self.assertRaises(OSError):
                sandbox.setup_mounts(libc, Path("/data/Alice"), 10001)
            mount.assert_not_called()
            source_open.assert_not_called()

    def test_bind_failure_closes_source_descriptor(self):
        libc = MagicMock()
        libc.unshare.return_value = 0
        failure = sandbox.SandboxOperationError(errno.EINVAL, "mount /home/opencode")
        with patch.object(sandbox.os, "open", return_value=9), patch.object(sandbox.os, "close") as close, \
                patch.object(sandbox.os, "fstat", return_value=MagicMock(st_uid=10001)), \
                patch.object(sandbox, "private_tmpfs"), patch.object(Path, "mkdir"), \
                patch.object(sandbox, "mount", side_effect=[None, failure]):
            with self.assertRaises(sandbox.SandboxOperationError) as caught:
                sandbox.setup_mounts(libc, Path("/data/Alice"), 10001)
        self.assertIs(caught.exception, failure)
        close.assert_called_once_with(9)

    def test_read_only_covers_nested_mounts(self):
        libc = MagicMock()
        libc.mount_setattr.return_value = 0
        with patch.object(sandbox, "mount"):
            sandbox.read_only(libc, Path("/etc"))
        call = libc.mount_setattr.call_args.args
        self.assertEqual(call[2], sandbox.AT_RECURSIVE)
        self.assertEqual(call[3]._obj.attr_set, sandbox.MOUNT_ATTR_RDONLY | sandbox.MOUNT_ATTR_NOSUID | sandbox.MOUNT_ATTR_NODEV)

    def test_privileges_are_removed_before_profile_and_no_new_privileges(self):
        events = []
        libc = MagicMock()
        libc.prctl.side_effect = lambda *a: events.append(("prctl", a[0])) or 0
        label = MagicMock()
        label.__enter__.return_value.write.side_effect = lambda value: events.append(("profile", value))
        with patch.object(sandbox.sys, "platform", "linux"), patch.object(sandbox.os, "geteuid", return_value=0), \
                patch.object(sandbox.ctypes, "CDLL", return_value=libc), \
                patch.object(sandbox, "sandbox_profile", return_value="local_opencode//sandbox"), \
                patch.object(sandbox, "setup_mounts", side_effect=lambda *a: events.append("mounts")), \
                patch.object(sandbox, "drop_privileges", side_effect=lambda *a: events.append("drop")), \
                patch("builtins.open", return_value=label), \
                patch.object(Path, "read_text", return_value="local_opencode//sandbox (enforce)"):
            sandbox.enter_sandbox(Path("/data/Alice"), 10001)
        self.assertEqual(events, ["mounts", "drop", ("profile", "changeprofile local_opencode//sandbox"),
                                  ("prctl", sandbox.PR_SET_NO_NEW_PRIVS)])

    def test_drop_clears_all_capabilities_and_identities(self):
        libc = MagicMock()
        libc.prctl.return_value = libc.capset.return_value = 0
        with patch.object(sandbox.os, "setgroups") as groups, \
                patch.object(sandbox.os, "setresgid", create=True) as gid, \
                patch.object(sandbox.os, "setresuid", create=True) as uid:
            sandbox.drop_privileges(libc, 10001)
        groups.assert_called_once_with([])
        gid.assert_called_once_with(0, 0, 0)
        uid.assert_called_once_with(10001, 10001, 10001)
        self.assertEqual(len(libc.prctl.call_args_list), 65)
        capabilities = libc.capset.call_args.args[1]
        self.assertTrue(all(not value.effective and not value.permitted and not value.inheritable for value in capabilities))

    def test_launch_never_executes_after_setup_failure(self):
        with patch.object(sandbox, "enter_sandbox", side_effect=OSError("denied")), \
                patch.object(sandbox.os, "execv") as execute:
            with self.assertRaises(OSError):
                sandbox.launch(Path("/data/Alice"), 10001)
            execute.assert_not_called()


class FailureDiagnosticsTests(unittest.TestCase):
    def read_diagnostics(self, error, proc):
        def read_proc(path, *args, **kwargs):
            value = proc[str(path)]
            if isinstance(value, OSError):
                raise value
            return value

        stderr = io.StringIO()
        with patch.object(Path, "read_text", autospec=True, side_effect=read_proc), \
                patch.object(sandbox.os, "geteuid", return_value=10001), \
                patch.object(sandbox.sys, "stderr", stderr):
            sandbox.log_failure(error)
        output = stderr.getvalue()
        lines = output.splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("Sandbox diagnostics: "))
        return json.loads(lines[0].removeprefix("Sandbox diagnostics: ")), output

    def test_checked_preserves_errno_message_and_operation_context(self):
        target = Path("/home")
        flags = sandbox.MS_NOSUID | sandbox.MS_NODEV
        with patch.object(sandbox.ctypes, "get_errno", return_value=errno.EPERM):
            with self.assertRaises(sandbox.SandboxOperationError) as caught:
                sandbox.checked(-1, "mount /home", target=target, flags=flags)
        error = caught.exception
        self.assertEqual(error.errno, errno.EPERM)
        self.assertEqual(error.operation, "mount /home")
        self.assertEqual(error.target, target)
        self.assertEqual(error.flags, flags)
        self.assertEqual(str(error), str(OSError(errno.EPERM, "mount /home: " + os.strerror(errno.EPERM))))

    def test_mount_failure_preserves_actual_target_and_flags(self):
        libc = MagicMock()
        libc.mount.return_value = -1
        flags = sandbox.MS_REC | sandbox.MS_PRIVATE
        with patch.object(sandbox.ctypes, "get_errno", return_value=errno.EPERM):
            with self.assertRaises(sandbox.SandboxOperationError) as caught:
                sandbox.mount(libc, None, Path("/"), flags)
        error = caught.exception
        self.assertEqual(error.operation, "mount /")
        self.assertEqual(error.errno, errno.EPERM)
        self.assertEqual(error.target, Path("/"))
        self.assertEqual(error.flags, flags)
        self.assertEqual(libc.mount.call_args.args[3].value, flags)

    def test_diagnostics_report_only_selected_process_fields(self):
        error = sandbox.SandboxOperationError(errno.EPERM, "mount /home", Path("/home"), 6)
        proc = {
            "/proc/self/attr/current": "local_opencode (enforce)\n",
            "/proc/self/status": (
                "Name:\tprivate-status-value\n"
                "CapEff:\t0000000000200000\n"
                "Seccomp:\t2\n"
                "NoNewPrivs:\t1\n"
                "Unrelated:\tprivate-status-secret\n"
            ),
        }
        with patch.dict(sandbox.os.environ, {
            "SUPERVISOR_TOKEN": "private-supervisor-token",
            "LITELLM_MASTER_KEY": "private-model-key",
        }):
            record, output = self.read_diagnostics(error, proc)
        self.assertEqual(record, {
            "operation": "mount /home", "errno": errno.EPERM,
            "target": "/home", "flags": "0x6",
            "apparmor_label": "local_opencode (enforce)", "euid": 10001,
            "sys_admin": True, "seccomp": 2, "no_new_privs": 1,
        })
        for value in ("private-status-value", "private-status-secret", "private-supervisor-token",
                      "private-model-key", "CapEff", "SUPERVISOR_TOKEN", "LITELLM_MASTER_KEY"):
            self.assertNotIn(value, output)

    def test_diagnostics_handle_missing_proc_support(self):
        error = sandbox.SandboxOperationError(errno.EPERM, "create the mount namespace")
        for problem in (FileNotFoundError(errno.ENOENT, "missing"), PermissionError(errno.EACCES, "denied")):
            with self.subTest(problem=type(problem).__name__):
                record, _ = self.read_diagnostics(error, {
                    "/proc/self/attr/current": problem,
                    "/proc/self/status": problem,
                })
                self.assertEqual(record, {
                    "operation": "create the mount namespace", "errno": errno.EPERM,
                    "target": None, "flags": None, "apparmor_label": "unavailable",
                    "euid": 10001, "sys_admin": "unavailable", "seccomp": "unavailable",
                    "no_new_privs": "unavailable",
                })

    def test_diagnostics_handle_missing_and_malformed_status_values(self):
        error = sandbox.SandboxOperationError(errno.EPERM, "create the mount namespace")
        cases = (
            ("missing", "", ("unavailable", "unavailable", "unavailable")),
            ("malformed capabilities", "CapEff:\tnot-hex\nSeccomp:\t2\nNoNewPrivs:\t1\n",
             ("unavailable", 2, 1)),
            ("malformed numeric fields", "CapEff:\t0000000000000000\nSeccomp:\tunknown\nNoNewPrivs:\tunknown\n",
             (False, "unavailable", "unavailable")),
        )
        for name, status, expected in cases:
            with self.subTest(name=name):
                record, _ = self.read_diagnostics(error, {
                    "/proc/self/attr/current": "local_opencode (enforce)\n",
                    "/proc/self/status": status,
                })
                self.assertEqual(tuple(record[key] for key in ("sys_admin", "seccomp", "no_new_privs")), expected)

    def test_main_preserves_original_failure_when_diagnostics_raise(self):
        error = sandbox.SandboxOperationError(errno.EPERM, "mount /home", Path("/home"), 6)
        stderr = io.StringIO()
        seen = []

        def fail_diagnostics(problem):
            seen.append((problem, stderr.getvalue()))
            raise RuntimeError("The diagnostics are unavailable.")

        with patch.object(sandbox.sys, "argv", ["sandbox.py", "--home", "/data/Alice", "--uid", "10001"]), \
                patch.object(sandbox.sys, "stderr", stderr), \
                patch.object(sandbox, "enter_sandbox", side_effect=error), \
                patch.object(sandbox, "log_failure", side_effect=fail_diagnostics), \
                patch.object(sandbox.os, "execv") as execute:
            result = sandbox.main()
        original = f"Cannot start the OpenCode sandbox: {error}\n"
        self.assertEqual(result, 1)
        self.assertEqual(stderr.getvalue(), original)
        self.assertEqual(seen, [(error, original)])
        execute.assert_not_called()


@unittest.skipUnless(sys.platform == "linux" and os.environ.get("OPENCODE_SANDBOX_MOUNT_TESTS") == "1",
                     "Requires Linux and SYS_ADMIN; runs mount setup in a child process")
class MountNamespaceTests(unittest.TestCase):
    def test_home_bind_uses_new_namespace_and_does_not_change_parent(self):
        with tempfile.TemporaryDirectory(prefix="sandbox-mount-test-", dir="/data") as directory, \
                tempfile.TemporaryDirectory(prefix="sandbox-parent-", dir="/tmp") as parent_tmp:
            source = Path(directory)
            (source / "marker").write_text("persistent")
            os.chown(source, 65000, 0)
            marker = Path(parent_tmp) / "parent-marker"
            marker.write_text("outside")
            before = {path: os.statvfs(path).f_flag for path in ("/etc", "/usr", "/opt")}
            script = '''
import ctypes, json, os, pathlib, sys
sys.path.insert(0, sys.argv[1])
import sandbox
libc = ctypes.CDLL(None, use_errno=True)
sandbox.setup_mounts(libc, pathlib.Path(sys.argv[2]), 65000)
home = pathlib.Path('/home/opencode')
assert os.getcwd() == str(home)
assert (home / 'marker').read_text() == 'persistent'
assert not pathlib.Path(sys.argv[2]).exists()
assert not pathlib.Path(sys.argv[3]).exists()
for path in ('/etc', '/usr', '/opt', '/etc/hosts'):
    assert os.statvfs(path).f_flag & os.ST_RDONLY, path
sandbox.drop_privileges(libc, 65000)
(home / 'created').write_text('saved')
assert os.geteuid() == 65000
print(json.dumps({'mounts': 'isolated'}))
'''
            result = subprocess.run([sys.executable, "-I", "-c", script, str(sandbox.APP_DIR), directory, str(marker)],
                                    cwd="/", capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {"mounts": "isolated"})
            self.assertEqual((source / "created").read_text(), "saved")
            self.assertEqual(marker.read_text(), "outside")
            self.assertEqual({path: os.statvfs(path).f_flag for path in before}, before)


@unittest.skipUnless(sys.platform == "linux" and os.environ.get("OPENCODE_SANDBOX_INTEGRATION") == "1",
                     "Requires the installed app's Linux capabilities and enforced AppArmor profile")
class EnforcementTests(unittest.TestCase):
    def test_home_alias_hidden_data_and_read_only_tools(self):
        with tempfile.TemporaryDirectory(prefix="sandbox-test-", dir="/data") as directory:
            source = Path(directory)
            (source / "marker").write_text("persistent")
            os.chown(source, 65000, 0)
            script = '''
import json, os, pathlib, sys
sys.path.insert(0, sys.argv[1])
import sandbox
sandbox.enter_sandbox(pathlib.Path(sys.argv[2]), 65000)
home = pathlib.Path('/home/opencode')
assert os.getcwd() == str(home)
assert (home / 'marker').read_text() == 'persistent'
(home / 'created').write_text('saved')
for name in ('/data/options.json', sys.argv[2] + '/marker', '/root/.config'):
    try:
        pathlib.Path(name).read_bytes()
    except OSError:
        pass
    else:
        raise AssertionError('Hidden path is accessible: ' + name)
try:
    pathlib.Path('/opt/mise/sandbox-write-check').write_text('forbidden')
except OSError:
    pass
else:
    raise AssertionError('Shared tools are writable')
status = pathlib.Path('/proc/self/status').read_text()
assert 'NoNewPrivs:\\t1' in status
for line in status.splitlines():
    if line.startswith(('CapEff:', 'CapPrm:', 'CapInh:', 'CapBnd:', 'CapAmb:')):
        assert int(line.split()[1], 16) == 0, line
print(json.dumps({'sandbox': 'enforced'}))
'''
            result = subprocess.run([sys.executable, "-I", "-c", script, str(sandbox.APP_DIR), directory],
                                    cwd="/", capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {"sandbox": "enforced"})
            self.assertEqual((source / "created").read_text(), "saved")


if __name__ == "__main__":
    unittest.main()
