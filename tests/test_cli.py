import codecs
from pathlib import Path
import threading

import pytest

from adbuart import __version__, cli


def test_package_version_matches_project_metadata():
    pyproject = Path(__file__).parents[1] / "pyproject.toml"

    assert f'version = "{__version__}"' in pyproject.read_text()


def test_parse_args_keeps_name_and_defaults():
    args = cli.parse_args(["-n", "uart-console"])

    assert cli.DEFAULT_REMOTE_TTY == "/dev/ttyHS1"
    assert cli.DEFAULT_BAUD == "2000000"
    assert cli.DEFAULT_CMD_BEFORE == "setprop persist.rtoslogd.uart 0"
    assert cli.DEFAULT_CMD_AFTER == "setprop persist.rtoslogd.uart 1"
    assert args.name == "uart-console"
    assert args.serial is None
    assert args.remote_tty == cli.DEFAULT_REMOTE_TTY
    assert args.baud == cli.DEFAULT_BAUD
    assert args.cmd_before is None
    assert args.cmd_after is None


def test_parse_args_accepts_existing_options():
    args = cli.parse_args([
        "-s",
        "SERIAL",
        "-n",
        "uart-console",
        "--remote-tty",
        "/dev/ttyHS2",
        "--baud",
        "115200",
        "--cmd-before",
        "echo before",
        "--cmd-before",
        "echo before2",
        "--cmd-after",
        "echo after",
    ])

    assert args.serial == "SERIAL"
    assert args.name == "uart-console"
    assert args.remote_tty == "/dev/ttyHS2"
    assert args.baud == "115200"
    assert args.cmd_before == ["echo before", "echo before2"]
    assert args.cmd_after == ["echo after"]


@pytest.mark.parametrize("bad_name", ["", ".", "..", "bad/name", " bad", "bad ", "bad:name"])
def test_validate_name_rejects_unsafe_names(bad_name):
    with pytest.raises(ValueError):
        cli.validate_name(bad_name)


def test_validate_name_accepts_safe_names():
    assert cli.validate_name("uart-console") == "uart-console"
    assert cli.validate_name("uart_console_1.0") == "uart_console_1.0"


def test_missing_name_selects_direct_terminal_mode():
    args = cli.parse_args([])

    assert args.name is None


def test_main_selects_bridge_when_name_is_set(monkeypatch):
    monkeypatch.setattr(cli, "PTY_BRIDGE_SUPPORTED", True)
    monkeypatch.setattr(cli, "install_signal_handlers", lambda: None)
    monkeypatch.setattr(cli, "resolve_serial", lambda serial: "SERIAL")
    monkeypatch.setattr(cli, "run_bridge", lambda args: ("bridge", args.serial))
    monkeypatch.setattr(cli, "run_terminal", lambda args: pytest.fail("terminal mode selected"))

    assert cli.main(["--name", "uart-console"]) == ("bridge", "SERIAL")


def test_main_selects_terminal_when_name_is_missing(monkeypatch):
    monkeypatch.setattr(cli, "install_signal_handlers", lambda: None)
    monkeypatch.setattr(cli, "resolve_serial", lambda serial: "SERIAL")
    monkeypatch.setattr(cli, "run_bridge", lambda args: pytest.fail("bridge mode selected"))
    monkeypatch.setattr(cli, "run_terminal", lambda args: ("terminal", args.serial))

    assert cli.main([]) == ("terminal", "SERIAL")


def test_main_rejects_bridge_before_resolving_device(monkeypatch, capsys):
    monkeypatch.setattr(cli, "PTY_BRIDGE_SUPPORTED", False)
    monkeypatch.setattr(cli, "install_signal_handlers", lambda: None)
    monkeypatch.setattr(cli, "resolve_serial", lambda serial: pytest.fail("device resolved"))

    assert cli.main(["--name", "uart-console"]) == 2
    assert "requires POSIX PTY support" in capsys.readouterr().err


class FakePipe:
    def __init__(self, reads=None):
        self.reads = list(reads or [])
        self.writes = []
        self.flush_count = 0

    def read(self, size):
        if not self.reads:
            return b""
        return self.reads.pop(0)

    def write(self, data):
        self.writes.append(data)
        return len(data)

    def flush(self):
        self.flush_count += 1


class FakeProcess:
    def __init__(self, stdout=None, stdin=None, returncode=None):
        self.stdout = stdout or FakePipe()
        self.stdin = stdin or FakePipe()
        self.returncode = returncode
        self.terminate_count = 0
        self.kill_count = 0

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminate_count += 1
        self.returncode = 0

    def kill(self):
        self.kill_count += 1
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


