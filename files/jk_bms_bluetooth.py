#!/usr/bin/env python3
"""
JK BMS V19 Bluetooth Parser
Connects to JK BMS via BLE and reads telemetry data
"""

import asyncio
import sys
from datetime import datetime
from bleak import BleakClient, BleakScanner
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

# Configuration
BMS_NAME = "JK_B2A8S20P"  # Change this to match your BMS Bluetooth name
BMS_MAC = None  # Set to MAC address like "AA:BB:CC:DD:EE:FF" to skip scanning

POLL_INTERVAL = 5

INFLUX_URL = "http://192.168.50.46:8086"
INFLUX_TOKEN = "XnE9ILRtsWQ0IZpxahUWnTvsPspdSagebJjTD_KsiH9KWzOiWztCRH4hGRHHqshRI_Tlb4xdl2SzhfO270QzrA=="
INFLUX_ORG = "home_monitoring"
INFLUX_BUCKET = "battery_data"

# JK BMS BLE UUIDs
SERVICE_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
CHAR_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"

# JK BMS telemetry command
CMD_TELEMETRY = bytes.fromhex("01 10 16 20 00 01 02 00 00 D6 F1")

# Response header
HEADER = b'\x55\xAA\xEB\x90\x02\x00'


class JKBMSBluetooth:
    def __init__(self):
        self.response_data = bytearray()
        self.data_ready = asyncio.Event()
        
    def notification_handler(self, sender, data):
        """Handle incoming BLE notifications"""
        self.response_data.extend(data)
        # Check if we have enough data
        if len(self.response_data) > 200:
            self.data_ready.set()
    
    async def scan_for_bms(self):
        """Scan for JK BMS devices"""
        print("Scanning for BMS devices...")
        devices = await BleakScanner.discover(timeout=10)
        
        jk_devices = []
        for d in devices:
            name = d.name or "Unknown"
            if "JK" in name.upper() or "BMS" in name.upper():
                jk_devices.append(d)
                print(f"  Found: {name} [{d.address}]")
        
        if not jk_devices:
            print("\nNo JK BMS devices found. All discovered devices:")
            for d in devices:
                print(f"  {d.name or 'Unknown'} [{d.address}]")
            return None
        
        return jk_devices[0].address
    
    async def connect_and_read(self, address):
        """Connect to BMS and read telemetry"""
        self.response_data.clear()
        self.data_ready.clear()
        
        async with BleakClient(address, timeout=20) as client:
            if not client.is_connected:
                return None
            
            # Start notifications
            await client.start_notify(CHAR_UUID, self.notification_handler)
            
            # Send telemetry command
            await client.write_gatt_char(CHAR_UUID, CMD_TELEMETRY, response=False)
            
            # Wait for response
            try:
                await asyncio.wait_for(self.data_ready.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass
            
            await client.stop_notify(CHAR_UUID)
            
            return bytes(self.response_data)


def parse_telemetry(data):
    """Parse telemetry response from JK BMS"""
    if not data or len(data) < 200:
        return None
    
    header_idx = data.find(HEADER)
    if header_idx == -1:
        return None
    
    try:
        result = {}
        offset = header_idx + 6
        
        # Parse 16 cell voltages (2 bytes each, little-endian, mV)
        cells = []
        for _ in range(16):
            cell_mv = int.from_bytes(data[offset:offset+2], 'little')
            if 2000 <= cell_mv <= 5000:
                cells.append(cell_mv / 1000.0)
            offset += 2
        
        if len(cells) < 8:
            return None
        
        result['cells'] = cells
        result['cell_count'] = len(cells)
        result['cell_max'] = max(cells)
        result['cell_min'] = min(cells)
        result['cell_diff'] = result['cell_max'] - result['cell_min']
        result['cell_avg'] = sum(cells) / len(cells)
        result['total_voltage'] = sum(cells)
        
        # Current at offset 158 from header (16-bit signed, mA)
        current_offset = header_idx + 158
        current_ma = int.from_bytes(data[current_offset:current_offset+2], 'little', signed=True)
        result['current'] = current_ma / 1000.0
        
        # Power calculation
        result['power'] = result['total_voltage'] * result['current']
        
        # SOC estimate from cell voltage (LiFePO4: 3.0V=0%, 3.65V=100%)
        avg_v = result['cell_avg']
        result['soc'] = max(0, min(100, int((avg_v - 3.0) / 0.65 * 100)))
        
        return result
    
    except Exception as e:
        print(f"Parse error: {e}")
        return None


def write_to_influxdb(write_api, data):
    """Write battery data to InfluxDB"""
    try:
        ts = datetime.utcnow()
        
        point = Point("battery") \
            .tag("battery_id", "jk_bms_1") \
            .tag("type", "jk_bms_v19") \
            .tag("connection", "bluetooth") \
            .field("soc", float(data['soc'])) \
            .field("voltage", float(data['total_voltage'])) \
            .field("current", float(data['current'])) \
            .field("power", float(data['power'])) \
            .time(ts)
        write_api.write(bucket=INFLUX_BUCKET, record=point)
        
        cell_point = Point("battery_cells") \
            .tag("battery_id", "jk_bms_1") \
            .field("cell_max", float(data['cell_max'])) \
            .field("cell_min", float(data['cell_min'])) \
            .field("cell_diff", float(data['cell_diff'])) \
            .field("cell_avg", float(data['cell_avg'])) \
            .field("cell_count", data['cell_count']) \
            .time(ts)
        write_api.write(bucket=INFLUX_BUCKET, record=cell_point)
        
        for i, voltage in enumerate(data['cells'], 1):
            cell_detail = Point("battery_cell_detail") \
                .tag("battery_id", "jk_bms_1") \
                .tag("cell_number", str(i)) \
                .field("voltage", float(voltage)) \
                .time(ts)
            write_api.write(bucket=INFLUX_BUCKET, record=cell_detail)
        
        return True
    except Exception as e:
        print(f"InfluxDB error: {e}")
        return False


async def main():
    print("JK BMS Bluetooth Parser")
    print("=" * 50)
    
    bms = JKBMSBluetooth()
    
    # Find BMS
    if BMS_MAC:
        address = BMS_MAC
        print(f"Using configured MAC: {address}")
    else:
        address = await bms.scan_for_bms()
        if not address:
            print("No BMS found. Set BMS_MAC manually if needed.")
            sys.exit(1)
    
    print(f"BMS Address: {address}")
    
    # Connect to InfluxDB
    try:
        client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
        write_api = client.write_api(write_options=SYNCHRONOUS)
        if client.health().status == "pass":
            print("InfluxDB: Connected")
        else:
            print("InfluxDB: Unhealthy")
            sys.exit(1)
    except Exception as e:
        print(f"InfluxDB error: {e}")
        sys.exit(1)
    
    print("=" * 50)
    print(f"Polling every {POLL_INTERVAL}s\n")
    
    poll_count = 0
    success_count = 0
    
    try:
        while True:
            poll_count += 1
            timestamp = datetime.now().strftime('%H:%M:%S')
            
            try:
                response = await bms.connect_and_read(address)
                data = parse_telemetry(response) if response else None
                
                if data:
                    success_count += 1
                    status = "CHG" if data['current'] > 0 else "DIS" if data['current'] < 0 else "IDL"
                    
                    print(f"[{timestamp}] {data['total_voltage']:.2f}V {data['current']:+.2f}A "
                          f"{data['power']:+.0f}W SOC:{data['soc']}% Δ:{data['cell_diff']*1000:.0f}mV "
                          f"[{status}]", end="")
                    
                    if write_to_influxdb(write_api, data):
                        print(" ✓")
                    else:
                        print(" ✗")
                else:
                    print(f"[{timestamp}] No data (got {len(response) if response else 0} bytes)")
                    
            except Exception as e:
                print(f"[{timestamp}] Connection error: {e}")
            
            await asyncio.sleep(POLL_INTERVAL)
    
    except KeyboardInterrupt:
        print(f"\n\nStopped. {success_count}/{poll_count} successful polls")
        client.close()


if __name__ == "__main__":
    asyncio.run(main())
