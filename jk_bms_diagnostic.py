#!/usr/bin/env python3
"""
JK BMS Diagnostic Tool
======================
Dumps raw frame data and helps identify byte offsets for:
- Current
- SOC
- Temperature

Run this while monitoring Grafana to match values with byte positions.
"""

import serial
import time
import sys
import struct
from datetime import datetime

SERIAL_PORT = "/dev/ttyUSB0"
BAUDRATE = 115200

# Proprietary command
CMD_TELEMETRY = bytes.fromhex("01 10 16 20 00 01 02 00 00 D6 F1")


def query_bms(ser, timeout=2.0):
    """Send command and read response"""
    ser.reset_input_buffer()
    ser.write(CMD_TELEMETRY)
    
    time.sleep(0.1)
    
    start = time.time()
    data = b''
    while (time.time() - start) < timeout:
        if ser.in_waiting:
            data += ser.read(ser.in_waiting)
            if len(data) >= 300:
                time.sleep(0.05)
                if ser.in_waiting:
                    data += ser.read(ser.in_waiting)
                break
        time.sleep(0.01)
    
    return data


def parse_cells(d):
    """Parse cell voltages from offset 6-37"""
    cells = []
    for i in range(16):
        offset = 6 + i * 2
        cell_mv = struct.unpack('<H', d[offset:offset+2])[0]
        if 1000 <= cell_mv <= 5000:
            cells.append(cell_mv / 1000.0)
    return cells


def scan_for_value(d, expected, tolerance=0.1, name="value"):
    """Scan frame for a value that matches expected within tolerance"""
    print(f"\n=== Scanning for {name}: expected ~{expected} ===")
    
    matches = []
    
    # Scan as various formats
    for offset in range(6, min(250, len(d) - 4)):
        # As unsigned 8-bit (for SOC, 0-100)
        val_u8 = d[offset]
        if abs(val_u8 - expected) < tolerance * 100:  # For percentage
            if 0 <= val_u8 <= 100:
                matches.append((offset, 'U8', val_u8, abs(val_u8 - expected)))
        
        # As signed 16-bit (for current in 10mA or 100mA)
        if offset + 2 <= len(d):
            val_s16 = struct.unpack('<h', d[offset:offset+2])[0]
            
            # Try as 10mA units (divide by 100 to get A)
            val_a_10ma = val_s16 / 100.0
            if abs(val_a_10ma - expected) < tolerance:
                matches.append((offset, 'S16/100', val_a_10ma, abs(val_a_10ma - expected)))
            
            # Try as 100mA units (divide by 10 to get A)
            val_a_100ma = val_s16 / 10.0
            if abs(val_a_100ma - expected) < tolerance:
                matches.append((offset, 'S16/10', val_a_100ma, abs(val_a_100ma - expected)))
            
            # Try as mA (divide by 1000 to get A)
            val_a_ma = val_s16 / 1000.0
            if abs(val_a_ma - expected) < tolerance:
                matches.append((offset, 'S16/1000', val_a_ma, abs(val_a_ma - expected)))
        
        # As signed 32-bit (for current in mA)
        if offset + 4 <= len(d):
            val_s32 = struct.unpack('<i', d[offset:offset+4])[0]
            
            # As mA (divide by 1000 to get A)
            val_a = val_s32 / 1000.0
            if abs(val_a - expected) < tolerance:
                matches.append((offset, 'S32/1000', val_a, abs(val_a - expected)))
            
            # As 10mA (divide by 100 to get A)
            val_a_10 = val_s32 / 100.0
            if abs(val_a_10 - expected) < tolerance:
                matches.append((offset, 'S32/100', val_a_10, abs(val_a_10 - expected)))
        
        # As unsigned 16-bit offset-10000 encoding
        if offset + 2 <= len(d):
            val_u16 = struct.unpack('<H', d[offset:offset+2])[0]
            # offset-10000 encoding: 10000 = 0A, 9000 = +10A (charging), 11000 = -10A (discharging)
            val_offset = (10000 - val_u16) * 0.01
            if abs(val_offset - expected) < tolerance:
                matches.append((offset, 'U16-off10k', val_offset, abs(val_offset - expected)))
    
    # Sort by closeness to expected value
    matches.sort(key=lambda x: x[3])
    
    # Print top 10 matches
    print(f"  Top matches:")
    for match in matches[:10]:
        offset, fmt, value, diff = match
        hex_bytes = d[offset:min(offset+4, len(d))].hex()
        print(f"    Offset {offset:3d} (0x{offset:02X}): {fmt:12s} = {value:10.3f}  (diff={diff:.3f})  bytes: {hex_bytes}")
    
    return matches


