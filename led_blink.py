"""
Blink an external LED on GPIO 45 on the Freenove ESP32-S3 board.

The FNK0104N has no onboard LED on GPIO 45; the pin only goes to pin 4 of the
"Extended IO" header (P3). Wire it as:

    P3 pin 4 (GPIO 45) --[ 220-330 ohm ]-->|-- P3 pin 2 (GND)

Wire the LED to GND (active-high), not 3V3: GPIO 45 is a strapping pin that
must be low at reset, or the board may not boot. See README.md, "External LED
on GPIO 45".
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
