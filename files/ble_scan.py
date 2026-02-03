#!/usr/bin/env python3
"""
Bluetooth LE Scanner - Find JK BMS devices
"""

import asyncio
from bleak import BleakScanner

async def scan():
    print("Scanning for Bluetooth LE devices (10 seconds)...")
    print("=" * 60)
    
    devices = await BleakScanner.discover(timeout=10, return_adv=True)
    
    jk_devices = []
    other_devices = []
    
    for address, (device, adv_data) in devices.items():
        name = device.name or adv_data.local_name or "Unknown"
        rssi = adv_data.rssi
        
        if "JK" in name.upper() or "BMS" in name.upper():
            jk_devices.append((name, address, rssi))
        else:
            other_devices.append((name, address, rssi))
    
    if jk_devices:
        print("\n🔋 JK BMS Devices Found:")
        print("-" * 60)
        for name, addr, rssi in sorted(jk_devices, key=lambda x: x[2], reverse=True):
            print(f"  {name:<30} {addr}  RSSI: {rssi} dBm")
    else:
        print("\n⚠️  No JK BMS devices found")
    
    print(f"\n📱 Other Devices ({len(other_devices)}):")
    print("-" * 60)
    for name, addr, rssi in sorted(other_devices, key=lambda x: x[2], reverse=True)[:20]:
        print(f"  {name:<30} {addr}  RSSI: {rssi} dBm")
    
    if len(other_devices) > 20:
        print(f"  ... and {len(other_devices) - 20} more")
    
    print("\n" + "=" * 60)
    if jk_devices:
        print(f"Found {len(jk_devices)} JK BMS device(s)")
        print(f"\nTo use, set BMS_MAC in jk_bms_bluetooth.py to:")
        print(f'  BMS_MAC = "{jk_devices[0][1]}"')

if __name__ == "__main__":
    asyncio.run(scan())
