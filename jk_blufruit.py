#!/usr/bin/env python3
"""
JK BMS V19 Monitor - UART to InfluxDB
Final working version with pattern-matching parser
"""

import serial
import time
import sys
from datetime import datetime
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

# ===== CONFIGURATION =====
SERIAL_PORT = "/dev/ttyUSB2"
BAUDRATE = 115200
POLL_INTERVAL = 20

INFLUX_URL = "http://192.168.50.46:8086"
INFLUX_TOKEN = "XnE9ILRtsWQ0IZpxahUWnTvsPspdSagebJjTD_KsiH9KWzOiWztCRH4hGRHHqshRI_Tlb4xdl2SzhfO270QzrA=="
INFLUX_ORG = "home_monitoring"
INFLUX_BUCKET = "battery_data"

# Commands to try
COMMANDS = [
    b'\x01\x03\x00\x00\x00\x7D\x84\x3E',
    b'\x01\x03\x00\x48\x00\x38\x44\x34',
    b'\x01\x03\x00\x00\x00\x20\x44\x0B',
]


def query_bms(ser, command):
    """Send command and read response"""
    try:
        ser.reset_input_buffer()
        ser.reset_output_buffer()
        ser.write(command)
        time.sleep(0.5)
        return ser.read(300)
    except Exception as e:
        print(f"  Serial error: {e}")
        return None


def parse_response(resp):
    """Parse JK BMS response - looks for cell voltage patterns"""
    if not resp or len(resp) < 30:
        return None
    
    try:
        data = {}
        cells = []
        
        # Method 1: Look for 4-byte pattern (voltage + 0x0000 padding)
        i = 0
        while i < len(resp) - 3:
            if resp[i+2] == 0x00 and resp[i+3] == 0x00:
                voltage_mv = int.from_bytes(resp[i:i+2], 'little')
                if 2500 <= voltage_mv <= 4500:
                    cells.append(voltage_mv / 1000.0)
                    i += 4
                    continue
            i += 1
        
        # Method 2: If method 1 didn't find enough cells, try scanning for any voltage-like values
        if len(cells) < 8:
            cells = []
            for i in range(len(resp) - 1):
                val_le = int.from_bytes(resp[i:i+2], 'little')
                val_be = int.from_bytes(resp[i:i+2], 'big')
                
                if 2500 <= val_le <= 4500:
                    if not cells or abs(cells[-1] - val_le/1000.0) > 0.01:
                        cells.append(val_le / 1000.0)
                elif 2500 <= val_be <= 4500:
                    if not cells or abs(cells[-1] - val_be/1000.0) > 0.01:
                        cells.append(val_be / 1000.0)
        
        # If we found reasonable number of cells
        if 8 <= len(cells) <= 16:
            data['cells'] = cells[:16]
            data['cell_max'] = max(data['cells'])
            data['cell_min'] = min(data['cells'])
            data['cell_diff'] = data['cell_max'] - data['cell_min']
            data['cell_avg'] = sum(data['cells']) / len(data['cells'])
            data['total_voltage'] = sum(data['cells'])
            
            # Estimate SOC from average cell voltage
            avg_v = data['cell_avg']
            data['soc'] = max(0, min(100, int((avg_v - 3.0) / 0.65 * 100)))
            
            data['current'] = 0.0
            data['power'] = 0.0
            
            return data
        
        return None
    
    except Exception as e:
        print(f"  Parse error: {e}")
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
        
        # Cell statistics
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
        print(f"  InfluxDB error: {e}")
        return False


def main():
    print("="*60)
    print("JK BMS Monitor → InfluxDB")
    print("="*60)
    
    # Open serial
    print(f"\nOpening {SERIAL_PORT} at {BAUDRATE} baud...")
    try:
        ser = serial.Serial(SERIAL_PORT, BAUDRATE, timeout=2)
        print("✓ Serial opened")
    except Exception as e:
        print(f"✗ Failed: {e}")
        sys.exit(1)
    
    # Connect to InfluxDB
    print(f"\nConnecting to InfluxDB...")
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
    
    # Find working command
    print("\nTesting commands...")
    working_cmd = None
    for cmd in COMMANDS:
        resp = query_bms(ser, cmd)
        data = parse_response(resp)
        if data:
            print(f"✓ Command {cmd.hex()} works!")
            working_cmd = cmd
            break
    
    if not working_cmd:
        print("✗ No working command found")
        ser.close()
        sys.exit(1)
    
    # Monitor loop
    print(f"\nStarting monitor (every {POLL_INTERVAL}s)")
    print("="*60)
    print()
    
    poll = 0
    try:
        while True:
            poll += 1
            print(f"[Poll #{poll}] {datetime.now().strftime('%H:%M:%S')}")
            
            resp = query_bms(ser, working_cmd)
            data = parse_response(resp)
            
            if data:
                print(f"  SOC: {data['soc']}%")
                print(f"  Voltage: {data['total_voltage']:.2f}V")
                print(f"  Cells: {len(data['cells'])}")
                print(f"    Max: {data['cell_max']:.3f}V | Min: {data['cell_min']:.3f}V | Diff: {data['cell_diff']:.3f}V")
                
                if write_to_influxdb(write_api, data):
                    print("  ✓ Written to InfluxDB")
                else:
                    print("  ✗ InfluxDB write failed")
            else:
                print("  ✗ No valid data")
            
            print()
            time.sleep(POLL_INTERVAL)
    
    except KeyboardInterrupt:
        print("\nShutting down...")
        ser.close()
        client.close()
        print("Goodbye!")


if __name__ == "__main__":
    main()
