#!/usr/bin/env python3
"""
EG4 Battery Monitor - Solar Assistant MQTT to InfluxDB Bridge
==============================================================
Subscribes to Solar Assistant MQTT topics and writes EG4 battery data to InfluxDB.

Usage:
    pip install paho-mqtt influxdb-client
    python eg4_solar_assistant_bridge.py

Configuration:
    Edit the CONFIG section below with your Solar Assistant IP and InfluxDB settings.
"""

import json
import time
import logging
from datetime import datetime
from typing import Dict, Any, Optional
import re

try:
    import paho.mqtt.client as mqtt
except ImportError:
    print("Please install paho-mqtt: pip install paho-mqtt --break-system-packages")
    exit(1)

try:
    from influxdb_client import InfluxDBClient, Point
    from influxdb_client.client.write_api import SYNCHRONOUS
except ImportError:
    print("Please install influxdb-client: pip install influxdb-client --break-system-packages")
    exit(1)

# ============================================================================
# CONFIGURATION - Edit these settings for your setup
# ============================================================================

CONFIG = {
    # Solar Assistant MQTT settings
    "mqtt": {
        "host": "192.168.1.XXX",  # Replace with your Solar Assistant IP
        "port": 1883,
        "username": "",           # Leave empty if no auth required
        "password": "",           # Leave empty if no auth required
        "client_id": "eg4_influx_bridge",
    },
    
    # InfluxDB settings (your existing Grafana database)
    "influxdb": {
        "url": "http://localhost:8086",     # InfluxDB URL
        "token": "your-influxdb-token",      # For InfluxDB 2.x, or leave empty for 1.x
        "org": "your-org",                   # For InfluxDB 2.x
        "bucket": "eg4_batteries",           # Database/bucket name
        # For InfluxDB 1.x compatibility:
        "database": "eg4_batteries",
        "username": "",
        "password": "",
    },
    
    # Battery configuration
    "batteries": {
        "count": 4,                          # Number of EG4 batteries
        "names": ["Battery_1", "Battery_2", "Battery_3", "Battery_4"],
    },
    
    # Logging
    "log_level": logging.INFO,
}

# ============================================================================
# MQTT Topic Mappings for Solar Assistant
# ============================================================================

# Solar Assistant publishes EG4 battery data to topics like:
# solar_assistant/battery_1/state_of_charge/state
# solar_assistant/battery_1/voltage/state
# solar_assistant/total/battery_power/state

BATTERY_TOPICS = {
    # Per-battery topics (battery_X where X is 1-based index)
    "state_of_charge": {"field": "soc", "unit": "%", "type": float},
    "voltage": {"field": "voltage", "unit": "V", "type": float},
    "current": {"field": "current", "unit": "A", "type": float},
    "power": {"field": "power", "unit": "W", "type": float},
    "temperature": {"field": "temperature", "unit": "°C", "type": float},
    "state_of_health": {"field": "soh", "unit": "%", "type": float},
    "capacity": {"field": "capacity", "unit": "Ah", "type": float},
    "charge_capacity": {"field": "charge_capacity", "unit": "Ah", "type": float},
    "cell_voltage_min": {"field": "cell_voltage_min", "unit": "V", "type": float},
    "cell_voltage_max": {"field": "cell_voltage_max", "unit": "V", "type": float},
    "cell_voltage_avg": {"field": "cell_voltage_average", "unit": "V", "type": float},
    "cell_voltage_1": {"field": "cell_1_voltage", "unit": "V", "type": float},
    "cell_voltage_2": {"field": "cell_2_voltage", "unit": "V", "type": float},
    "cell_voltage_3": {"field": "cell_3_voltage", "unit": "V", "type": float},
    "cell_voltage_4": {"field": "cell_4_voltage", "unit": "V", "type": float},
    "cell_voltage_5": {"field": "cell_5_voltage", "unit": "V", "type": float},
    "cell_voltage_6": {"field": "cell_6_voltage", "unit": "V", "type": float},
    "cell_voltage_7": {"field": "cell_7_voltage", "unit": "V", "type": float},
    "cell_voltage_8": {"field": "cell_8_voltage", "unit": "V", "type": float},
    "cell_voltage_9": {"field": "cell_9_voltage", "unit": "V", "type": float},
    "cell_voltage_10": {"field": "cell_10_voltage", "unit": "V", "type": float},
    "cell_voltage_11": {"field": "cell_11_voltage", "unit": "V", "type": float},
    "cell_voltage_12": {"field": "cell_12_voltage", "unit": "V", "type": float},
    "cell_voltage_13": {"field": "cell_13_voltage", "unit": "V", "type": float},
    "cell_voltage_14": {"field": "cell_14_voltage", "unit": "V", "type": float},
    "cell_voltage_15": {"field": "cell_15_voltage", "unit": "V", "type": float},
    "cell_voltage_16": {"field": "cell_16_voltage", "unit": "V", "type": float},
}

