#!/bin/bash
#
# JK BMS MQTT Cleanup Script
# Run this on your XU4 to clear all old retained messages
# Then restart jk_bms_mqtt_publisher_v8.py
#

MQTT_HOST="localhost"
MQTT_USER="jkbms"
MQTT_PASS="admin"

echo "========================================"
echo "JK BMS MQTT Cleanup Script"
echo "========================================"
echo ""
echo "Clearing all retained JK BMS discovery messages..."

# List of all sensor suffixes to clear
SENSORS=(
    "soc"
    "soh"
    "voltage"
    "current"
    "power"
    "temp_mos"
    "temp_battery"
    "capacity_remaining"
    "capacity_full"
    "cycle_count"
    "cycle_capacity"
    "cell_max"
    "cell_min"
    "cell_diff"
    "balance_current"
    "cell_1_voltage"
    "cell_2_voltage"
    "cell_3_voltage"
    "cell_4_voltage"
    "cell_5_voltage"
    "cell_6_voltage"
    "cell_7_voltage"
    "cell_8_voltage"
    "cell_9_voltage"
    "cell_10_voltage"
    "cell_11_voltage"
    "cell_12_voltage"
    "cell_13_voltage"
    "cell_14_voltage"
    "cell_15_voltage"
    "cell_16_voltage"
    "cell_count"
    "cell_avg"
    "charge_mos"
    "discharge_mos"
    "balancing"
    "balance_current"
)

# Clear discovery configs
for sensor in "${SENSORS[@]}"; do
    echo "  Clearing: homeassistant/sensor/jk_bms_1_${sensor}/config"
    mosquitto_pub -h "$MQTT_HOST" -u "$MQTT_USER" -P "$MQTT_PASS" -t "homeassistant/sensor/jk_bms_1_${sensor}/config" -n -r 2>/dev/null
done

echo ""
echo "Clearing retained data topics..."

# Data topics to clear
DATA_TOPICS=(
    "jk_bms/battery_1/soc"
    "jk_bms/battery_1/soh"
    "jk_bms/battery_1/voltage"
    "jk_bms/battery_1/current"
    "jk_bms/battery_1/power"
    "jk_bms/battery_1/temp_mos"
    "jk_bms/battery_1/temp_battery"
    "jk_bms/battery_1/capacity_remaining"
    "jk_bms/battery_1/capacity_full"
    "jk_bms/battery_1/cycle_count"
    "jk_bms/battery_1/cycle_capacity"
    "jk_bms/battery_1/cell_max"
    "jk_bms/battery_1/cell_min"
    "jk_bms/battery_1/cell_diff"
    "jk_bms/battery_1/balance_current"
    "jk_bms/battery_1/cell_1_voltage"
    "jk_bms/battery_1/cell_2_voltage"
    "jk_bms/battery_1/cell_3_voltage"
    "jk_bms/battery_1/cell_4_voltage"
    "jk_bms/battery_1/cell_5_voltage"
    "jk_bms/battery_1/cell_6_voltage"
    "jk_bms/battery_1/cell_7_voltage"
    "jk_bms/battery_1/cell_8_voltage"
    "jk_bms/battery_1/cell_9_voltage"
    "jk_bms/battery_1/cell_10_voltage"
    "jk_bms/battery_1/cell_11_voltage"
    "jk_bms/battery_1/cell_12_voltage"
    "jk_bms/battery_1/cell_13_voltage"
    "jk_bms/battery_1/cell_14_voltage"
    "jk_bms/battery_1/cell_15_voltage"
    "jk_bms/battery_1/cell_16_voltage"
    "jk_bms/battery_1/json"
)

for topic in "${DATA_TOPICS[@]}"; do
    echo "  Clearing: ${topic}"
    mosquitto_pub -h "$MQTT_HOST" -u "$MQTT_USER" -P "$MQTT_PASS" -t "$topic" -n -r 2>/dev/null
done

echo ""
echo "========================================"
echo "Done! Now:"
echo "  1. Go to Home Assistant"
echo "  2. Settings -> Devices & Services -> MQTT"
echo "  3. Delete any JK BMS devices you see"
echo "  4. Restart the V8 script:"
echo "     python3 jk_bms_mqtt_publisher_v8.py"
echo "  5. Reload MQTT integration in HA"
echo "========================================"
