"""
Landscape picture slideshow with a right-to-left sweeping fade, for the Freenove FNK0104N's
onboard 3.5" display (ST77922, 320x480, QSPI, shown as 480x320 landscape) and integrated touch.

A variant of seasons_fade.py. Instead of the whole picture brightening and dimming at once,
a soft-edged fade front sweeps across the screen from right to left:
  - fade-in: the picture appears at the right edge first and is revealed leftward;
  - fade-out: the right edge goes dark first, and the left edge is the last part to fade.
Swipe left for the next picture, right for the previous one (the current one fades out
early, from whatever is on screen at that moment).

Uses the same pre-rotated RGB565 pictures in /seasons as seasons_fade.py -- see that file's
docstring for how to make and copy them, and for why the fade is drawn directly instead of
through LVGL.

The rotation makes the sweep cheap: a logical (landscape) column is a physical (portrait) row,
with the right edge (logical x 479) at physical row 0. So every physical row has one
brightness level, the sweep is a per-row level table, and a viper function scales each row
by its own level. Each frame only sends the rows whose level changed since the last frame
(the soft edge, plus the rows the front just passed), as full-width bands, so it runs faster
than seasons_fade.py, which resends the whole screen every frame.

Requires the custom lvgl_micropython firmware -- see hello_world_display.py / README.md.
"""

import gc
import os
import time

import lvgl as lv
import lcd_bus
import micropython
from machine import SPI

import st77922
from task_handler import TaskHandler

DISPLAY_WIDTH = 480   # logical/landscape size LVGL renders into -- see ST77922Landscape
DISPLAY_HEIGHT = 320
BUFFER_ROWS = 26      # same proven-safe chunk size as ntp_clock.py

PHYS_W = 320          # the panel's native portrait size, which the pictures are stored in
PHYS_H = 480

PICTURE_DIR = "/seasons"
ORDER = ("spring", "summer", "fall", "winter")

FADE_MS = 1500
HOLD_MS = 4000
EDGE_ROWS = 120  # width of the soft fade edge in pixels; _sweep has it hard-coded too

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

# Loose-file touch imports after display init -- see CLAUDE.md.
gc.collect()
import i2c
import st77922_touch

touch_i2c_bus = i2c.I2C.Bus(host=0, scl=39, sda=38, freq=100_000)
touch_device = i2c.I2C.Device(
    bus=touch_i2c_bus, dev_id=st77922_touch.I2C_ADDR, reg_bits=st77922_touch.BITS
)
touch = st77922_touch.ST77922LTouch(device=touch_device, reset_pin=48, int_pin=47)

screen = lv.screen_active()
screen.set_style_bg_color(lv.color_hex(0x000000), 0)
screen.set_style_bg_opa(lv.OPA.COVER, 0)
screen.remove_flag(lv.obj.FLAG.SCROLLABLE)

# Load every picture into RAM (PSRAM) once: 307,200 bytes each.
pictures = []
names = []
files = os.listdir(PICTURE_DIR)
for name in ORDER:
    fname = name + ".rgb565"
    if fname not in files:
        print("Missing", PICTURE_DIR + "/" + fname, "-- skipped")
        continue
    buf = bytearray(PHYS_W * PHYS_H * 2)
    with open(PICTURE_DIR + "/" + fname, "rb") as f:
        f.readinto(buf)
    pictures.append(buf)
    names.append(name)
    gc.collect()
if not pictures:
    raise OSError("no pictures in " + PICTURE_DIR + " -- see seasons_fade.py's docstring")
print("Loaded", names)

_band = display._send_buf  # NOQA -- see seasons_fade.py's docstring
_band_rows = display._send_rows  # NOQA

_shown = bytearray(PHYS_H)  # level (0..32) of each physical row now on screen
_want = bytearray(PHYS_H)   # level each row should have this frame
_from = bytearray(PHYS_H)   # levels when a fade-out started (a swipe can cut a fade-in short)


@micropython.viper
def _scale_rows(src, dst, levels, nrows: int):
    """Writes nrows 320-pixel native RGB565 rows of src into dst, row i scaled by levels[i]/32.

    Two pixels per 32-bit word, as in seasons_fade.py's _scale.
    """
    s = ptr32(src)
    d = ptr32(dst)
    lv8 = ptr8(levels)
    row = 0
    i = 0
    while row < nrows:
        level = lv8[row]
        end = i + 160  # 320 pixels = 160 words
        if level >= 32:
            while i < end:
                d[i] = s[i]
                i += 1
        elif level == 0:
            while i < end:
                d[i] = 0
                i += 1
        else:
            while i < end:
                w = s[i]
                b = (((w & 0x001F001F) * level) >> 5) & 0x001F001F
                g = ((((w >> 5) & 0x003F003F) * level) >> 5) & 0x003F003F
                r = ((((w >> 11) & 0x001F001F) * level) >> 5) & 0x001F001F
                d[i] = (r << 11) | (g << 5) | b
                i += 1
        row += 1


