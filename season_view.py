"""
Shows one landscape picture on the Freenove FNK0104N's onboard 3.5" display (ST77922,
320x480, QSPI, shown as 480x320 landscape). A simplified seasons_fade.py: one picture, no
fade, no touch.

Set PICTURE to the season to show. The picture must already be converted with
jpg_to_rgb565.py and copied to /seasons on the board -- see seasons_fade.py's docstring.

The picture is stored pre-rotated in the panel's native portrait orientation, so it's sent
straight to the panel in full-width (0..319) 40-row bands with the same synchronous
tx_color() sequence ST77922Landscape._flush_cb uses. LVGL never draws anything: there's no
task handler, so the picture stays on screen after the script ends.

Each band is copied into the band buffer before it's sent, because the bus byte-swaps
tx_color() data in place (see CLAUDE.md) and that would corrupt the stored picture.

Requires the custom lvgl_micropython firmware -- see hello_world_display.py / README.md.
"""

import gc

import lvgl as lv
import lcd_bus
from machine import SPI

import st77922

PICTURE = "spring"    # spring, summer, fall or winter
PICTURE_DIR = "/seasons"

DISPLAY_WIDTH = 480   # logical/landscape size -- see ST77922Landscape
DISPLAY_HEIGHT = 320
BUFFER_ROWS = 26      # same proven-safe chunk size as ntp_clock.py

PHYS_W = 320          # the panel's native portrait size, which the picture is stored in
PHYS_H = 480

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

# Load the picture (307,200 bytes, into PSRAM).
gc.collect()
picture = bytearray(PHYS_W * PHYS_H * 2)
with open(PICTURE_DIR + "/" + PICTURE + ".rgb565", "rb") as f:
    f.readinto(picture)

# Send it in full-width bands through LVGL's draw buffer (see the module docstring).
src = memoryview(picture)
band = memoryview(display._send_buf)  # NOQA
band_rows = display._send_rows  # NOQA
row_bytes = PHYS_W * 2

data_bus.register_callback(None)  # synchronous tx_color, no LVGL flush_ready callback
try:
    y = 0
    while y < PHYS_H:
        y2 = min(y + band_rows, PHYS_H) - 1
        n = (y2 - y + 1) * row_bytes
        band[:n] = src[y * row_bytes:y * row_bytes + n]
        cmd = display._set_memory_location(0, y, PHYS_W - 1, y2)  # NOQA
        data_bus.tx_color(cmd, band[:n], 0, y, PHYS_W - 1, y2, display._rotation,  # NOQA
                          y2 == PHYS_H - 1)
        y = y2 + 1
finally:
    data_bus.register_callback(display._flush_ready_cb)  # NOQA

print("Showing", PICTURE)
