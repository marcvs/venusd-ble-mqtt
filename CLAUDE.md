# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A headless-Linux daemon that polls a Marstek Venus/HM-series battery over **Bluetooth Low Energy** (via BlueZ/`bleak`) and republishes its telemetry as JSON to **MQTT** (via `paho-mqtt`). BLE is used deliberately instead of the Marstek local UDP API, which can crash the device on some firmware. The BLE protocol is reverse-engineered (from rweijnen/marstek-venus-monitor) and mapped mainly against the Venus E — field maps may be wrong on other models, which is why every parser is defensive and `publish_raw` exists.

## Commands

```bash
pip install -e ".[dev]"          # install with dev deps (pytest)
pytest                           # run all tests (no hardware needed)
pytest tests/test_protocol.py::test_bms_parser_fields   # single test
marstek-ble-mqtt --list-commands # list supported BLE opcodes (no hardware/config)
marstek-ble-mqtt --address AA:BB:CC:DD:EE:FF --once --publish-raw --log-level DEBUG  # one cycle, for bring-up
```

Tests are pure-Python protocol/parser tests — they never touch BLE or MQTT, so they run anywhere. When changing `protocol.py`, validate against `tests/test_protocol.py`.

## Architecture

Data flows: **`ble.py` (transport) → `protocol.py` (decode) → `mqtt.py` (publish)**, orchestrated by `main.py`, configured by `config.py`.

- **`protocol.py`** is the heart of the project and the source of truth. It defines:
  - Frame format `[0x73][Length][0x23][Command][Payload][XOR-Checksum]` where `Length` covers the whole frame including checksum. `build_frame`/`verify_frame`/`extract_payload` handle framing.
  - One `parse_*` function per opcode, each returning a flat `{leaf: value}` dict that **always includes `"raw"`** (full payload hex) so unmapped bytes are never lost. Parsers must stay defensive: every field read goes through `_u16`/`_s16`/`_u32` helpers that return `None` on short payloads rather than raising. Byte offsets in the parsers are reverse-engineered constants — change them only with real device data.
  - `COMMANDS`: the registry mapping config name → `Command(opcode, name, parser, notes)`. **Adding a new command = add one entry here** plus its parser; everything else (CLI `--list-commands`, validation, polling) is data-driven off this dict. `DEFAULT_COMMANDS` is the routine-polling subset.

- **`ble.py`** (`MarstekBLE`): async context manager over `bleak`. Resolves device by `address` or by scanning for an advertised-name prefix. The device answers a command by streaming notification chunks; `_on_notify` reassembles them into frames using the length byte and a header-resync fallback, pushing complete frames onto an asyncio queue. `request()` drains stale frames, writes the command, and waits (with timeout) for one response frame.

- **`main.py`**: arg parsing, CLI-over-config merge (`merge_cli`), and the `run()` loop with **capped exponential backoff (5s→60s)** on BLE session failures plus interruptible sleeps via an `asyncio.Event` driven by SIGINT/SIGTERM. `poll_once` isolates per-command failures so one bad command/parser never kills the loop.

- **`config.py`**: `Config` dataclass + `load_config` (INI via `configparser`). Defaults live on the dataclass; the INI overrides them; CLI flags override the INI. Command names are validated against `protocol.COMMANDS` in **both** `load_config` and `merge_cli` so typos fail loudly.

- **`mqtt.py`** (`MqttPublisher`): publishes one JSON doc per command to `<topic_prefix>/<command_name>`, plus a retained availability topic `<topic_prefix>/status` (online/offline) backed by an MQTT last-will. Handles both paho-mqtt 1.x and 2.x callback-API versions.

## Conventions

- All read-only. The device write path (local-API enable, power settings) is intentionally **not** implemented — keep it that way unless explicitly asked.
- `raw` hex is stripped from payloads before publish unless `publish_raw` is set (see `poll_once`); keep emitting it from parsers.
- Config files (`*.ini`) are gitignored except `marstek-ble-mqtt.example.ini` — update the example when adding config keys.
