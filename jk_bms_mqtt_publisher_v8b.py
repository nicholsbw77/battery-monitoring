#!/usr/bin/env python3
"""
JK BMS MQTT Publisher V8 - Bulk Download Protocol
==================================================
Based on: https://github.com/phinix-org/Multiple-JK-BMS-by-Modbus-RS485

The JK BMS uses a special bulk download method instead of standard Modbus reads:
- Write to register 0x1620 triggers a 0x90 (144) byte bulk download of 0x1200 data
- Response format: 55 AA EB 90 02 [addr] [data...] [checksum]
- Data is in LITTLE ENDIAN format

Command to trigger 0x1200 data download:
  TX: [addr] 10 16 20 00 01 02 00 00 [CRC]
  RX: 55 AA EB 90 02 [addr] [~300 bytes of data] [checksum]

RS485 Connection: Use RS485-1 (UART1) port
Baud: 115200, 8N1
"""

import serial
import time
import sys
import struct
import json
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List

try:
    import paho.mqtt.client as mqtt
except ImportError:
    print("Please install paho-mqtt: pip install paho-mqtt")
    exit(1)

try:
    from influxdb_client import InfluxDBClient, Point
    from influxdb_client.client.write_api import SYNCHRONOUS
    INFLUX_AVAILABLE = True
except ImportError:
    INFLUX_AVAILABLE = False

# =============================================================================
# CONFIGURATION
# =============================================================================

SERIAL_PORT = "/dev/ttyUSB0"
BAUDRATE = 115200
SLAVE_ID = 0x00  # BMS address (DIP switch 0x00-0x0F)
POLL_INTERVAL = 5
NUM_CELLS = 16

BATTERY_NOMINAL_CAPACITY_AH = 314

MQTT_CONFIG = {
    "host": "localhost",
    "port": 1883,
    "username": "jkbms",
    "password": "admin",
    "client_id": "jk_bms_publisher",
    "topic_prefix": "jk_bms",
    "ha_discovery_prefix": "homeassistant",
}

INFLUX_CONFIG = {
    "enabled": True,
    "url": "http://192.168.50.46:8086",
    "token": "XnE9ILRtsWQ0IZpxahUWnTvsPspdSagebJjTD_KsiH9KWzOiWztCRH4hGRHHqshRI_Tlb4xdl2SzhfO270QzrA==",
    "org": "home_monitoring",
    "bucket": "battery_data",
}

DEBUG = True
LOG_LEVEL = logging.DEBUG if DEBUG else logging.INFO

logging.basicConfig(
    level=LOG_LEVEL,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)


# =============================================================================
# PROTOCOL CONSTANTS
# =============================================================================

# Bulk download trigger registers (write to these to get data)
REG_DOWNLOAD_1000 = 0x161E  # Settings data, response: 55 AA EB 90 01 XX
REG_DOWNLOAD_1200 = 0x1620  # Runtime data, response: 55 AA EB 90 02 XX  
REG_DOWNLOAD_1400 = 0x161C  # Device info, response: 55 AA EB 90 03 XX

# Response header
RESPONSE_HEADER = bytes([0x55, 0xAA, 0xEB, 0x90])

# Data offsets within the 0x1200 response (AFTER the 6-byte header)
# These are BYTE offsets, data is LITTLE ENDIAN
class Offset:
    """Byte offsets within 0x1200 bulk download data (after 6-byte header)"""
    # Cell voltages (0x0000 - 0x003E) - 16 cells x 2 bytes each
    CELL_VOLTAGE_START = 0x0000
    
    # Cell statistics
    CELL_AVG_VOLTAGE = 0x0044    # 68
    CELL_DIFF_MAX = 0x0046       # 70
    CELL_MAX_NUM = 0x0048        # 72
    CELL_MIN_NUM = 0x0049        # 73
    
    # Temperatures
    TEMP_MOS = 0x008A            # 138 - INT16, 0.1°C
    TEMP_BAT1 = 0x009C           # 156 - INT16, 0.1°C
    TEMP_BAT2 = 0x009E           # 158 - INT16, 0.1°C
    
    # Pack electrical
    TOTAL_VOLTAGE = 0x0090       # 144 - UINT32, mV
    POWER = 0x0094               # 148 - UINT32, mW (can be negative)
    CURRENT = 0x0098             # 152 - INT32, mA
    
    # Alarms
    ALARM_BITS = 0x00A0          # 160 - UINT32
    
    # Balance
    BALANCE_CURRENT = 0x00A4     # 164 - INT16, mA
    BALANCE_STATE = 0x00A6       # 166 - UINT8 (0=off, 1=charge, 2=discharge)
    SOC = 0x00A7                 # 167 - UINT8, %
    
    # Capacity
    CAPACITY_REMAIN = 0x00A8     # 168 - INT32, mAh
    CAPACITY_FULL = 0x00AC       # 172 - UINT32, mAh
    
    # Cycles
    CYCLE_COUNT = 0x00B0         # 176 - UINT32
    CYCLE_CAPACITY = 0x00B4      # 180 - UINT32, mAh
    
    # SOH
    SOH = 0x00B8                 # 184 - UINT8, %
    PRECHARGE = 0x00B9           # 185 - UINT8
    
    # Status
    CHARGE_STATUS = 0x00C0       # 192 - UINT8
    DISCHARGE_STATUS = 0x00C1    # 193 - UINT8
    
    # Alternative voltage
    VOLTAGE_ALT = 0x00E4         # 228 - UINT16, 0.01V


