"""
Landscape picture slideshow with fade-in / fade-out, for the Freenove FNK0104N's onboard 3.5"
display (ST77922, 320x480, QSPI, shown as 480x320 landscape) and integrated touch.

Cycles through the four pictures in pictures/seasons: each one fades in from black, holds,
then fades out to black. Swipe left for the next picture, right for the previous one (the
current one fades out early).

The pictures are converted on the PC first, with jpg_to_rgb565.py, into raw RGB565 files
that are already scaled to 480x320 and rotated into the panel's native portrait orientation.
Copy them to /seasons on the board:
    .venv\\Scripts\\python.exe jpg_to_rgb565.py pictures\\seasons\\*.jpg -o pictures\\seasons\\rgb565
    mpremote connect COM12 mkdir :seasons
    mpremote connect COM12 cp pictures/seasons/rgb565/*.rgb565 :seasons/

Why the fade isn't done by LVGL: LVGL could fade an lv.image by animating its opacity, but
every step of a full-screen fade is a full-screen redraw, and LVGL draws at ~1us per pixel on
this board (see CLAUDE.md) -- several frames a second at best, before ST77922Landscape's
rotation. Instead, each frame is drawn directly: a viper function scales every pixel of the
stored picture toward black into a band buffer, and the band is sent to the panel with the
same synchronous tx_color() sequence ST77922Landscape._flush_cb uses. Because the pictures
are pre-rotated, every band is a full-width (0..319) portrait window, which is the shape this
panel handles correctly. LVGL only runs the touch input (gesture detection) and the timer
that draws the frames, and is left an empty black screen it never needs to redraw.

The band buffer is LVGL's own draw buffer (display._send_buf, the same one
ST77922Landscape._flush_cb copies through). The bus only tracks two framebuffers, and
ST77922Landscape already uses both. Drawing from an LVGL timer is safe: LVGL has finished its
own flushes by the time timers run. Each frame rebuilds every band from the stored picture,
so the bus's in-place byte swap of the band buffer never matters.

Requires the custom lvgl_micropython firmware -- see hello_world_display.py / README.md.
Display/touch init is copied from swipe_demo_landscape.py.
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
    raise OSError("no pictures in " + PICTURE_DIR + " -- see the module docstring")
print("Loaded", names)

_band = display._send_buf  # NOQA -- see the module docstring
_band_rows = display._send_rows  # NOQA


@micropython.viper
def _scale(src, dst, n: int, level: int):
    """Writes n (even) native RGB565 pixels of src into dst, each channel scaled by level/32.

    Works on two pixels per 32-bit word: each channel of both pixels is pulled into its own
    field with at least 5 spare bits above it, so one multiply scales that channel of both
    pixels at once without them running into each other.
    """
    s = ptr32(src)
    d = ptr32(dst)
    n2 = n >> 1
    i = 0
    if level >= 32:
        while i < n2:
            d[i] = s[i]
            i += 1
        return
    while i < n2:
        w = s[i]
        b = (((w & 0x001F001F) * level) >> 5) & 0x001F001F
        g = ((((w >> 5) & 0x003F003F) * level) >> 5) & 0x003F003F
        r = ((((w >> 11) & 0x001F001F) * level) >> 5) & 0x001F001F
        d[i] = (r << 11) | (g << 5) | b
        i += 1


def _draw(picture, level):
    """Sends the whole picture at brightness level (0..32), synchronously."""
    src = memoryview(picture)
    band = memoryview(_band)
    row_bytes = PHYS_W * 2
    data_bus.register_callback(None)  # synchronous tx_color, no LVGL flush_ready callback
    try:
        y = 0
        while y < PHYS_H:
            y2 = min(y + _band_rows, PHYS_H) - 1
            n = (y2 - y + 1) * row_bytes
            _scale(src[y * row_bytes:y * row_bytes + n], band, n >> 1, level)
            cmd = display._set_memory_location(0, y, PHYS_W - 1, y2)  # NOQA
            data_bus.tx_color(cmd, band[:n], 0, y, PHYS_W - 1, y2, display._rotation,  # NOQA
                              y2 == PHYS_H - 1)
            y = y2 + 1
    finally:
        data_bus.register_callback(display._flush_ready_cb)  # NOQA


def _ease(t):
    """0..1 -> 0..1, smoothstep, so the fade starts and ends gently."""
    return t * t * (3 - 2 * t)


FADE_IN, HOLD, FADE_OUT = 0, 1, 2
_state = FADE_IN
_index = 0
_next: int | None = None  # picture index chosen by a swipe (set by _on_gesture)
_t0 = time.ticks_ms()
_level = 0  # brightness actually on screen
_fade_out_from = 32  # level a fade-out starts at (lower when a swipe cuts a fade-in short)
_frames = 0


def _start(state, now):
    global _state, _t0, _frames
    _state = state
    _t0 = now
    _frames = 0


def _tick(_t):
    global _index, _next, _level, _fade_out_from, _frames
    now = time.ticks_ms()
    elapsed = time.ticks_diff(now, _t0)

    if _next is not None and _state != FADE_OUT:
        _fade_out_from = _level
        _start(FADE_OUT, now)
        elapsed = 0

    if _state == FADE_IN:
        t = min(elapsed / FADE_MS, 1.0)
        _level = int(32 * _ease(t) + 0.5)
        _draw(pictures[_index], _level)
        _frames += 1
        if t >= 1.0:
            print("%s: faded in, %d frames, %.1f fps"
                  % (names[_index], _frames, _frames * 1000 / max(elapsed, 1)))
            _start(HOLD, now)
    elif _state == HOLD:
        if elapsed >= HOLD_MS:
            _fade_out_from = 32
            _start(FADE_OUT, now)
    else:  # FADE_OUT
        duration = FADE_MS * _fade_out_from // 32
        t = min(elapsed / duration, 1.0) if duration else 1.0
        _level = int(_fade_out_from * (1 - _ease(t)) + 0.5)
        _draw(pictures[_index], _level)
        _frames += 1
        if t >= 1.0:
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
# Period 1 ms: a new frame on every LVGL pass. Each frame blocks for the whole panel send.
_timer = lv.timer_create(_tick, 1, None)

print("Seasons fade running. Swipe left/right to change picture.")
