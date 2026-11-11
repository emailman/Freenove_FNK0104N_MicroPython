"""
Swipe gesture demo for the Freenove FNK0104N's onboard 3.5" display (ST77922, 320x480, QSPI,
portrait) and integrated capacitive touch.

Swipe anywhere on the screen. LVGL's built-in gesture detection (lv.EVENT.GESTURE +
indev.get_gesture_dir()) reports the direction; the screen shows a big arrow, changes
background color per direction, and keeps a running count of each direction. Every swipe is
also printed to the REPL.

The screen's SCROLLABLE flag is removed: otherwise LVGL treats a drag as a scroll and never
fires the gesture event.

Requires the custom lvgl_micropython firmware -- see hello_world_display.py / README.md.
Display/touch init is copied from touch_keyboard_demo.py (touch imports after display init).
"""

import gc

import lvgl as lv
import lcd_bus
from machine import SPI

import st77922
from task_handler import TaskHandler

DISPLAY_WIDTH = 320
DISPLAY_HEIGHT = 480
BUFFER_ROWS = 40

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
    rgb565_byte_swap=True,
)
display.init()
display.set_power(True)
display.set_backlight(100)

# Loose-file touch imports after display init -- see CLAUDE.md.
gc.collect()
import i2c
import st77922_touch

touch_i2c_bus = i2c.I2C.Bus(host=0, scl=39, sda=38, freq=100_000)
touch_device = i2c.I2C.Device(
    bus=touch_i2c_bus, dev_id=st77922_touch.I2C_ADDR, reg_bits=st77922_touch.BITS
)
touch = st77922_touch.ST77922Touch(device=touch_device, reset_pin=48, int_pin=47)

screen = lv.screen_active()
screen.set_style_bg_color(lv.color_hex(0x000000), 0)
screen.remove_flag(lv.obj.FLAG.SCROLLABLE)

# direction -> (name, arrow symbol, background color)
DIRS = {
    lv.DIR.LEFT: ("LEFT", lv.SYMBOL.LEFT, 0x803000),
    lv.DIR.RIGHT: ("RIGHT", lv.SYMBOL.RIGHT, 0x006030),
    lv.DIR.TOP: ("UP", lv.SYMBOL.UP, 0x003080),
    lv.DIR.BOTTOM: ("DOWN", lv.SYMBOL.DOWN, 0x600060),
}
counts = {d: 0 for d in DIRS}


def _scaled_label(text, scale, y):
    label = lv.label(screen)
    label.set_text(text)
    label.set_style_text_color(lv.color_hex(0xFFFFFF), 0)
    label.set_style_transform_pivot_x(lv.pct(50), 0)
    label.set_style_transform_pivot_y(lv.pct(50), 0)
    label.set_style_transform_scale(lv.SCALE_NONE * scale, 0)
    label.align(lv.ALIGN.CENTER, 0, y)
    return label


arrow = _scaled_label("Swipe!", 4, -60)
name_label = _scaled_label("", 2, 40)
count_label = lv.label(screen)
count_label.set_style_text_color(lv.color_hex(0xC0C0C0), 0)
count_label.align(lv.ALIGN.BOTTOM_MID, 0, -30)


def _update_counts():
    count_label.set_text(
        "L %d   R %d   U %d   D %d"
        % (counts[lv.DIR.LEFT], counts[lv.DIR.RIGHT], counts[lv.DIR.TOP], counts[lv.DIR.BOTTOM])
    )


_update_counts()


def _on_gesture(event):
    d = lv.indev_active().get_gesture_dir()
    if d not in DIRS:
        return
    name, symbol, color = DIRS[d]
    counts[d] += 1
    screen.set_style_bg_color(lv.color_hex(color), 0)
    arrow.set_text(symbol)
    name_label.set_text(name)
    arrow.align(lv.ALIGN.CENTER, 0, -60)
    name_label.align(lv.ALIGN.CENTER, 0, 40)
    _update_counts()
    # Scaled labels can leave stale fragments when their text changes (see CLAUDE.md,
    # "Text sizing"); the background changes anyway, so redraw everything.
    screen.invalidate()
    print("Swipe", name, counts[d])


screen.add_event_cb(_on_gesture, lv.EVENT.GESTURE, None)

task_handler = TaskHandler()

print("Swipe demo running. Swipe left/right/up/down anywhere on the screen.")
