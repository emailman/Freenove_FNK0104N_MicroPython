"""
On-screen touch keyboard demo for the Freenove FNK0104N's onboard 3.5" display (ST77922,
320x480, QSPI, portrait) and integrated capacitive touch.

Confirmed on hardware (2026-09-14): `lv.keyboard` and `lv.textarea` are compiled into this
firmware build (`hasattr(lv, "keyboard")`/`hasattr(lv, "textarea")` both True), so this uses
LVGL's built-in keyboard widget rather than a hand-built one -- no fallback needed. The mode
enum lives at `lv.keyboard.MODE` (not a top-level `lv.KEYBOARD_MODE`), consistent with the
"object flags/enums nested under the widget class" pattern CLAUDE.md documents for
`lv.obj.FLAG`.

A textarea near the top of the screen shows what's typed; the keyboard fills the rest. Tapping
keys inserts into the textarea directly -- LVGL's pointer (touch) indev clicks the keyboard's
buttonmatrix without needing any `lv.group`/encoder-focus setup (that machinery is for
encoder/keypad indevs, not touchscreens). The keyboard's own Enter/OK key fires lv.EVENT.READY;
its Hide/Cancel key (only present in some modes) fires lv.EVENT.CANCEL -- both just print here.

Requires the custom lvgl_micropython firmware -- see hello_world_display.py / README.md.
Wiring/init pattern (QSPI display bus, then I2C touch after it) is copied from
hello_world_display.py; see that file's comments for why the ordering matters.

Portrait only -- see touch_keyboard_demo_landscape.py for the landscape version, which pairs
st77922.ST77922Landscape with st77922_touch.ST77922TouchLandscape (a touch coordinate
remapping this driver's raw output otherwise doesn't have).
"""

import gc

import lvgl as lv
import lcd_bus
from machine import SPI

import st77922
from task_handler import TaskHandler

# i2c/st77922_touch are imported further down, after the display's QSPI bus is up --
# see hello_world_display.py's comment for why.

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

# Importing/compiling st77922_touch.py (a loose, uncompiled file) here -- after the display's
# esp_lcd panel IO is already up -- rather than at the top of the file keeps that
# compilation's transient memory use out of the way of esp_lcd_new_panel_io_spi's allocation
# above; doing it beforehand was enough to make that allocation fail (ESP_ERR_NO_MEM).
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
screen.set_style_bg_color(lv.color_hex(0x000000), 0)

TA_HEIGHT = 80

textarea = lv.textarea(screen)
textarea.set_size(DISPLAY_WIDTH, TA_HEIGHT)
textarea.set_pos(0, 0)
textarea.set_placeholder_text("Tap keys below")
textarea.set_one_line(False)

keyboard = lv.keyboard(screen)
keyboard.set_size(DISPLAY_WIDTH, DISPLAY_HEIGHT - TA_HEIGHT)
keyboard.set_pos(0, TA_HEIGHT)
keyboard.set_textarea(textarea)


def _on_kb_event(event):
    code = event.get_code()
    if code == lv.EVENT.READY:
        print("Keyboard READY (Enter/OK) -- text so far:", textarea.get_text())
    elif code == lv.EVENT.CANCEL:
        print("Keyboard CANCEL (Hide) -- text so far:", textarea.get_text())


keyboard.add_event_cb(_on_kb_event, lv.EVENT.READY, None)
keyboard.add_event_cb(_on_kb_event, lv.EVENT.CANCEL, None)

# Drives lv.tick_inc()/lv.task_handler() on a background hardware timer, so the demo keeps
# rendering/responding to touch after this script finishes running.
task_handler = TaskHandler()

print("Touch keyboard demo running. Tap keys to type into the textarea above.")
