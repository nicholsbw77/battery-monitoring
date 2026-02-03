#!/usr/bin/env python3
"""
JK BMS MQTT Publisher - NW Protocol Version
============================================
Based on the official JK BMS "NW" protocol documentation.

Frame Format:
  STX: 0x4E 0x57 ("NW")
  LENGTH: 2 bytes (total frame length including checksum)
  TERMINAL_ID: 4 bytes (BMS ID, usually 0x00000000)
  COMMAND: 1 byte (0x03 = read, 0x02 = write, 0x06 = read all)
  FRAME_SOURCE: 1 byte (0x00=BMS, 0x01=Bluetooth, 0x02=GPS, 0x03=PC)
  TRANSFER_TYPE: 1 byte (0x00=request, 0x01=response, 0x02=active upload)
  DATA: variable (data identifier + data)
  RECORD_NUM: 4 bytes
  END: 0x68
  CHECKSUM: 4 bytes (high 2 = CRC16 unused, low 2 = sum checksum)

Data Identifiers:
  0x79: Cell voltages (3 bytes per cell: cell_num + voltage_mV)
  0x80: MOS temperature (2 bytes, 0-140 maps to -40°C to 100°C)
  0x81: Battery box temperature
  0x82: Battery temperature
  0x83: Total voltage (2 bytes, unit 0.01V)
  0x84: Current (2 bytes, offset-10000 encoding OR bit15=direction)
  0x85: SOC (1 byte, 0-100%)
  0x87: Cycle count (2 bytes)
  0x89: Cycle capacity (4 bytes, Ah)
  0x8a: Total cell count (2 bytes)
  0x8b: Warning info (2 bytes, bitmap)
  0x8c: Status info (2 bytes, bitmap)
"""

import serial
import time
import sys
import struct
import json
from datetime import datetime
from typing import Dict, Any, Optional, List

try:
    import paho.mqtt.client as mqtt
except ImportError:
    print("Please install paho-mqtt: pip install paho-mqtt --break-system-packages")
    sys.exit(1)

# ============================================================================
# CONFIGURATION
# ============================================================================

SERIAL_CONFIG = {
    "port": "/dev/ttyUSB0",
    "baudrate": 115200,
    "timeout": 1.0,
}

MQTT_CONFIG = {
    "host": "localhost",
    "port": 1883,
    "username": "jkbms",
    "password": "admin",  # Update to your password
    "client_id": "jk_bms_nw_publisher",
    "topic_prefix": "jk_bms",
}

POLL_INTERVAL = 5
DEBUG = True

# Terminal ID (4 bytes) - usually 0x00000000 or read from BMS
TERMINAL_ID = bytes([0x00, 0x00, 0x00, 0x00])

# ============================================================================
# NW PROTOCOL IMPLEMENTATION
# ============================================================================

