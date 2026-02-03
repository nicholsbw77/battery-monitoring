#!/usr/bin/env python3
"""
JK BMS Parser V4 - Specifically for JK-PB2A16S Series
Based on actual protocol analysis from diagnostic capture

Frame Type 02 (Telemetry) Format:
  Offset 0-5:   Header (55 AA EB 90 02 00)
  Offset 6-37:  16 cell voltages (2 bytes each, little-endian, mV)
  Offset 38-69: 16 cell resistances/padding (2 bytes each)
  Offset 70-71: Unknown (FF FF)
  Offset 72-73: Unknown (00 00)  
  Offset 74-75: Average cell voltage (little-endian, mV)
  Offset 76-77: Delta cell voltage (little-endian, mV)
  Offset 78-79: Max cell number
  Offset 80-81: Min cell number
  ... more fields ...
  Offset 144:   MOS temperature (with offset)
  Offset 150-151: Current (needs analysis)
  ... etc ...

This parser is based on reverse-engineering the actual responses
from your JK-PB2A16S-20P BMS.
"""

import serial
import time
import sys
import struct
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

# JK BMS Telemetry Command (this works with your BMS)
CMD_TELEMETRY = bytes.fromhex("01 10 16 20 00 01 02 00 00 D6 F1")

# Debug mode
DEBUG = True


def query_bms(ser, command, timeout=2.0):
    """Send command and read response"""
    try:
        ser.reset_input_buffer()
        ser.reset_output_buffer()
        ser.write(command)
        
        time.sleep(0.1)
        
        start = time.time()
        data = b''
        while (time.time() - start) < timeout:
            if ser.in_waiting:
                chunk = ser.read(ser.in_waiting)
                data += chunk
                if len(data) >= 300:  # Expected ~308 bytes
                    time.sleep(0.05)
                    if ser.in_waiting:
                        data += ser.read(ser.in_waiting)
                    break
            time.sleep(0.01)
        
        return data
    except Exception as e:
        print(f"Query error: {e}")
        return None


