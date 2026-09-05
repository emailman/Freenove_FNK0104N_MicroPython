"""
LVGL "Hello World" on the Freenove FNK0104N's onboard 3.5" display (ST77922, 320x480, QSPI).

Requires the custom lvgl_micropython firmware built for this project (ESP32_GENERIC_S3,
SPIRAM_OCT, with the st77922 display driver -- see st77922.py / _st77922_init.py) instead of
stock MicroPython, since plain MicroPython has no lvgl/lcd_bus modules.

Panel wiring (from Freenove's own ST77922.h -- QSPI on SPI2_HOST @ 80MHz, mode 0):
    CS=10  BL=41 (plain GPIO, active-high)  SCLK=12  D0=11  D1=13  D2=14  D3=9

Uses a partial (320x40-row) frame buffer, matching Freenove's own driver -- a single
full-screen (320x480) buffer overflows the SPI/DMA transaction resources for one flush.
"""

import gc

import lvgl as lv
import lcd_bus
from machine import SPI

import st77922
from task_handler import TaskHandler
# i2c/st77922_touch are imported further down, after the display's QSPI bus is up --
# see the comment there for why.

DISPLAY_WIDTH = 320
DISPLAY_HEIGHT = 480
BUFFER_ROWS = 40  # matches Freenove's own st77922_lcd_init buffer chunk size

qspi_bus = SPI.Bus(host=2, sck=12, quad_pins=(11, 13, 14, 9))
data_bus = lcd_bus.SPIBus(spi_bus=qspi_bus, dc=-1, cs=10, freq=80_000_000, spi_mode=0, quad=True)

gc.collect()
frame_buffer = data_bus.allocate_framebuffer(
    DISPLAY_WIDTH * BUFFER_ROWS * 2, lcd_bus.MEMORY_SPIRAM | lcd_bus.MEMORY_DMA
)

display = st77922.ST77922(
    data_bus=data_bus,
    display_width=DISPLAY_WIDTH,
    display_height=DISPLAY_HEIGHT,
    frame_buffer1=frame_buffer,
    backlight_pin=41,
    backlight_on_state=st77922.STATE_HIGH,
    color_space=lv.COLOR_FORMAT.RGB565,
    rgb565_byte_swap=True,  # matches Freenove's documented LV_COLOR_16_SWAP=1 for this panel
)
display.init()
display.set_power(True)
display.set_backlight(100)

# Importing/compiling st77922_touch.py (a loose, uncompiled file, like st77922.py) here --
# after the display's esp_lcd panel IO is already up -- rather than at the top of the file
# keeps that compilation's transient memory use out of the way of esp_lcd_new_panel_io_spi's
# allocation above, which only has a small internal (non-PSRAM) DMA-capable pool to draw
# from; doing it beforehand was enough to make that allocation fail (ESP_ERR_NO_MEM).
gc.collect()
import i2c
import st77922_touch

# Touch is a separate I2C peripheral from the QSPI display bus (see st77922_touch.py for
# wiring/register details, ported from Freenove's own ST77922_Touch.h/.cpp).
touch_i2c_bus = i2c.I2C.Bus(host=0, scl=39, sda=38, freq=100_000)
touch_device = i2c.I2C.Device(
    bus=touch_i2c_bus, dev_id=st77922_touch.I2C_ADDR, reg_bits=st77922_touch.BITS
)
touch = st77922_touch.ST77922Touch(device=touch_device, reset_pin=48, int_pin=47)

screen = lv.screen_active()

# Top half: red background.
top = lv.obj(screen)
top.set_size(DISPLAY_WIDTH, DISPLAY_HEIGHT // 2)
top.align(lv.ALIGN.TOP_MID, 0, 0)
top.set_style_bg_color(lv.color_hex(0xFF0000), 0)
top.set_style_bg_opa(lv.OPA.COVER, 0)
top.set_style_border_width(0, 0)
top.set_style_radius(0, 0)

# Bottom half: blue background.
bottom = lv.obj(screen)
bottom.set_size(DISPLAY_WIDTH, DISPLAY_HEIGHT // 2)
bottom.align(lv.ALIGN.BOTTOM_MID, 0, 0)
bottom.set_style_bg_color(lv.color_hex(0x0000FF), 0)
bottom.set_style_bg_opa(lv.OPA.COVER, 0)
bottom.set_style_border_width(0, 0)
bottom.set_style_radius(0, 0)

label = lv.label(screen)
label.set_text(
    "Hello ESP32-S3!! LVGL v%d.%d.%d"
    % (lv.version_major(), lv.version_minor(), lv.version_patch())
)
label.set_style_text_color(lv.color_hex(0xFFFFFF), 0)
label.center()

# Touch either half to toggle it between red and blue, independently of the other half.
_RED = lv.color_hex(0xFF0000)
_BLUE = lv.color_hex(0x0000FF)


def _make_toggle_cb(panel, starts_red):
    is_red = [starts_red]  # mutable cell, closed over by _on_tap below

    def _on_tap(_event):
        is_red[0] = not is_red[0]
        panel.set_style_bg_color(_RED if is_red[0] else _BLUE, 0)

    return _on_tap


for panel, starts_red in ((top, True), (bottom, False)):
    panel.add_flag(lv.obj.FLAG.CLICKABLE)
    panel.add_event_cb(_make_toggle_cb(panel, starts_red), lv.EVENT.CLICKED, None)

# Drives lv.tick_inc()/lv.task_handler() on a background hardware timer, so the demo
# keeps rendering after this script finishes running.
task_handler = TaskHandler()

print("Hello World display demo mostly running.")
