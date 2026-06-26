"""Tests for config loading, defaults, and default-path discovery."""

import pytest

from marstek_ble_mqtt import config as c


def test_default_abnormal_timeout():
    assert c.Config().abnormal_timeout == 300.0


def test_default_connect_retry_knobs():
    cfg = c.Config()
    assert cfg.connect_retries == 3
    assert cfg.retry_delay == 3.0


def test_load_poll_values_from_ini(tmp_path):
    p = tmp_path / "x.ini"
    p.write_text("[poll]\ninterval = 7\nabnormal_timeout = 42\n")
    cfg = c.load_config(str(p))
    assert cfg.interval == 7.0
    assert cfg.abnormal_timeout == 42.0


def test_unknown_command_in_config_raises(tmp_path):
    p = tmp_path / "x.ini"
    p.write_text("[poll]\ncommands = runtime, bogus\n")
    with pytest.raises(ValueError):
        c.load_config(str(p))


def test_explicit_missing_path_raises():
    with pytest.raises(FileNotFoundError):
        c.load_config("/no/such/marstek-ble-mqtt.ini")


def test_default_config_discovery_picks_first(tmp_path, monkeypatch):
    cfgfile = tmp_path / "marstek-ble-mqtt.ini"
    cfgfile.write_text("[poll]\ninterval = 7\n")
    monkeypatch.setattr(c, "DEFAULT_CONFIG_PATHS", [str(cfgfile)])
    assert c.find_default_config() == str(cfgfile)
    assert c.load_config(None).interval == 7.0


def test_default_config_discovery_falls_back_to_defaults(monkeypatch):
    monkeypatch.setattr(
        c, "DEFAULT_CONFIG_PATHS", ["/no/such/a.ini", "/no/such/b.ini"]
    )
    assert c.find_default_config() is None
    assert c.load_config(None).interval == 120.0  # built-in default