TOTAL_TOPICS = {
    # Aggregate/total topics
    "battery_power": {"field": "total_power", "unit": "W", "type": float},
    "battery_current": {"field": "total_current", "unit": "A", "type": float},
    "battery_voltage": {"field": "total_voltage", "unit": "V", "type": float},
    "battery_state_of_charge": {"field": "total_soc", "unit": "%", "type": float},
    "battery_temperature": {"field": "total_temperature", "unit": "°C", "type": float},
    "battery_capacity": {"field": "total_capacity", "unit": "Ah", "type": float},
}

# ============================================================================
# Global state
# ============================================================================

logging.basicConfig(
    level=CONFIG["log_level"],
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Data cache for batching writes
battery_data: Dict[str, Dict[str, Any]] = {}
total_data: Dict[str, Any] = {}
last_write_time = time.time()
WRITE_INTERVAL = 5  # Seconds between InfluxDB writes

# ============================================================================
# InfluxDB Writer
# ============================================================================

class InfluxWriter:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.client = None
        self.write_api = None
        self.connected = False
        
    def connect(self):
        """Connect to InfluxDB."""
        try:
            # Try InfluxDB 2.x style connection
            if self.config.get("token"):
                self.client = InfluxDBClient(
                    url=self.config["url"],
                    token=self.config["token"],
                    org=self.config["org"]
                )
                self.write_api = self.client.write_api(write_options=SYNCHRONOUS)
                self.bucket = self.config["bucket"]
            else:
                # InfluxDB 1.x compatibility mode
                self.client = InfluxDBClient(
                    url=self.config["url"],
                    token=f'{self.config.get("username", "")}:{self.config.get("password", "")}',
                    org="-"
                )
                self.write_api = self.client.write_api(write_options=SYNCHRONOUS)
                self.bucket = f'{self.config["database"]}/autogen'
            
            self.connected = True
            logger.info(f"Connected to InfluxDB at {self.config['url']}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to InfluxDB: {e}")
            return False
    
    def write_battery_data(self, battery_id: str, data: Dict[str, Any]):
        """Write battery data to InfluxDB."""
        if not self.connected:
            return
            
        try:
            point = Point("eg4_battery")
            point.tag("battery_id", battery_id)
            point.tag("battery_type", "EG4_LifePower4_V2")
            
            for field, value in data.items():
                if value is not None:
                    point.field(field, value)
            
            point.time(datetime.utcnow())
            self.write_api.write(bucket=self.bucket, record=point)
            logger.debug(f"Wrote data for {battery_id}: {data}")
        except Exception as e:
            logger.error(f"Failed to write battery data: {e}")
    
    def write_total_data(self, data: Dict[str, Any]):
        """Write aggregate battery data to InfluxDB."""
        if not self.connected:
            return
            
        try:
            point = Point("eg4_battery_total")
            point.tag("battery_type", "EG4_LifePower4_V2")
            
            for field, value in data.items():
                if value is not None:
                    point.field(field, value)
            
            point.time(datetime.utcnow())
            self.write_api.write(bucket=self.bucket, record=point)
            logger.debug(f"Wrote total data: {data}")
        except Exception as e:
            logger.error(f"Failed to write total data: {e}")
    
    def close(self):
        """Close InfluxDB connection."""
        if self.client:
            self.client.close()
            logger.info("InfluxDB connection closed")

# ============================================================================
# MQTT Message Handler
# ============================================================================

def parse_topic(topic: str) -> Optional[Dict[str, Any]]:
    """
    Parse Solar Assistant MQTT topic.
    
    Topics follow patterns like:
    - solar_assistant/battery_1/state_of_charge/state
    - solar_assistant/total/battery_power/state
    """
    parts = topic.split("/")
    
    if len(parts) < 4 or parts[0] != "solar_assistant":
        return None
    
    # Check for per-battery topic
    battery_match = re.match(r"battery_(\d+)", parts[1])
    if battery_match:
        battery_num = int(battery_match.group(1))
        metric = parts[2]
        return {
            "type": "battery",
            "battery_num": battery_num,
            "metric": metric,
        }
    
    # Check for total/aggregate topic
    if parts[1] == "total":
        metric = parts[2]
        return {
            "type": "total",
            "metric": metric,
        }
    
    return None

def on_connect(client, userdata, flags, rc, properties=None):
    """MQTT connection callback."""
    if rc == 0:
        logger.info("Connected to Solar Assistant MQTT broker")
        # Subscribe to all battery topics
        client.subscribe("solar_assistant/battery_#")
        client.subscribe("solar_assistant/total/#")
        logger.info("Subscribed to solar_assistant/battery_# and solar_assistant/total/#")
    else:
        logger.error(f"MQTT connection failed with code {rc}")

def on_message(client, userdata, msg):
    """MQTT message callback."""
    global battery_data, total_data, last_write_time
    
    try:
        topic = msg.topic
        payload = msg.payload.decode("utf-8")
        
        # Parse the value
        try:
            value = float(payload)
        except ValueError:
            value = payload
        
        # Parse the topic
        parsed = parse_topic(topic)
        if not parsed:
            return
        
        if parsed["type"] == "battery":
            battery_id = f"battery_{parsed['battery_num']}"
            metric = parsed["metric"]
            
            # Look up field mapping
            if metric in BATTERY_TOPICS:
                field_info = BATTERY_TOPICS[metric]
                field_name = field_info["field"]
                
                if battery_id not in battery_data:
                    battery_data[battery_id] = {}
                
                battery_data[battery_id][field_name] = field_info["type"](value)
                logger.debug(f"Cached {battery_id}.{field_name} = {value}")
        
        elif parsed["type"] == "total":
            metric = parsed["metric"]
            
            if metric in TOTAL_TOPICS:
                field_info = TOTAL_TOPICS[metric]
                field_name = field_info["field"]
                total_data[field_name] = field_info["type"](value)
                logger.debug(f"Cached total.{field_name} = {value}")
        
    except Exception as e:
        logger.error(f"Error processing message from {msg.topic}: {e}")

def on_disconnect(client, userdata, rc, properties=None):
    """MQTT disconnection callback."""
    logger.warning(f"Disconnected from MQTT broker (rc={rc})")

# ============================================================================
# Main Loop
# ============================================================================

def flush_data_to_influx(influx: InfluxWriter):
    """Write cached data to InfluxDB."""
    global battery_data, total_data
    
    # Write per-battery data
    for battery_id, data in battery_data.items():
        if data:
            influx.write_battery_data(battery_id, data.copy())
    
    # Write total data
    if total_data:
        influx.write_total_data(total_data.copy())
    
    # Clear cache (keep structure)
    for battery_id in battery_data:
        battery_data[battery_id] = {}
    total_data = {}

def main():
    global last_write_time
    
    logger.info("=" * 60)
    logger.info("EG4 Battery Monitor - Solar Assistant MQTT to InfluxDB Bridge")
    logger.info("=" * 60)
    
    # Connect to InfluxDB
    influx = InfluxWriter(CONFIG["influxdb"])
    if not influx.connect():
        logger.error("Failed to connect to InfluxDB. Exiting.")
        return
    
    # Set up MQTT client
    mqtt_config = CONFIG["mqtt"]
    
    # Use MQTT v5 callback API
    client = mqtt.Client(
        client_id=mqtt_config["client_id"],
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2
    )
    
    client.on_connect = on_connect
    client.on_message = on_message
    client.on_disconnect = on_disconnect
    
    if mqtt_config.get("username"):
        client.username_pw_set(mqtt_config["username"], mqtt_config["password"])
    
    logger.info(f"Connecting to MQTT broker at {mqtt_config['host']}:{mqtt_config['port']}...")
    
    try:
        client.connect(mqtt_config["host"], mqtt_config["port"], 60)
    except Exception as e:
        logger.error(f"Failed to connect to MQTT broker: {e}")
        logger.info("Make sure Solar Assistant MQTT is enabled and the IP is correct.")
        influx.close()
        return
    
    # Start MQTT loop in background
    client.loop_start()
    
    logger.info(f"Running... Writing to InfluxDB every {WRITE_INTERVAL} seconds")
    logger.info("Press Ctrl+C to stop")
    
    try:
        while True:
            time.sleep(1)
            
            # Periodically flush data to InfluxDB
            if time.time() - last_write_time >= WRITE_INTERVAL:
                flush_data_to_influx(influx)
                last_write_time = time.time()
                
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        client.loop_stop()
        client.disconnect()
        influx.close()

if __name__ == "__main__":
    main()
