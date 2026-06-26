"""Tests for the poll cycle and the abnormal-data health gate (no hardware)."""

import asyncio

from marstek_ble_mqtt import main as m
from marstek_ble_mqtt.ble import MarstekBLEError
from marstek_ble_mqtt.config import Config


class FakeBLE:
    """Stand-in for MarstekBLE that returns canned command results."""

    def __init__(self, system_normal=True, fail=()):
        self.system_normal = system_normal
        self.fail = set(fail)

    async def poll_command(self, name: str) -> dict:
        if name in self.fail:
            raise MarstekBLEError(f"{name} boom")
        if name == "system":
            return {
                "system_status": 1 if self.system_normal else 116,
                "system_normal": self.system_normal,
                "raw": "00",
            }
        return {"value": 1, "raw": "ff"}


class FakePub:
    def __init__(self):
        self.published: dict[str, dict] = {}

    def publish_command(self, name: str, data: dict) -> None:
        self.published[name] = data


def _cfg(**kw) -> Config:
    cfg = Config()
    cfg.commands = ["runtime", "bms", "system"]
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


def test_poll_once_publishes_when_healthy():
    pub = FakePub()
    ok = asyncio.run(m.poll_once(FakeBLE(system_normal=True), _cfg(), pub))
    assert ok is True
    assert set(pub.published) == {"runtime", "bms", "system"}


def test_poll_once_skips_publish_when_abnormal():
    pub = FakePub()
    ok = asyncio.run(m.poll_once(FakeBLE(system_normal=False), _cfg(), pub))
    assert ok is False
    assert pub.published == {}  # garbage must never be published


def test_poll_once_skips_publish_when_system_normal_none():
    pub = FakePub()
    ok = asyncio.run(m.poll_once(FakeBLE(system_normal=None), _cfg(), pub))
    assert ok is False
    assert pub.published == {}  # unconfirmed health -> no publish


def test_poll_once_skips_publish_when_system_command_missing():
    # 'system' is configured but its command failed, so there is no
    # system_normal at all -> nothing should be published.
    pub = FakePub()
    ok = asyncio.run(m.poll_once(FakeBLE(fail=["system"]), _cfg(), pub))
    assert ok is False
    assert pub.published == {}


def test_poll_once_without_system_does_not_gate():
    pub = FakePub()
    cfg = _cfg(commands=["runtime", "bms"])
    # system_normal is False but there's no 'system' command to read it from,
    # so publishing proceeds (gate is inert without it).
    ok = asyncio.run(m.poll_once(FakeBLE(system_normal=False), cfg, pub))
    assert ok is True
    assert set(pub.published) == {"runtime", "bms"}


def test_poll_once_strips_raw_by_default():
    pub = FakePub()
    asyncio.run(m.poll_once(FakeBLE(), _cfg(publish_raw=False), pub))
    assert "raw" not in pub.published["runtime"]


def test_poll_once_keeps_raw_when_enabled():
    pub = FakePub()
    asyncio.run(m.poll_once(FakeBLE(), _cfg(publish_raw=True), pub))
    assert "raw" in pub.published["runtime"]


def test_poll_once_survives_single_command_failure():
    pub = FakePub()
    ok = asyncio.run(m.poll_once(FakeBLE(fail=["bms"]), _cfg(), pub))
    assert ok is True
    assert "bms" not in pub.published
    assert {"runtime", "system"} <= set(pub.published)


def test_wait_events_returns_early_on_event():
    async def go():
        ev = asyncio.Event()
        asyncio.get_running_loop().call_later(0.05, ev.set)
        t0 = asyncio.get_running_loop().time()
        await m._wait_events(5.0, ev)
        return asyncio.get_running_loop().time() - t0

    assert asyncio.run(go()) < 1.0


def test_wait_events_times_out_without_event():
    async def go():
        ev = asyncio.Event()
        t0 = asyncio.get_running_loop().time()
        await m._wait_events(0.2, ev)
        return asyncio.get_running_loop().time() - t0

    assert asyncio.run(go()) >= 0.2
