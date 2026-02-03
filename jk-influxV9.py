#!/usr/bin/env python3
"""
JK BMS V19 Monitor - UART/RS485 to InfluxDB
Reads JK BMS data via UART (Modbus v1.0 protocol) and writes to InfluxDB
"""

import serial
import time
import sys
from datetime import datetime
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

# ===== CONFIGURATION =====
# Serial Configuration
SERIAL_PORT = "/dev/ttyUSB1"  # FTDI adapter
BAUDRATE = 115200  # JK BMS WOW Modbus uses 115200
POLL_INTERVAL = 20  # seconds between polls

# InfluxDB Configuration
INFLUX_URL = "http://192.168.50.46:8086"
INFLUX_TOKEN = "XnE9ILRtsWQ0IZpxahUWnTvsPspdSagebJjTD_KsiH9KWzOiWztCRH4hGRHHqshRI_Tlb4xdl2SzhfO270QzrA=="
INFLUX_ORG = "home_monitoring"
INFLUX_BUCKET = "battery_data"

# JK BMS WOW Modbus v1.3 Commands
# WOW protocol uses different frame structure
STATUS_CMDS = [
    b'\x01\x03\x00\x00\x00\x7D\x84\x3E',  # WOW Modbus v1.3 - read all registers
    b'\x01\x03\x00\x48\x00\x38\x44\x34',  # Alternative WOW command
    b'\x01\x03\x00\x00\x00\x20\x44\x0B',  # Standard Modbus fallback
]

CELLS_CMDS = [
    b'\x01\x03\x00\x48\x00\x10\x85\xCB',  # WOW Modbus cells
    b'\x01\x03\x10\x00\x00\x10\xC5\xE0',  # Standard cells fallback
]


def query_jk_bms(ser, command):
    """Send command to JK BMS and return response"""
    try:
        ser.reset_input_buffer()
        ser.reset_output_buffer()
        ser.write(command)
        time.sleep(0.5)
        response = ser.read(300)  # JK BMS can have longer responses
        return response
    except Exception as e:
        print(f"  Serial error: {e}")
        return None


def decode_hex_response(resp):
    """Decode JK BMS WOW Modbus response - handles the actual format we're seeing"""
    if not resp or len(resp) < 20:
        return None
    
    try:
        data = {}
        
        # The actual format we're seeing:
        # 4 bytes per cell: [voltage_low, voltage_high, 0x00, 0x00]
        # Voltages are in mV, little-endian
        
        cells = []
        
        # Scan through the response looking for the cell voltage pattern
        i = 0
        while i < len(resp) - 3:
            # Check if this looks like our pattern: voltage (2 bytes) + padding (0x0000)
            if resp[i+2] == 0x00 and resp[i+3] == 0x00:
                voltage_mv = int.from_bytes(resp[i:i+2], 'little')
                
                # Valid cell voltage range in mV
                if 2000 <= voltage_mv <= 5000:
                    cells.append(voltage_mv / 1000.0)
                    i += 4  # Skip to next cell (4 bytes per cell)
                    continue
            
            i += 1
        
        # We should have found cells
        if len(cells) >= 8:
            # Take first 16 cells (typical for JK BMS)
            data['cells'] = cells[:16]
            data['cell_max'] = max(data['cells'])
            data['cell_min'] = min(data['cells'])
            data['cell_diff'] = data['cell_max'] - data['cell_min']
            data['cell_avg'] = sum(data['cells']) / len(data['cells'])
            data['total_voltage'] = sum(data['cells'])
            
            # Try to find SOC in the response (single byte, 0-100)
            for j in range(len(resp)):
                if 0 <= resp[j] <= 100:
                    # Make sure it's not part of voltage data
                    if (j == 0 or resp[j-1] != 0x00) and (j+1 >= len(resp) or resp[j+1] != 0x00):
                        data['soc'] = resp[j]
                        break
            
            # If no SOC found, estimate from voltage
            if 'soc' not in data:
                # Rough estimate: 3.0V = 0%, 3.65V = 100% per cell
                avg_v = data['cell_avg']
                data['soc'] = max(0, min(100, int((avg_v - 3.0) / 0.65 * 100)))
            
            # Current not in this response format, set to 0
            data['current'] = 0.0
            data['power'] = 0.0
            
            return data
        
        # If that didn't work, try generic pattern matching
        return decode_generic_response(resp)
    
    except Exception as e:
        print(f"  Decode error: {e}")
        import traceback
        traceback.print_exc()
        return decode_generic_response(resp)