@micropython.viper
def _sweep(want, cap, front: int, fade_out: int):
    """Fills want with each row's level for a fade front at row `front`.

    Rows the front has passed by EDGE_ROWS or more are done, rows ahead of the front are
    untouched, and the rows in between ramp linearly. Done means full brightness for a
    fade-in and black for a fade-out; a fade-out never makes a row brighter than cap.
    """
    w = ptr8(want)
    c = ptr8(cap)
    row = 0
    while row < 480:
        done = ((front - row) * 32) // 120  # 120 = EDGE_ROWS
        if done < 0:
            done = 0
        if done > 32:
            done = 32
        if fade_out:
            level = 32 - done
            if level > int(c[row]):
                level = int(c[row])
        else:
            level = done
        w[row] = level
        row += 1


@micropython.viper
def _changed(a, b) -> int:
    """First and last row where a and b differ, packed as first * 1024 + last; -1 if none."""
    p = ptr8(a)
    q = ptr8(b)
    first = -1
    last = -1
    row = 0
    while row < 480:
        if p[row] != q[row]:
            if first < 0:
                first = row
            last = row
        row += 1
    if first < 0:
        return -1
    return first * 1024 + last


def _draw(picture):
    """Sends the rows whose level changed, at their new levels, synchronously.

    Returns the number of rows sent.
    """
    span = _changed(_want, _shown)
    if span < 0:
        return 0
    y, y_end = span >> 10, span & 1023
    sent = y_end - y + 1
    src = memoryview(picture)
    band = memoryview(_band)
    want = memoryview(_want)
    row_bytes = PHYS_W * 2
    data_bus.register_callback(None)  # synchronous tx_color, no LVGL flush_ready callback
    try:
        while y <= y_end:
            y2 = min(y + _band_rows - 1, y_end)
            n = (y2 - y + 1) * row_bytes
            _scale_rows(src[y * row_bytes:], band, want[y:], y2 - y + 1)
            cmd = display._set_memory_location(0, y, PHYS_W - 1, y2)  # NOQA
            data_bus.tx_color(cmd, band[:n], 0, y, PHYS_W - 1, y2, display._rotation,  # NOQA
                              y2 == y_end)
            y = y2 + 1
    finally:
        data_bus.register_callback(display._flush_ready_cb)  # NOQA
    _shown[:] = _want
    return sent


def _ease(t):
    """0..1 -> 0..1, smoothstep, so the sweep starts and ends gently."""
    return t * t * (3 - 2 * t)


FADE_IN, HOLD, FADE_OUT = 0, 1, 2
_state = FADE_IN
_index = 0
_next: int | None = None  # picture index chosen by a swipe (set by _on_gesture)
_t0 = time.ticks_ms()
_frames = 0
_rows_sent = 0


def _start(state, now):
    global _state, _t0, _frames, _rows_sent
    _state = state
    _t0 = now
    _frames = 0
    _rows_sent = 0
    if state == FADE_OUT:
        _from[:] = _shown


def _tick(_t):
    global _index, _next, _frames, _rows_sent
    now = time.ticks_ms()
    elapsed = time.ticks_diff(now, _t0)

    if _next is not None and _state != FADE_OUT:
        _start(FADE_OUT, now)
        elapsed = 0

    if _state == HOLD:
        if elapsed >= HOLD_MS:
            _start(FADE_OUT, now)
        return

    t = min(elapsed / FADE_MS, 1.0)
    front = int(_ease(t) * (PHYS_H + EDGE_ROWS))
    _sweep(_want, _from, front, _state == FADE_OUT)
    _rows_sent += _draw(pictures[_index])
    _frames += 1
    if t < 1.0:
        return
    print("%s: %s, %d frames, %.1f fps, %d rows/frame"
          % (names[_index], "faded in" if _state == FADE_IN else "faded out", _frames,
             _frames * 1000 / max(elapsed, 1), _rows_sent // _frames))
    if _state == FADE_IN:
        _start(HOLD, now)
    else:
        if _next is not None:
            _index, _next = _next, None
        else:
            _index = (_index + 1) % len(pictures)
        _start(FADE_IN, time.ticks_ms())


def _on_gesture(_e):
    global _next
    d = lv.indev_active().get_gesture_dir()
    base = _next if _next is not None else _index
    if d == lv.DIR.LEFT:
        _next = (base + 1) % len(pictures)
    elif d == lv.DIR.RIGHT:
        _next = (base - 1) % len(pictures)
    else:
        return
    print("Swipe ->", names[_next])


screen.add_event_cb(_on_gesture, lv.EVENT.GESTURE, None)

task_handler = TaskHandler()
# Period 1 ms: a new frame on every LVGL pass. Each frame blocks while its rows are sent.
_timer = lv.timer_create(_tick, 1, None)

print("Seasons right-to-left fade running. Swipe left/right to change picture.")
