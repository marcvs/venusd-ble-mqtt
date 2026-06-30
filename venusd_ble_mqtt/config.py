"""Configuration handling via configparser, mirroring the user's usual layout."""

from __future__ import annotations

import configparser
import logging
import os
from dataclasses import dataclass, field

from . import protocol

log = logging.getLogger(__name__)

# Searched in order when no config path is given explicitly; first hit wins.
DEFAULT_CONFIG_PATHS = [
    "venusd-ble-mqtt.ini",
    os.path.expanduser("~/.config/venusd-ble-mqtt.ini"),
    "/etc/venusd-ble-mqtt.ini",
]


def find_default_config() -> str | None:
    """Return the first existing default config path, or None."""
    for path in DEFAULT_CONFIG_PATHS:
        if os.path.isfile(path):
            return path
    return None


@dataclass
class Config:
    # [ble]
    address: str | None = None
    name_prefix: str | None = None
    notify_uuid: str = protocol.CHAR_NOTIFY
    write_uuid: str = protocol.CHAR_WRITE
    connect_timeout: float = 20.0
    response_timeout: float = 10.0
    connect_retries: int = 3
    retry_delay: float = 3.0

    # [poll]
    interval: float = 120.0
    commands: list[str] = field(default_factory=lambda: list(protocol.DEFAULT_COMMANDS))
    # Seconds of continuous abnormal data (system_normal == False) tolerated
    # before forcing a full BLE disconnect/reconnect to recover. 0 disables the
    # forced reconnect (abnormal data is still never published).
    abnormal_timeout: float = 300.0

    # [mqtt]
    mqtt_host: str = "localhost"
    mqtt_port: int = 1883
    mqtt_username: str | None = None
    mqtt_password: str | None = None
    mqtt_client_id: str = "venusd-ble-mqtt"
    topic_prefix: str = "venusd"
    qos: int = 0
    retain: bool = False
    publish_raw: bool = False  # include raw hex fields in payloads

    # [log]
    log_level: str = "INFO"


def _split_commands(value: str) -> list[str]:
    items = [c.strip() for c in value.replace("\n", ",").split(",")]
    return [c for c in items if c]


def load_config(path: str | None) -> Config:
    """Load config from an INI file; missing file yields defaults.

    If `path` is None, auto-discover one from DEFAULT_CONFIG_PATHS. An
    explicit path that does not exist is an error; auto-discovery quietly
    falls back to built-in defaults when nothing is found.
    """
    cfg = Config()
    if not path:
        path = find_default_config()
        if path:
            log.info("Loading config from %s", path)
        else:
            return cfg

    parser = configparser.ConfigParser()
    read = parser.read(path)
    if not read:
        raise FileNotFoundError(f"Config file not found or unreadable: {path}")

    if parser.has_section("ble"):
        s = parser["ble"]
        cfg.address = s.get("address", cfg.address) or None
        cfg.name_prefix = s.get("name_prefix", cfg.name_prefix) or None
        cfg.notify_uuid = s.get("notify_uuid", cfg.notify_uuid)
        cfg.write_uuid = s.get("write_uuid", cfg.write_uuid)
        cfg.connect_timeout = s.getfloat("connect_timeout", fallback=cfg.connect_timeout)
        cfg.response_timeout = s.getfloat("response_timeout", fallback=cfg.response_timeout)
        cfg.connect_retries = s.getint("connect_retries", fallback=cfg.connect_retries)
        cfg.retry_delay = s.getfloat("retry_delay", fallback=cfg.retry_delay)

    if parser.has_section("poll"):
        s = parser["poll"]
        cfg.interval = s.getfloat("interval", fallback=cfg.interval)
        cfg.abnormal_timeout = s.getfloat("abnormal_timeout", fallback=cfg.abnormal_timeout)
        if "commands" in s:
            cfg.commands = _split_commands(s["commands"])

    if parser.has_section("mqtt"):
        s = parser["mqtt"]
        cfg.mqtt_host = s.get("host", cfg.mqtt_host)
        cfg.mqtt_port = s.getint("port", fallback=cfg.mqtt_port)
        cfg.mqtt_username = s.get("username", cfg.mqtt_username) or None
        cfg.mqtt_password = s.get("password", cfg.mqtt_password) or None
        cfg.mqtt_client_id = s.get("client_id", cfg.mqtt_client_id)
        cfg.topic_prefix = s.get("topic_prefix", cfg.topic_prefix)
        cfg.qos = s.getint("qos", fallback=cfg.qos)
        cfg.retain = s.getboolean("retain", fallback=cfg.retain)
        cfg.publish_raw = s.getboolean("publish_raw", fallback=cfg.publish_raw)

    if parser.has_section("log"):
        cfg.log_level = parser["log"].get("level", cfg.log_level)

    # Validate command names early so typos fail loudly, not silently.
    unknown = [c for c in cfg.commands if c not in protocol.COMMANDS]
    if unknown:
        raise ValueError(
            f"Unknown command(s) in config: {', '.join(unknown)}. "
            f"Known: {', '.join(protocol.COMMANDS)}"
        )
    return cfg
