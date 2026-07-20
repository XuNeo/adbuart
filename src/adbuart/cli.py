#!/usr/bin/env python3
import argparse
import errno
import fcntl
import os
import pty
import re
import select
import signal
import subprocess
import sys
import termios
import time
import tty

try:
    from . import __version__
except ImportError:  # pragma: no cover - direct script execution fallback
    __version__ = "0.1.1"

DEFAULT_REMOTE_TTY = "/dev/ttyHS1"
DEFAULT_BAUD = "2000000"
DEFAULT_CMD_BEFORE = "setprop persist.rtoslogd.uart 0"
DEFAULT_CMD_AFTER = "setprop persist.rtoslogd.uart 1"
BAUD_SYNC_POLL_MS = 20
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
BAUD_TO_TERMIOS = {
    name[1:]: value
    for name, value in vars(termios).items()
    if name.startswith("B") and name[1:].isdigit()
}
TERMIOS_TO_BAUD = {value: name for name, value in BAUD_TO_TERMIOS.items()}


def warn_stderr(message):
    print(f"warning: {message}", file=sys.stderr)


def validate_name(name: str) -> str:
    if not name or name in (".", ".."):
        raise ValueError("--name must be a non-empty basename")
    if "/" in name or name.strip() != name or not SAFE_NAME_RE.match(name):
        raise ValueError("--name may contain only letters, digits, dot, underscore, and dash")
    return name


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Expose an Android device UART as a local /tmp/<name> PTY over adb.",
        epilog=f"version: {__version__}",
    )
    parser.add_argument("--version", action="version", version=f"adbuart {__version__}")
    parser.add_argument(
        "-s",
        "--serial",
        help="ADB serial, passed to every adb command with -s; auto-selects when exactly one device is connected",
    )
    parser.add_argument("-n", "--name", required=True, type=validate_name, help="Local node name created as /tmp/<name>")
    parser.add_argument("--remote-tty", default=DEFAULT_REMOTE_TTY, help=f"Remote UART device path (default: {DEFAULT_REMOTE_TTY})")
    parser.add_argument("--baud", default=DEFAULT_BAUD, help=f"Remote UART baud rate for stty (default: {DEFAULT_BAUD})")
    parser.add_argument(
        "--cmd-before",
        action="append",
        help=f"Remote shell command before bridging; repeatable (default: {DEFAULT_CMD_BEFORE!r})",
    )
    parser.add_argument(
        "--cmd-after",
        action="append",
        help=f"Remote shell command during teardown; repeatable (default: {DEFAULT_CMD_AFTER!r} when UART logging was enabled)",
    )
    return parser.parse_args(argv)


def build_adb_args(serial: str, *args: str) -> list[str]:
    return ["adb", "-s", serial, *args]


