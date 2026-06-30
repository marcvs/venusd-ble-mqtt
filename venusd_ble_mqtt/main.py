"""
venusd-ble-mqtt: poll a Marstek Venus/HM battery over BLE and publish to MQTT.

Designed for headless Linux with a local mosquitto broker. Polls a configurable
subset of read-only BLE commands at a configurable interval and publishes one
JSON document per command under a topic prefix.

Why BLE instead of the local UDP API: on some units the local API crashes when
polled, so this deliberately uses the independent BLE code path instead.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from . import __version__
from .ble import VenusBLE, VenusBLEError
from .config import Config, load_config
from .mqtt import MqttPublisher
from . import protocol

log = logging.getLogger("venusd_ble_mqtt")


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="venusd-ble-mqtt",
        description="Poll a Marstek Venus/HM battery over BLE and publish to MQTT.",
    )
    p.add_argument(
        "-c", "--config", metavar="FILE",
        help="Path to INI config file. CLI flags override its values.",
    )
    p.add_argument(
        "--address", metavar="MAC",
        help="BLE address of the battery (skips name-based discovery).",
    )
    p.add_argument(
        "--name-prefix", metavar="PREFIX",
        help="Advertised-name prefix to scan for (e.g. MST_ACCP).",
    )
    p.add_argument(
        "-i", "--interval", type=float, metavar="SECONDS",
        help="Polling interval in seconds.",
    )
    p.add_argument(
        "--commands", metavar="LIST",
        help="Comma-separated commands to poll. "
             f"Known: {', '.join(protocol.COMMANDS)}.",
    )
    p.add_argument(
        "--abnormal-timeout", type=float, metavar="SECONDS",
        help="Seconds of abnormal data before forcing a bluetoothctl "
             "disconnect to recover (0 disables).",
    )
    p.add_argument("--mqtt-host", metavar="HOST", help="MQTT broker host.")
    p.add_argument("--mqtt-port", type=int, metavar="PORT", help="MQTT broker port.")
    p.add_argument("--topic-prefix", metavar="PREFIX", help="MQTT topic prefix.")
    p.add_argument(
        "--retain", action="store_true", default=None,
        help="Publish messages with the MQTT retain flag.",
    )
    p.add_argument(
        "--publish-raw", action="store_true", default=None,
        help="Include raw hex fields in published payloads.",
    )
    p.add_argument(
        "--once", action="store_true",
        help="Poll a single cycle and exit (useful for testing).",
    )
    p.add_argument(
        "--list-commands", action="store_true",
        help="List supported BLE commands and exit.",
    )
    p.add_argument(
        "--log-level", metavar="LEVEL",
        help="Logging level (DEBUG, INFO, WARNING, ERROR).",
    )
    p.add_argument(
        "-V", "--version", action="version",
        version=f"%(prog)s {__version__}",
    )
    return p


def merge_cli(cfg: Config, args: argparse.Namespace) -> Config:
    """Overlay any explicitly-provided CLI flags on top of the file config."""
    if args.address is not None:
        cfg.address = args.address
    if args.name_prefix is not None:
        cfg.name_prefix = args.name_prefix
    if args.interval is not None:
        cfg.interval = args.interval
    if args.commands is not None:
        cfg.commands = [c.strip() for c in args.commands.split(",") if c.strip()]
    if args.abnormal_timeout is not None:
        cfg.abnormal_timeout = args.abnormal_timeout
    if args.mqtt_host is not None:
        cfg.mqtt_host = args.mqtt_host
    if args.mqtt_port is not None:
        cfg.mqtt_port = args.mqtt_port
    if args.topic_prefix is not None:
        cfg.topic_prefix = args.topic_prefix
    if args.retain is not None:
        cfg.retain = args.retain
    if args.publish_raw is not None:
        cfg.publish_raw = args.publish_raw
    if args.log_level is not None:
        cfg.log_level = args.log_level

    unknown = [c for c in cfg.commands if c not in protocol.COMMANDS]
    if unknown:
        raise ValueError(
            f"Unknown command(s): {', '.join(unknown)}. "
            f"Known: {', '.join(protocol.COMMANDS)}"
        )
    return cfg


async def _wait_events(timeout: float, *events: asyncio.Event) -> None:
    """Sleep up to `timeout`, returning early if any event is set."""
    waiters = [asyncio.ensure_future(e.wait()) for e in events]
    try:
        await asyncio.wait(
            waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        for w in waiters:
            w.cancel()


async def poll_once(ble: VenusBLE, cfg: Config, pub: MqttPublisher) -> bool:
    """Run one full poll cycle. Returns False if the device reports an abnormal
    system state, in which case the (garbage) data is *not* published.

    A second BLE client (e.g. the rweijnen browser app) sharing the notify
    characteristic interleaves response frames, corrupting reassembly so every
    field decodes as nonsense. The `system` command's system_normal flag is a
    reliable tell, so we poll everything, then publish only when it is
    confirmed normal: an abnormal *or absent* reading suppresses publishing.
    """
    results: dict[str, dict] = {}
    for name in cfg.commands:
        try:
            results[name] = await ble.poll_command(name)
        except VenusBLEError as e:
            log.warning("Command %s failed: %s", name, e)
        except Exception as e:  # keep the loop alive on parser bugs
            log.exception("Unexpected error polling %s: %s", name, e)

    # Health gate: when polling 'system', require a confirmed-normal reading.
    # system_normal being False, None, or missing (command failed) all mean we
    # can't trust the data, so nothing is published.
    if "system" in cfg.commands:
        sys_data = results.get("system")
        normal = sys_data.get("system_normal") if sys_data else None
        if normal is not True:
            log.warning(
                "System health not confirmed normal (system_normal=%r); "
                "skipping publish", normal,
            )
            return False

    for name, data in results.items():
        if not cfg.publish_raw:
            data.pop("raw", None)
        pub.publish_command(name, data)
    return True


async def run(cfg: Config, once: bool, stop: asyncio.Event) -> int:
    pub = MqttPublisher(
        host=cfg.mqtt_host,
        port=cfg.mqtt_port,
        client_id=cfg.mqtt_client_id,
        topic_prefix=cfg.topic_prefix,
        username=cfg.mqtt_username,
        password=cfg.mqtt_password,
        qos=cfg.qos,
        retain=cfg.retain,
    )
    try:
        pub.connect()
    except Exception as e:
        log.error("Could not connect to MQTT broker: %s", e)
        return 2

    backoff = 5.0
    try:
        while not stop.is_set():
            try:
                ble = VenusBLE(
                    address=cfg.address,
                    name_prefix=cfg.name_prefix,
                    notify_uuid=cfg.notify_uuid,
                    write_uuid=cfg.write_uuid,
                    connect_timeout=cfg.connect_timeout,
                    response_timeout=cfg.response_timeout,
                    connect_retries=cfg.connect_retries,
                    retry_delay=cfg.retry_delay,
                )
                async with ble:
                    backoff = 5.0  # reset after a clean connect
                    abnormal_since: float | None = None
                    while not stop.is_set():
                        # poll_once swallows per-command VenusBLEErrors, so a
                        # dropped link would otherwise loop forever logging
                        # "Not connected". Bail out to trigger a reconnect.
                        if not ble.is_connected:
                            raise VenusBLEError("BLE link dropped")
                        healthy = await poll_once(ble, cfg, pub)
                        if once:
                            return 0
                        if healthy:
                            abnormal_since = None
                        elif cfg.abnormal_timeout > 0:
                            now = asyncio.get_running_loop().time()
                            if abnormal_since is None:
                                abnormal_since = now
                                log.warning(
                                    "Abnormal data; will force-reconnect if it "
                                    "persists > %.0fs", cfg.abnormal_timeout,
                                )
                            elif now - abnormal_since >= cfg.abnormal_timeout:
                                # Exiting the context manager disconnects via
                                # bleak (BlueZ Device1.Disconnect), tearing down
                                # the shared ACL link and resetting the device —
                                # the same effect as `bluetoothctl disconnect`.
                                log.warning(
                                    "Abnormal data persisted %.0fs; disconnecting "
                                    "%s to recover",
                                    now - abnormal_since, ble.resolved_address,
                                )
                                raise VenusBLEError(
                                    "abnormal data persisted; forcing reconnect"
                                )
                        # Interruptible sleep: wake early on shutdown or a
                        # dropped link so we don't waste a full interval.
                        await _wait_events(cfg.interval, stop, ble.disconnected)
            except VenusBLEError as e:
                log.warning("BLE session ended: %s", e)
            except Exception as e:
                log.exception("Unexpected session error: %s", e)

            if once:
                return 1
            if not stop.is_set():
                log.info("Reconnecting in %.0fs ...", backoff)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, 60.0)  # capped exponential backoff
        return 0
    finally:
        pub.disconnect()


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    if args.list_commands:
        for name, cmd in protocol.COMMANDS.items():
            print(f"{name:<12} 0x{cmd.opcode:02x}  {cmd.notes}")
        return 0

    # Set up logging before load_config so its config-discovery message is
    # visible; the final level is applied once the config is known.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )

    try:
        cfg = load_config(args.config)
        cfg = merge_cli(cfg, args)
    except (FileNotFoundError, ValueError) as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 2

    logging.getLogger().setLevel(
        getattr(logging, cfg.log_level.upper(), logging.INFO)
    )

    if cfg.abnormal_timeout > 0 and "system" not in cfg.commands:
        log.warning(
            "abnormal_timeout is set but 'system' is not in commands; "
            "abnormal-data detection/recovery is inactive without it."
        )

    stop = asyncio.Event()

    async def _amain() -> int:
        loop = asyncio.get_running_loop()
        main_task = asyncio.current_task()

        def _request_stop() -> None:
            log.info("Shutdown requested")
            stop.set()
            # Setting the event only takes effect between awaits; cancel the
            # task too so a blocking BLE call (e.g. connect) aborts at once.
            main_task.cancel()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _request_stop)
            except NotImplementedError:
                pass  # e.g. on platforms without signal handlers
        try:
            return await run(cfg, args.once, stop)
        except asyncio.CancelledError:
            return 0

    try:
        return asyncio.run(_amain())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
