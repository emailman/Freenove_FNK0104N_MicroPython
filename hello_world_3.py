"""
"Hello World" in large text, in landscape (480x320), on the Freenove FNK0104N's onboard 3.5"
display (ST77922, 320x480 native portrait, QSPI). hello_world_2.py with bigger text.

This firmware only has Montserrat 12/14/16 compiled in, so the text is made bigger by scaling
the label with transform_scale instead of switching fonts (see CLAUDE.md, "Text sizing"). The
glyphs are upscaled, so they look a little softer than a real large font would.

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

TEXT_SCALE = 3        # 3x the 14px default font

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
label.set_text("Hello World")
# Scale around the label's own center; the default pivot is its top-left corner, which
# would push the enlarged text toward the bottom-right.
label.set_style_transform_pivot_x(lv.pct(50), 0)
label.set_style_transform_pivot_y(lv.pct(50), 0)
label.set_style_transform_scale(lv.SCALE_NONE * TEXT_SCALE, 0)
label.center()

# Drives lv.tick_inc()/lv.task_handler() on a background hardware timer, so the display
# keeps rendering after this script finishes running.
task_handler = TaskHandler()

print("Hello World (landscape, large) running.")
