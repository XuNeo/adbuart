import pytest

from adbuart import __version__, cli


def test_parse_args_keeps_required_name_and_defaults():
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


def test_missing_required_name_exits_parser():
    with pytest.raises(SystemExit) as excinfo:
        cli.parse_args([])

    assert excinfo.value.code == 2


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