def decode_generic_response(resp):
    """Generic decoder for unknown formats - pattern matching"""
    if not resp or len(resp) < 20:
        return None
    
    try:
        data = {}
        cells = []
        
        # Scan for cell voltage patterns
        i = 0
        while i < len(resp) - 1:
            # Try little-endian
            val_le = int.from_bytes(resp[i:i+2], 'little')
            # Try big-endian  
            val_be = int.from_bytes(resp[i:i+2], 'big')
            
            # Check if this looks like a cell voltage (in mV)
            if 2500 <= val_le <= 4500:
                voltage = val_le / 1000.0
                if not cells or abs(cells[-1] - voltage) > 0.001:
                    cells.append(voltage)
            elif 2500 <= val_be <= 4500:
                voltage = val_be / 1000.0
                if not cells or abs(cells[-1] - voltage) > 0.001:
                    cells.append(voltage)
            
            i += 1
        
        # If we found reasonable cell count
        if 8 <= len(cells) <= 16:
            data['cells'] = cells[:16]
            data['cell_max'] = max(data['cells'])
            data['cell_min'] = min(data['cells'])
            data['cell_diff'] = data['cell_max'] - data['cell_min']
            data['cell_avg'] = sum(data['cells']) / len(data['cells'])
            data['total_voltage'] = sum(data['cells'])
            
            # Try to find SOC
            for j in range(len(resp)):
                if 0 <= resp[j] <= 100:
                    if j > 0 and resp[j-1] == 0:
                        data['soc'] = resp[j]
                        break
            
            data['current'] = 0.0
            data['power'] = data['total_voltage'] * data['current']
            
            return data
        
        return None
    
    except Exception as e:
        print(f"  Generic decode error: {e}")
        return None


def write_to_influxdb(write_api, data, battery_id="jk_bms_1"):
    """Write battery data to InfluxDB"""
    try:
        timestamp = datetime.utcnow()
        
        # Main battery metrics
        point = Point("battery") \
            .tag("battery_id", battery_id) \
            .tag("type", "jk_bms_v19") \
            .field("soc", float(data['soc'])) \
            .field("voltage", float(data['total_voltage'])) \
            .field("current", float(data['current'])) \
            .field("power", float(data['power'])) \
            .time(timestamp)
        
        write_api.write(bucket=INFLUX_BUCKET, record=point)
        
        # Cell statistics
        if 'cells' in data and data['cells']:
            cell_point = Point("battery_cells") \
                .tag("battery_id", battery_id) \
                .field("cell_max", float(data['cell_max'])) \
                .field("cell_min", float(data['cell_min'])) \
                .field("cell_diff", float(data['cell_diff'])) \
                .field("cell_avg", float(data['cell_avg'])) \
                .field("cell_count", len(data['cells'])) \
                .time(timestamp)
            
            write_api.write(bucket=INFLUX_BUCKET, record=cell_point)
            
            # Individual cell voltages
            for i, voltage in enumerate(data['cells'], 1):
                cell_detail = Point("battery_cell_detail") \
                    .tag("battery_id", battery_id) \
                    .tag("cell_number", str(i)) \
                    .field("voltage", float(voltage)) \
                    .time(timestamp)
                
                write_api.write(bucket=INFLUX_BUCKET, record=cell_detail)
        
        return True
    
    except Exception as e:
        print(f"  InfluxDB write error: {e}")
        return False


def test_connection(ser):
    """Test different command protocols to find what works"""
    print("\n[Testing JK BMS Communication]")
    print("Trying different protocol variants...\n")
    
    for i, cmd in enumerate(STATUS_CMDS, 1):
        print(f"Test {i}: Sending command: {cmd.hex()}")
        resp = query_jk_bms(ser, cmd)
        
        if resp and len(resp) > 0:
            print(f"  ✓ Got response! Length: {len(resp)} bytes")
            print(f"  Raw (first 50 bytes): {resp[:50].hex()}")
            
            # Try to parse
            data = decode_hex_response(resp)
            if data:
                print(f"  ✓ Parsed successfully!")
                print(f"    SOC: {data.get('soc', 'N/A')}%")
                print(f"    Voltage: {data.get('total_voltage', 'N/A')}V")
                print(f"    Current: {data.get('current', 'N/A')}A")
                return cmd  # Return working command
            else:
                print(f"  ⚠ Got response but couldn't parse")
        else:
            print(f"  ✗ No response")
        
        print()
        time.sleep(1)
    
    return None


