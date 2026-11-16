"""
GY-521 (MPU-6050) test: shows the accelerometer, gyroscope, tilt and chip temperature on the
Freenove FNK0104N's 3.5" display, in portrait, refreshed every UPDATE_MS.

- A tilt indicator: the dot moves toward the lowered side, like a ball rolling downhill,
  reaching the ring's edge at LEVEL_FULL_G (0.5 g, about 30 degrees of tilt). It's right when the module lies flat with
  its X arrow pointing to the screen's right and its Y arrow pointing to the screen's top.
- Pitch and roll in degrees, from the accelerometer.
- Acceleration in g and rotation rate in deg/s for each axis.
- Chip temperature (it reads warmer than the room, since it's the die temperature).

At startup the gyro's zero offset is measured and, with ZERO_LEVEL, the level is zeroed:
whatever position the module is in then reads as flat (0 deg pitch/roll, 1.00 g). So keep it
flat and still for the first second ("Calibrating..." on the status line). Readings are lightly filtered (moving average,
FILTER_ALPHA) like ads1115_display.py.

Module wiring: see mpu6050.py (the "I2C" header, P4; address 0x68). Touch isn't used, so the
module gets a plain machine.I2C on host 0, the way bme280_display.py does it. A script that also
uses touch must pass the touch driver's i2c.I2C.Bus instead, since host 0 can't be opened twice.

Requires the custom lvgl_micropython firmware -- see hello_world_display.py / README.md.
"""

import gc

import lvgl as lv
import lcd_bus
from machine import SPI

import st77922
from task_handler import TaskHandler

# mpu6050 is imported further down, after the display's QSPI bus is up -- see
# hello_world_display.py's comment for why.

DISPLAY_WIDTH = 320
DISPLAY_HEIGHT = 480
BUFFER_ROWS = 40  # matches Freenove's own st77922_lcd_init buffer chunk size

UPDATE_MS = 100
MPU_ADDRESS = 0x68
FILTER_ALPHA = 0.3  # 1.0 = no filtering
LEVEL_FULL_G = 0.5  # tilt (as g along X/Y) that puts the bubble at the ring's edge
ZERO_LEVEL = True  # zero the level at startup (False: gyro offset only)

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
import mpu6050

mpu = mpu6050.MPU6050(I2C(0, scl=Pin(39), sda=Pin(38), freq=400_000), address=MPU_ADDRESS)

# --- UI ------------------------------------------------------------------------------------

BG = lv.color_hex(0x101418)
CARD_BG = lv.color_hex(0x1E252C)
TRACK = lv.color_hex(0x2C353E)
DIM = lv.color_hex(0x8A96A3)
WHITE = lv.color_hex(0xFFFFFF)
ACCEL_COLOR = lv.color_hex(0x3DB5FF)
GYRO_COLOR = lv.color_hex(0xFF8A3D)
LEVEL_COLOR = lv.color_hex(0x3DDC84)
TEMP_COLOR = lv.color_hex(0x9B7BFF)
ERR = lv.color_hex(0xFF5A5A)

screen = lv.screen_active()
screen.set_style_bg_color(BG, 0)
screen.set_style_bg_opa(lv.OPA.COVER, 0)
screen.remove_flag(lv.obj.FLAG.SCROLLABLE)

title = lv.label(screen)
title.set_text("GY-521  (MPU-6050)")
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


def _label(parent, text, color, x, y, font=None):
    lbl = lv.label(parent)
    lbl.set_text(text)
    lbl.set_style_text_color(color, 0)
    if font:
        lbl.set_style_text_font(font, 0)
    lbl.set_pos(x, y)
    return lbl


def _box(parent, x, y, w, h, color, radius=0):
    o = lv.obj(parent)
    o.set_size(w, h)
    o.set_pos(x, y)
    o.set_style_bg_color(color, 0)
    o.set_style_bg_opa(lv.OPA.COVER, 0)
    o.set_style_border_width(0, 0)
    o.set_style_radius(radius, 0)
    o.set_style_pad_all(0, 0)
    o.remove_flag(lv.obj.FLAG.SCROLLABLE)
    o.remove_flag(lv.obj.FLAG.CLICKABLE)
    return o


# Level card: bubble on the left, pitch/roll on the right. (lv.line crashed this firmware
# once -- see flight_tracker.py -- so the crosshair is thin boxes.)
LEVEL_D = 150  # ring diameter
DOT_D = 22
level_card = _card(38, 176)
_label(level_card, "Tilt", LEVEL_COLOR, 14, 10, lv.font_montserrat_16)

RING_X, RING_Y = 14, 18
ring = _box(level_card, RING_X, RING_Y, LEVEL_D, LEVEL_D, TRACK, lv.RADIUS_CIRCLE)
ring.set_style_bg_opa(lv.OPA.TRANSP, 0)
ring.set_style_border_width(2, 0)
ring.set_style_border_color(DIM, 0)
_c = LEVEL_D // 2
_box(level_card, RING_X + _c, RING_Y + 8, 1, LEVEL_D - 16, TRACK)
_box(level_card, RING_X + 8, RING_Y + _c, LEVEL_D - 16, 1, TRACK)
centre = _box(level_card, RING_X + _c - DOT_D // 2 - 2, RING_Y + _c - DOT_D // 2 - 2,
              DOT_D + 4, DOT_D + 4, TRACK, lv.RADIUS_CIRCLE)