class JKBMSProtocol:
    """JK BMS NW Protocol communication"""
    
    # Data identifiers
    ID_CELL_VOLTAGES = 0x79
    ID_TEMP_MOS = 0x80
    ID_TEMP_BOX = 0x81
    ID_TEMP_BATTERY = 0x82
    ID_TOTAL_VOLTAGE = 0x83
    ID_CURRENT = 0x84
    ID_SOC = 0x85
    ID_TEMP_SENSOR_COUNT = 0x86
    ID_CYCLE_COUNT = 0x87
    ID_CYCLE_CAPACITY = 0x89
    ID_CELL_COUNT = 0x8a
    ID_WARNING = 0x8b
    ID_STATUS = 0x8c
    ID_CAPACITY_SETTING = 0xaa
    ID_ACTUAL_CAPACITY = 0xb9
    ID_PROTOCOL_VERSION = 0xc0
    
    # Commands
    CMD_READ = 0x03
    CMD_WRITE = 0x02
    CMD_READ_ALL = 0x06
    
    # Frame markers
    STX = bytes([0x4E, 0x57])  # "NW"
    END = 0x68
    
    def __init__(self, port: str, baudrate: int = 115200):
        self.port = port
        self.baudrate = baudrate
        self.serial = None
        self.protocol_version = 0x00  # Will be read from BMS
        self.record_num = 0
        
    def connect(self) -> bool:
        """Open serial connection"""
        try:
            self.serial = serial.Serial(
                self.port,
                self.baudrate,
                timeout=1.0,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                bytesize=serial.EIGHTBITS
            )
            if DEBUG:
                print(f"✓ Serial opened: {self.port} @ {self.baudrate}")
            return True
        except Exception as e:
            print(f"✗ Serial error: {e}")
            return False
    
    def close(self):
        """Close serial connection"""
        if self.serial:
            self.serial.close()
    
    def _calculate_checksum(self, data: bytes) -> int:
        """Calculate sum checksum (low 2 bytes of sum of all bytes)"""
        return sum(data) & 0xFFFF
    
    def _build_read_all_frame(self) -> bytes:
        """
        Build frame to read all data (command 0x06)
        This reads all data identifiers in one request
        """
        self.record_num = (self.record_num + 1) & 0xFFFFFF
        
        # Frame without length and checksum first
        frame_content = bytearray()
        frame_content.extend(TERMINAL_ID)           # Terminal ID (4 bytes)
        frame_content.append(self.CMD_READ_ALL)     # Command: read all
        frame_content.append(0x03)                  # Frame source: PC
        frame_content.append(0x00)                  # Transfer type: request
        frame_content.append(0x00)                  # Data identifier: 0x00 for read all
        # Record number (4 bytes: 1 random + 3 sequence)
        frame_content.append(0x00)                  # Random byte
        frame_content.append((self.record_num >> 16) & 0xFF)
        frame_content.append((self.record_num >> 8) & 0xFF)
        frame_content.append(self.record_num & 0xFF)
        frame_content.append(self.END)              # End marker
        
        # Calculate length (includes STX, length field, content, and checksum)
        total_length = 2 + 2 + len(frame_content) + 4
        
        # Build complete frame
        frame = bytearray()
        frame.extend(self.STX)
        frame.append((total_length >> 8) & 0xFF)
        frame.append(total_length & 0xFF)
        frame.extend(frame_content)
        
        # Calculate and append checksum
        checksum = self._calculate_checksum(frame)
        frame.extend([0x00, 0x00])  # CRC16 placeholder (unused)
        frame.append((checksum >> 8) & 0xFF)
        frame.append(checksum & 0xFF)
        
        return bytes(frame)
    
    def _parse_response(self, data: bytes) -> Optional[Dict[str, Any]]:
        """Parse NW protocol response frame"""
        
        if len(data) < 20:
            if DEBUG:
                print(f"  Response too short: {len(data)} bytes")
            return None
        
        # Find frame start "NW" (0x4E 0x57)
        start_idx = data.find(self.STX)
        if start_idx == -1:
            if DEBUG:
                print("  NW header not found, trying proprietary...")
            # Fall back to proprietary protocol
            return self._parse_proprietary_response(data)
        
        frame = data[start_idx:]
        
        if len(frame) < 13:
            return None
        
        # Parse header
        length = (frame[2] << 8) | frame[3]
        if DEBUG:
            print(f"  NW frame: length={length}, actual={len(frame)}")
        
        # Extract data section (after header, before record_num)
        # Header: STX(2) + Length(2) + TermID(4) + Cmd(1) + Src(1) + Type(1) = 11 bytes
        # Tail: RecordNum(4) + End(1) + Checksum(4) = 9 bytes
        
        if len(frame) < length:
            if DEBUG:
                print(f"  Frame incomplete: have {len(frame)}, need {length}")
            # Try to parse what we have
        
        # Find data section
        data_start = 11  # After fixed header
        data_end = len(frame) - 9  # Before tail
        
        if data_end <= data_start:
            if DEBUG:
                print("  No data section in frame")
            return None
        
        data_section = frame[data_start:data_end]
        
        if DEBUG:
            print(f"  Data section ({len(data_section)} bytes): {data_section[:50].hex()}...")
        
        return self._parse_data_identifiers(data_section)
    
    def _parse_data_identifiers(self, data: bytes) -> Dict[str, Any]:
        """Parse data identifiers from response data section"""
        result = {}
        pos = 0
        
        while pos < len(data):
            if pos >= len(data):
                break
                
            identifier = data[pos]
            pos += 1
            
            if identifier == self.ID_CELL_VOLTAGES:
                # Cell voltages: first byte is length, then 3 bytes per cell
                if pos >= len(data):
                    break
                cell_data_len = data[pos]
                pos += 1
                
                cells = []
                cell_end = pos + cell_data_len
                while pos + 2 < cell_end and pos + 2 < len(data):
                    cell_num = data[pos]
                    cell_mv = (data[pos + 1] << 8) | data[pos + 2]
                    pos += 3
                    if 1000 <= cell_mv <= 5000:
                        cells.append(cell_mv / 1000.0)
                
                if cells:
                    result['cells'] = cells
                    result['cell_count'] = len(cells)
                    result['cell_max'] = max(cells)
                    result['cell_min'] = min(cells)
                    result['cell_diff'] = (max(cells) - min(cells)) * 1000
                    result['cell_avg'] = sum(cells) / len(cells)
                    result['total_voltage'] = sum(cells)
            
            elif identifier == self.ID_TEMP_MOS:
                # MOS temperature: 2 bytes, 0-140 maps to -40°C to 100°C
                if pos + 1 < len(data):
                    raw = (data[pos] << 8) | data[pos + 1]
                    pos += 2
                    # Temperature = raw - 100 (where 100 = 0°C)
                    # But raw is 0-140, so actual range is -40 to +40
                    # Actually: 0 = -40°C, 140 = 100°C, so temp = raw - 40
                    # OR: documented as "100 benchmark" meaning 100 raw = 0°C
                    result['temp_mos'] = raw - 100
                    if DEBUG:
                        print(f"    MOS temp: raw={raw}, temp={result['temp_mos']}°C")
            
            elif identifier == self.ID_TEMP_BOX:
                if pos + 1 < len(data):
                    raw = (data[pos] << 8) | data[pos + 1]
                    pos += 2
                    result['temp_box'] = raw - 100
            
            elif identifier == self.ID_TEMP_BATTERY:
                if pos + 1 < len(data):
                    raw = (data[pos] << 8) | data[pos + 1]
                    pos += 2
                    result['temp_battery'] = raw - 100
            
            elif identifier == self.ID_TOTAL_VOLTAGE:
                # Total voltage: 2 bytes, unit 0.01V
                if pos + 1 < len(data):
                    raw = (data[pos] << 8) | data[pos + 1]
                    pos += 2
                    result['total_voltage_reported'] = raw * 0.01
                    if DEBUG:
                        print(f"    Voltage: raw={raw}, V={result['total_voltage_reported']:.2f}")
            
            elif identifier == self.ID_CURRENT:
                # Current: 2 bytes
                # Standard: offset-10000 encoding (10000 = 0A)
                # Protocol C0:0x01: bit15=direction, magnitude in 10mA
                if pos + 1 < len(data):
                    raw = (data[pos] << 8) | data[pos + 1]
                    pos += 2
                    
                    if self.protocol_version == 0x01:
                        # New protocol: bit 15 = charge (1) / discharge (0)
                        # Magnitude is in 10mA units
                        magnitude = (raw & 0x7FFF) * 0.01
                        if raw & 0x8000:
                            result['current'] = magnitude  # Charging (positive)
                        else:
                            result['current'] = -magnitude  # Discharging (negative)
                    else:
                        # Standard offset-10000 encoding
                        # (10000 - raw) * 0.01 = Amps
                        # If raw > 10000: discharging (negative current)
                        # If raw < 10000: charging (positive current)
                        result['current'] = (10000 - raw) * 0.01
                    
                    if DEBUG:
                        print(f"    Current: raw={raw} (0x{raw:04X}), I={result['current']:.2f}A")
            
            elif identifier == self.ID_SOC:
                # SOC: 1 byte, 0-100%
                if pos < len(data):
                    result['soc'] = data[pos]
                    pos += 1
                    if DEBUG:
                        print(f"    SOC: {result['soc']}%")
            
            elif identifier == self.ID_TEMP_SENSOR_COUNT:
                if pos < len(data):
                    result['temp_sensor_count'] = data[pos]
                    pos += 1
            
            elif identifier == self.ID_CYCLE_COUNT:
                # Cycle count: 2 bytes
                if pos + 1 < len(data):
                    raw = (data[pos] << 8) | data[pos + 1]
                    pos += 2
                    result['cycle_count'] = raw
            
            elif identifier == self.ID_CYCLE_CAPACITY:
                # Cycle capacity: 4 bytes, Ah
                if pos + 3 < len(data):
                    raw = (data[pos] << 24) | (data[pos+1] << 16) | (data[pos+2] << 8) | data[pos+3]
                    pos += 4
                    result['cycle_capacity'] = raw
            
            elif identifier == self.ID_CELL_COUNT:
                # Cell count: 2 bytes
                if pos + 1 < len(data):
                    raw = (data[pos] << 8) | data[pos + 1]
                    pos += 2
                    result['cell_count_setting'] = raw
            
            elif identifier == self.ID_WARNING:
                # Warning bitmap: 2 bytes
                if pos + 1 < len(data):
                    raw = (data[pos] << 8) | data[pos + 1]
                    pos += 2
                    result['warning'] = raw
                    result['warning_low_capacity'] = bool(raw & 0x0001)
                    result['warning_mos_overtemp'] = bool(raw & 0x0002)
                    result['warning_charge_overvolt'] = bool(raw & 0x0004)
                    result['warning_discharge_undervolt'] = bool(raw & 0x0008)
                    result['warning_battery_overtemp'] = bool(raw & 0x0010)
                    result['warning_charge_overcurrent'] = bool(raw & 0x0020)
                    result['warning_discharge_overcurrent'] = bool(raw & 0x0040)
                    result['warning_cell_diff'] = bool(raw & 0x0080)
            
            elif identifier == self.ID_STATUS:
                # Status bitmap: 2 bytes
                if pos + 1 < len(data):
                    raw = (data[pos] << 8) | data[pos + 1]
                    pos += 2
                    result['status'] = raw
                    result['charge_enabled'] = bool(raw & 0x0001)
                    result['discharge_enabled'] = bool(raw & 0x0002)
                    result['balance_enabled'] = bool(raw & 0x0004)
                    result['battery_online'] = bool(raw & 0x0008)
            
            elif identifier == self.ID_CAPACITY_SETTING:
                # Capacity setting: 4 bytes, Ah
                if pos + 3 < len(data):
                    raw = (data[pos] << 24) | (data[pos+1] << 16) | (data[pos+2] << 8) | data[pos+3]
                    pos += 4
                    result['capacity_setting'] = raw
            
            elif identifier == self.ID_ACTUAL_CAPACITY:
                # Actual capacity: 4 bytes, Ah
                if pos + 3 < len(data):
                    raw = (data[pos] << 24) | (data[pos+1] << 16) | (data[pos+2] << 8) | data[pos+3]
                    pos += 4
                    result['actual_capacity'] = raw
            
            elif identifier == self.ID_PROTOCOL_VERSION:
                # Protocol version: 1 byte
                if pos < len(data):
                    self.protocol_version = data[pos]
                    result['protocol_version'] = self.protocol_version
                    pos += 1
                    if DEBUG:
                        print(f"    Protocol version: 0x{self.protocol_version:02X}")
            
            else:
                # Unknown identifier - try to skip
                # This is tricky without knowing the length
                if DEBUG:
                    print(f"    Unknown identifier 0x{identifier:02X} at pos {pos-1}")
                # Try to continue - might break parsing
                break
        
        # Calculate power
        if 'total_voltage' in result and 'current' in result:
            result['power'] = result['total_voltage'] * result['current']
        elif 'total_voltage_reported' in result and 'current' in result:
            result['power'] = result['total_voltage_reported'] * result['current']
        
        return result
    
    def _parse_proprietary_response(self, data: bytes) -> Optional[Dict[str, Any]]:
        """
        Fallback: Parse proprietary protocol (Type 02 frame)
        Header: 55 AA EB 90 02 00
        """
        header_idx = data.find(b'\x55\xAA\xEB\x90\x02\x00')
        if header_idx == -1:
            if DEBUG:
                print("  Proprietary header not found either")
            return None
        
        d = data[header_idx:]
        if len(d) < 200:
            return None
        
        if DEBUG:
            print(f"  Proprietary frame found, {len(d)} bytes")
        
        result = {}
        
        # Cell voltages: offset 6-37 (16 cells × 2 bytes, little-endian, mV)
        cells = []
        for i in range(16):
            offset = 6 + i * 2
            cell_mv = struct.unpack('<H', d[offset:offset+2])[0]
            if 1000 <= cell_mv <= 5000:
                cells.append(cell_mv / 1000.0)
        
        if cells:
            result['cells'] = cells
            result['cell_count'] = len(cells)
            result['cell_max'] = max(cells)
            result['cell_min'] = min(cells)
            result['cell_diff'] = (max(cells) - min(cells)) * 1000
            result['cell_avg'] = sum(cells) / len(cells)
            result['total_voltage'] = sum(cells)
        
        # ===== CURRENT DETECTION =====
        # Using exact same logic as working v4 InfluxDB parser
        
        current = 0.0
        current_found = False
        
        # Method 1: Check around offset 150 (common location) - signed 16-bit, 10mA units
        for test_offset in [150, 152, 154, 156, 134, 136, 138]:
            if test_offset + 1 < len(d):
                raw_signed = struct.unpack('<h', d[test_offset:test_offset+2])[0]
                raw_unsigned = struct.unpack('<H', d[test_offset:test_offset+2])[0]
                
                if DEBUG and test_offset <= 156:
                    print(f"    Offset {test_offset}: signed={raw_signed}, unsigned={raw_unsigned} (0x{raw_unsigned:04X})")
                
                # Check if this looks like a valid current value
                # Current in 10mA units: ±30000 = ±300A range
                if raw_signed != 0 and -30000 < raw_signed < 30000:
                    if abs(raw_signed) > 10:  # More than 100mA
                        current = raw_signed / 100.0  # Convert 10mA to A
                        current_found = True
                        if DEBUG:
                            print(f"    -> Possible current at offset {test_offset}: {current:.2f}A")
        
        # Method 2: Check for offset-10000 encoding
        if not current_found:
            for test_offset in [150, 152, 154]:
                if test_offset + 1 < len(d):
                    raw = struct.unpack('<H', d[test_offset:test_offset+2])[0]
                    if 8000 <= raw <= 12000:  # Looks like offset-10000 encoding
                        current = (10000 - raw) * 0.01
                        current_found = True
                        if DEBUG:
                            print(f"    -> Offset-10000 current at {test_offset}: raw={raw}, current={current:.2f}A")
                        break
        
        # Method 3: Check for new protocol (bit 15 = direction)
        if not current_found:
            for test_offset in [150, 152, 154]:
                if test_offset + 1 < len(d):
                    raw = struct.unpack('<H', d[test_offset:test_offset+2])[0]
                    if raw & 0x8000:  # Bit 15 set = charging
                        magnitude = (raw & 0x7FFF) * 0.01
                        current = magnitude
                        current_found = True
                        if DEBUG:
                            print(f"    -> New protocol (charging) at {test_offset}: {current:.2f}A")
                        break
        
        result['current'] = current
        
        # ===== SOC (State of Charge) =====
        # Using exact same logic as working v4 InfluxDB parser
        # SOC is typically a single byte, value 0-100
        # Common locations: around offset 164-180
        
        soc_found = False
        if cells:
            expected_soc = int((result['cell_avg'] - 2.5) / (3.65 - 2.5) * 100)
            expected_soc = max(0, min(100, expected_soc))
            
            # Check same offsets as v4 parser
            for test_offset in [151, 161, 164, 165, 166, 167, 180, 181, 102]:
                if test_offset < len(d):
                    potential_soc = d[test_offset]
                    if 0 <= potential_soc <= 100:
                        # Verify it's a reasonable SOC (cross-check with voltage)
                        # Allow some tolerance
                        if abs(potential_soc - expected_soc) < 40 or potential_soc == 100:
                            result['soc'] = potential_soc
                            soc_found = True
                            if DEBUG:
                                print(f"    SOC found at offset {test_offset}: {potential_soc}%")
                            break
            
            if not soc_found:
                # Estimate from voltage (LiFePO4: 2.5V=0%, 3.65V=100%)
                result['soc'] = expected_soc
                if DEBUG:
                    print(f"    SOC estimated from voltage: {result['soc']}%")
        
        # Find MOS temperature
        for test_offset in [144, 145, 146]:
            if test_offset < len(d):
                raw = d[test_offset]
                if 40 <= raw <= 160:  # Valid range for temp encoding
                    temp = raw - 100  # 100 = 0°C
                    if -20 <= temp <= 80:
                        result['temp_mos'] = temp
                        if DEBUG:
                            print(f"    MOS temp at offset {test_offset}: raw={raw}, T={temp}°C")
                        break
        
        if 'temp_mos' not in result:
            result['temp_mos'] = 25  # Default
        
        # Capacity and cycles
        if 0xB4 + 4 <= len(d):
            cap = struct.unpack('<I', d[0xB4:0xB4+4])[0]
            if 10000 < cap < 1000000:
                result['capacity_mah'] = cap
                result['capacity_ah'] = cap / 1000.0
        
        if 0xB8 + 4 <= len(d):
            cycles = struct.unpack('<I', d[0xB8:0xB8+4])[0]
            if cycles < 50000:
                result['cycle_count'] = cycles
        
        # Power calculation
        if 'total_voltage' in result and 'current' in result:
            result['power'] = result['total_voltage'] * result['current']
        
        return result
    
    def read_all_data(self) -> Optional[Dict[str, Any]]:
        """Read all battery data from BMS"""
        if not self.serial:
            return None
        
        # Build and send read-all command
        cmd = self._build_read_all_frame()
        
        if DEBUG:
            print(f"  TX: {cmd.hex()}")
        
        try:
            self.serial.reset_input_buffer()
            self.serial.write(cmd)
            
            time.sleep(0.1)
            
            # Read response
            start = time.time()
            response = b''
            while (time.time() - start) < 2.0:
                if self.serial.in_waiting:
                    response += self.serial.read(self.serial.in_waiting)
                    if len(response) >= 300:
                        time.sleep(0.05)
                        if self.serial.in_waiting:
                            response += self.serial.read(self.serial.in_waiting)
                        break
                time.sleep(0.01)
            
            if not response:
                if DEBUG:
                    print("  No response")
                return None
            
            if DEBUG:
                print(f"  RX ({len(response)} bytes): {response[:30].hex()}...")
            
            return self._parse_response(response)
            
        except Exception as e:
            print(f"  Error: {e}")
            return None


