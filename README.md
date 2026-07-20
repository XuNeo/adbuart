# adbuart

Expose an Android-device UART as a local PTY over `adb`, so host serial tools such as `minicom`, `picocom`, or pyserial can talk to it like a normal serial port.

This is useful when a device has no host-side USB serial adapter and the RTOS console is reachable only through an Android UART device, for example `/dev/ttyHS1` on G2 Air style targets.

## Requirements

- Linux or another POSIX-like host with Python 3.10+
- `adb` installed and available in `PATH`
- An adb-connected Android device where `adb root` is allowed
- Target-side UART device access, defaulting to `/dev/ttyHS1`

## Install

```bash
python3 -m pip install adbuart
```

## Usage

Start the bridge in one terminal:

```bash
adbuart -n g2air
```

With multiple adb devices attached, pass the adb serial explicitly:

```bash
adbuart -s d7a6340b -n g2air
```

The command creates `/tmp/g2air` and keeps running in the foreground. It prints a minicom command when the PTY is ready.

Open the PTY from another terminal:

```bash
minicom -D /tmp/g2air -b 2000000
# or
python3 -m serial.tools.miniterm /tmp/g2air 2000000
```

Stop the bridge with `Ctrl-C` in the bridge terminal. Clean shutdown restores RTOS logging when it was enabled before startup.

## Options

Run:

```bash
adbuart --help
```

Important options:

- `-n, --name` creates `/tmp/<name>` and is required.
- `-s, --serial` selects an adb device. If omitted, exactly one device must be connected.
- `--remote-tty` selects the target UART device path. Default: `/dev/ttyHS1`.
- `--baud` sets the initial remote UART baud rate. Default: `2000000`.
- `--cmd-before` runs a remote shell command before bridging. Repeatable.
- `--cmd-after` runs a remote shell command during teardown. Repeatable.

By default the tool sets `persist.rtoslogd.uart` to `0` before bridging so Android log collection releases the UART. During teardown it restores the property to `1` only when it was `1` at startup.

## Operating notes

- The bridge owns the target UART exclusively while it is running. Stop it before flashing, resetting, or opening the same UART through another tool.
- The local node is protected with a lock file, but unrelated tools can still collide at the device UART level.
- Avoid `kill -9`; it skips teardown and can leave RTOS logging disabled or a stale `/tmp/<name>` symlink.
- Serial disconnects during target reset are normal. Restart the bridge after the adb UART stream closes.

## Development

```bash
python3 -m pip install -e '.[dev]'
pytest -q
python3 -m build
```

## License

MIT
