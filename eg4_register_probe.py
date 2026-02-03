#!/usr/bin/env python3
"""
EG4 LifePower4 V2 - Register Discovery Tool
============================================
Probes the battery to discover which Modbus registers contain cell voltages.

Run this FIRST to find the correct register layout before running the main parser.

IMPORTANT: Disconnect Solar Assistant before running this!
Only ONE master can communicate on the RS485 bus.

Usage:
    python eg4_register_probe.py --port /dev/ttyUSB1 --address 2
"""

import serial
import struct
import time
import argparse

def crc16_modbus(data: bytes) -> bytes:
    """Calculate CRC-16 Modbus."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return struct.pack('<H', crc)

def send_modbus_command(ser, address: int, func: int, start_reg: int, count: int) -> bytes:
    """Send Modbus command and return response."""
    command = struct.pack('>BBHH', address, func, start_reg, count)
    command += crc16_modbus(command)
    
    ser.reset_input_buffer()
    ser.reset_output_buffer()
    ser.write(command)
    
    time.sleep(0.3)
    
    response = ser.read(5 + count * 2 + 10)  # Extra buffer
    return response

def parse_as_voltages(data: bytes, start_offset: int = 3) -> list:
    """Try to parse bytes as cell voltages."""
    voltages = []
    for i in range(0, min(32, len(data) - start_offset - 2), 2):
        try:
            value = struct.unpack('>H', data[start_offset + i:start_offset + i + 2])[0]
            
            # Try different scaling factors
            v_raw = value
            v_mv = value / 1000.0  # mV to V
            v_10mv = value / 100.0  # 10mV to V
            
            # Check which scale gives valid LFP voltage (2.5V - 3.65V)
            if 2500 < v_raw < 3700:
                voltages.append(("mV", v_raw, v_raw / 1000.0))
            elif 250 < v_raw < 370:
                voltages.append(("10mV", v_raw, v_raw / 100.0))
            elif 25 < v_raw < 37:
                voltages.append(("100mV", v_raw, v_raw / 10.0))
            else:
                voltages.append(("raw", v_raw, None))
        except:
            break
    return voltages

def probe_register_range(ser, address: int, start: int, count: int, name: str):
    """Probe a register range and analyze response."""
    print(f"\n{'='*60}")
    print(f"Probing: {name}")
    print(f"  Register: 0x{start:04X}, Count: {count}")
    print(f"{'='*60}")
    
    response = send_modbus_command(ser, address, 0x03, start, count)
    
    if len(response) == 0:
        print("  ❌ No response")
        return None
    
    print(f"  Response length: {len(response)} bytes")
    print(f"  Raw HEX: {response.hex().upper()}")
    
    if len(response) < 5:
        print("  ❌ Response too short")
        return None
    
    # Parse header
    resp_addr = response[0]
    resp_func = response[1]
    byte_count = response[2]
    
    print(f"  Address: {resp_addr}, Function: {resp_func}, Byte count: {byte_count}")
    
    if resp_func == 0x83:
        error_code = response[2]
        print(f"  ❌ Modbus exception: {error_code}")
        return None
    
    # Extract data portion
    data = response[3:3+byte_count] if len(response) > 3+byte_count else response[3:-2]
    print(f"  Data ({len(data)} bytes): {data.hex().upper()}")
    
    # Try to interpret as voltages
    voltages = parse_as_voltages(response)
    
    valid_voltages = [v for v in voltages if v[2] is not None and 2.5 < v[2] < 3.7]
    
    if valid_voltages:
        print(f"\n  ✅ Found {len(valid_voltages)} valid cell voltages:")
        for i, (unit, raw, volts) in enumerate(valid_voltages, 1):
            print(f"     Cell {i}: {volts:.3f}V (raw: {raw}, unit: {unit})")
    else:
        print(f"\n  📊 Register values (first 16):")
        for i in range(0, min(32, len(data)), 2):
            try:
                val = struct.unpack('>H', data[i:i+2])[0]
                print(f"     Reg {start + i//2}: {val} (0x{val:04X})")
            except:
                break
    
    return response

def main():
    parser = argparse.ArgumentParser(description="EG4 LifePower4 V2 Register Probe")
    parser.add_argument("--port", default="/dev/ttyUSB1", help="Serial port")
    parser.add_argument("--address", type=int, default=2, help="Battery Modbus address")
    parser.add_argument("--baud", type=int, default=9600, help="Baud rate")
    args = parser.parse_args()
    
    print("=" * 60)
    print("EG4 LifePower4 V2 - Register Discovery Tool")
    print("=" * 60)
    print(f"Port: {args.port}")
    print(f"Address: {args.address}")
    print(f"Baud: {args.baud}")
    print()
    print("⚠️  Make sure Solar Assistant is DISCONNECTED!")
    print("    Only one RS485 master can communicate at a time.")
    print()
    
    input("Press ENTER to start probing (Ctrl+C to cancel)...")
    
    try:
        ser = serial.Serial(
            port=args.port,
            baudrate=args.baud,
            timeout=2.0,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            bytesize=serial.EIGHTBITS
        )
        print(f"\n✅ Serial port opened: {args.port}")
    except serial.SerialException as e:
        print(f"\n❌ Cannot open serial port: {e}")
        return
    
    # Known register ranges from community research
    register_ranges = [
        # Format: (start, count, name)
        (0x0000, 32, "Status block A (your eg4term.py uses this)"),
        (0x0013, 16, "Inverter query registers (Solar Assistant style)"),
        (0x1000, 32, "Cell voltages block (your eg4term.py tries this)"),
        (0x1010, 16, "Cell voltages alt offset"),
        (0x0069, 23, "Hardware info (EG4-LL style)"),
        (0x002D, 32, "BMS config (EG4-LL style)"),
        (0x0100, 16, "Extended status"),
        (0x0007, 16, "Cell voltages (embedded in status)"),
    ]
    
    results = {}
    
    for start, count, name in register_ranges:
        try:
            result = probe_register_range(ser, args.address, start, count, name)
            if result:
                results[name] = result
            time.sleep(0.5)
        except Exception as e:
            print(f"  Error probing {name}: {e}")
    
    ser.close()
    
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    
    if results:
        print(f"Got responses from {len(results)} register ranges.")
        print("\nLook for ranges that contain values between 2500-3650 (mV)")
        print("Those are likely your cell voltages!")
    else:
        print("❌ No responses received. Check:")
        print("   - RS485 adapter connection (pins 1&2 on Battery-Comm)")
        print("   - Battery address (DIP switch setting)")
        print("   - Solar Assistant is disconnected")
        print("   - Baud rate (9600 for external monitoring port)")

if __name__ == "__main__":
    main()