# =============================================================================
# MODBUS CRC
# =============================================================================

def calc_crc16(data: bytes) -> int:
    """Calculate Modbus CRC16."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


def build_write_single_register(slave_id: int, register: int, value: int) -> bytes:
    """Build Modbus Write Multiple Registers (0x10) command for single register."""
    # Function 0x10 = Write Multiple Registers
    # Format: [slave] [0x10] [reg_hi] [reg_lo] [count_hi] [count_lo] [byte_count] [data...] [crc]
    frame = struct.pack('>B B H H B H', 
                        slave_id, 
                        0x10,           # Function code
                        register,       # Start register
                        1,              # Number of registers
                        2,              # Byte count
                        value)          # Value to write
    crc = calc_crc16(frame)
    frame += struct.pack('<H', crc)
    return frame


# =============================================================================
# JK BMS READER - BULK DOWNLOAD PROTOCOL
# =============================================================================

class JKBMSReader:
    """JK BMS Reader using Bulk Download Protocol"""
    
    def __init__(self, port: str, baudrate: int = 115200, slave_id: int = 0,
                 timeout: float = 2.0, num_cells: int = 16,
                 nominal_capacity: float = 314):
        self.port = port
        self.baudrate = baudrate
        self.slave_id = slave_id
        self.timeout = timeout
        self.num_cells = num_cells
        self.nominal_capacity = nominal_capacity
        
        self.serial = None
        self.connected = False
        
    def connect(self) -> bool:
        """Open serial connection."""
        try:
            self.serial = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=self.timeout
            )
            self.connected = True
            logger.info(f"Connected to {self.port} @ {self.baudrate}")
            return True
        except Exception as e:
            logger.error(f"Connection failed: {e}")
            return False
    
    def disconnect(self):
        """Close serial connection."""
        if self.serial:
            self.serial.close()
            self.connected = False
    
    def _send_receive(self, command: bytes, min_response: int = 100) -> Optional[bytes]:
        """Send command and receive response."""
        if not self.connected:
            return None
        
        try:
            # Clear buffers
            self.serial.reset_input_buffer()
            self.serial.reset_output_buffer()
            
            logger.debug(f"TX: {command.hex(' ')}")
            self.serial.write(command)
            
            # Wait for response
            time.sleep(0.3)
            
            data = b''
            start = time.time()
            
            while (time.time() - start) < self.timeout:
                if self.serial.in_waiting:
                    chunk = self.serial.read(self.serial.in_waiting)
                    data += chunk
                    logger.debug(f"  +{len(chunk)} bytes, total: {len(data)}")
                    time.sleep(0.05)
                else:
                    if len(data) >= min_response:
                        time.sleep(0.1)
                        if self.serial.in_waiting:
                            data += self.serial.read(self.serial.in_waiting)
                        break
                    time.sleep(0.02)
            
            if data:
                logger.debug(f"RX: {len(data)} bytes total")
            
            return data if data else None
            
        except Exception as e:
            logger.error(f"Communication error: {e}")
            return None
    
    def request_bulk_download(self, data_type: int = 0x1200) -> Optional[bytes]:
        """
        Request bulk download of register data.
        
        data_type: 0x1000, 0x1200, or 0x1400
        Returns raw response bytes or None
        """
        # Select trigger register
        if data_type == 0x1000:
            trigger_reg = REG_DOWNLOAD_1000
            expected_marker = 0x01
        elif data_type == 0x1200:
            trigger_reg = REG_DOWNLOAD_1200
            expected_marker = 0x02
        elif data_type == 0x1400:
            trigger_reg = REG_DOWNLOAD_1400
            expected_marker = 0x03
        else:
            logger.error(f"Invalid data type: {data_type}")
            return None
        
        # Build write command
        cmd = build_write_single_register(self.slave_id, trigger_reg, 0x0000)
        
        # Send and receive
        response = self._send_receive(cmd, min_response=150)
        
        if not response:
            return None
        
        # Find the 55 AA EB 90 header in response
        header_pos = response.find(RESPONSE_HEADER)
        
        if header_pos == -1:
            logger.warning("Response header 55 AA EB 90 not found")
            logger.debug(f"Response: {response[:50].hex(' ')}...")
            return None
        
        # Extract response starting from header
        response = response[header_pos:]
        
        if len(response) < 10:
            logger.warning(f"Response too short: {len(response)} bytes")
            return None
        
        # Verify response format
        # Bytes 0-3: 55 AA EB 90
        # Byte 4: Data type marker (01, 02, or 03)
        # Byte 5: Device address
        data_marker = response[4]
        device_addr = response[5]
        
        logger.debug(f"Response: marker=0x{data_marker:02X}, addr=0x{device_addr:02X}, len={len(response)}")
        
        if data_marker != expected_marker:
            logger.warning(f"Unexpected data marker: got 0x{data_marker:02X}, expected 0x{expected_marker:02X}")
        
        return response
    
    def read_all(self) -> Optional[Dict[str, Any]]:
        """Read all BMS runtime data using bulk download."""
        if not self.connected:
            if not self.connect():
                return None
        
        # Request 0x1200 data (runtime values)
        response = self.request_bulk_download(0x1200)
        
        if not response or len(response) < 100:
            logger.error("Failed to get bulk download response")
            return None
        
        # Parse the response
        return self._parse_bulk_response(response)
    
    def _parse_bulk_response(self, response: bytes) -> Dict[str, Any]:
        """Parse 55 AA EB 90 02 XX bulk download response."""
        
        result = {
            'cells': [],
            'protocol': 'JK-BulkDownload-0x1200',
            'raw_len': len(response),
        }
        
        # Data starts after 6-byte header (55 AA EB 90 XX XX)
        HEADER_SIZE = 6
        
        if len(response) <= HEADER_SIZE:
            logger.error("Response too short for parsing")
            return result
        
        data = response[HEADER_SIZE:]
        data_len = len(data)
        
        logger.info(f"Parsing {data_len} bytes of register data")
        
        # Helper functions - DATA IS LITTLE ENDIAN
        def read_uint16(offset: int) -> Optional[int]:
            if offset + 2 <= data_len:
                return struct.unpack('<H', data[offset:offset+2])[0]
            return None
        
        def read_int16(offset: int) -> Optional[int]:
            if offset + 2 <= data_len:
                return struct.unpack('<h', data[offset:offset+2])[0]
            return None
        
        def read_uint32(offset: int) -> Optional[int]:
            if offset + 4 <= data_len:
                return struct.unpack('<I', data[offset:offset+4])[0]
            return None
        
        def read_int32(offset: int) -> Optional[int]:
            if offset + 4 <= data_len:
                return struct.unpack('<i', data[offset:offset+4])[0]
            return None
        
        def read_uint8(offset: int) -> Optional[int]:
            if offset < data_len:
                return data[offset]
            return None
        
        # =====================================================================
        # CELL VOLTAGES (offset 0x0000 - 0x001E for 16 cells)
        # =====================================================================
        for i in range(self.num_cells):
            offset = Offset.CELL_VOLTAGE_START + i * 2
            mv = read_uint16(offset)
            if mv and 1000 <= mv <= 5000:
                voltage = mv / 1000.0
                result['cells'].append(voltage)
                logger.debug(f"Cell {i}: {mv}mV = {voltage:.3f}V")
        
        if result['cells']:
            result['cell_count'] = len(result['cells'])
            result['cell_max'] = max(result['cells'])
            result['cell_min'] = min(result['cells'])
            result['cell_diff'] = result['cell_max'] - result['cell_min']
            result['cell_avg'] = sum(result['cells']) / len(result['cells'])
            result['total_voltage'] = sum(result['cells'])
            logger.info(f"Found {len(result['cells'])} cells, sum={result['total_voltage']:.2f}V")
        
        # =====================================================================
        # MOS TEMPERATURE (offset 0x008A) - INT16, 0.1°C
        # =====================================================================
        temp_raw = read_int16(Offset.TEMP_MOS)
        if temp_raw is not None:
            result['temp_mos'] = temp_raw / 10.0
            logger.info(f"MOS Temp: {result['temp_mos']:.1f}°C (raw={temp_raw})")
        
        # =====================================================================
        # TOTAL VOLTAGE (offset 0x0090) - UINT32, mV
        # =====================================================================
        vol_mv = read_uint32(Offset.TOTAL_VOLTAGE)
        if vol_mv and vol_mv > 0:
            result['total_voltage_reported'] = vol_mv / 1000.0
            logger.info(f"Pack Voltage: {result['total_voltage_reported']:.2f}V (raw={vol_mv})")
            if result['cells']:
                if abs(result['total_voltage'] - result['total_voltage_reported']) < 5:
                    result['total_voltage'] = result['total_voltage_reported']
        
        # =====================================================================
        # POWER (offset 0x0094) - UINT32/INT32, mW
        # =====================================================================
        power_raw = read_int32(Offset.POWER)
        if power_raw is not None:
            result['power_reported'] = power_raw / 1000.0
            logger.debug(f"Power: {result['power_reported']:.1f}W")
        
        # =====================================================================
        # CURRENT (offset 0x0098) - INT32, mA
        # =====================================================================
        current_ma = read_int32(Offset.CURRENT)
        if current_ma is not None:
            result['current'] = current_ma / 1000.0
            logger.info(f"Current: {result['current']:.2f}A (raw={current_ma})")
        
        # =====================================================================
        # BATTERY TEMPERATURES (offset 0x009C, 0x009E) - INT16, 0.1°C
        # =====================================================================
        temp1 = read_int16(Offset.TEMP_BAT1)
        if temp1 is not None:
            result['temp_battery_1'] = temp1 / 10.0
            logger.debug(f"Bat Temp 1: {result['temp_battery_1']:.1f}°C")
        
        temp2 = read_int16(Offset.TEMP_BAT2)
        if temp2 is not None:
            result['temp_battery_2'] = temp2 / 10.0
        
        # =====================================================================
        # BALANCE CURRENT (offset 0x00A4) - INT16, mA
        # =====================================================================
        bal_cur = read_int16(Offset.BALANCE_CURRENT)
        if bal_cur is not None:
            result['balance_current_ma'] = bal_cur
            logger.debug(f"Balance Current: {bal_cur}mA")
        
        # =====================================================================
        # BALANCE STATE + SOC (offset 0x00A6, 0x00A7)
        # =====================================================================
        bal_state = read_uint8(Offset.BALANCE_STATE)
        if bal_state is not None:
            result['balance_state'] = bal_state
            result['balancing_on'] = bal_state > 0
        
        soc = read_uint8(Offset.SOC)
        if soc is not None and 0 <= soc <= 100:
            result['soc'] = soc
            logger.info(f"SOC: {soc}%")
        
        # =====================================================================
        # REMAINING CAPACITY (offset 0x00A8) - INT32, mAh
        # =====================================================================
        cap_remain = read_int32(Offset.CAPACITY_REMAIN)
        if cap_remain is not None and cap_remain > 0:
            result['capacity_remaining_ah'] = cap_remain / 1000.0
            logger.info(f"Remaining Capacity: {result['capacity_remaining_ah']:.2f}Ah")
        
        # =====================================================================
        # FULL CHARGE CAPACITY (offset 0x00AC) - UINT32, mAh
        # =====================================================================
        cap_full = read_uint32(Offset.CAPACITY_FULL)
        if cap_full is not None and cap_full > 0:
            result['capacity_full_ah'] = cap_full / 1000.0
            result['capacity_nominal_ah'] = result['capacity_full_ah']
            logger.info(f"Full Capacity: {result['capacity_full_ah']:.2f}Ah")
        else:
            result['capacity_nominal_ah'] = self.nominal_capacity
        
        # =====================================================================
        # CYCLE COUNT (offset 0x00B0) - UINT32
        # =====================================================================
        cycles = read_uint32(Offset.CYCLE_COUNT)
        if cycles is not None and cycles < 100000:
            result['cycle_count'] = cycles
            logger.info(f"Cycles: {cycles}")
        
        # =====================================================================
        # CYCLE CAPACITY (offset 0x00B4) - UINT32, mAh
        # =====================================================================
        cycle_cap = read_uint32(Offset.CYCLE_CAPACITY)
        if cycle_cap is not None:
            result['cycle_capacity_ah'] = cycle_cap / 1000.0
            logger.info(f"Cycle Capacity: {result['cycle_capacity_ah']:.2f}Ah")
        
        # =====================================================================
        # SOH (offset 0x00B8) - UINT8
        # =====================================================================
        soh = read_uint8(Offset.SOH)
        if soh is not None and 0 <= soh <= 100:
            result['soh'] = soh
            logger.info(f"SOH: {soh}%")
        
        # =====================================================================
        # CHARGE/DISCHARGE STATUS (offset 0x00C0, 0x00C1)
        # =====================================================================
        charge_status = read_uint8(Offset.CHARGE_STATUS)
        discharge_status = read_uint8(Offset.DISCHARGE_STATUS)
        
        if charge_status is not None:
            result['charge_mos_on'] = charge_status == 1
        if discharge_status is not None:
            result['discharge_mos_on'] = discharge_status == 1
        
        # =====================================================================
        # ALTERNATIVE VOLTAGE (offset 0x00E4) - UINT16, 0.01V
        # =====================================================================
        vol_alt = read_uint16(Offset.VOLTAGE_ALT)
        if vol_alt and vol_alt > 0:
            volt = vol_alt * 0.01
            logger.debug(f"Alt Voltage: {volt:.2f}V")
            if 'total_voltage' not in result or result['total_voltage'] == 0:
                result['total_voltage'] = volt
        
        # =====================================================================
        # DERIVED VALUES
        # =====================================================================
        voltage = result.get('total_voltage', 0)
        current = result.get('current', 0)
        result['power'] = voltage * current
        
        if 'capacity_remaining_ah' not in result and 'soc' in result:
            nominal = result.get('capacity_nominal_ah', self.nominal_capacity)
            result['capacity_remaining_ah'] = nominal * result['soc'] / 100.0
        
        if 'soh' not in result:
            result['soh'] = 100
        
        result['timestamp'] = datetime.now().isoformat()
        
        return result
    
    def dump_raw_data(self):
        """Dump raw response for debugging."""
        print("\n" + "=" * 70)
        print("RAW BULK DOWNLOAD DUMP")
        print("=" * 70)
        
        cmd = build_write_single_register(self.slave_id, REG_DOWNLOAD_1200, 0x0000)
        print(f"\nWrite command to trigger 0x1200 download:")
        print(f"  TX: {cmd.hex(' ')}")
        print(f"  Slave: 0x{self.slave_id:02X}, Register: 0x{REG_DOWNLOAD_1200:04X}, Value: 0x0000")
        
        response = self._send_receive(cmd, min_response=100)
        
        if not response:
            print("\n✗ No response received!")
            return
        
        print(f"\nResponse: {len(response)} bytes")
        
        # Full hex dump
        print("\nHex dump:")
        for i in range(0, len(response), 16):
            hex_part = ' '.join(f'{b:02X}' for b in response[i:i+16])
            ascii_part = ''.join(chr(b) if 32 <= b < 127 else '.' for b in response[i:i+16])
            print(f"  {i:04X}: {hex_part:<48} {ascii_part}")
        
        # Find header
        header_pos = response.find(RESPONSE_HEADER)
        if header_pos >= 0:
            print(f"\n✓ Found 55 AA EB 90 header at offset {header_pos}")
            
            if len(response) > header_pos + 6:
                data_type = response[header_pos + 4]
                device_addr = response[header_pos + 5]
                print(f"  Data type: 0x{data_type:02X} ({'0x1000' if data_type == 1 else '0x1200' if data_type == 2 else '0x1400' if data_type == 3 else 'unknown'})")
                print(f"  Device address: 0x{device_addr:02X}")
                
                # Parse key values
                data = response[header_pos + 6:]
                print(f"\n  Data portion: {len(data)} bytes")
                
                print("\n  Key register values (LITTLE ENDIAN):")
                
                key_regs = [
                    (0x0000, "Cell 0 Voltage", "UINT16", "mV"),
                    (0x0002, "Cell 1 Voltage", "UINT16", "mV"),
                    (0x001E, "Cell 15 Voltage", "UINT16", "mV"),
                    (0x008A, "MOS Temp", "INT16", "0.1°C"),
                    (0x0090, "Total Voltage", "UINT32", "mV"),
                    (0x0094, "Power", "INT32", "mW"),
                    (0x0098, "Current", "INT32", "mA"),
                    (0x009C, "Bat Temp 1", "INT16", "0.1°C"),
                    (0x00A4, "Balance Current", "INT16", "mA"),
                    (0x00A6, "Balance State", "UINT8", ""),
                    (0x00A7, "SOC", "UINT8", "%"),
                    (0x00A8, "Remaining Cap", "INT32", "mAh"),
                    (0x00AC, "Full Cap", "UINT32", "mAh"),
                    (0x00B0, "Cycle Count", "UINT32", ""),
                    (0x00B4, "Cycle Capacity", "UINT32", "mAh"),
                    (0x00B8, "SOH", "UINT8", "%"),
                    (0x00C0, "Charge Status", "UINT8", ""),
                    (0x00C1, "Discharge Status", "UINT8", ""),
                    (0x00E4, "Alt Voltage", "UINT16", "0.01V"),
                ]
                
                for offset, name, dtype, unit in key_regs:
                    if offset >= len(data):
                        continue
                    
                    raw_hex = data[offset:offset+4].hex(' ') if offset + 4 <= len(data) else data[offset:].hex(' ')
                    
                    if dtype == "UINT8":
                        val = data[offset]
                        print(f"    0x{offset:04X} {name:20s}: {val:8d} {unit:10s} [{raw_hex}]")
                    elif dtype == "UINT16":
                        if offset + 2 <= len(data):
                            val = struct.unpack('<H', data[offset:offset+2])[0]
                            print(f"    0x{offset:04X} {name:20s}: {val:8d} {unit:10s} [{raw_hex}]")
                    elif dtype == "INT16":
                        if offset + 2 <= len(data):
                            val = struct.unpack('<h', data[offset:offset+2])[0]
                            print(f"    0x{offset:04X} {name:20s}: {val:8d} {unit:10s} [{raw_hex}]")
                    elif dtype == "UINT32":
                        if offset + 4 <= len(data):
                            val = struct.unpack('<I', data[offset:offset+4])[0]
                            print(f"    0x{offset:04X} {name:20s}: {val:8d} {unit:10s} [{raw_hex}]")
                    elif dtype == "INT32":
                        if offset + 4 <= len(data):
                            val = struct.unpack('<i', data[offset:offset+4])[0]
                            print(f"    0x{offset:04X} {name:20s}: {val:8d} {unit:10s} [{raw_hex}]")
        else:
            print("\n✗ Header 55 AA EB 90 not found!")
            print("  This might indicate the BMS is in master mode (address 0x00)")
            print("  or the protocol is different than expected.")
        
        print("\n" + "=" * 70)


# =============================================================================
# HOME ASSISTANT DISCOVERY
# =============================================================================

HA_DEVICE_INFO = {
    "identifiers": ["jk_bms_1"],
    "name": "JK BMS Battery",
    "manufacturer": "Jikong/JK",
    "model": "JK-PB2A16S-20P",
    "sw_version": "v8.0-bulk",
}

HA_SENSORS = [
    {"name": "SOC", "unique_id": "jk_bms_1_soc", "state_topic": "jk_bms/battery_1/soc",
     "unit_of_measurement": "%", "device_class": "battery", "state_class": "measurement"},
    {"name": "SOH", "unique_id": "jk_bms_1_soh", "state_topic": "jk_bms/battery_1/soh",
     "unit_of_measurement": "%", "state_class": "measurement"},
    {"name": "Voltage", "unique_id": "jk_bms_1_voltage", "state_topic": "jk_bms/battery_1/voltage",
     "unit_of_measurement": "V", "device_class": "voltage", "state_class": "measurement"},
    {"name": "Current", "unique_id": "jk_bms_1_current", "state_topic": "jk_bms/battery_1/current",
     "unit_of_measurement": "A", "device_class": "current", "state_class": "measurement"},
    {"name": "Power", "unique_id": "jk_bms_1_power", "state_topic": "jk_bms/battery_1/power",
     "unit_of_measurement": "W", "device_class": "power", "state_class": "measurement"},
    {"name": "MOS Temp", "unique_id": "jk_bms_1_temp_mos", "state_topic": "jk_bms/battery_1/temp_mos",
     "unit_of_measurement": "°C", "device_class": "temperature", "state_class": "measurement"},
    {"name": "Battery Temp", "unique_id": "jk_bms_1_temp_battery", "state_topic": "jk_bms/battery_1/temp_battery",
     "unit_of_measurement": "°C", "device_class": "temperature", "state_class": "measurement"},
    {"name": "Capacity Remaining", "unique_id": "jk_bms_1_capacity_remaining", "state_topic": "jk_bms/battery_1/capacity_remaining",
     "unit_of_measurement": "Ah", "state_class": "measurement"},
    {"name": "Capacity Full", "unique_id": "jk_bms_1_capacity_full", "state_topic": "jk_bms/battery_1/capacity_full",
     "unit_of_measurement": "Ah", "state_class": "measurement"},
    {"name": "Cycle Count", "unique_id": "jk_bms_1_cycle_count", "state_topic": "jk_bms/battery_1/cycle_count",
     "state_class": "total_increasing"},
    {"name": "Cycle Capacity", "unique_id": "jk_bms_1_cycle_capacity", "state_topic": "jk_bms/battery_1/cycle_capacity",
     "unit_of_measurement": "Ah", "state_class": "total_increasing"},
    {"name": "Cell Max", "unique_id": "jk_bms_1_cell_max", "state_topic": "jk_bms/battery_1/cell_max",
     "unit_of_measurement": "V", "device_class": "voltage", "state_class": "measurement", "suggested_display_precision": 3},
    {"name": "Cell Min", "unique_id": "jk_bms_1_cell_min", "state_topic": "jk_bms/battery_1/cell_min",
     "unit_of_measurement": "V", "device_class": "voltage", "state_class": "measurement", "suggested_display_precision": 3},
    {"name": "Cell Delta", "unique_id": "jk_bms_1_cell_diff", "state_topic": "jk_bms/battery_1/cell_diff",
     "unit_of_measurement": "mV", "state_class": "measurement"},
    {"name": "Balance Current", "unique_id": "jk_bms_1_balance_current", "state_topic": "jk_bms/battery_1/balance_current",
     "unit_of_measurement": "mA", "state_class": "measurement"},
]

for i in range(1, 17):
    HA_SENSORS.append({
        "name": f"Cell {i}",
        "unique_id": f"jk_bms_1_cell_{i}_voltage",
        "state_topic": f"jk_bms/battery_1/cell_{i}_voltage",
        "unit_of_measurement": "V",
        "device_class": "voltage",
        "state_class": "measurement",
        "suggested_display_precision": 3,
    })


# =============================================================================
# MQTT CLIENT
# =============================================================================

class MQTTPublisher:
    def __init__(self, config):
        self.config = config
        self.client = None
        self.connected = False
        self.discovery_published = False

    def connect(self):
        try:
            try:
                self.client = mqtt.Client(
                    client_id=self.config["client_id"],
                    callback_api_version=mqtt.CallbackAPIVersion.VERSION1
                )
            except (AttributeError, TypeError):
                self.client = mqtt.Client(client_id=self.config["client_id"])

            if self.config.get("username"):
                self.client.username_pw_set(self.config["username"], self.config["password"])

            self.client.on_connect = self._on_connect
            self.client.on_disconnect = self._on_disconnect

            self.client.connect(self.config["host"], self.config["port"], 60)
            self.client.loop_start()

            timeout = 10
            while not self.connected and timeout > 0:
                time.sleep(0.5)
                timeout -= 0.5

            return self.connected
        except Exception as e:
            logger.error(f"MQTT connection error: {e}")
            return False

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self.connected = True
            logger.info("Connected to MQTT")
            self.publish_ha_discovery()

    def _on_disconnect(self, client, userdata, rc):
        self.connected = False

    def publish_ha_discovery(self):
        if self.discovery_published:
            return

        prefix = self.config.get("ha_discovery_prefix", "homeassistant")
        for sensor in HA_SENSORS:
            topic = f"{prefix}/sensor/{sensor['unique_id']}/config"
            config = {
                "name": sensor["name"],
                "unique_id": sensor["unique_id"],
                "state_topic": sensor["state_topic"],
                "device": HA_DEVICE_INFO,
            }
            for k in ["unit_of_measurement", "device_class", "state_class", "suggested_display_precision"]:
                if k in sensor:
                    config[k] = sensor[k]
            self.client.publish(topic, json.dumps(config), retain=True)

        self.discovery_published = True
        logger.info(f"Published {len(HA_SENSORS)} HA sensors")

    def publish(self, battery_id: str, data: Dict[str, Any]) -> bool:
        if not self.connected:
            return False

        p = f"{self.config['topic_prefix']}/{battery_id}"

        try:
            self.client.publish(f"{p}/soc", data.get('soc', 0), retain=True)
            self.client.publish(f"{p}/voltage", round(data.get('total_voltage', 0), 2), retain=True)
            self.client.publish(f"{p}/current", round(data.get('current', 0), 2), retain=True)
            self.client.publish(f"{p}/power", round(data.get('power', 0), 1), retain=True)

            if 'soh' in data:
                self.client.publish(f"{p}/soh", data['soh'], retain=True)
            if 'temp_mos' in data:
                self.client.publish(f"{p}/temp_mos", round(data['temp_mos'], 1), retain=True)
            if 'temp_battery_1' in data:
                self.client.publish(f"{p}/temp_battery", round(data['temp_battery_1'], 1), retain=True)
            if 'capacity_remaining_ah' in data:
                self.client.publish(f"{p}/capacity_remaining", round(data['capacity_remaining_ah'], 2), retain=True)
            if 'capacity_full_ah' in data:
                self.client.publish(f"{p}/capacity_full", round(data['capacity_full_ah'], 2), retain=True)
            if 'cycle_count' in data:
                self.client.publish(f"{p}/cycle_count", data['cycle_count'], retain=True)
            if 'cycle_capacity_ah' in data:
                self.client.publish(f"{p}/cycle_capacity", round(data['cycle_capacity_ah'], 2), retain=True)
            if 'balance_current_ma' in data:
                self.client.publish(f"{p}/balance_current", data['balance_current_ma'], retain=True)

            self.client.publish(f"{p}/cell_max", round(data.get('cell_max', 0), 3), retain=True)
            self.client.publish(f"{p}/cell_min", round(data.get('cell_min', 0), 3), retain=True)
            self.client.publish(f"{p}/cell_diff", round(data.get('cell_diff', 0) * 1000, 1), retain=True)

            for i, v in enumerate(data.get('cells', []), 1):
                self.client.publish(f"{p}/cell_{i}_voltage", round(v, 3), retain=True)

            self.client.publish(f"{p}/json", json.dumps(data, default=str), retain=True)
            return True
        except Exception as e:
            logger.error(f"MQTT publish error: {e}")
            return False

    def close(self):
        if self.client:
            self.client.loop_stop()
            self.client.disconnect()


# =============================================================================
# INFLUXDB
# =============================================================================

def write_influxdb(write_api, bucket: str, data: Dict[str, Any]) -> bool:
    if not INFLUX_AVAILABLE:
        return False
    try:
        ts = datetime.utcnow()

        pt = Point("battery").tag("battery_id", "jk_bms_1")
        pt.field("soc", float(data.get('soc', 0)))
        pt.field("voltage", float(data.get('total_voltage', 0)))
        pt.field("current", float(data.get('current', 0)))
        pt.field("power", float(data.get('power', 0)))

        for k in ['soh', 'cycle_count', 'temp_mos', 'capacity_remaining_ah', 'capacity_full_ah', 'cycle_capacity_ah']:
            if k in data:
                pt.field(k, float(data[k]))

        pt.time(ts)
        write_api.write(bucket=bucket, record=pt)

        for i, v in enumerate(data.get('cells', []), 1):
            cp = Point("cell").tag("battery_id", "jk_bms_1").tag("cell", str(i))
            cp.field("voltage", float(v)).time(ts)
            write_api.write(bucket=bucket, record=cp)

        return True
    except Exception as e:
        logger.error(f"InfluxDB error: {e}")
        return False


# =============================================================================
# MAIN
# =============================================================================

def main():
    print("=" * 70)
    print("JK BMS MQTT Publisher V8 - Bulk Download Protocol")
    print("=" * 70)
    print(f"Serial: {SERIAL_PORT} @ {BAUDRATE}")
    print(f"Slave ID: 0x{SLAVE_ID:02X}")
    print(f"Protocol: Write to 0x1620 -> 55 AA EB 90 02 response")

    bms = JKBMSReader(
        port=SERIAL_PORT,
        baudrate=BAUDRATE,
        slave_id=SLAVE_ID,
        num_cells=NUM_CELLS,
        nominal_capacity=BATTERY_NOMINAL_CAPACITY_AH
    )

    print(f"\n[1/4] Connecting to BMS...")
    if not bms.connect():
        print("✗ Failed")
        sys.exit(1)
    print("✓ Connected")

    print(f"\n[2/4] Connecting to MQTT...")
    mqtt_pub = MQTTPublisher(MQTT_CONFIG)
    if not mqtt_pub.connect():
        print("✗ MQTT failed")
        bms.disconnect()
        sys.exit(1)

    influx_client = None
    write_api = None
    if INFLUX_CONFIG["enabled"] and INFLUX_AVAILABLE:
        print(f"\n[3/4] Connecting to InfluxDB...")
        try:
            influx_client = InfluxDBClient(
                url=INFLUX_CONFIG["url"],
                token=INFLUX_CONFIG["token"],
                org=INFLUX_CONFIG["org"]
            )
            write_api = influx_client.write_api(write_options=SYNCHRONOUS)
            print("✓ InfluxDB connected")
        except Exception as e:
            print(f"⚠ InfluxDB failed: {e}")

    print(f"\n[4/4] Starting monitor (interval: {POLL_INTERVAL}s)")
    print("=" * 70)

    poll = 0
    failures = 0

    try:
        while True:
            poll += 1
            print(f"\n[Poll #{poll}] {datetime.now().strftime('%H:%M:%S')}")

            data = bms.read_all()

            if data and data.get('cells'):
                failures = 0

                cur = data.get('current', 0)
                cur_str = f"+{cur:.2f}A (CHG)" if cur > 0.01 else f"{cur:.2f}A (DSC)" if cur < -0.01 else f"{cur:.2f}A (IDLE)"

                print(f"  ╔═══════════════════════════════════════════════════════════════")
                print(f"  ║ SOC: {data.get('soc', '?'):>3}%  │  SOH: {data.get('soh', '?')}%  │  Voltage: {data.get('total_voltage', 0):6.2f}V")
                print(f"  ║ Current: {cur_str}  │  Power: {data.get('power', 0):>7.1f}W")

                cells = data.get('cells', [])
                if cells:
                    print(f"  ║ Cells: {len(cells):2d}   │  Max: {data['cell_max']:.3f}V  │  Min: {data['cell_min']:.3f}V  │  Δ: {data['cell_diff']*1000:.1f}mV")

                rem = data.get('capacity_remaining_ah')
                full = data.get('capacity_full_ah', data.get('capacity_nominal_ah'))
                if rem:
                    print(f"  ║ Capacity: {rem:.1f}Ah / {full:.1f}Ah")

                cyc = data.get('cycle_count')
                cyc_cap = data.get('cycle_capacity_ah')
                if cyc:
                    s = f"Cycles: {cyc}"
                    if cyc_cap:
                        s += f" ({cyc_cap:.0f}Ah total)"
                    print(f"  ║ {s}")

                if 'temp_mos' in data:
                    print(f"  ║ Temp MOS: {data['temp_mos']:.1f}°C")

                print(f"  ╚═══════════════════════════════════════════════════════════════")

                if mqtt_pub.publish("battery_1", data):
                    print("  → MQTT ✓")

                if write_api and write_influxdb(write_api, INFLUX_CONFIG["bucket"], data):
                    print("  → InfluxDB ✓")
            else:
                failures += 1
                print(f"  ✗ No valid data (failures: {failures})")

                if failures >= 3:
                    print("  Reconnecting...")
                    bms.disconnect()
                    time.sleep(1)
                    if bms.connect():
                        failures = 0

            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print("\n\nShutting down...")
    finally:
        bms.disconnect()
        mqtt_pub.close()
        if influx_client:
            influx_client.close()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        if sys.argv[1] == "--dump":
            bms = JKBMSReader(SERIAL_PORT, BAUDRATE, SLAVE_ID, num_cells=NUM_CELLS)
            if bms.connect():
                bms.dump_raw_data()
                bms.disconnect()
        elif sys.argv[1] == "--help":
            print("JK BMS MQTT Publisher V8 - Bulk Download Protocol")
            print("\nUsage:")
            print("  python script.py        # Normal operation")
            print("  python script.py --dump # Dump raw response")
            print("\nProtocol:")
            print("  TX: [addr] 10 16 20 00 01 02 00 00 [CRC]")
            print("  RX: 55 AA EB 90 02 [addr] [data...] [checksum]")
            print("\nNote: Use UART1 port (RS485-1) for slave mode communication")
    else:
        main()
