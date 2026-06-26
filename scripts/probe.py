"""Probe which characteristic to write to and which one replies.

The Marstek vendor service (ff00) exposes several characteristics that all
advertise write-without-response + notify, so the right write/notify pair is
not obvious. This subscribes to every notify-capable characteristic, then
writes the 'runtime' (0x03) command to each candidate in turn and reports
which characteristic produced a response.

Usage:
    python3 scripts/probe.py BC:2A:33:14:C4:B9
"""

import asyncio
import sys

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

from marstek_ble_mqtt import protocol

CANDIDATES = [
    "0000ff01-0000-1000-8000-00805f9b34fb",
    "0000ff02-0000-1000-8000-00805f9b34fb",
    "0000ff06-0000-1000-8000-00805f9b34fb",
]


async def connect_with_retry(address: str, attempts: int = 5) -> BleakClient:
    """Connect, riding through BlueZ's transient le-connection-abort-by-local.

    A fresh scan refreshes BlueZ's device cache, which makes the connect far
    more reliable than hitting the address cold.
    """
    last: Exception | None = None
    for i in range(1, attempts + 1):
        try:
            await BleakScanner.find_device_by_address(address, timeout=10.0)
            client = BleakClient(address, timeout=30.0)
            await client.connect()
            return client
        except (BleakError, EOFError, asyncio.TimeoutError) as e:
            last = e
            print(f"connect attempt {i}/{attempts} failed: {e}")
            await asyncio.sleep(2.0)
    raise SystemExit(f"could not connect after {attempts} attempts: {last}")


async def main(address: str) -> None:
    got: dict[str, bytes] = {}

    def make_cb(uuid: str):
        def cb(_char, data: bytearray) -> None:
            print(f"  <- notify on {uuid}: {bytes(data).hex()}")
            got[uuid] = bytes(data)
        return cb

    client = await connect_with_retry(address)
    try:
        print(f"Connected to {address}\n")
        for uuid in CANDIDATES:
            await client.start_notify(uuid, make_cb(uuid))

        frame = protocol.build_frame(0x03)  # runtime
        print(f"command frame: {frame.hex()}\n")

        for write_uuid in CANDIDATES:
            got.clear()
            print(f"writing 0x03 to {write_uuid} ...")
            try:
                await client.write_gatt_char(write_uuid, frame, response=False)
            except Exception as e:
                print(f"  write failed: {e}")
                continue
            await asyncio.sleep(3.0)
            if not got:
                print("  (no response)")
            print()
    finally:
        await client.disconnect()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: probe.py <BT-ADDRESS>")
    asyncio.run(main(sys.argv[1]))