def main():
    print("=" * 70)
    print("JK BMS Diagnostic Tool")
    print("=" * 70)
    
    # Get expected values from user
    print("\nEnter current values from Grafana (or press Enter to scan all):")
    
    expected_current = input("Current (A, e.g., -9.53): ").strip()
    expected_soc = input("SOC (%, e.g., 49): ").strip()
    expected_temp = input("MOS Temp (°C, e.g., 25): ").strip()
    
    # Open serial
    print(f"\nOpening {SERIAL_PORT}...")
    try:
        ser = serial.Serial(SERIAL_PORT, BAUDRATE, timeout=1)
        print("✓ Serial opened")
    except Exception as e:
        print(f"✗ Failed: {e}")
        sys.exit(1)
    
    # Query BMS
    print("\nQuerying BMS...")
    data = query_bms(ser)
    ser.close()
    
    if not data or len(data) < 100:
        print(f"✗ No response (got {len(data)} bytes)")
        sys.exit(1)
    
    print(f"✓ Got {len(data)} bytes")
    
    # Find frame header
    header_idx = data.find(b'\x55\xAA\xEB\x90\x02\x00')
    if header_idx == -1:
        print("✗ Frame header not found")
        print(f"  Raw data: {data[:50].hex()}")
        sys.exit(1)
    
    d = data[header_idx:]
    print(f"✓ Frame starts at byte {header_idx}, frame length: {len(d)}")
    
    # Parse cells
    cells = parse_cells(d)
    if cells:
        total_v = sum(cells)
        avg_v = sum(cells) / len(cells)
        print(f"\n✓ Cells parsed: {len(cells)} cells")
        print(f"  Total voltage: {total_v:.2f}V")
        print(f"  Average cell: {avg_v:.3f}V")
        print(f"  Cell range: {min(cells):.3f}V - {max(cells):.3f}V (Δ{(max(cells)-min(cells))*1000:.1f}mV)")
    
    # Dump frame bytes in sections
    print("\n" + "=" * 70)
    print("FRAME DUMP (after header)")
    print("=" * 70)
    
    for start in range(0, min(len(d), 256), 16):
        end = min(start + 16, len(d))
        hex_str = ' '.join(f'{b:02X}' for b in d[start:end])
        ascii_str = ''.join(chr(b) if 32 <= b < 127 else '.' for b in d[start:end])
        print(f"  {start:3d}: {hex_str:<48}  {ascii_str}")
    
    # Scan for expected values
    if expected_current:
        try:
            scan_for_value(d, float(expected_current), tolerance=1.0, name=f"Current ({expected_current}A)")
        except ValueError:
            pass
    
    if expected_soc:
        try:
            soc_val = float(expected_soc)
            scan_for_value(d, soc_val, tolerance=5, name=f"SOC ({expected_soc}%)")
            
            # Also scan specifically for SOC byte
            print(f"\n=== Single-byte SOC scan (looking for {int(soc_val)}) ===")
            for offset in range(100, min(200, len(d))):
                val = d[offset]
                if abs(val - soc_val) <= 5:
                    print(f"    Offset {offset:3d} (0x{offset:02X}): {val}%  (diff={abs(val-soc_val)})")
        except ValueError:
            pass
    
    if expected_temp:
        try:
            temp_val = float(expected_temp)
            
            print(f"\n=== Temperature scan (looking for {temp_val}°C) ===")
            for offset in range(100, min(200, len(d))):
                raw = d[offset]
                
                # Format 1: raw - 100 (where 100 = 0°C)
                temp1 = raw - 100
                if abs(temp1 - temp_val) <= 5:
                    print(f"    Offset {offset:3d}: raw={raw:3d}, (raw-100)={temp1}°C")
                
                # Format 2: raw - 40 (where 40 = 0°C, used in some protocols)
                temp2 = raw - 40
                if abs(temp2 - temp_val) <= 5:
                    print(f"    Offset {offset:3d}: raw={raw:3d}, (raw-40)={temp2}°C")
                
                # Format 3: raw directly (if stored as actual temp + offset)
                if abs(raw - temp_val) <= 5:
                    print(f"    Offset {offset:3d}: raw={raw:3d} (direct)")
            
            # Also try 16-bit 0.1°C format
            print(f"\n=== Temperature scan (16-bit, 0.1°C units) ===")
            for offset in range(100, min(200, len(d) - 1)):
                raw_16 = struct.unpack('<h', d[offset:offset+2])[0]
                temp = raw_16 / 10.0
                if abs(temp - temp_val) <= 5:
                    print(f"    Offset {offset:3d}: raw={raw_16:5d}, temp={temp:.1f}°C")
        except ValueError:
            pass
    
    # Summary
    print("\n" + "=" * 70)
    print("LIKELY OFFSETS BASED ON PROTOCOL DOCS:")
    print("=" * 70)
    print("""
  Based on JK BMS proprietary protocol (Frame Type 02):
  
  Offset   Content
  ------   -------
  0-5      Header (55 AA EB 90 02 00)
  6-37     Cell voltages (16 × 2 bytes, little-endian, mV)
  38-69    Cell resistances/padding
  70-73    Unknown
  74-75    Average cell voltage (mV)
  76-77    Delta cell voltage (mV)
  78-79    Max cell number
  80-81    Min cell number
  ...
  118-121  Possibly current (INT32, mA) - CHECK THIS
  ...
  144-145  MOS temperature (needs decode)
  ...
  150-153  Possibly current alternate location
  ...
  164-167  Possibly SOC location
  ...
  180      Possibly SOC (single byte)
  ...
  0xB4     Capacity (UINT32, mAh)
  0xB8     Cycle count (UINT32)
  
  Run this script multiple times while charging/discharging to 
  find values that change correctly.
""")


if __name__ == "__main__":
    main()
