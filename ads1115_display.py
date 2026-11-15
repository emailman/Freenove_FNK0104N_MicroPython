"""
ADS1115 test: shows the analog input on A0 (single-ended, vs GND) on the Freenove FNK0104N's
3.5" display, in portrait, refreshed every UPDATE_MS.

Shows the voltage in large text, the raw 16-bit count, a bar across 0..VREF_MAX volts, and the
session's min/max. Module wiring: see ads1115.py (the "I2C" header, P4; address 0x48).

Touch isn't used, so the ADC gets a plain machine.I2C on host 0, the way bme280_display.py
does it. A script that also uses touch must pass the touch driver's i2c.I2C.Bus instead,
since host 0 can't be opened twice.

The big number is a 2x transform_scale label (no bigger font is compiled into this firmware;
see CLAUDE.md, "Text sizing"), so each update invalidates its whole card to avoid stale
fragments.

Requires the custom lvgl_micropython firmware -- see hello_world_display.py / README.md.
"""

import gc

import lvgl as lv
import lcd_bus
from machine import SPI

import st77922
from task_handler import TaskHandler

# ads1115 is imported further down, after the display's QSPI bus is up -- see
# hello_world_display.py's comment for why.

DISPLAY_WIDTH = 320
DISPLAY_HEIGHT = 480
BUFFER_ROWS = 40  # matches Freenove's own st77922_lcd_init buffer chunk size

UPDATE_MS = 250
CHANNEL = 0
ADC_ADDRESS = 0x48
FSR = 4.096  # PGA range; inputs must still stay within 0..VDD (3.3V)
VREF_MAX = 3.3  # bar full scale, in volts
PCT_ZERO_V = 0.005  # voltage shown as 0%
PCT_FULL_V = 3.28  # voltage shown as 100%
# Light low-pass filter on the raw count (exponential moving average): each update moves
# FILTER_ALPHA of the way toward the new reading. 1.0 = no filtering; 0.3 at 250ms settles
# a step change to within ~1% in about 3s.
FILTER_ALPHA = 0.3

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
import ads1115

adc = ads1115.ADS1115(I2C(0, scl=Pin(39), sda=Pin(38), freq=400_000),
                      address=ADC_ADDRESS, fsr=FSR, rate=128)

# --- UI ------------------------------------------------------------------------------------

BG = lv.color_hex(0x101418)
CARD_BG = lv.color_hex(0x1E252C)
DIM = lv.color_hex(0x8A96A3)
WHITE = lv.color_hex(0xFFFFFF)
ACCENT = lv.color_hex(0x3DDC84)
ERR = lv.color_hex(0xFF5A5A)

screen = lv.screen_active()
screen.set_style_bg_color(BG, 0)
screen.set_style_bg_opa(lv.OPA.COVER, 0)
screen.remove_flag(lv.obj.FLAG.SCROLLABLE)

title = lv.label(screen)
title.set_text("ADS1115  A%d" % CHANNEL)
title.set_style_text_font(lv.font_montserrat_16, 0)
title.set_style_text_color(DIM, 0)
title.align(lv.ALIGN.TOP_MID, 0, 10)


def _card(y, h):
    c = lv.obj(screen)
    c.set_size(296, h)
    c.align(lv.ALIGN.TOP_MID, 0, y)
    c.set_style_bg_color(CARD_BG, 0)
    c.set_style_bg_opa(lv.OPA.COVER, 0)
    c.set_style_border_width(0, 0)
    c.set_style_radius(12, 0)
    c.set_style_pad_all(0, 0)
    c.remove_flag(lv.obj.FLAG.SCROLLABLE)
    return c


def _label(parent, text, color, align, dx, dy, font=None):
    lbl = lv.label(parent)
    lbl.set_text(text)
    lbl.set_style_text_color(color, 0)
    if font:
        lbl.set_style_text_font(font, 0)
    lbl.align(align, dx, dy)
    return lbl


# Voltage card: big value + raw count + bar
volt_card = _card(40, 220)
_label(volt_card, "Voltage", ACCENT, lv.ALIGN.TOP_LEFT, 16, 12, lv.font_montserrat_16)