def main():
    print("=" * 60)
    print("JK BMS V19 Monitor → InfluxDB (UART)")
    print("=" * 60)
    
    # Initialize serial connection
    print(f"\n[1/3] Opening serial port {SERIAL_PORT} at {BAUDRATE} baud...")
    try:
        ser = serial.Serial(
            SERIAL_PORT, 
            BAUDRATE, 
            timeout=2,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            bytesize=serial.EIGHTBITS
        )
        print("✓ Serial port opened successfully")
    except serial.SerialException as e:
        print(f"✗ Failed to open serial port: {e}")
        print("\nTroubleshooting:")
        print(f"  1. Check port exists: ls -la {SERIAL_PORT}")
        print(f"  2. Check permissions: groups odroid (should include 'dialout')")
        print(f"  3. Try: sudo chmod 666 {SERIAL_PORT} (temporary fix)")
        print(f"  4. Check JK BMS connections and power")
        sys.exit(1)
    
    # Test communication and find working protocol
    working_cmd = test_connection(ser)
    
    if not working_cmd:
        print("\n✗ Could not communicate with JK BMS")
        print("\nTroubleshooting:")
        print("  1. Check JK BMS is set to 'Modbus v1.0' mode")
        print("  2. Try different baudrate (9600 or 115200)")
        print("  3. Verify UART wiring (TX→RX, RX→TX, GND→GND)")
        print("  4. Check if BMS is powered on")
        print("  5. Try BLE connection instead")
        ser.close()
        sys.exit(1)
    
    print(f"\n✓ Found working protocol!")
    
    # Initialize InfluxDB connection
    print(f"\n[2/3] Connecting to InfluxDB at {INFLUX_URL}...")
    try:
        client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
        write_api = client.write_api(write_options=SYNCHRONOUS)
        
        health = client.health()
        if health.status == "pass":
            print("✓ InfluxDB connection successful")
        else:
            print(f"✗ InfluxDB health check failed: {health.message}")
            sys.exit(1)
    
    except Exception as e:
        print(f"✗ Failed to connect to InfluxDB: {e}")
        sys.exit(1)
    
    # Start monitoring loop
    print(f"\n[3/3] Starting monitoring loop (polling every {POLL_INTERVAL}s)")
    print("=" * 60)
    print("\nPress Ctrl+C to stop\n")
    
    poll_count = 0
    
    try:
        while True:
            poll_count += 1
            print(f"[Poll #{poll_count}] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            
            # Query BMS using working command
            status_resp = query_jk_bms(ser, working_cmd)
            cells_resp = query_jk_bms(ser, CELLS_CMDS[0])
            
            # Parse data
            data = decode_hex_response(status_resp)
            
            if data:
                # Display on console
                print(f"  SOC: {data.get('soc', 'N/A')}%")
                print(f"  Voltage: {data.get('total_voltage', 'N/A'):.2f}V")
                print(f"  Current: {data.get('current', 'N/A'):.2f}A")
                print(f"  Power: {data.get('power', 'N/A'):.1f}W")
                
                if 'cells' in data and data['cells']:
                    print(f"  Cells: {len(data['cells'])} cells detected")
                    print(f"    Max: {data['cell_max']:.3f}V | Min: {data['cell_min']:.3f}V | Diff: {data['cell_diff']:.3f}V")
                
                # Write to InfluxDB
                if write_to_influxdb(write_api, data):
                    print("  ✓ Data written to InfluxDB")
                else:
                    print("  ✗ Failed to write to InfluxDB")
            else:
                print("  ✗ No valid data received from BMS")
            
            print()
            time.sleep(POLL_INTERVAL)
    
    except KeyboardInterrupt:
        print("\n\nShutting down...")
        ser.close()
        client.close()
        print("Goodbye!")
        sys.exit(0)
    
    except Exception as e:
        print(f"\nUnexpected error: {e}")
        import traceback
        traceback.print_exc()
        ser.close()
        client.close()
        sys.exit(1)


if __name__ == "__main__":
    main()