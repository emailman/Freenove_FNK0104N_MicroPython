"""
Cycle the onboard RGB LED on the Freenove FNK0104N (ESP32-S3 Display, 3.5" model)
through the primary colors

Onboard WS2812 (NeoPixel) RGB LED -- GPIO 40
"""

from machine import Pin
from neopixel import NeoPixel
from utime import sleep_ms

RED = (255, 0, 0)
GREEN = (0, 255, 0)
BLUE = (0, 0, 255)
OFF = (0, 0, 0)

colors = (RED, GREEN, BLUE)

# The onboard RGB LED is a single WS2812 pixel on GPIO 40
pin = Pin(40, Pin.OUT)
led = NeoPixel(pin, 1)


def set_color(color_):
    led[0] = color_
    led.write()


# Cycle the LED through the primary colors, one per second
try:
    while True:
        for color in colors:
            set_color(color)
            sleep_ms(1000)

except KeyboardInterrupt:
    print("Program Stopped")
    set_color(OFF)
