"""
Minimal LVGL "Hello World" in landscape (480x320) on the Freenove FNK0104N's onboard 3.5"
display (ST77922, 320x480 native portrait, QSPI). The landscape version of hello_world_1.py:
just the hello message on LVGL's default background, with no colors and no touch.

Landscape uses st77922.ST77922Landscape, which gives LVGL a 480x320 canvas and rotates every
flush onto the portrait panel in software (hardware rotation doesn't work on this panel --
see CLAUDE.md).

Requires the custom lvgl_micropython firmware -- see hello_world_display.py / README.md.
"""

import gc

import lvgl as lv
import lcd_bus
from machine import SPI

import st77922
from task_handler import TaskHandler

DISPLAY_WIDTH = 480   # logical/landscape size LVGL renders into -- see ST77922Landscape
DISPLAY_HEIGHT = 320
BUFFER_ROWS = 26      # same proven-safe chunk size as ntp_clock.py

qspi_bus = SPI.Bus(host=2, sck=12, quad_pins=(11, 13, 14, 9))
data_bus = lcd_bus.SPIBus(spi_bus=qspi_bus, dc=-1, cs=10, freq=80_000_000, spi_mode=0, quad=True)

gc.collect()
frame_buffer = data_bus.allocate_framebuffer(
    DISPLAY_WIDTH * BUFFER_ROWS * 2, lcd_bus.MEMORY_SPIRAM | lcd_bus.MEMORY_DMA
)

display = st77922.ST77922Landscape(
    data_bus=data_bus,
    display_width=DISPLAY_WIDTH,
    display_height=DISPLAY_HEIGHT,
    frame_buffer1=frame_buffer,
    backlight_pin=41,
    backlight_on_state=st77922.STATE_HIGH,
    color_space=lv.COLOR_FORMAT.RGB565,
    rgb565_byte_swap=True,
)
display.init()
display.set_power(True)
display.set_backlight(100)

label = lv.label(lv.screen_active())
label.set_text(
    "Hello ESP32-S3!! LVGL v%d.%d.%d"
    % (lv.version_major(), lv.version_minor(), lv.version_patch())
)
label.center()

# Drives lv.tick_inc()/lv.task_handler() on a background hardware timer, so the display
# keeps rendering after this script finishes running.
task_handler = TaskHandler()

print("Hello World (landscape) running.")