def test_adb_serial_adapter_reads_and_writes_process_pipes():
    stdout = FakePipe([b"NSH> "])
    stdin = FakePipe()
    serial_port = cli.AdbSerial(FakeProcess(stdout=stdout, stdin=stdin), "adb:SERIAL:/dev/ttyHS1", "2000000")

    assert serial_port.read(5) == b"NSH> "
    assert serial_port.write(b"help\r\n") == 6
    assert stdin.writes == [b"help\r\n"]
    assert stdin.flush_count == 1


def test_adb_serial_adapter_updates_remote_baud():
    baudrates = []
    serial_port = cli.AdbSerial(FakeProcess(), "adb:SERIAL:/dev/ttyHS1", "2000000", baudrates.append)

    serial_port.baudrate = 115200

    assert serial_port.baudrate == 115200
    assert baudrates == [115200]


def test_adb_serial_adapter_keeps_baud_when_remote_update_fails():
    def fail_baudrate(baudrate):
        raise RuntimeError("unsupported")

    serial_port = cli.AdbSerial(FakeProcess(), "adb:SERIAL:/dev/ttyHS1", "2000000", fail_baudrate)

    with pytest.raises(cli.serial.SerialException, match="failed to set remote baudrate 115200"):
        serial_port.baudrate = 115200

    assert serial_port.baudrate == 2000000


def test_adb_serial_adapter_raises_serial_exception_on_eof():
    serial_port = cli.AdbSerial(FakeProcess(stdout=FakePipe([b""])), "adb:SERIAL:/dev/ttyHS1", "2000000")

    with pytest.raises(cli.serial.SerialException, match="ADB UART stream closed"):
        serial_port.read(1)


def test_adb_serial_adapter_cancel_read_keeps_process_alive():
    process = FakeProcess(stdout=FakePipe([b"N"]))
    serial_port = cli.AdbSerial(process, "adb:SERIAL:/dev/ttyHS1", "2000000")

    serial_port.cancel_read()

    assert serial_port.read(1) == b""
    assert process.terminate_count == 0
    assert serial_port.read(1) == b"N"


@pytest.mark.skipif(not cli.PTY_BRIDGE_SUPPORTED, reason="POSIX pipe polling")
def test_adb_serial_adapter_reads_no_more_than_requested(monkeypatch):
    read_sizes = []
    stdout = type("FdPipe", (), {"fileno": lambda self: 7})()
    serial_port = cli.AdbSerial(FakeProcess(stdout=stdout), "adb:SERIAL:/dev/ttyHS1", "2000000")
    monkeypatch.setattr(cli.select, "poll", lambda: type("Poll", (), {
        "register": lambda self, *args: None,
        "poll": lambda self, timeout: [(7, cli.select.POLLIN)],
    })())
    monkeypatch.setattr(cli.os, "read", lambda fd, size: read_sizes.append(size) or b"N")

    assert serial_port.read(1) == b"N"
    assert read_sizes == [1]


def test_windows_adb_serial_buffers_and_cancels_reads(monkeypatch):
    ready = threading.Event()
    release = threading.Event()

    class BlockingPipe(FakePipe):
        def read(self, size):
            ready.set()
            release.wait(1)
            if self.reads:
                return self.reads.pop(0)
            return b""

    monkeypatch.setattr(cli, "WINDOWS_HOST", True)
    process = FakeProcess(stdout=BlockingPipe([b"NSH> ", b""]))
    serial_port = cli.AdbSerial(process, "adb:SERIAL:/dev/ttyHS1", "2000000")
    assert ready.wait(1)

    result = []
    reader = threading.Thread(target=lambda: result.append(serial_port.read(1)))
    reader.start()
    serial_port.cancel_read()
    reader.join(1)

    assert result == [b""]
    assert process.terminate_count == 0

    release.set()
    serial_port._reader_thread.join(1)
    assert serial_port.in_waiting == 5
    assert serial_port.read(2) == b"NS"
    assert serial_port.in_waiting == 3


def test_adb_miniterm_reader_stops_on_adb_eof_without_console_cancel():
    serial_port = cli.AdbSerial(FakeProcess(stdout=FakePipe([b""])), "adb:SERIAL:/dev/ttyHS1", "2000000")
    terminal = object.__new__(cli.AdbMiniterm)
    terminal.serial = serial_port
    terminal.alive = True
    terminal._reader_alive = True
    terminal.reader_error = None

    terminal.reader()

    assert terminal.alive is False
    assert isinstance(terminal.reader_error, cli.serial.SerialException)


