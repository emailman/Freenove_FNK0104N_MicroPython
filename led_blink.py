"""
Blink the LED on GPIO 45 on the Freenove ESP32-S3 board
"""

from machine import Pin
from utime import sleep_ms

led = Pin(45, Pin.OUT)

try:
    # Blink the LED once per second
    while True:
        led.toggle()
        sleep_ms(500)
except KeyboardInterrupt:
    led.off()
    print('Program Stopped')
