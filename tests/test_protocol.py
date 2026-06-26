"""Tests for the BLE protocol framing and parsers (no hardware required)."""

import struct

from marstek_ble_mqtt import protocol as p
from marstek_ble_mqtt.ble import MarstekBLE


def test_frame_roundtrip():
    f = p.build_frame(0x03, b"")
    assert f[0] == p.FRAME_HEADER
    assert f[1] == len(f)          # length byte covers whole frame
    assert f[2] == p.FRAME_MARKER
    assert f[3] == 0x03
    assert p.verify_frame(f)


def test_frame_checksum_detects_corruption():
    f = bytearray(p.build_frame(0x14, b"\x01\x02"))
    f[4] ^= 0xFF                    # corrupt a payload byte
    assert not p.verify_frame(bytes(f))


def test_bms_parser_fields():
    payload = bytearray(80)
    struct.pack_into("<H", payload, 0, 215)
    struct.pack_into("<H", payload, 8, 62)
    struct.pack_into("<H", payload, 10, 98)
    struct.pack_into("<H", payload, 12, 5120)
    struct.pack_into("<H", payload, 14, 5312)
    struct.pack_into("<h", payload, 16, -123)
    struct.pack_into("<h", payload, 18, 30)    # battery temp, whole degrees -> 30 C
    struct.pack_into("<h", payload, 38, 308)   # mosfet temp,  tenths -> 30.8 C
    struct.pack_into("<h", payload, 40, -15)   # temp_1, tenths -> -1.5 C
    struct.pack_into("<H", payload, 48, 3019)
    struct.pack_into("<H", payload, 50, 3023)
    d = p.parse_bms(bytes(payload))
    assert d["bms_version"] == 215
    assert d["soc_pct"] == 62
    assert d["soh_pct"] == 98
    assert d["design_capacity_wh"] == 5120
    assert abs(d["battery_voltage_v"] - 53.12) < 1e-6
    assert abs(d["battery_current_a"] + 12.3) < 1e-6
    assert d["battery_temp_c"] == 30  # whole degrees
    assert abs(d["mosfet_temp_c"] - 30.8) < 1e-6
    assert abs(d["temp_1_c"] + 1.5) < 1e-6
    assert d["cell_count"] == 2
    assert abs(d["cell_delta_v"] - 0.004) < 1e-6
    # derived: power = 53.12 V * -12.3 A; stored = 5120 Wh * 62%
    assert abs(d["power_w"] + 653.4) < 0.05
    assert d["stored_energy_wh"] == 3174


def test_bms_skips_empty_cell_slots():
    payload = bytearray(80)
    struct.pack_into("<H", payload, 48, 3000)
    # remaining cell slots left 0x0000 -> must be ignored
    d = p.parse_bms(bytes(payload))
    assert d["cell_count"] == 1


def test_kv_text_parser():
    d = p.parse_device_info(b"type=HMG-50,id=123,mac=aabb")
    assert d["type"] == "HMG-50"
    assert d["id"] == "123"
    assert d["mac"] == "aabb"


def test_event_record_timestamp():
    rec = bytearray(8)
    struct.pack_into("<H", rec, 0, 2026)
    rec[2], rec[3], rec[4], rec[5] = 6, 25, 14, 30
    rec[6], rec[7] = 0x94, 0x01
    d = p.parse_event_log(bytes(rec))
    assert d["events_count"] == 1
    assert d["events"][0]["ts"] == "2026-06-25T14:30"
    assert d["events"][0]["event_type"] == 0x94


def test_chunked_reassembly():
    ble = MarstekBLE(address="X")
    full = p.build_frame(0x0D, bytes(15))
    ble._on_notify(None, bytearray(full[:6]))
    ble._on_notify(None, bytearray(full[6:]))
    assert not ble._frame_q.empty()
    assert ble._frame_q.get_nowait() == full


def test_resync_on_garbage_prefix():
    ble = MarstekBLE(address="X")
    full = p.build_frame(0x0D, bytes(15))
    ble._on_notify(None, bytearray(b"\xde\xad" + full))
    assert not ble._frame_q.empty()
    assert ble._frame_q.get_nowait() == full