def list_adb_device_serials() -> list[str]:
    result = subprocess.run(
        ["adb", "devices"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    serials = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices attached"):
            continue
        fields = line.split()
        if len(fields) >= 2 and fields[1] == "device":
            serials.append(fields[0])
    return serials


def resolve_serial(serial: str | None) -> str:
    if serial:
        return serial
    try:
        serials = list_adb_device_serials()
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or exc.stdout.strip() or str(exc)
        raise RuntimeError(f"failed to run adb devices: {detail}") from exc
    if len(serials) == 1:
        return serials[0]
    if not serials:
        raise RuntimeError("no adb devices in state 'device'; pass --serial explicitly")
    raise RuntimeError(
        "multiple adb devices in state 'device' ("
        + ", ".join(serials)
        + "); pass --serial explicitly"
    )


def build_stty_command(remote_tty: str, baud: str) -> str:
    return f"stty -F {remote_tty} {baud} raw -echo -crtscts cs8 -parenb -cstopb min 1 time 0"


def build_remote_command(remote_tty: str = DEFAULT_REMOTE_TTY, baud: str = DEFAULT_BAUD) -> str:
    return (
        f"{build_stty_command(remote_tty, baud)} || exit $?; "
        "trap 'kill \"$rx\" 2>/dev/null; wait \"$rx\" 2>/dev/null' EXIT HUP INT TERM; "
        f"cat {remote_tty} & rx=$!; "
        f"cat > {remote_tty}"
    )


def termios_baud_name(fd):
    speed = termios.tcgetattr(fd)[5]
    return TERMIOS_TO_BAUD.get(speed)


def set_fd_baud(fd, baud: str) -> bool:
    speed = BAUD_TO_TERMIOS.get(str(baud))
    if speed is None:
        return False
    attrs = termios.tcgetattr(fd)
    attrs[4] = speed
    attrs[5] = speed
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    return True


def sync_pty_baud_if_changed(slave_fd, adb, remote_tty, last_baud):
    current_baud = termios_baud_name(slave_fd)
    if current_baud is None or current_baud == last_baud:
        return last_baud
    adb.shell(build_stty_command(remote_tty, current_baud))
    return current_baud


class BaudSynchronizer:
    def __init__(self, slave_fd, adb, remote_tty, initial_baud, poll_ms=BAUD_SYNC_POLL_MS):
        self.slave_fd = slave_fd
        self.adb = adb
        self.remote_tty = remote_tty
        self.last_baud = initial_baud
        self.poll_interval = poll_ms / 1000.0
        self.next_poll = 0.0

    def sync_if_due(self):
        now = time.monotonic()
        if now < self.next_poll:
            return
        self.next_poll = now + self.poll_interval
        self.last_baud = sync_pty_baud_if_changed(self.slave_fd, self.adb, self.remote_tty, self.last_baud)

    def sync_now(self):
        self.next_poll = time.monotonic() + self.poll_interval
        self.last_baud = sync_pty_baud_if_changed(self.slave_fd, self.adb, self.remote_tty, self.last_baud)


class BridgePaths:
    def __init__(self, name, tmp_dir="/tmp"):
        safe_name = validate_name(name)
        self.node_path = os.path.join(tmp_dir, safe_name)
        self.lock_path = os.path.join(tmp_dir, safe_name + ".lock")


def create_symlink(node_path, slave_path, lock=None):
    if os.path.lexists(node_path):
        if not os.path.islink(node_path):
            raise FileExistsError(f"refusing to overwrite non-symlink: {node_path}")
        current_target = os.readlink(node_path)
        if os.path.exists(node_path):
            raise FileExistsError(f"refusing to overwrite live symlink: {node_path} -> {current_target}")
        os.unlink(node_path)
    os.symlink(slave_path, node_path)


def remove_symlink_if_owned(node_path, slave_path):
    if not os.path.islink(node_path):
        return
    if os.readlink(node_path) == slave_path:
        os.unlink(node_path)


class LocalLock:
    def __init__(self, lock_path):
        self.lock_path = lock_path
        self.fd = None

    def acquire(self):
        self.fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(self.fd)
            self.fd = None
            raise RuntimeError(f"lock already held: {self.lock_path}") from exc

    def is_held(self):
        return self.fd is not None

    def release(self):
        if self.fd is None:
            return
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)
        self.fd = None


class Adb:
    def __init__(self, serial):
        self.serial = serial

    def run(self, *args, check=True):
        return subprocess.run(
            build_adb_args(self.serial, *args),
            check=check,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def shell(self, command, check=True):
        return self.run("shell", command, check=check)

    def popen_shell_t(self, command):
        return subprocess.Popen(
            build_adb_args(self.serial, "shell", "-T", command),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )


def run_remote_commands(adb, commands):
    for command in commands:
        adb.shell(command)


def get_rtos_uart_state(adb):
    result = adb.shell("getprop persist.rtoslogd.uart")
    return result.stdout.strip()


def set_rtos_uart_state(adb, state):
    adb.shell(f"setprop persist.rtoslogd.uart {state}")


def restore_rtos_logging(adb, startup_state):
    if startup_state != "1":
        return True
    try:
        set_rtos_uart_state(adb, "1")
        return True
    except Exception as exc:
        warn_stderr(f"failed to restore RTOS logging: {exc}")
        return False


def run_cmd_after(adb, startup_state, commands):
    try:
        if commands is None:
            return restore_rtos_logging(adb, startup_state)
        run_remote_commands(adb, commands)
        return True
    except Exception as exc:
        warn_stderr(f"failed to run teardown commands: {exc}")
        return False


COPY_SIZE = 65536


def read_chunk(fd):
    while True:
        try:
            return os.read(fd, COPY_SIZE)
        except InterruptedError:
            continue


def write_all(fd, data):
    view = memoryview(data)
    offset = 0
    while offset < len(view):
        try:
            written = os.write(fd, view[offset:])
        except InterruptedError:
            continue
        if written == 0:
            raise OSError(errno.EPIPE, "write returned zero")
        offset += written


def copy_available(src_fd, dst_fd):
    data = read_chunk(src_fd)
    if not data:
        raise EOFError("input closed")
    write_all(dst_fd, data)
    return len(data)


def drain_to_eof(src_fd, dst_fd):
    while True:
        data = read_chunk(src_fd)
        if not data:
            return
        write_all(dst_fd, data)


def forward_loop(pty_fd, adb_proc, baud_sync=None):
    adb_stdout_fd = adb_proc.stdout.fileno()
    adb_stdin_fd = adb_proc.stdin.fileno()
    adb_stderr_fd = adb_proc.stderr.fileno()
    poller = select.poll()
    poller.register(pty_fd, select.POLLIN | select.POLLERR | select.POLLHUP)
    poller.register(adb_stdout_fd, select.POLLIN | select.POLLERR | select.POLLHUP)
    poller.register(adb_stderr_fd, select.POLLIN | select.POLLERR | select.POLLHUP)

    def drain_adb_pipes():
        drain_to_eof(adb_stdout_fd, pty_fd)
        drain_to_eof(adb_stderr_fd, sys.stderr.fileno())

    while True:
        if adb_proc.poll() is not None:
            drain_adb_pipes()
            return
        if baud_sync is not None:
            baud_sync.sync_if_due()
        events = poller.poll(BAUD_SYNC_POLL_MS if baud_sync is not None else 1000)
        for fd, event in events:
            if adb_proc.poll() is not None:
                drain_adb_pipes()
                return
            if fd == pty_fd:
                if event & select.POLLIN:
                    if baud_sync is not None:
                        baud_sync.sync_now()
                    copy_available(pty_fd, adb_stdin_fd)
                elif event & (select.POLLERR | select.POLLHUP):
                    raise EOFError(f"fd closed: {fd}")
            elif fd == adb_stdout_fd:
                if event & select.POLLIN:
                    copy_available(adb_stdout_fd, pty_fd)
                elif event & (select.POLLERR | select.POLLHUP):
                    raise EOFError(f"fd closed: {fd}")
            elif fd == adb_stderr_fd:
                if event & select.POLLIN:
                    copy_available(adb_stderr_fd, sys.stderr.fileno())
                elif event & (select.POLLERR | select.POLLHUP):
                    raise EOFError(f"fd closed: {fd}")


class BridgeRuntime:
    def __init__(self, adb, node_path, slave_path, lock, startup_rtos_state, cmd_after=None):
        self.adb = adb
        self.node_path = node_path
        self.slave_path = slave_path
        self.lock = lock
        self.startup_rtos_state = startup_rtos_state
        self.cmd_after = cmd_after
        self.adb_proc = None
        self.pty_fd = None
        self.slave_fd = None
        self.restore_ok = None
        self._torn_down = False

    def teardown(self, reason):
        if self._torn_down:
            return self.restore_ok
        self._torn_down = True
        if self.adb_proc is not None and self.adb_proc.poll() is None:
            try:
                self.adb_proc.terminate()
            except Exception as exc:
                warn_stderr(f"failed to terminate adb bridge: {exc}")
            try:
                self.adb_proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    self.adb_proc.kill()
                except Exception as exc:
                    warn_stderr(f"failed to kill adb bridge: {exc}")
                try:
                    self.adb_proc.wait(timeout=2)
                except subprocess.TimeoutExpired as exc:
                    warn_stderr(f"adb bridge did not exit after kill: {exc}")
                except Exception as exc:
                    warn_stderr(f"failed waiting for killed adb bridge: {exc}")
            except Exception as exc:
                warn_stderr(f"failed waiting for adb bridge: {exc}")
        if self.pty_fd is not None:
            try:
                os.close(self.pty_fd)
            except OSError:
                pass
            self.pty_fd = None
        if self.slave_fd is not None:
            try:
                os.close(self.slave_fd)
            except OSError:
                pass
            self.slave_fd = None
        remove_symlink_if_owned(self.node_path, self.slave_path)
        self.restore_ok = run_cmd_after(self.adb, self.startup_rtos_state, self.cmd_after)
        self.lock.release()
        print(f"teardown complete: {reason}", file=sys.stderr)
        return self.restore_ok


_active_runtime = None


def set_raw(fd):
    tty.setraw(fd, when=termios.TCSANOW)


class PrepareRemoteUartError(RuntimeError):
    def __init__(self, message, startup_state):
        super().__init__(message)
        self.startup_state = startup_state


def prepare_remote_uart(adb, cmd_before=None, cmd_after=None):
    adb.run("wait-for-device")
    adb.run("root")
    adb.run("wait-for-device")
    startup_state = ""
    if cmd_after is None:
        startup_state = get_rtos_uart_state(adb)
    try:
        if cmd_before is None:
            set_rtos_uart_state(adb, "0")
        else:
            run_remote_commands(adb, cmd_before)
    except BaseException as exc:
        exc.startup_state = startup_state
        raise
    return startup_state


def _signal_handler(signum, frame):
    if _active_runtime is not None:
        _active_runtime.teardown(f"signal {signum}")
    raise SystemExit(128 + signum)


def install_signal_handlers():
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)


def _fd_is_tty(fd):
    try:
        return os.isatty(fd)
    except OSError:
        return False


def close_fd_if_owned(fd, owned):
    if not owned or fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass


def cleanup_before_runtime(paths, lock, adb, startup_state, cmd_after, master_fd, master_fd_owned, slave_fd, slave_fd_owned, slave_path):
    close_fd_if_owned(master_fd, master_fd_owned)
    close_fd_if_owned(slave_fd, slave_fd_owned)
    if slave_path is not None:
        remove_symlink_if_owned(paths.node_path, slave_path)
    run_cmd_after(adb, startup_state, cmd_after)
    lock.release()


def run_bridge(args, tmp_dir="/tmp"):
    global _active_runtime
    paths = BridgePaths(args.name, tmp_dir=tmp_dir)
    lock = LocalLock(paths.lock_path)
    lock.acquire()
    adb = Adb(args.serial)
    startup_state = ""
    master_fd = None
    master_fd_owned = False
    slave_fd = None
    slave_fd_owned = False
    slave_path = None
    runtime = None
    try:
        startup_state = prepare_remote_uart(adb, args.cmd_before, args.cmd_after)
        master_fd, slave_fd = pty.openpty()
        slave_path = os.ttyname(slave_fd)
        slave_fd_owned = _fd_is_tty(slave_fd)
        master_fd_owned = _fd_is_tty(master_fd)
        if master_fd_owned:
            set_raw(master_fd)
        if slave_fd_owned and not set_fd_baud(slave_fd, args.baud):
            warn_stderr(f"cannot observe local PTY baud {args.baud}; remote baud sync disabled until a supported rate is set")
        create_symlink(paths.node_path, slave_path, lock=lock)
        runtime = BridgeRuntime(adb, paths.node_path, slave_path, lock, startup_state, args.cmd_after)
        runtime.pty_fd = master_fd if master_fd_owned else None
        runtime.slave_fd = slave_fd if slave_fd_owned else None
        baud_sync = BaudSynchronizer(slave_fd, adb, args.remote_tty, args.baud) if slave_fd_owned else None
        runtime.adb_proc = adb.popen_shell_t(build_remote_command(args.remote_tty, args.baud))
        _active_runtime = runtime
        print(f"PTY ready: {paths.node_path}", file=sys.stderr)
        print(f"Target: adb -s {args.serial} {args.remote_tty}", file=sys.stderr)
        print(f"Use another terminal: minicom -D {paths.node_path} -b {args.baud}", file=sys.stderr)
        print("Press Ctrl-C here to teardown.", file=sys.stderr)
        forward_loop(master_fd, runtime.adb_proc, baud_sync=baud_sync)
        adb_returncode = runtime.adb_proc.poll()
        restore_ok = runtime.teardown("forward loop ended")
        if adb_returncode not in (None, 0):
            return 1
        if restore_ok is False:
            return 1
        return 0
    except PrepareRemoteUartError as exc:
        startup_state = exc.startup_state
        run_cmd_after(adb, startup_state, args.cmd_after)
        lock.release()
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        startup_state = getattr(exc, "startup_state", startup_state)
        if runtime is not None:
            runtime.teardown(str(exc))
        else:
            cleanup_before_runtime(
                paths, lock, adb, startup_state, args.cmd_after,
                master_fd, master_fd_owned, slave_fd, slave_fd_owned, slave_path,
            )
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except BaseException as exc:
        startup_state = getattr(exc, "startup_state", startup_state)
        if runtime is not None:
            runtime.teardown(str(exc))
        else:
            cleanup_before_runtime(
                paths, lock, adb, startup_state, args.cmd_after,
                master_fd, master_fd_owned, slave_fd, slave_fd_owned, slave_path,
            )
        raise
    finally:
        _active_runtime = None


def main(argv: list[str] | None = None) -> int:
    install_signal_handlers()
    args = parse_args(argv)
    try:
        args.serial = resolve_serial(args.serial)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return run_bridge(args)


if __name__ == "__main__":
    raise SystemExit(main())
