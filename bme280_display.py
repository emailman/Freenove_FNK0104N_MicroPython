"""
BME280 readings (temperature, humidity, pressure) on the Freenove FNK0104N's 3.5" display,
in portrait, refreshed every UPDATE_MS.

Sensor wiring: see bme280.py (the "I2C" header, P4; address 0x76). Touch isn't used, so the
sensor gets a plain machine.I2C on host 0, the way sound_meter.py drives the codec. A script
that also uses touch must pass the touch driver's i2c.I2C.Bus to BME280 instead, since host 0
can't be opened twice.

Pressure is the sensor's raw station pressure. Set ALTITUDE_M to your elevation to show
sea-level pressure instead, which is what weather reports quote.

The large numbers are 2x transform_scale labels (no bigger font is compiled into this
firmware; see CLAUDE.md, "Text sizing"). set_text() on a scaled label doesn't always invalidate
its old box, which can leave stale fragments on screen (see ntp_clock.py), so each update also
invalidates the whole card the label sits in.

Requires the custom lvgl_micropython firmware -- see hello_world_display.py / README.md.
"""

import gc

import lvgl as lv
import lcd_bus
from machine import SPI

import st77922
from task_handler import TaskHandler

# bme280 is imported further down, after the display's QSPI bus is up -- see
# hello_world_display.py's comment for why.

DISPLAY_WIDTH = 320
DISPLAY_HEIGHT = 480
BUFFER_ROWS = 40  # matches Freenove's own st77922_lcd_init buffer chunk size

UPDATE_MS = 2000
ALTITUDE_M = 1006  # 3300 ft; shows sea-level pressure. None shows station pressure

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

# Loose-file import after the display's esp_lcd panel IO is up -- see hello_world_display.py.
gc.collect()
from machine import I2C, Pin
import bme280

sensor = bme280.BME280(I2C(0, scl=Pin(39), sda=Pin(38), freq=400_000))

# --- UI ------------------------------------------------------------------------------------

BG = lv.color_hex(0x101418)
CARD_BG = lv.color_hex(0x1E252C)
DIM = lv.color_hex(0x8A96A3)
WHITE = lv.color_hex(0xFFFFFF)
TEMP_COLOR = lv.color_hex(0xFF8A3D)
HUM_COLOR = lv.color_hex(0x3DB5FF)
PRES_COLOR = lv.color_hex(0x9B7BFF)

screen = lv.screen_active()
screen.set_style_bg_color(BG, 0)
screen.set_style_bg_opa(lv.OPA.COVER, 0)
screen.remove_flag(lv.obj.FLAG.SCROLLABLE)

title = lv.label(screen)
title.set_text("BME280")
title.set_style_text_font(lv.font_montserrat_16, 0)
title.set_style_text_color(DIM, 0)
title.align(lv.ALIGN.TOP_MID, 0, 10)

CARD_W = 296
CARD_H = 134
CARD_GAP = 12
CARD_TOP = 40


def _make_card(index, name, accent):
    """One reading's card: name at top left, big value, small secondary line. Returns
    (card, value_label, sub_label)."""
    card = lv.obj(screen)
    card.set_size(CARD_W, CARD_H)
    card.align(lv.ALIGN.TOP_MID, 0, CARD_TOP + index * (CARD_H + CARD_GAP))
    card.set_style_bg_color(CARD_BG, 0)
    card.set_style_bg_opa(lv.OPA.COVER, 0)
    card.set_style_border_width(0, 0)
    card.set_style_radius(12, 0)
    card.set_style_pad_all(0, 0)
    card.remove_flag(lv.obj.FLAG.SCROLLABLE)

    bar = lv.obj(card)  # accent stripe down the left edge
    bar.set_size(6, CARD_H - 24)
    bar.align(lv.ALIGN.LEFT_MID, 10, 0)
    bar.set_style_bg_color(accent, 0)
    bar.set_style_bg_opa(lv.OPA.COVER, 0)
    bar.set_style_border_width(0, 0)
    bar.set_style_radius(3, 0)

    name_label = lv.label(card)
    name_label.set_text(name)
    name_label.set_style_text_font(lv.font_montserrat_16, 0)
    name_label.set_style_text_color(accent, 0)
    name_label.align(lv.ALIGN.TOP_LEFT, 28, 12)

    value = lv.label(card)
    value.set_text("--")
    value.set_style_text_font(lv.font_montserrat_16, 0)
    value.set_style_text_color(WHITE, 0)
    value.set_style_transform_pivot_x(lv.pct(50), 0)
    value.set_style_transform_pivot_y(lv.pct(50), 0)
    value.set_style_transform_scale(lv.SCALE_NONE * 2, 0)
    value.align(lv.ALIGN.CENTER, 10, 4)

    sub = lv.label(card)
    sub.set_text("")
    sub.set_style_text_color(DIM, 0)
    sub.align(lv.ALIGN.BOTTOM_RIGHT, -14, -10)
    return card, value, sub


temp_card, temp_value, temp_sub = _make_card(0, "Temperature", TEMP_COLOR)
hum_card, hum_value, hum_sub = _make_card(1, "Humidity", HUM_COLOR)
pres_card, pres_value, pres_sub = _make_card(2, "Pressure", PRES_COLOR)

_cards = (temp_card, hum_card, pres_card)
_min_f = [None]
_max_f = [None]


def _update(_timer=None):
    try:
        t_c, p_hpa, rh = sensor.read()
    except OSError as e:
        print("BME280 read failed:", e)
        temp_sub.set_text("sensor read failed")
        return

    t_f = t_c * 9 / 5 + 32
    if _min_f[0] is None or t_f < _min_f[0]:
        _min_f[0] = t_f
    if _max_f[0] is None or t_f > _max_f[0]:
        _max_f[0] = t_f

    if ALTITUDE_M is not None:
        p_hpa = bme280.BME280.sea_level_hpa(p_hpa, ALTITUDE_M)
        p_kind = "sea level"
    else:
        p_kind = "station"

    temp_value.set_text("%.1f°F" % t_f)
    temp_sub.set_text("%.1f°C   low %.1f  high %.1f" % (t_c, _min_f[0], _max_f[0]))
    hum_value.set_text("%.0f%%" % rh)
    hum_sub.set_text("relative humidity")
    pres_value.set_text("%.2f inHg" % (p_hpa * 0.0295300))
    pres_sub.set_text("%.1f hPa  (%s)" % (p_hpa, p_kind))

    # Scaled labels can leave stale fragments behind -- repaint each card in full.
    for card in _cards:
        card.invalidate()


_update()
_timer = lv.timer_create(_update, UPDATE_MS, None)

# Drives lv.tick_inc()/lv.task_handler() on a background hardware timer, so the display
# keeps updating after this script finishes running.
task_handler = TaskHandler()

print("BME280 display running.")
