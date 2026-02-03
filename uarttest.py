# Quick test - just read raw data from both ports
#python3 -c "
import serial
import time

ports = ['/dev/ttyUSB0', '/dev/ttyUSB1']
baudrates = [9600, 115200]

for port in ports:
    for baud in baudrates:
        try:
            print(f'\n--- Testing {port} at {baud} baud ---')
            ser = serial.Serial(port, baud, timeout=2)
            time.sleep(1)
            # Just read whatever is coming
            data = ser.read(500)
            if data:
                print(f'Got {len(data)} bytes: {data[:100].hex()}')
            else:
                print('No data received')
            ser.close()
        except Exception as e:
            print(f'Error: {e}')
        time.sleep(1)
#"