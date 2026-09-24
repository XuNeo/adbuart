# adbuart

Open an Android-device UART in a local miniterm session over `adb`, or expose it as a local PTY for tools such as `minicom`, `picocom`, and pyserial.

This is useful when a device has no host-side USB serial adapter and its console is reachable only through an Android UART device.

## Requirements

- Windows, macOS, or Linux with Python 3.10+ for direct miniterm mode
- A POSIX host for the optional local PTY mode
- `adb` installed and available in `PATH`
- An adb-connected Android device where `adb root` is allowed
- Target-side UART device access, defaulting to `/dev/ttyHS1`

## Install

```bash
python3 -m pip install adbuart
```

## Usage

Open the UART directly in miniterm:

```bash
adbuart
```

With multiple adb devices attached, pass the adb serial explicitly:

```bash
adbuart -s SERIAL
```

Press `Ctrl+]` to exit, or `Ctrl+T` followed by `Ctrl+H` for miniterm help.

On Linux or macOS, create a PTY for another serial tool by passing a name:

```bash
adbuart -n uart-console
```

This creates `/tmp/uart-console` and keeps the bridge running in the foreground. Open it from another terminal:

```bash
minicom -D /tmp/uart-console -b 2000000
# or
python3 -m serial.tools.miniterm /tmp/uart-console 2000000
```

Stop PTY mode with `Ctrl-C` in the bridge terminal. Clean shutdown in either mode restores RTOS logging when it was enabled before startup.

## Options

Run:

```bash
adbuart --help
adbuart --version
```

Important options:

- Without `--name`, the command opens the UART directly in miniterm.
- `-n, --name` creates `/tmp/<name>` for use by another serial tool on POSIX hosts; Windows direct mode does not create a virtual COM port.
- `-s, --serial` selects an adb device. If omitted, exactly one device must be connected.
- `--remote-tty` selects the target UART device path. Default: `/dev/ttyHS1`.
- `--baud` sets the initial remote UART baud rate. Default: `2000000`.
- `--cmd-before` runs a remote shell command before bridging. Repeatable.
- `--cmd-after` runs a remote shell command during teardown. Repeatable.

By default the tool sets `persist.rtoslogd.uart` to `0` before bridging so Android log collection releases the UART. During teardown it restores the property to `1` only when it was `1` at startup.

## Operating notes

- The bridge owns the target UART exclusively while it is running. Stop it before flashing, resetting, or opening the same UART through another tool.
- In PTY mode, the local node is protected with a lock file, but unrelated tools can still collide at the device UART level.
- Avoid `kill -9`; it skips teardown and can leave RTOS logging disabled or, in PTY mode, a stale `/tmp/<name>` symlink.
- Serial disconnects during target reset are normal. Restart the bridge after the adb UART stream closes.

## Development

```bash
python3 -m pip install -e '.[dev]'
pytest -q
python3 -m build
```

## License

MIT