# ============================================================================
# MQTT PUBLISHING
# ============================================================================

class MQTTPublisher:
    """MQTT client for publishing battery data with HA auto-discovery"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.client = mqtt.Client(
            client_id=config['client_id'],
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2
        )
        self.connected = False
        self.discovery_sent = False
    
    def connect(self) -> bool:
        try:
            if self.config.get('username'):
                self.client.username_pw_set(
                    self.config['username'],
                    self.config['password']
                )
            
            self.client.on_connect = self._on_connect
            self.client.on_disconnect = self._on_disconnect
            
            self.client.connect(
                self.config['host'],
                self.config['port'],
                60
            )
            self.client.loop_start()
            time.sleep(1)
            return self.connected
        except Exception as e:
            print(f"✗ MQTT error: {e}")
            return False
    
    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            self.connected = True
            print(f"✓ MQTT connected to {self.config['host']}")
        else:
            print(f"✗ MQTT failed: rc={rc}")
    
    def _on_disconnect(self, client, userdata, rc, properties=None):
        self.connected = False
        print(f"⚠ MQTT disconnected")
    
    def publish(self, topic: str, payload: Any, retain: bool = True):
        if not self.connected:
            return
        full_topic = f"{self.config['topic_prefix']}/{topic}"
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload)
        self.client.publish(full_topic, str(payload), retain=retain)
    
    def send_ha_discovery(self, battery_id: str = "battery_1"):
        """Send Home Assistant MQTT auto-discovery messages"""
        if self.discovery_sent:
            return
        
        prefix = self.config['topic_prefix']
        device = {
            "identifiers": [f"jk_bms_{battery_id}"],
            "name": f"JK BMS {battery_id}",
            "manufacturer": "JK BMS",
            "model": "JK-PB2A16S-20P"
        }
        
        sensors = [
            ("soc", "State of Charge", "%", "battery", "measurement"),
            ("voltage", "Voltage", "V", "voltage", "measurement"),
            ("current", "Current", "A", "current", "measurement"),
            ("power", "Power", "W", "power", "measurement"),
            ("temp_mos", "MOS Temperature", "°C", "temperature", "measurement"),
            ("cell_max", "Cell Max Voltage", "V", "voltage", "measurement"),
            ("cell_min", "Cell Min Voltage", "V", "voltage", "measurement"),
            ("cell_diff", "Cell Delta", "mV", None, "measurement"),
            ("cell_avg", "Cell Average", "V", "voltage", "measurement"),
            ("cycle_count", "Cycle Count", None, None, "total_increasing"),
        ]
        
        for sensor_id, name, unit, device_class, state_class in sensors:
            config_topic = f"homeassistant/sensor/jk_bms_{battery_id}/{sensor_id}/config"
            config = {
                "name": name,
                "unique_id": f"jk_bms_{battery_id}_{sensor_id}",
                "state_topic": f"{prefix}/{battery_id}/{sensor_id}",
                "device": device,
            }
            if unit:
                config["unit_of_measurement"] = unit
            if device_class:
                config["device_class"] = device_class
            if state_class:
                config["state_class"] = state_class
            
            self.client.publish(config_topic, json.dumps(config), retain=True)
        
        # Cell voltage sensors
        for i in range(1, 17):
            config_topic = f"homeassistant/sensor/jk_bms_{battery_id}/cell_{i}/config"
            config = {
                "name": f"Cell {i} Voltage",
                "unique_id": f"jk_bms_{battery_id}_cell_{i}",
                "state_topic": f"{prefix}/{battery_id}/cell_{i}_voltage",
                "unit_of_measurement": "V",
                "device_class": "voltage",
                "state_class": "measurement",
                "device": device,
            }
            self.client.publish(config_topic, json.dumps(config), retain=True)
        
        self.discovery_sent = True
        print("✓ HA auto-discovery sent")
    
    def publish_battery_data(self, data: Dict[str, Any], battery_id: str = "battery_1"):
        """Publish all battery data"""
        self.send_ha_discovery(battery_id)
        
        prefix = battery_id
        
        if 'soc' in data:
            self.publish(f"{prefix}/soc", data['soc'])
        if 'total_voltage' in data:
            self.publish(f"{prefix}/voltage", round(data['total_voltage'], 2))
        if 'current' in data:
            self.publish(f"{prefix}/current", round(data['current'], 2))
        if 'power' in data:
            self.publish(f"{prefix}/power", round(data['power'], 1))
        if 'cell_max' in data:
            self.publish(f"{prefix}/cell_max", round(data['cell_max'], 3))
        if 'cell_min' in data:
            self.publish(f"{prefix}/cell_min", round(data['cell_min'], 3))
        if 'cell_diff' in data:
            self.publish(f"{prefix}/cell_diff", round(data['cell_diff'], 1))
        if 'cell_avg' in data:
            self.publish(f"{prefix}/cell_avg", round(data['cell_avg'], 3))
        if 'temp_mos' in data:
            self.publish(f"{prefix}/temp_mos", round(data['temp_mos'], 1))
        if 'cycle_count' in data:
            self.publish(f"{prefix}/cycle_count", data['cycle_count'])
        
        if 'cells' in data:
            for i, v in enumerate(data['cells'], 1):
                self.publish(f"{prefix}/cell_{i}_voltage", round(v, 3))
        
        self.publish(f"{prefix}/json", data)
    
    def close(self):
        self.client.loop_stop()
        self.client.disconnect()


# ============================================================================
# MAIN
# ============================================================================

def main():
    global DEBUG
    
    print("=" * 70)
    print("JK BMS MQTT Publisher - NW Protocol")
    print("=" * 70)
    
    if "--no-debug" in sys.argv:
        DEBUG = False
    
    # Connect to BMS
    print(f"\n[1/3] Opening {SERIAL_CONFIG['port']}...")
    bms = JKBMSProtocol(SERIAL_CONFIG['port'], SERIAL_CONFIG['baudrate'])
    
    if not bms.connect():
        sys.exit(1)
    
    # Connect to MQTT
    print(f"\n[2/3] Connecting to MQTT...")
    mqtt_pub = MQTTPublisher(MQTT_CONFIG)
    
    if not mqtt_pub.connect():
        bms.close()
        sys.exit(1)
    
    print(f"\n[3/3] Starting monitor (poll every {POLL_INTERVAL}s)")
    print("=" * 70)
    
    poll_count = 0
    success_count = 0
    
    try:
        while True:
            poll_count += 1
            print(f"\n[Poll #{poll_count}] {datetime.now().strftime('%H:%M:%S')}")
            
            data = bms.read_all_data()
            
            if data and data.get('cells'):
                success_count += 1
                
                current = data.get('current', 0)
                if current > 0.01:
                    current_str = f"+{current:.2f}A (CHG)"
                elif current < -0.01:
                    current_str = f"{current:.2f}A (DSC)"
                else:
                    current_str = "0.00A (IDLE)"
                
                print(f"  ╔════════════════════════════════════════════════════════")
                print(f"  ║ SOC: {data.get('soc', '?'):3}%  │  V: {data.get('total_voltage', 0):.2f}V  │  I: {current_str}")
                print(f"  ║ Cells: {data.get('cell_count', 0)}  │  Max: {data.get('cell_max', 0):.3f}V  │  Min: {data.get('cell_min', 0):.3f}V  │  Δ: {data.get('cell_diff', 0):.1f}mV")
                if 'temp_mos' in data:
                    print(f"  ║ MOS Temp: {data['temp_mos']}°C")
                print(f"  ╚════════════════════════════════════════════════════════")
                
                mqtt_pub.publish_battery_data(data)
                print("  → MQTT ✓")
            else:
                print("  ✗ No data")
            
            if poll_count % 10 == 0:
                print(f"\n  [Stats] {success_count}/{poll_count} ({success_count/poll_count*100:.0f}%)")
            
            time.sleep(POLL_INTERVAL)
    
    except KeyboardInterrupt:
        print("\n\nShutting down...")
    finally:
        mqtt_pub.close()
        bms.close()
        print(f"Final: {success_count}/{poll_count}")


if __name__ == "__main__":
    main()
