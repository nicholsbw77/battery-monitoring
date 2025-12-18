import serial
import paho.mqtt.client as mqtt
import time

# Configuration
SERIAL_PORT = "/dev/ttyUSB1"  # Secondary adapter port
BAUDRATE = 9600
MQTT_BROKER = "192.168.50.46"
MQTT_TOPIC_PREFIX = "batteries/eg4/"

client = mqtt.Client()
client.connect(MQTT_BROKER, 1883, 60)

ser = serial.Serial(SERIAL_PORT, BAUDRATE, timeout=1)

def query_eg4(command):
    # EG4 protocol: simple query frames (community-derived)
    ser.write(command)
    time.sleep(0.5)
    response = ser.read(1000)
    return response

while True:
    # Example queries for status and cells (adapt from dbus-serialbattery EG4 code)
    status_resp = query_eg4(b'\x01\x03\x00\x00\x00\x20\xC4\x0B')  # Basic status
    cells_resp = query_eg4(b'\x01\x03\x10\x00\x00\x20\xC5\xF2')   # Cell voltages block

    if len(status_resp) > 20:
        # Parsing example (offsets from community docs)
        soc = status_resp[3]  # SOC %
        total_voltage = int.from_bytes(status_resp[5:7], 'big') / 100
        current = int.from_bytes(status_resp[9:11], 'big', signed=True) / 100

        cell_voltages = [int.from_bytes(cells_resp[i:i+2], 'big') / 1000 for i in range(3, 67, 2)]

        # Publish
        client.publish(MQTT_TOPIC_PREFIX + "soc", soc)
        client.publish(MQTT_TOPIC_PREFIX + "total_voltage", total_voltage)
        client.publish(MQTT_TOPIC_PREFIX + "current", current)
        for i, volt in enumerate(cell_voltages):
            client.publish(f"{MQTT_TOPIC_PREFIX}cell_{i+1}_voltage", volt)

    time.sleep(20)  # Poll interval