def parse_type02_telemetry(data):
    """
    Parse Frame Type 02 (Telemetry) response
    Based on actual capture from JK-PB2A16S-20P
    """
    if not data or len(data) < 200:
        return None
    
    # Find header: 55 AA EB 90 02 00
    header_idx = data.find(b'\x55\xAA\xEB\x90\x02\x00')
    if header_idx == -1:
        if DEBUG:
            print("  Header 55 AA EB 90 02 00 not found")
        return None
    
    # Offset from start of frame
    d = data[header_idx:]
    
    if len(d) < 200:
        if DEBUG:
            print(f"  Frame too short: {len(d)} bytes")
        return None
    
    result = {}
    
    # ===== CELL VOLTAGES (offset 6-37, 16 cells × 2 bytes, little-endian) =====
    cells = []
    for i in range(16):
        offset = 6 + i * 2
        cell_mv = struct.unpack('<H', d[offset:offset+2])[0]
        if 1000 <= cell_mv <= 5000:  # Valid cell voltage range
            cells.append(cell_mv / 1000.0)
        elif cell_mv == 0:
            pass  # Skip unused cells
        else:
            if DEBUG:
                print(f"  Invalid cell {i+1} voltage: {cell_mv} mV")
    
    if len(cells) < 4:
        if DEBUG:
            print(f"  Too few valid cells: {len(cells)}")
        return None
    
    result['cells'] = cells
    result['cell_count'] = len(cells)
    result['cell_max'] = max(cells)
    result['cell_min'] = min(cells)
    result['cell_diff'] = result['cell_max'] - result['cell_min']
    result['cell_avg'] = sum(cells) / len(cells)
    result['total_voltage'] = sum(cells)
    
    # ===== CELL BALANCE CURRENT/RESISTANCE (offset 38-69, 16 × 2 bytes) =====
    # These appear to be balance-related values
    # Skipping for now
    
    # ===== DIAGNOSTIC: Print bytes around suspected current location =====
    if DEBUG:
        print(f"  Cells: {len(cells)}, Total: {result['total_voltage']:.2f}V")
        print(f"  Cell range: {result['cell_min']:.3f}V - {result['cell_max']:.3f}V (Δ{result['cell_diff']*1000:.1f}mV)")
        
        # Print bytes from offset 70 onwards to find current
        print(f"  Bytes 70-100: {d[70:100].hex()}")
        print(f"  Bytes 140-170: {d[140:170].hex()}")
    
    # ===== TEMPERATURES (multiple locations to check) =====
    # Based on frame analysis, temperatures seem to be around offset 144+
    # Temperature format: raw value where 100 = 0°C (so temp = raw - 100, or raw + offset)
    
    # Offset 144 (0x90): First byte after lots of zeros
    # From capture: A0 00 = 160, which could be 60°C (160-100) - seems high
    # Let's try: temperature = raw value directly if < 100, else raw - 100
    
    temp_offset = 144
    if temp_offset + 1 < len(d):
        temp_raw = d[temp_offset]
        # JK BMS temp format: 0-100 maps to -40 to 60°C or similar
        # Let's use: if raw > 100, temp = raw - 100; else temp = raw - 40
        if temp_raw > 100:
            result['temp_mos'] = temp_raw - 100
        else:
            result['temp_mos'] = temp_raw - 40
    
    # ===== CURRENT DETECTION =====
    # This is the tricky part. Let's check multiple locations.
    # From your diagnostic, current is likely 0 (idle), so we need to find
    # where a non-zero value would appear.
    
    # Common locations for current in JK BMS Type 02 frames:
    # - After cell data and padding
    # - Often as a 16-bit signed value in 10mA or 100mA units
    
    # Let's check offset 150-151 based on protocol patterns
    current = 0.0
    current_found = False
    
    # Method 1: Check around offset 150 (common location)
    for test_offset in [150, 152, 154, 156, 134, 136, 138]:
        if test_offset + 1 < len(d):
            # Try as signed 16-bit, 10mA units
            raw_signed = struct.unpack('<h', d[test_offset:test_offset+2])[0]
            raw_unsigned = struct.unpack('<H', d[test_offset:test_offset+2])[0]
            
            if DEBUG and test_offset <= 156:
                print(f"  Offset {test_offset}: signed={raw_signed}, unsigned={raw_unsigned} (0x{raw_unsigned:04X})")
            
            # Check if this looks like a valid current value
            # Current in 10mA units: ±30000 = ±300A range
            if raw_signed != 0 and -30000 < raw_signed < 30000:
                if abs(raw_signed) > 10:  # More than 100mA
                    current = raw_signed / 100.0  # Convert 10mA to A
                    current_found = True
                    if DEBUG:
                        print(f"  -> Possible current at offset {test_offset}: {current:.2f}A")
    
    # Method 2: Check for offset-10000 encoding
    for test_offset in [150, 152, 154]:
        if test_offset + 1 < len(d):
            raw = struct.unpack('<H', d[test_offset:test_offset+2])[0]
            if 8000 <= raw <= 12000:  # Looks like offset-10000 encoding
                current = (10000 - raw) * 0.01
                current_found = True
                if DEBUG:
                    print(f"  -> Offset-10000 current at {test_offset}: raw={raw}, current={current:.2f}A")
                break
    
    # Method 3: Check for new protocol (bit 15 = direction)
    for test_offset in [150, 152, 154]:
        if test_offset + 1 < len(d):
            raw = struct.unpack('<H', d[test_offset:test_offset+2])[0]
            if raw & 0x8000:  # Bit 15 set = charging
                magnitude = (raw & 0x7FFF) * 0.01
                current = magnitude
                current_found = True
                if DEBUG:
                    print(f"  -> New protocol (charging) at {test_offset}: {current:.2f}A")
                break
    
    # If still not found, check the byte at offset 158 which had 9E 2D 02 00
    # This could be accumulated Ah (0x00022D9E = 142750 mAh = 142.75 Ah)
    
    result['current'] = current
    
    # ===== SOC (State of Charge) =====
    # SOC is typically a single byte, value 0-100
    # Common locations: around offset 164-180
    
    soc_found = False
    for test_offset in [151, 161, 164, 165, 166, 167, 180, 181]:
        if test_offset < len(d):
            potential_soc = d[test_offset]
            if 0 <= potential_soc <= 100:
                # Verify it's a reasonable SOC (cross-check with voltage)
                expected_soc = int((result['cell_avg'] - 2.5) / (3.65 - 2.5) * 100)
                expected_soc = max(0, min(100, expected_soc))
                
                # Allow some tolerance
                if abs(potential_soc - expected_soc) < 40 or potential_soc == 100:
                    result['soc'] = potential_soc
                    soc_found = True
                    if DEBUG:
                        print(f"  SOC found at offset {test_offset}: {potential_soc}%")
                    break
    
    if not soc_found:
        # Estimate from voltage (LiFePO4: 2.5V=0%, 3.65V=100%)
        avg_v = result['cell_avg']
        result['soc'] = max(0, min(100, int((avg_v - 2.5) / (3.65 - 2.5) * 100)))
        if DEBUG:
            print(f"  SOC estimated from voltage: {result['soc']}%")
    
    # ===== POWER CALCULATION =====
    result['power'] = result['total_voltage'] * result['current']
    
    # ===== CHECK SPECIFIC KNOWN LOCATIONS IN YOUR FRAME =====
    # From your capture at offset 0xB0 (176): 
    # 63 4E BE 04 00 = possibly capacity or cycle info
    # At 0xB4: 90 CA 04 00 = 0x0004CA90 = 314000 (mAh = 314 Ah capacity?)
    
    if 0xB4 + 4 <= len(d):
        capacity_raw = struct.unpack('<I', d[0xB4:0xB4+4])[0]
        if 10000 < capacity_raw < 1000000:  # Reasonable mAh range
            result['capacity_mah'] = capacity_raw
            if DEBUG:
                print(f"  Capacity at 0xB4: {capacity_raw} mAh ({capacity_raw/1000:.1f} Ah)")
    
    # ===== CYCLE COUNT =====
    # Often at offset 0xB8
    if 0xB8 + 2 <= len(d):
        cycles = struct.unpack('<H', d[0xB8:0xB8+2])[0]
        if cycles < 10000:  # Reasonable cycle count
            result['cycle_count'] = cycles
            if DEBUG:
                print(f"  Cycle count at 0xB8: {cycles}")
    
    return result


