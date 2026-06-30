# venusd-ble-mqtt

Poll a Marstek **Venus / HM-series** battery over **Bluetooth Low Energy** and
publish its telemetry to **MQTT**. Headless-Linux friendly, no Home Assistant
required.

## Why BLE?

The Marstek local UDP API can crash the device when polled on some firmware. BLE
is an independent code path on the unit, so this polls over BLE instead and
leaves the flaky API alone. As a bonus, BLE exposes the error/event logs, which
are handy for diagnosing exactly that crash.

> ⚠️ The BLE protocol here is reverse-engineered (credit:
> [rweijnen/marstek-venus-monitor](https://github.com/rweijnen/marstek-venus-monitor),
> MIT) and was mapped mainly against the Venus **E**. The Venus **D** shares the
> HM protocol but some fields may decode oddly — set `publish_raw = true` while
> validating field maps on your unit.

## Install

Requires Python ≥ 3.10, a working BlueZ stack, and a Bluetooth adapter
(`bluetoothctl list` should show a controller).

For local development:

```bash
pip install -e ".[dev]"
```

For a system service, install into an isolated venv so the daemon does not
depend on the system Python's packages:

```bash
sudo python3 -m venv /opt/venusd-ble-mqtt
sudo /opt/venusd-ble-mqtt/bin/pip install .      # run from the repo checkout
```

This gives you `/opt/venusd-ble-mqtt/bin/venusd-ble-mqtt`. (A plain
`sudo pip install .` instead drops the entry point at
`/usr/local/bin/venusd-ble-mqtt`, which is what the bundled unit file expects —
adjust `ExecStart` if you use the venv path.)

## Usage

Find the device address first:

```bash
bluetoothctl scan on   # look for MST_ACCP (battery) or MST-TPM (meter)
```

Then either pass everything on the CLI:

```bash
venusd-ble-mqtt --address AA:BB:CC:DD:EE:FF \
                 --mqtt-host localhost \
                 --interval 120 \
                 --commands runtime,bms,system
```

…or use a config file (CLI flags override file values):

```bash
cp venusd-ble-mqtt.example.ini /etc/venusd-ble-mqtt.ini
$EDITOR /etc/venusd-ble-mqtt.ini
venusd-ble-mqtt -c /etc/venusd-ble-mqtt.ini
```

Test a single cycle without committing to the loop:

```bash
venusd-ble-mqtt --address AA:BB:CC:DD:EE:FF --once --publish-raw --log-level DEBUG
```

List supported commands:

```bash
venusd-ble-mqtt --list-commands
```

## MQTT topics

One retained-or-not JSON document per command under the prefix:

```
venusd/runtime    {"in1_power_w": 78, "wifi_connected": true, ...}
venusd/bms        {"soc_pct": 62, "soh_pct": 98, "cell_delta_v": 0.012, ...}
venusd/system     {"system_status": 1, "system_normal": true, ...}
venusd/status     online | offline   (retained availability)
```

## Supported commands

| name          | opcode | contents                          |
|---------------|--------|-----------------------------------|
| `runtime`     | 0x03   | input/output power, temps, flags  |
| `device_info` | 0x04   | model, MAC, firmware versions     |
| `wifi`        | 0x08   | connected SSID                    |
| `system`      | 0x0D   | system status                     |
| `errors`      | 0x13   | timestamped fault history         |
| `bms`         | 0x14   | cells, SoC, SoH, currents, temps  |
| `events`      | 0x1C   | timestamped event history         |
| `network`     | 0x24   | IP / gateway / DNS                |

All are read-only. The device write path (e.g. local-API enable, power
settings) is intentionally **not** implemented here.

## Run as a systemd service

A ready-to-use, hardened unit file ships in the repo as
[`venusd-ble-mqtt.service`](venusd-ble-mqtt.service). To install it:

```bash
# 1. Install the package (see Install above) so the binary exists.
# 2. Drop your config where the unit expects it.
sudo cp venusd-ble-mqtt.example.ini /etc/venusd-ble-mqtt.ini
sudo $EDITOR /etc/venusd-ble-mqtt.ini      # set address, mqtt_host, etc.

# 3. Install and enable the service.
sudo cp venusd-ble-mqtt.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now venusd-ble-mqtt.service
```

Check it is running and watch the logs:

```bash
systemctl status venusd-ble-mqtt.service
journalctl -u venusd-ble-mqtt.service -f
```

The unit runs as `root` by default because BlueZ normally requires it to
connect to a peripheral, and `ExecStart` points at
`/usr/local/bin/venusd-ble-mqtt` — edit it if you installed into the
`/opt` venv from the Install section. Header comments in the file explain how
to run unprivileged and which hardening options to relax if BLE access fails.

## License

MIT.
