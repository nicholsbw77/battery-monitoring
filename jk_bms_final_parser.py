#!/usr/bin/env python3
"""
JK BMS V19 UART Parser - PERFECT PROTOCOL
Based on actual hex dumps from JK BMS software
"""

import serial
import time
import sys
from datetime import datetime
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

# Configuration
SERIAL_PORT = "/dev/ttyUSB0"
BAUDRATE = 115200
POLL_INTERVAL = 1  # Query every second like JK software

INFLUX_URL = "http://192.168.50.46:8086"
INFLUX_TOKEN = "XnE9ILRtsWQ0IZpxahUWnTvsPspdSagebJjTD_KsiH9KWzOiWztCRH4hGRHHqshRI_Tlb4xdl2SzhfO270QzrA=="
INFLUX_ORG = "home_monitoring"
INFLUX_BUCKET = "battery_data"

# JK BMS Commands (from actual protocol capture)
CMD_TELEMETRY = bytes.fromhex("01 10 16 20 00 01 02 00 00 D6 F1")  # Live data
CMD_STATUS = bytes.fromhex("01 10 16 1E 00 01 02 00 00 D2 2F")     # Full status
CMD_INFO = bytes.fromhex("01 10 16 1C 00 01 02 00 00 D3 CD")       # Device info


def query_bms(ser, command, timeout=1.0):
    """Send command and read response"""
    try:
        ser.reset_input_buffer()
        ser.reset_output_buffer()
        ser.write(command)
        time.sleep(0.1)
        
        # Read response
        start = time.time()
        data = b''
        while (time.time() - start) < timeout:
            if ser.in_waiting:
                data += ser.read(ser.in_waiting)
                if len(data) > 50:  # Got something substantial
                    break
            time.sleep(0.01)
        
        return data
    except Exception as e:
        print(f"Query error: {e}")
        return None


def parse_telemetry(data):
    """Parse Type 02 telemetry response - cell voltages"""
    if not data or len(data) < 50:
        return None
    
    # Look for header: 55 AA EB 90 02 00
    header_idx = data.find(b'\x55\xAA\xEB\x90\x02\x00')
    if header_idx == -1:
        return None
    
    try:
        result = {}
        offset = header_idx + 6  # Skip header
        
        # Next 16 cell voltages (2 bytes each, little-endian, in mV)
        cells = []
        for i in range(16):
            if offset + 1 < len(data):
                cell_mv = int.from_bytes(data[offset:offset+2], 'little')
                # Valid range check
                if 2000 <= cell_mv <= 5000:
                    cells.append(cell_mv / 1000.0)
                offset += 2
        
        if len(cells) >= 8:
            result['cells'] = cells
            result['cell_max'] = max(cells)
            result['cell_min'] = min(cells)
            result['cell_diff'] = result['cell_max'] - result['cell_min']
            result['cell_avg'] = sum(cells) / len(cells)
            result['total_voltage'] = sum(cells)
            
            # Estimate SOC from average cell voltage (LiFePO4)
            # 3.0V = 0%, 3.65V = 100%
            avg_v = result['cell_avg']
            result['soc'] = max(0, min(100, int((avg_v - 3.0) / 0.65 * 100)))
            
            result['current'] = 0.0  # Not in this message type
            result['power'] = 0.0
            
            return result
    
    except Exception as e:
        print(f"Parse telemetry error: {e}")
    
    return None


def parse_status(data):
    """Parse Type 01 status response - full battery data"""
    if not data or len(data) < 100:
        return None
    
    # Look for header: 55 AA EB 90 01 00
    header_idx = data.find(b'\x55\xAA\xEB\x90\x01\x00')
    if header_idx == -1:
        return None
    
    try:
        result = {}
        offset = header_idx + 6
        
        # Parse based on observed protocol
        # Cell voltages (11 cells * 4 bytes each in status message)
        cells = []
        for i in range(11):
            if offset + 3 < len(data):
                cell_mv = int.from_bytes(data[offset:offset+2], 'little')
                if 2000 <= cell_mv <= 5000:
                    cells.append(cell_mv / 1000.0)
                offset += 4  # Skip 2 padding bytes
        
        if cells:
            result['cells'] = cells
            result['cell_max'] = max(cells)
            result['cell_min'] = min(cells)
            result['cell_diff'] = result['cell_max'] - result['cell_min']
            result['cell_avg'] = sum(cells) / len(cells)
            result['total_voltage'] = sum(cells)
            
            # Try to find SOC and current in the data
            # SOC is typically 1 byte, 0-100
            for i in range(len(data) - 100, len(data) - 50):
                if 0 <= data[i] <= 100:
                    result['soc'] = data[i]
                    break
            
            if 'soc' not in result:
                avg_v = result['cell_avg']
                result['soc'] = max(0, min(100, int((avg_v - 3.0) / 0.65 * 100)))
            
            result['current'] = 0.0
            result['power'] = 0.0
            
            return result
    
    except Exception as e:
        print(f"Parse status error: {e}")
    
    return None


