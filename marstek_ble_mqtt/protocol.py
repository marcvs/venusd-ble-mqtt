"""
Marstek HM/Venus BLE protocol.

Framing and field maps are derived from the reverse-engineering work in
rweijnen/marstek-venus-monitor (MIT). The wire format is:

    [0x73][Length][0x23][Command][Payload...][Checksum]

where Length covers the whole frame and Checksum is the XOR of every byte
except the checksum byte itself.

NOTE: This is experimental, reverse-engineered, and was mapped primarily
against the Venus E. The Venus D shares the HM protocol but some fields may
decode oddly. Every parser here is intentionally defensive: it returns
whatever it can and stashes the raw hex so nothing is silently lost.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Callable

# BLE GATT service exposing the HM protocol characteristics.
SERVICE_UUID = "0000ff00-0000-1000-8000-00805f9b34fb"

# Notify (device -> host) and write (host -> device) characteristics.
# On the Venus E the device both accepts commands on and replies on ff02
# (ff02 advertises write-without-response + notify); ff01 also accepts writes
# but the device does not notify on it. If a given unit differs, override
# these from config.
CHAR_NOTIFY = "0000ff02-0000-1000-8000-00805f9b34fb"
CHAR_WRITE = "0000ff02-0000-1000-8000-00805f9b34fb"

FRAME_HEADER = 0x73
FRAME_MARKER = 0x23

# BLE advertised names for Marstek storage / meter devices.
DEVICE_NAME_PREFIXES = ("MST_ACCP", "MST-TPM")


def build_frame(command: int, payload: bytes = b"") -> bytes:
    """Build a command frame for the given opcode and optional payload."""
    body = bytes([FRAME_HEADER, 0x00, FRAME_MARKER, command]) + payload
    length = len(body) + 1  # +1 for the checksum byte we are about to add
    body = bytes([FRAME_HEADER, length, FRAME_MARKER, command]) + payload
    checksum = 0
    for b in body:
        checksum ^= b
    return body + bytes([checksum])


def verify_frame(frame: bytes) -> bool:
    """Validate header marker and trailing XOR checksum of a response frame."""
    if len(frame) < 5 or frame[0] != FRAME_HEADER:
        return False
    expected = 0
    for b in frame[:-1]:
        expected ^= b
    return expected == frame[-1]


def extract_payload(frame: bytes) -> bytes:
    """Strip framing (header/len/marker/command + checksum) -> raw payload."""
    if len(frame) < 5:
        return b""
    return frame[4:-1]


# --- helpers --------------------------------------------------------------

def _u16(data: bytes, off: int) -> int | None:
    if off + 2 > len(data):
        return None
    return struct.unpack_from("<H", data, off)[0]


def _s16(data: bytes, off: int) -> int | None:
    if off + 2 > len(data):
        return None
    return struct.unpack_from("<h", data, off)[0]


def _u32(data: bytes, off: int) -> int | None:
    if off + 4 > len(data):
        return None
    return struct.unpack_from("<I", data, off)[0]


def _temp(data: bytes, off: int) -> float | None:
    """Signed 16-bit temperature in tenths of a degree -> degrees C."""
    v = _s16(data, off)
    return v / 10 if v is not None else None


# --- per-command parsers --------------------------------------------------
# Each parser takes the raw payload and returns a flat dict of {topic_leaf: value}.
# Always includes "raw" so unmapped bytes are never lost during early bring-up.

def parse_runtime(p: bytes) -> dict:
    """Command 0x03 - Runtime Info (~101 bytes)."""
    out: dict = {"raw": p.hex()}
    out["in1_power_w"] = _s16(p, 2)
    v = _s16(p, 4)
    out["in2_power_w"] = v / 100 if v is not None else None
    out["dev_version"] = p[10] if len(p) > 10 else None
    flags = p[15] if len(p) > 15 else None
    if flags is not None:
        out["wifi_connected"] = bool(flags & 0x01)
        out["mqtt_connected"] = bool(flags & 0x02)
    out["out1_power_w"] = _s16(p, 20)
    v = _s16(p, 24)
    out["out2_power_w"] = v / 10 if v is not None else None
    v = _s16(p, 33)
    out["temp_low_c"] = v / 10 if v is not None else None
    v = _s16(p, 35)
    out["temp_high_c"] = v / 10 if v is not None else None
    return out


def parse_bms(p: bytes) -> dict:
    """Command 0x14 - BMS Data (~80 bytes)."""
    out: dict = {"raw": p.hex()}
    out["bms_version"] = _u16(p, 0)
    v = _u16(p, 2)
    out["voltage_limit_v"] = v / 10 if v is not None else None
    v = _s16(p, 4)
    out["charge_current_limit_a"] = v / 10 if v is not None else None
    v = _s16(p, 6)
    out["discharge_current_limit_a"] = v / 10 if v is not None else None
    out["soc_pct"] = _u16(p, 8)
    out["soh_pct"] = _u16(p, 10)
    out["design_capacity_wh"] = _u16(p, 12)
    v = _u16(p, 14)
    out["battery_voltage_v"] = v / 100 if v is not None else None
    v = _s16(p, 16)
    out["battery_current_a"] = v / 10 if v is not None else None
    out["battery_temp_c"] = _s16(p, 18)  # whole degrees (not tenths)
    out["error_code"] = _u16(p, 26)
    out["warning_code"] = _u32(p, 28)
    out["runtime_ms"] = _u32(p, 32)
    out["mosfet_temp_c"] = _temp(p, 38)
    for i in range(4):
        out[f"temp_{i + 1}_c"] = _temp(p, 40 + i * 2)
    cells = []
    off = 48
    while off + 2 <= len(p) and len(cells) < 17:
        raw = _u16(p, off)
        off += 2
        if raw is None:
            break
        # Skip unpopulated/implausible slots; real cells are ~2.5-3.7V.
        if raw == 0x0000 or raw == 0xFFFF:
            continue
        cells.append(raw / 1000)
    for i, cv in enumerate(cells):
        out[f"cell_{i + 1}_v"] = cv
    if cells:
        out["cell_count"] = len(cells)
        out["cell_min_v"] = min(cells)
        out["cell_max_v"] = max(cells)
        out["cell_delta_v"] = round(max(cells) - min(cells), 4)

    # --- derived values (computed here, not present on the wire) ---
    # DC power across the battery terminals. Sign follows battery_current_a:
    # positive = charging, negative = discharging (Venus E convention).
    v = out.get("battery_voltage_v")
    i = out.get("battery_current_a")
    if v is not None and i is not None:
        out["power_w"] = round(v * i, 1)
    # Energy currently stored, as a fraction of nameplate capacity.
    soc = out.get("soc_pct")
    cap = out.get("design_capacity_wh")
    if soc is not None and cap is not None:
        out["stored_energy_wh"] = round(cap * soc / 100)
    return out


def parse_system(p: bytes) -> dict:
    """Command 0x0D - System Data (~19 bytes)."""
    out: dict = {"raw": p.hex()}
    out["system_status"] = p[0] if len(p) > 0 else None
    out["system_normal"] = (p[0] == 1) if len(p) > 0 else None
    for i in range(5):
        out[f"value_{i + 1}"] = _u16(p, 1 + i * 2)
    return out


def _parse_kv_text(p: bytes) -> dict:
    """Decode comma-separated key=value text responses (device/network info)."""
    out: dict = {"raw": p.hex()}
    try:
        text = p.decode("ascii", errors="replace").strip("\x00").strip()
    except Exception:
        return out
    out["text"] = text
    for part in text.split(","):
        if "=" in part:
            k, _, val = part.partition("=")
            k = k.strip()
            if k:
                out[k.strip()] = val.strip()
    return out


def parse_device_info(p: bytes) -> dict:
    """Command 0x04 - Device Info (text key=value: type,id,mac,dev_ver,...)."""
    return _parse_kv_text(p)


def parse_network_info(p: bytes) -> dict:
    """Command 0x24 - Network Info (text key=value: ip,gate,mask,dns)."""
    return _parse_kv_text(p)


def parse_wifi_info(p: bytes) -> dict:
    """Command 0x08 - WiFi Info (SSID as text)."""
    out: dict = {"raw": p.hex()}
    try:
        out["ssid"] = p.decode("ascii", errors="replace").strip("\x00").strip()
    except Exception:
        pass
    return out


def parse_error_codes(p: bytes) -> dict:
    """Command 0x13 - Error Codes (14-byte timestamped records)."""
    return _parse_records(p, 14, _parse_error_record, "errors")


def parse_event_log(p: bytes) -> dict:
    """Command 0x1C - Event Log (8-byte timestamped records)."""
    return _parse_records(p, 8, _parse_event_record, "events")


def _parse_records(p: bytes, size: int, fn: Callable, key: str) -> dict:
    out: dict = {"raw": p.hex()}
    records = []
    for off in range(0, len(p) - size + 1, size):
        chunk = p[off:off + size]
        if chunk == b"\x00" * size or chunk == b"\xff" * size:
            continue
        rec = fn(chunk)
        if rec:
            records.append(rec)
    out[key] = records
    out[f"{key}_count"] = len(records)
    return out


def _ts(chunk: bytes) -> str | None:
    year = struct.unpack_from("<H", chunk, 0)[0]
    month, day, hour, minute = chunk[2], chunk[3], chunk[4], chunk[5]
    if not (2000 <= year <= 2099 and 1 <= month <= 12 and 1 <= day <= 31):
        return None
    return f"{year:04d}-{month:02d}-{day:02d}T{hour:02d}:{minute:02d}"


def _parse_event_record(chunk: bytes) -> dict | None:
    ts = _ts(chunk)
    if ts is None:
        return None
    return {"ts": ts, "event_type": chunk[6], "event_code": chunk[7]}


def _parse_error_record(chunk: bytes) -> dict | None:
    ts = _ts(chunk)
    if ts is None:
        return None
    return {"ts": ts, "error_code": chunk[6], "data": chunk[7:14].hex()}


@dataclass
class Command:
    """A pollable BLE command: opcode, human name, and its parser."""
    opcode: int
    name: str
    parser: Callable[[bytes], dict]
    # Some commands are large/slow; allow per-command notes for future tuning.
    notes: str = ""


# Registry of safe, read-only commands keyed by the name used in config.
COMMANDS: dict[str, Command] = {
    "runtime": Command(0x03, "runtime", parse_runtime, "power, temps, flags"),
    "device_info": Command(0x04, "device_info", parse_device_info, "model/mac/fw"),
    "wifi": Command(0x08, "wifi", parse_wifi_info, "ssid"),
    "system": Command(0x0D, "system", parse_system, "system status"),
    "errors": Command(0x13, "errors", parse_error_codes, "fault history"),
    "bms": Command(0x14, "bms", parse_bms, "cells, soc, soh, temps"),
    "events": Command(0x1C, "events", parse_event_log, "event history"),
    "network": Command(0x24, "network", parse_network_info, "ip/gw/dns"),
}

# A sensible default subset for routine polling (fast, high-value telemetry).
DEFAULT_COMMANDS = ["runtime", "bms", "system"]