def test_adb_miniterm_reader_stops_when_console_output_closes():
    terminal = object.__new__(cli.AdbMiniterm)
    terminal.serial = type("Serial", (), {
        "in_waiting": 1,
        "read": lambda self, size: b"N",
    })()
    terminal.console = type("Console", (), {
        "write": lambda self, text: (_ for _ in ()).throw(BrokenPipeError()),
    })()
    terminal.alive = True
    terminal._reader_alive = True
    terminal.reader_error = None
    terminal.raw = False
    terminal.rx_decoder = codecs.getincrementaldecoder("UTF-8")("replace")
    terminal.rx_transformations = []

    terminal.reader()

    assert terminal.alive is False
    assert isinstance(terminal.reader_error, BrokenPipeError)


def test_windows_miniterm_reader_cancels_console_on_disconnect(monkeypatch):
    cancelled = []
    terminal = object.__new__(cli.AdbMiniterm)
    terminal.serial = type("Serial", (), {
        "in_waiting": 1,
        "read": lambda self, size: (_ for _ in ()).throw(cli.serial.SerialException("closed")),
    })()
    terminal.console = type("Console", (), {"cancel": lambda self: cancelled.append(True)})()
    terminal.alive = True
    terminal._reader_alive = True
    terminal.reader_error = None
    monkeypatch.setattr(cli, "WINDOWS_HOST", True)

    terminal.reader()

    assert cancelled == [True]
    assert isinstance(terminal.reader_error, cli.serial.SerialException)


def test_windows_miniterm_uses_pyserial_writer(monkeypatch):
    calls = []
    terminal = object.__new__(cli.AdbMiniterm)
    terminal.writer_error = None
    monkeypatch.setattr(cli, "WINDOWS_HOST", True)
    monkeypatch.setattr(cli.miniterm.Miniterm, "writer", lambda self: calls.append(True))

    terminal.writer()

    assert calls == [True]
    assert terminal.writer_error is None


@pytest.mark.skipif(not cli.PTY_BRIDGE_SUPPORTED, reason="POSIX console polling")
def test_adb_miniterm_writer_consumes_buffered_input(monkeypatch):
    writes = []
    terminal = object.__new__(cli.AdbMiniterm)
    terminal.alive = True
    terminal.console = type("Console", (), {"fd": 7})()
    terminal.serial = type("Serial", (), {"write": lambda self, data: writes.append(data)})()
    terminal.menu_character = "\x14"
    terminal.exit_character = "\x1d"
    terminal.tx_transformations = []
    terminal.tx_encoder = codecs.getincrementalencoder("UTF-8")("replace")
    terminal.echo = False
    terminal.writer_error = None
    monkeypatch.setattr(cli.select, "poll", lambda: type("Poll", (), {
        "register": lambda self, *args: None,
        "poll": lambda self, timeout: [(7, cli.select.POLLIN)],
    })())
    monkeypatch.setattr(cli.os, "read", lambda fd, size: b"help\x1d")

    terminal.writer()

    assert writes == [b"h", b"e", b"l", b"p"]
    assert terminal.alive is False


def run_fake_terminal(monkeypatch, process_returncode=None, reader_error=None, writer_error=None):
    events = []
    process = FakeProcess(returncode=process_returncode)
    adb = type("FakeAdb", (), {"popen_shell_t": lambda self, command, stderr=None: process})()

    class FakeMiniterm:
        def __init__(self, serial_port, echo, eol, filters):
            events.append(("init", serial_port.name, echo, eol, filters))
            self.console = type("Console", (), {"cleanup": lambda self: events.append("cleanup")})()
            self.exit_character = None
            self.menu_character = None
            self.reader_error = None
            self.writer_error = None
            self.raw = False

        def set_rx_encoding(self, encoding):
            events.append(("rx", encoding))

        def set_tx_encoding(self, encoding):
            events.append(("tx", encoding))

        def start(self):
            events.append("start")

        def join(self, transmit_only=False):
            events.append(("join", transmit_only))
            if not transmit_only:
                self.reader_error = reader_error
                self.writer_error = writer_error

        def stop(self):
            events.append("stop")

    class FakeRuntime:
        def __init__(self, *args, **kwargs):
            self.adb_proc = None

        def teardown(self, reason):
            events.append(("teardown", reason))
            return True

    monkeypatch.setattr(cli, "Adb", lambda serial_number: adb)
    monkeypatch.setattr(cli, "prepare_remote_uart", lambda adb_value, before, after: "1")
    monkeypatch.setattr(cli, "build_remote_command", lambda remote_tty, baud: "REMOTE")
    monkeypatch.setattr(cli, "TerminalRuntime", FakeRuntime)
    monkeypatch.setattr(cli, "AdbMiniterm", FakeMiniterm)
    args = cli.parse_args(["--serial", "SERIAL"])

    return cli.run_terminal(args), events