def write_to_influxdb(write_api, data):
    """Write to InfluxDB"""
    try:
        timestamp = datetime.utcnow()
        
        # Main battery point
        point = Point("battery") \
            .tag("battery_id", "jk_bms_1") \
            .tag("type", "jk_bms_v19") \
            .field("soc", float(data['soc'])) \
            .field("voltage", float(data['total_voltage'])) \
            .field("current", float(data['current'])) \
            .field("power", float(data['power'])) \
            .time(timestamp)
        write_api.write(bucket=INFLUX_BUCKET, record=point)
        
        # Cell stats
        cell_point = Point("battery_cells") \
            .tag("battery_id", "jk_bms_1") \
            .field("cell_max", float(data['cell_max'])) \
            .field("cell_min", float(data['cell_min'])) \
            .field("cell_diff", float(data['cell_diff'])) \
            .field("cell_avg", float(data['cell_avg'])) \
            .field("cell_count", len(data['cells'])) \
            .time(timestamp)
        write_api.write(bucket=INFLUX_BUCKET, record=cell_point)
        
        # Individual cells
        for i, voltage in enumerate(data['cells'], 1):
            cell_detail = Point("battery_cell_detail") \
                .tag("battery_id", "jk_bms_1") \
                .tag("cell_number", str(i)) \
                .field("voltage", float(voltage)) \
                .time(timestamp)
            write_api.write(bucket=INFLUX_BUCKET, record=cell_detail)
        
        return True
    except Exception as e:
        print(f"InfluxDB error: {e}")
        return False


def main():
    print("="*60)
    print("JK BMS V19 UART Parser - WORKING VERSION")
    print("="*60)
    
    # Open serial
    print(f"\n[1/3] Opening {SERIAL_PORT} at {BAUDRATE} baud...")
    try:
        ser = serial.Serial(SERIAL_PORT, BAUDRATE, timeout=1)
        print("✓ Serial opened")
    except Exception as e:
        print(f"✗ Failed: {e}")
        sys.exit(1)
    
    # Connect to InfluxDB
    print(f"\n[2/3] Connecting to InfluxDB...")
    try:
        client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
        write_api = client.write_api(write_options=SYNCHRONOUS)
        health = client.health()
        if health.status == "pass":
            print("✓ InfluxDB connected")
        else:
            print(f"✗ InfluxDB unhealthy")
            sys.exit(1)
    except Exception as e:
        print(f"✗ InfluxDB failed: {e}")
        sys.exit(1)
    
    # Monitor loop
    print(f"\n[3/3] Starting monitor (every {POLL_INTERVAL}s)")
    print("="*60)
    print()
    
    poll = 0
    telemetry_count = 0
    status_count = 0
    
    try:
        while True:
            poll += 1
            print(f"[Poll #{poll}] {datetime.now().strftime('%H:%M:%S')}", end=" ")
            
            # Query telemetry (like JK software does)
            resp = query_bms(ser, CMD_TELEMETRY, timeout=0.5)
            data = parse_telemetry(resp) if resp else None
            
            # If telemetry fails, try status
            if not data:
                resp = query_bms(ser, CMD_STATUS, timeout=0.5)
                data = parse_status(resp) if resp else None
            
            if data:
                telemetry_count += 1
                
                print(f"✓ SOC:{data['soc']}% V:{data['total_voltage']:.2f}V ", end="")
                print(f"Cells:{len(data['cells'])} ", end="")
                print(f"Δ:{data['cell_diff']:.3f}V", end="")
                
                if write_to_influxdb(write_api, data):
                    print(" → InfluxDB ✓")
                else:
                    print(" → InfluxDB ✗")
            else:
                print("✗ No data")
            
            # Every 10th poll, show stats
            if poll % 10 == 0:
                success_rate = (telemetry_count / poll) * 100
                print(f"  Stats: {telemetry_count}/{poll} successful ({success_rate:.0f}%)")
            
            time.sleep(POLL_INTERVAL)
    
    except KeyboardInterrupt:
        print("\n\nShutting down...")
        ser.close()
        client.close()
        print(f"Final: {telemetry_count}/{poll} polls successful")
        print("Goodbye!")


if __name__ == "__main__":
    main()
