#!/usr/bin/env python3
"""
JK BMS V19 UART Parser - Clean Version
Parses telemetry data from JK BMS over serial connection
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
POLL_INTERVAL = 5

INFLUX_URL = "http://192.168.50.46:8086"
INFLUX_TOKEN = "XnE9ILRtsWQ0IZpxahUWnTvsPspdSagebJjTD_KsiH9KWzOiWztCRH4hGRHHqshRI_Tlb4xdl2SzhfO270QzrA=="
INFLUX_ORG = "home_monitoring"
INFLUX_BUCKET = "battery_data"

# JK BMS telemetry command
CMD_TELEMETRY = bytes.fromhex("01 10 16 20 00 01 02 00 00 D6 F1")

# Response header
HEADER = b'\x55\xAA\xEB\x90\x02\x00'


def query_bms(ser, timeout=0.5):
    """Send telemetry command and read response"""
    ser.reset_input_buffer()
    ser.write(CMD_TELEMETRY)
    time.sleep(0.1)
    
    data = b''
    start = time.time()
    while (time.time() - start) < timeout:
        if ser.in_waiting:
            data += ser.read(ser.in_waiting)
            if len(data) > 200:
                break
        time.sleep(0.01)
    return data


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
        
        # Main battery metrics
        point = Point("battery") \
            .tag("battery_id", "jk_bms_1") \
            .tag("type", "jk_bms_v19") \
            .field("soc", float(data['soc'])) \
            .field("voltage", float(data['total_voltage'])) \
            .field("current", float(data['current'])) \
            .field("power", float(data['power'])) \
            .time(ts)
        write_api.write(bucket=INFLUX_BUCKET, record=point)
        
        # Cell statistics
        cell_point = Point("battery_cells") \
            .tag("battery_id", "jk_bms_1") \
            .field("cell_max", float(data['cell_max'])) \
            .field("cell_min", float(data['cell_min'])) \
            .field("cell_diff", float(data['cell_diff'])) \
            .field("cell_avg", float(data['cell_avg'])) \
            .field("cell_count", data['cell_count']) \
            .time(ts)
        write_api.write(bucket=INFLUX_BUCKET, record=cell_point)
        
        # Individual cell voltages
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


def main():
    print("JK BMS V19 Parser")
    print("=" * 50)
    
    # Open serial port
    try:
        ser = serial.Serial(SERIAL_PORT, BAUDRATE, timeout=1)
        print(f"Serial: {SERIAL_PORT} @ {BAUDRATE}")
    except Exception as e:
        print(f"Serial error: {e}")
        sys.exit(1)
    
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
            
            response = query_bms(ser)
            data = parse_telemetry(response)
            
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
                print(f"[{timestamp}] No data")
            
            time.sleep(POLL_INTERVAL)
    
    except KeyboardInterrupt:
        print(f"\n\nStopped. {success_count}/{poll_count} successful polls")
        ser.close()
        client.close()


if __name__ == "__main__":
    main()
