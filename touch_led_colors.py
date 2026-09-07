"""
Touch-to-color demo for the Freenove FNK0104N's onboard 3.5" display (ST77922, 320x480,
QSPI) and onboard WS2812 RGB LED (GPIO 40).

Divides the screen into 5 touch areas -- red, green, blue, white, black -- arranged as a 2x2
grid of 160x160 quadrants (red/green on top, blue/white below) with a full-width black bar
along the bottom. Tapping any area sets the onboard RGB LED to that area's color (black turns
the LED off).

Requires the custom lvgl_micropython firmware -- see hello_world_display.py / README.md.
Wiring/init pattern (QSPI display bus, then I2C touch after it) is copied from
hello_world_display.py; see that file's comments for why the ordering matters.
"""

import gc

import lvgl as lv
import lcd_bus
from machine import SPI, Pin
from neopixel import NeoPixel

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

# Onboard WS2812 (NeoPixel) RGB LED -- GPIO 40 (see rgb_led_blink_fnk0104n.py).
_led_pin = Pin(40, Pin.OUT)
led = NeoPixel(_led_pin, 1)


def set_led_color(rgb):
    led[0] = rgb
    led.write()


set_led_color((0, 0, 0))  # start off

screen = lv.screen_active()
screen.set_style_bg_color(lv.color_hex(0x000000), 0)

QUAD_SIZE = DISPLAY_WIDTH // 2  # 160 -- also used as the row height for the top two rows
BAR_HEIGHT = DISPLAY_HEIGHT - 2 * QUAD_SIZE  # 160 -- remaining height for the bottom bar

# (label, bg color, label text color, LED RGB, x, y, width, height)
AREAS = (
    ("RED", 0xFF0000, 0xFFFFFF, (255, 0, 0), 0, 0, QUAD_SIZE, QUAD_SIZE),
    ("GREEN", 0x00FF00, 0x000000, (0, 255, 0), QUAD_SIZE, 0, QUAD_SIZE, QUAD_SIZE),
    ("BLUE", 0x0000FF, 0xFFFFFF, (0, 0, 255), 0, QUAD_SIZE, QUAD_SIZE, QUAD_SIZE),
    ("WHITE", 0xFFFFFF, 0x000000, (255, 255, 255), QUAD_SIZE, QUAD_SIZE, QUAD_SIZE, QUAD_SIZE),
    ("BLACK", 0x000000, 0xFFFFFF, (0, 0, 0), 0, 2 * QUAD_SIZE, DISPLAY_WIDTH, BAR_HEIGHT),
)


def _make_tap_cb(rgb):
    def _on_tap(_event):
        set_led_color(rgb)

    return _on_tap


for name, bg_color, text_color, led_rgb, x, y, w, h in AREAS:
    panel = lv.obj(screen)
    panel.set_size(w, h)
    panel.set_pos(x, y)
    panel.set_style_bg_color(lv.color_hex(bg_color), 0)
    panel.set_style_bg_opa(lv.OPA.COVER, 0)
    panel.set_style_border_width(0, 0)
    panel.set_style_radius(0, 0)
    panel.add_flag(lv.obj.FLAG.CLICKABLE)
    panel.add_event_cb(_make_tap_cb(led_rgb), lv.EVENT.CLICKED, None)

    label = lv.label(panel)
    label.set_text(name)
    label.set_style_text_color(lv.color_hex(text_color), 0)
    # This firmware only has font_montserrat_12/14/16 compiled in (grepped from lv_mp.c) --
    # no size double the default 14px font is available without a firmware rebuild -- so
    # 2x size is done via a render-time scale transform instead of a bigger font. Pivot must
    # be set to the label's own center (50%/50%), not the default top-left (0,0), or the
    # scale-up would grow away from center() below instead of in place.
    label.set_style_transform_pivot_x(lv.pct(50), 0)
    label.set_style_transform_pivot_y(lv.pct(50), 0)
    label.set_style_transform_scale(lv.SCALE_NONE * 2, 0)
    label.center()

# Drives lv.tick_inc()/lv.task_handler() on a background hardware timer, so the demo keeps
# rendering/responding to touch after this script finishes running.
task_handler = TaskHandler()

print("Touch-to-color demo running. Tap an area to set the RGB LED to its color.")
