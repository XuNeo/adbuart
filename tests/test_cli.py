import argparse

import pytest

from adbuart import cli


def test_parse_args_keeps_required_name_and_defaults():
    args = cli.parse_args(["-n", "g2air"])

    assert args.name == "g2air"
    assert args.serial is None
    assert args.remote_tty == cli.DEFAULT_REMOTE_TTY
    assert args.baud == cli.DEFAULT_BAUD
    assert args.cmd_before is None
    assert args.cmd_after is None


def test_parse_args_accepts_existing_options():
    args = cli.parse_args([
        "-s",
        "d7a6340b",
        "-n",
        "g2air",
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

    assert args.serial == "d7a6340b"
    assert args.name == "g2air"
    assert args.remote_tty == "/dev/ttyHS2"
    assert args.baud == "115200"
    assert args.cmd_before == ["echo before", "echo before2"]
    assert args.cmd_after == ["echo after"]


@pytest.mark.parametrize("bad_name", ["", ".", "..", "bad/name", " bad", "bad ", "bad:name"])
def test_validate_name_rejects_unsafe_names(bad_name):
    with pytest.raises(ValueError):
        cli.validate_name(bad_name)


def test_validate_name_accepts_safe_names():
    assert cli.validate_name("g2air") == "g2air"
    assert cli.validate_name("g2-air_1.0") == "g2-air_1.0"


def test_missing_required_name_exits_parser():
    with pytest.raises(SystemExit) as excinfo:
        cli.parse_args([])

    assert excinfo.value.code == 2


def test_build_remote_command_keeps_existing_shape():
    command = cli.build_remote_command("/dev/ttyHS2", "115200")

    assert "stty -F /dev/ttyHS2 115200 raw -echo -crtscts cs8 -parenb -cstopb min 1 time 0" in command
    assert "cat /dev/ttyHS2 & rx=$!" in command
    assert "cat > /dev/ttyHS2" in command