def write_to_influxdb(write_api, data):
    """Write to InfluxDB"""
    try:
        timestamp = datetime.utcnow()
        
        # Main battery point
        point = Point("battery") \
            .tag("battery_id", "jk_bms_1") \
            .tag("type", "jk_bms_pb2a16s") \
            .field("soc", float(data.get('soc', 0))) \
            .field("voltage", float(data.get('total_voltage', 0))) \
            .field("current", float(data.get('current', 0))) \
            .field("power", float(data.get('power', 0))) \
            .time(timestamp)
        
        if 'cycle_count' in data:
            point.field("cycle_count", int(data['cycle_count']))
        if 'capacity_mah' in data:
            point.field("capacity_ah", float(data['capacity_mah'] / 1000))
        if 'temp_mos' in data:
            point.field("temp_mos", float(data['temp_mos']))
        
        write_api.write(bucket=INFLUX_BUCKET, record=point)
        
        # Cell stats
        cell_point = Point("battery_cells") \
            .tag("battery_id", "jk_bms_1") \
            .field("cell_max", float(data['cell_max'])) \
            .field("cell_min", float(data['cell_min'])) \
            .field("cell_diff", float(data['cell_diff'])) \
            .field("cell_avg", float(data['cell_avg'])) \
            .field("cell_count", int(data['cell_count'])) \
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
    global DEBUG
    
    print("=" * 70)
    print("JK BMS Parser V4 - For JK-PB2A16S Series (Frame Type 02)")
    print("=" * 70)
    
    # Check command line args
    if "--no-debug" in sys.argv:
        DEBUG = False
    
    # Open serial
    print(f"\n[1/3] Opening {SERIAL_PORT} at {BAUDRATE} baud...")
    try:
        ser = serial.Serial(SERIAL_PORT, BAUDRATE, timeout=1)
        print("✓ Serial opened")
    except Exception as e:
        print(f"✗ Failed: {e}")
        sys.exit(1)
    
    # Connect to InfluxDB
    print(f"\n[2/3] Connecting to InfluxDB at {INFLUX_URL}...")
    try:
        client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
        write_api = client.write_api(write_options=SYNCHRONOUS)
        health = client.health()
        if health.status == "pass":
            print("✓ InfluxDB connected")
        else:
            print(f"✗ InfluxDB unhealthy: {health.status}")
            sys.exit(1)
    except Exception as e:
        print(f"✗ InfluxDB failed: {e}")
        sys.exit(1)
    
    print(f"\n[3/3] Starting monitor (every {POLL_INTERVAL}s)")
    print("=" * 70)
    print()
    
    poll = 0
    success_count = 0
    
    try:
        while True:
            poll += 1
            print(f"[Poll #{poll}] {datetime.now().strftime('%H:%M:%S')}")
            
            resp = query_bms(ser, CMD_TELEMETRY, timeout=1.5)
            
            if resp:
                if DEBUG:
                    print(f"  Received {len(resp)} bytes")
                
                data = parse_type02_telemetry(resp)
                
                if data:
                    success_count += 1
                    
                    # Format output
                    current = data.get('current', 0)
                    if current > 0.01:
                        current_str = f"+{current:.2f}A (CHG)"
                    elif current < -0.01:
                        current_str = f"{current:.2f}A (DSC)"
                    else:
                        current_str = f"{current:.2f}A (IDLE)"
                    
                    power = data.get('power', 0)
                    power_str = f"{power:+.1f}W" if power != 0 else "0W"
                    
                    print(f"  ╔══════════════════════════════════════════════════════════════")
                    print(f"  ║ SOC: {data.get('soc', '?'):3d}%  │  "
                          f"Voltage: {data.get('total_voltage', 0):6.2f}V  │  "
                          f"Current: {current_str}")
                    print(f"  ║ Cells: {data['cell_count']:2d}   │  "
                          f"Max: {data['cell_max']:.3f}V  │  "
                          f"Min: {data['cell_min']:.3f}V  │  "
                          f"Δ: {data['cell_diff']*1000:.1f}mV")
                    if 'temp_mos' in data:
                        print(f"  ║ MOS Temp: {data['temp_mos']}°C")
                    if 'cycle_count' in data:
                        print(f"  ║ Cycles: {data['cycle_count']}")
                    print(f"  ╚══════════════════════════════════════════════════════════════")
                    
                    # Write to InfluxDB
                    if write_to_influxdb(write_api, data):
                        print("  → InfluxDB ✓")
                    else:
                        print("  → InfluxDB ✗")
                else:
                    print("  ✗ Failed to parse response")
            else:
                print("  ✗ No response from BMS")
            
            # Stats
            if poll % 10 == 0:
                success_rate = (success_count / poll) * 100
                print(f"\n  [Stats] {success_count}/{poll} successful ({success_rate:.0f}%)\n")
            
            print()
            time.sleep(POLL_INTERVAL)
    
    except KeyboardInterrupt:
        print("\n\nShutting down...")
        ser.close()
        client.close()
        print(f"Final: {success_count}/{poll} polls successful")
        print("Goodbye!")


if __name__ == "__main__":
    main()