centre.set_style_bg_opa(lv.OPA.TRANSP, 0)
centre.set_style_border_width(1, 0)
centre.set_style_border_color(DIM, 0)
DOT_HOME_X = RING_X + _c - DOT_D // 2
DOT_HOME_Y = RING_Y + _c - DOT_D // 2
DOT_TRAVEL = _c - DOT_D // 2 - 3  # px from centre to the ring's inner edge
dot = _box(level_card, DOT_HOME_X, DOT_HOME_Y, DOT_D, DOT_D, LEVEL_COLOR, lv.RADIUS_CIRCLE)

_label(level_card, "pitch", DIM, 184, 40)
pitch_label = _label(level_card, "--", WHITE, 184, 60, lv.font_montserrat_16)
_label(level_card, "roll", DIM, 184, 100)
roll_label = _label(level_card, "--", WHITE, 184, 120, lv.font_montserrat_16)


def _axes_card(y, name, color, unit):
    """A card with a name and X/Y/Z value columns. Returns the three value labels."""
    c = _card(y, 80)
    _label(c, name, color, 14, 10, lv.font_montserrat_16)
    _label(c, unit, DIM, 0, 12).align(lv.ALIGN.TOP_RIGHT, -14, 12)
    vals = []
    for i, axis in enumerate("XYZ"):
        x = 14 + i * 94
        _label(c, axis, color, x, 46)
        vals.append(_label(c, "--", WHITE, x + 16, 44, lv.font_montserrat_16))
    return vals


accel_vals = _axes_card(226, "Acceleration", ACCEL_COLOR, "g")
gyro_vals = _axes_card(318, "Rotation", GYRO_COLOR, "deg/s")

temp_card = _card(410, 40)
_label(temp_card, "Chip temp", TEMP_COLOR, 14, 11)
temp_label = _label(temp_card, "--", WHITE, 0, 0)
temp_label.align(lv.ALIGN.RIGHT_MID, -14, 0)

STATUS_OK = "addr 0x%02X  WHO_AM_I 0x%02X" % (MPU_ADDRESS, mpu.who_am_i)
status = lv.label(screen)
status.set_style_text_color(DIM, 0)
status.set_text("Calibrating -- keep the module flat and still..."
                if ZERO_LEVEL else "Calibrating gyro -- keep the module still...")
status.align(lv.ALIGN.BOTTOM_MID, 0, -8)

# Draw the screen once so the calibration message shows while it runs (~1 s, blocking).
lv.refr_now(None)
mpu.calibrate(zero_level=ZERO_LEVEL)
status.set_text(STATUS_OK)

_f = {"a": None, "g": None, "t": None, "err": False}


def _ema(old, new):
    if old is None:
        return new  # start from the first reading instead of ramping up from 0
    return tuple(o + FILTER_ALPHA * (n - o) for o, n in zip(old, new))


def _update(_timer=None):
    try:
        accel, temp, gyro = mpu.read()
    except OSError as e:
        if not _f["err"]:
            print("MPU-6050 read failed:", e)
        _f["err"] = True
        status.set_text("read failed: %s" % e)
        status.set_style_text_color(ERR, 0)
        return
    if _f["err"]:
        _f["err"] = False
        status.set_text(STATUS_OK)
        status.set_style_text_color(DIM, 0)

    a = _f["a"] = _ema(_f["a"], accel)
    g = _f["g"] = _ema(_f["g"], gyro)
    t = _f["t"] = _ema(_f["t"], (temp,))

    pitch, roll = mpu6050.MPU6050.tilt(a)
    pitch_label.set_text("%+.1f°" % pitch)
    roll_label.set_text("%+.1f°" % roll)
    for i in range(3):
        accel_vals[i].set_text("%+.2f" % a[i])
        gyro_vals[i].set_text("%+.1f" % g[i])
    temp_label.set_text("%.1f°C  /  %.1f°F" % (t[0], t[0] * 9 / 5 + 32))

    # Dot: moves toward the lowered side (like a ball), clamped to the ring.
    dx = -a[0] / LEVEL_FULL_G
    dy = a[1] / LEVEL_FULL_G
    r2 = dx * dx + dy * dy
    if r2 > 1:
        r = r2 ** 0.5
        dx /= r
        dy /= r
    dot.set_pos(DOT_HOME_X + int(dx * DOT_TRAVEL), DOT_HOME_Y + int(dy * DOT_TRAVEL))


_update()
_timer = lv.timer_create(_update, UPDATE_MS, None)

# Drives lv.tick_inc()/lv.task_handler() on a background hardware timer, so the display
# keeps updating after this script finishes running.
task_handler = TaskHandler()

print("MPU-6050 display running.")