@pytest.mark.parametrize("process_returncode, expected", [(None, 0), (1, 1)])
def test_run_terminal_starts_miniterm_and_tears_down(monkeypatch, process_returncode, expected):
    result, events = run_fake_terminal(monkeypatch, process_returncode=process_returncode)

    assert result == expected
    assert events == [
        ("init", "adb:SERIAL:/dev/ttyHS1", False, "crlf", ("direct",)),
        ("rx", "UTF-8"),
        ("tx", "UTF-8"),
        "start",
        ("join", True),
        ("join", False),
        "cleanup",
        ("teardown", "terminal ended"),
    ]


def error_lines(captured):
    return [line for line in captured.err.splitlines() if line.startswith("error:")]


def test_run_terminal_reports_adb_disconnect_once(monkeypatch, capsys):
    result, _ = run_fake_terminal(
        monkeypatch,
        process_returncode=0,
        reader_error=cli.serial.SerialException("ADB UART stream closed"),
    )

    assert result == 1
    assert error_lines(capsys.readouterr()) == ["error: ADB connection lost"]


def test_run_terminal_reports_internal_error_once(monkeypatch, capsys):
    result, _ = run_fake_terminal(
        monkeypatch,
        writer_error=cli.serial.SerialException("failed to write ADB UART stream: pipe closed"),
    )

    assert result == 1
    assert error_lines(capsys.readouterr()) == [
        "error: adbuart internal error: failed to write ADB UART stream: pipe closed"
    ]


@pytest.mark.skipif(not cli.PTY_BRIDGE_SUPPORTED, reason="POSIX PTY bridge")
def test_run_bridge_reports_adb_exit_code(monkeypatch, capsys):
    monkeypatch.setattr(cli, "PTY_BRIDGE_SUPPORTED", True)
    process = FakeProcess(returncode=1)
    adb = type("FakeAdb", (), {"popen_shell_t": lambda self, command: process})()

    class FakeLock:
        def __init__(self, path):
            pass

        def acquire(self):
            pass

        def release(self):
            pass

    class FakeRuntime:
        def __init__(self, *args):
            self.adb_proc = None
            self.pty_fd = None
            self.slave_fd = None

        def teardown(self, reason):
            return True

    monkeypatch.setattr(cli, "LocalLock", FakeLock)
    monkeypatch.setattr(cli, "Adb", lambda serial_number: adb)
    monkeypatch.setattr(cli, "prepare_remote_uart", lambda adb_value, before, after: "1")
    monkeypatch.setattr(cli.pty, "openpty", lambda: (7, 8))
    monkeypatch.setattr(cli.os, "ttyname", lambda fd: "/dev/pts/1")
    monkeypatch.setattr(cli, "_fd_is_tty", lambda fd: False)
    monkeypatch.setattr(cli, "create_symlink", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "BridgeRuntime", FakeRuntime)
    monkeypatch.setattr(cli, "build_remote_command", lambda remote_tty, baud: "REMOTE")
    monkeypatch.setattr(cli, "forward_loop", lambda *args, **kwargs: None)
    args = cli.parse_args(["--serial", "SERIAL", "--name", "uart-console"])

    assert cli.run_bridge(args) == 1
    assert error_lines(capsys.readouterr()) == ["error: ADB connection lost"]


def test_build_remote_command_keeps_existing_shape():
    command = cli.build_remote_command("/dev/ttyHS2", "115200")

    assert "stty -F /dev/ttyHS2 115200 raw -echo -crtscts cs8 -parenb -cstopb min 1 time 0" in command
    assert "cat /dev/ttyHS2 & rx=$!" in command
    assert "cat > /dev/ttyHS2" in command


def test_parse_args_supports_version(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.parse_args(["--version"])

    assert excinfo.value.code == 0
    assert capsys.readouterr().out.strip() == f"adbuart {__version__}"


def test_help_includes_version(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.parse_args(["--help"])

    assert excinfo.value.code == 0
    assert f"version: {__version__}" in capsys.readouterr().out