def _big_label(text, color, dy):
    """A 2x transform_scale label centered at (0, dy) in the voltage card."""
    lbl = _label(volt_card, text, color, lv.ALIGN.CENTER, 0, dy, lv.font_montserrat_16)
    lbl.set_style_transform_pivot_x(lv.pct(50), 0)
    lbl.set_style_transform_pivot_y(lv.pct(50), 0)
    lbl.set_style_transform_scale(lv.SCALE_NONE * 2, 0)
    return lbl


pct_label = _big_label("--", ACCENT, -40)
volt_value = _big_label("--", WHITE, 0)
raw_label = _label(volt_card, "", DIM, lv.ALIGN.CENTER, 0, 36)

bar = lv.bar(volt_card)
bar.set_size(256, 16)
bar.align(lv.ALIGN.BOTTOM_MID, 0, -30)
bar.set_range(0, int(VREF_MAX * 1000))
bar.set_style_bg_color(lv.color_hex(0x2C353E), lv.PART.MAIN)
bar.set_style_bg_color(ACCENT, lv.PART.INDICATOR)

_label(volt_card, "0 V", DIM, lv.ALIGN.BOTTOM_LEFT, 20, -8)
_label(volt_card, "%.1f V" % VREF_MAX, DIM, lv.ALIGN.BOTTOM_RIGHT, -20, -8)

# Stats card: min / max / samples
stats_card = _card(272, 110)
_label(stats_card, "Session", ACCENT, lv.ALIGN.TOP_LEFT, 16, 12, lv.font_montserrat_16)
min_label = _label(stats_card, "min  --", WHITE, lv.ALIGN.TOP_LEFT, 16, 44)
max_label = _label(stats_card, "max  --", WHITE, lv.ALIGN.TOP_LEFT, 16, 70)
count_label = _label(stats_card, "", DIM, lv.ALIGN.TOP_RIGHT, -16, 44)

status = _label(screen, "PGA +/-%.3f V, 128 SPS, addr 0x%02X" % (FSR, ADC_ADDRESS),
                DIM, lv.ALIGN.BOTTOM_MID, 0, -12)

_state = {"min": None, "max": None, "n": 0, "err": False, "filt": None}


def _update(_timer=None):
    try:
        raw = adc.read_raw(CHANNEL)
    except OSError as e:
        if not _state["err"]:
            print("ADS1115 read failed:", e)
        _state["err"] = True
        status.set_text("read failed: %s" % e)
        status.set_style_text_color(ERR, 0)
        return
    if _state["err"]:
        _state["err"] = False
        status.set_text("PGA +/-%.3f V, 128 SPS, addr 0x%02X" % (FSR, ADC_ADDRESS))
        status.set_style_text_color(DIM, 0)

    if _state["filt"] is None:
        _state["filt"] = raw  # start from the first reading instead of ramping up from 0
    else:
        _state["filt"] += FILTER_ALPHA * (raw - _state["filt"])
    raw = _state["filt"]

    v = adc.to_volts(raw)
    if _state["min"] is None or v < _state["min"]:
        _state["min"] = v
    if _state["max"] is None or v > _state["max"]:
        _state["max"] = v
    _state["n"] += 1

    pct = (v - PCT_ZERO_V) * 100 / (PCT_FULL_V - PCT_ZERO_V)
    pct = min(100.0, max(0.0, pct))

    volt_value.set_text("%.3f V" % v)
    pct_label.set_text("%.1f %%" % pct)
    raw_label.set_text("raw %d" % round(raw))
    bar.set_value(max(0, int(v * 1000)), False)  # no animation (no lv.ANIM enum here)
    min_label.set_text("min  %.3f V" % _state["min"])
    max_label.set_text("max  %.3f V" % _state["max"])
    count_label.set_text("%d samples" % _state["n"])

    # Scaled labels can leave stale fragments behind -- repaint the card in full.
    volt_card.invalidate()


_update()
_timer = lv.timer_create(_update, UPDATE_MS, None)

# Drives lv.tick_inc()/lv.task_handler() on a background hardware timer, so the display
# keeps updating after this script finishes running.
task_handler = TaskHandler()

print("ADS1115 display running.")
