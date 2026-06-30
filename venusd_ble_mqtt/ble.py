"""
BLE transport for the Marstek HM protocol, built on bleak.

Responsibilities:
  - discover/connect to the device (by address or by advertised name)
  - subscribe to the notify characteristic
  - reassemble notification chunks into complete frames
  - issue a command and await its response frame

The device replies to a command by streaming one or more notification
packets. We buffer them and use the frame's length byte (offset 1) to know
when a complete frame has arrived.
"""

from __future__ import annotations

import asyncio
import logging

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

from . import protocol

log = logging.getLogger(__name__)


class VenusBLEError(Exception):
    pass


class VenusBLE:
    def __init__(
        self,
        address: str | None = None,
        name_prefix: str | None = None,
        notify_uuid: str = protocol.CHAR_NOTIFY,
        write_uuid: str = protocol.CHAR_WRITE,
        connect_timeout: float = 20.0,
        response_timeout: float = 10.0,
        connect_retries: int = 3,
        retry_delay: float = 3.0,
    ) -> None:
        if not address and not name_prefix:
            name_prefix = protocol.DEVICE_NAME_PREFIXES[0]
        self.address = address
        # The address we actually connected to (filled in once resolved); used
        # e.g. to target `bluetoothctl disconnect` during recovery.
        self.resolved_address = address
        self.name_prefix = name_prefix
        self.notify_uuid = notify_uuid
        self.write_uuid = write_uuid
        self.connect_timeout = connect_timeout
        self.response_timeout = response_timeout
        self.connect_retries = max(1, connect_retries)
        self.retry_delay = retry_delay

        self._client: BleakClient | None = None
        self._buf = bytearray()
        self._frame_q: asyncio.Queue[bytes] = asyncio.Queue()
        # Set by bleak's disconnect callback so a dropped link is noticed
        # immediately instead of at the next poll-loop check.
        self.disconnected = asyncio.Event()

    async def _resolve_address(self) -> str:
        if self.address:
            return self.address
        log.info("Scanning for device with name prefix %r ...", self.name_prefix)
        device = await BleakScanner.find_device_by_filter(
            lambda d, ad: bool(d.name and d.name.startswith(self.name_prefix)),
            timeout=self.connect_timeout,
        )
        if device is None:
            raise VenusBLEError(
                f"No device advertising name prefix {self.name_prefix!r} found"
            )
        log.info("Found %s (%s)", device.name, device.address)
        return device.address

    def _on_notify(self, _char, data: bytearray) -> None:
        # Accumulate and try to slice off complete frames.
        self._buf.extend(data)
        while len(self._buf) >= 2:
            if self._buf[0] != protocol.FRAME_HEADER:
                # Resync: drop bytes until a header appears.
                idx = self._buf.find(bytes([protocol.FRAME_HEADER]))
                if idx == -1:
                    self._buf.clear()
                    return
                del self._buf[:idx]
                if len(self._buf) < 2:
                    return
            frame_len = self._buf[1]
            if frame_len < 5 or frame_len > 255:
                # Implausible length; drop the header byte and resync.
                del self._buf[0]
                continue
            if len(self._buf) < frame_len:
                return  # wait for more chunks
            frame = bytes(self._buf[:frame_len])
            del self._buf[:frame_len]
            self._frame_q.put_nowait(frame)

    async def __aenter__(self) -> "VenusBLE":
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.disconnect()

    def _on_disconnect(self, _client) -> None:
        log.warning("Device disconnected")
        self.disconnected.set()

    async def connect(self) -> None:
        address = await self._resolve_address()
        self.resolved_address = address
        last: Exception | None = None
        for attempt in range(1, self.connect_retries + 1):
            try:
                # A fresh scan refreshes BlueZ's cache; connecting cold is what
                # tends to fail with "not found" / le-connection-abort-by-local.
                try:
                    await BleakScanner.find_device_by_address(
                        address, timeout=min(self.connect_timeout, 10.0)
                    )
                except Exception as e:  # best-effort cache warm; don't fail here
                    log.debug("Pre-connect scan did not find %s: %s", address, e)

                self._buf.clear()
                self.disconnected.clear()
                self._client = BleakClient(
                    address,
                    timeout=self.connect_timeout,
                    disconnected_callback=self._on_disconnect,
                )
                await self._client.connect()
                await self._client.start_notify(self.notify_uuid, self._on_notify)
                log.info("Connected and subscribed to notifications")
                return
            except (BleakError, EOFError, asyncio.TimeoutError) as e:
                last = e
                # repr(), not str(): EOFError/TimeoutError stringify to "" and
                # would otherwise log a blank, hiding which failure it was.
                log.warning(
                    "Connect attempt %d/%d failed: %r",
                    attempt, self.connect_retries, e,
                )
                await self.disconnect()
                if attempt < self.connect_retries:
                    await asyncio.sleep(self.retry_delay)
        raise VenusBLEError(
            f"Could not connect to {address} after "
            f"{self.connect_retries} attempts: {last!r}"
        )

    async def disconnect(self) -> None:
        if self._client is not None:
            try:
                if self._client.is_connected:
                    await self._client.stop_notify(self.notify_uuid)
                    await self._client.disconnect()
            except Exception as e:  # best-effort teardown
                log.debug("Error during disconnect: %s", e)
            finally:
                self._client = None

    @property
    def is_connected(self) -> bool:
        return (
            self._client is not None
            and self._client.is_connected
            and not self.disconnected.is_set()
        )

    async def request(self, command: int, payload: bytes = b"") -> bytes:
        """Send a command frame and return the raw payload of the response."""
        if not self.is_connected:
            raise VenusBLEError("Not connected")
        # Drain any stale frames before issuing a fresh request.
        while not self._frame_q.empty():
            self._frame_q.get_nowait()

        frame = protocol.build_frame(command, payload)
        await self._client.write_gatt_char(self.write_uuid, frame, response=False)

        try:
            resp = await asyncio.wait_for(
                self._frame_q.get(), timeout=self.response_timeout
            )
        except asyncio.TimeoutError as e:
            raise VenusBLEError(
                f"Timeout waiting for response to command 0x{command:02x}"
            ) from e

        if not protocol.verify_frame(resp):
            log.warning(
                "Checksum/format mismatch on 0x%02x response: %s",
                command, resp.hex(),
            )
        return protocol.extract_payload(resp)

    async def poll_command(self, name: str) -> dict:
        """Run a named command from the registry and return parsed data."""
        cmd = protocol.COMMANDS[name]
        payload = await self.request(cmd.opcode)
        return cmd.parser(payload)
