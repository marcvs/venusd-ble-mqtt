"""Dump the GATT services/characteristics of a BLE device.

Usage:
    python3 scripts/gatt_dump.py BC:2A:33:14:C4:B9

Look for the characteristic whose properties include 'notify' (or 'indicate')
-> that's notify_uuid, and the one with 'write'/'write-without-response'
-> that's write_uuid. Put them under [ble] in your config.
"""

import asyncio
import sys

from bleak import BleakClient


async def main(address: str) -> None:
    async with BleakClient(address, timeout=30.0) as client:
        print(f"Connected to {address}\n")
        for service in client.services:
            print(f"[service] {service.uuid}  ({service.description})")
            for char in service.characteristics:
                props = ",".join(char.properties)
                print(f"    [char] {char.uuid}  props={props}")
                for desc in char.descriptors:
                    print(f"        [desc] {desc.uuid}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: gatt_dump.py <BT-ADDRESS>")
    asyncio.run(main(sys.argv[1]))
