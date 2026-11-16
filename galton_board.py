"""
Galton board animation for the Freenove FNK0104N's 3.5" display (ST77922, 320x480, QSPI,
portrait). Balls drop out of a funnel into a triangle of 12 rows of pegs, bounce left or right
at random at every peg, and pile up in 13 bins at the bottom. The stacks grow into the binomial
(roughly bell-shaped) distribution. A red tick over each bin marks where that bin's stack is
expected to be for the number of balls so far (total * C(12, j) / 2^12). When any bin is full,
the board stops dropping balls, holds the result for HOLD_MS, then empties and starts over.

Path of a ball. Each ball's 12 left/right choices are picked when it is spawned
(random.getrandbits), so its bin, and its slot in that bin's stack, are known from the start.
Its position is a function of one progress value s, in peg rows, which grows at ROWS_PER_SEC:
from the funnel spout it falls to the top peg (s = -1 .. 0), then from peg row i to row i + 1 it
moves half a peg spacing sideways while following a small bounce-and-fall parabola. After the
last row it keeps falling, ever faster, into its bin until it reaches its slot, where it is
painted into the background for good.

Rendering bypasses LVGL (see CLAUDE.md: LVGL costs ~3ms per dirty area and ~1us per pixel, too
slow for dozens of moving objects). LVGL only draws the title and ball counter in the top
TOP_ROWS rows. Everything below is direct drawing:
  - _bg: a full-screen RGB565 background (pegs, funnel, bins, landed balls) in an ordinary
    bytearray. Landed balls are painted into it once.
  - Each tick only the screen rows that changed are sent: where each ball was and is now, a
    landed ball, and expected-height ticks that moved. _row_runs merges those row ranges into
    runs of at most 40 rows. Each run is composed in _band (DMA memory): copy those rows of _bg,
    draw the ticks and moving balls over them, then send synchronously with tx_color(), the
    same sequence as rainbow_fade.py. Runs are full width, so 8 px column aligned.
  - tx_color() gets a memoryview of exactly the run's rows. Given the whole 40-row _band, it
    sends all of it: a 1-row send then cost 2.9ms instead of 0.7ms (and the extra rows wrap
    around the window).
  - Colors are native RGB565: the bus byte-swaps tx_color() data itself, in place, which is why
    each run is recomposed from _bg before every send rather than kept and resent.

Frame time, measured on hardware (2026-11-16): ~50ms per tick with ~20 balls in flight (~0.1ms
per row sent, plus ~0.6ms per send). The first version sent every 40-row band each tick (~62ms)
with a 10ms timer period: the board animated fine but never went idle, so the REPL and
mpremote (even `mpremote reset`) couldn't get in until a RESET press. Now each tick sets the
timer's period to its own frame time + IDLE_MS, so the board always idles between frames.

Runs from an LVGL timer, so the REPL stays free. Requires the custom lvgl_micropython firmware --
see hello_world_display.py / README.md.
"""

import gc
import random
import time
from array import array

import micropython

import lvgl as lv
import lcd_bus
from machine import SPI

import st77922
from task_handler import TaskHandler

DISPLAY_WIDTH = 320
DISPLAY_HEIGHT = 480
BUFFER_ROWS = 40  # matches Freenove's own st77922_lcd_init buffer chunk size
TOP_ROWS = 40  # left to LVGL (title and counter); direct drawing starts below

# --- Board geometry (pixels) ------------------------------------------------------------------
N = 12  # peg rows; N + 1 bins
SP = 22  # horizontal peg / bin spacing
ROW_H = 17  # vertical peg row spacing
CX = DISPLAY_WIDTH // 2
PEG_Y0 = 88  # center of the top peg
BALL_D = 6
BALL_LIFT = 6  # ball center above a peg's center while resting on it
HOP = 6  # how far a ball bounces up off a peg
SPAWN_Y = 54  # ball center at the funnel spout
FLOOR_Y = 478  # bin floor (2 rows); stacks sit on it
BIN_TOP = 292  # top of the bin dividers
DIV0_X = CX - (N + 1) * SP // 2  # leftmost divider (17)
COLS = 3  # balls side by side in one bin
LAYERS = (FLOOR_Y - BIN_TOP) // BALL_D  # 31
CAPACITY = LAYERS * COLS  # balls per bin

# --- Timing -----------------------------------------------------------------------------------
ROWS_PER_SEC = 5.0  # ball speed, in peg rows per second
SPAWN_MS = 110  # a new ball this often
HOLD_MS = 5000  # how long a full board is shown before it starts over
TICK_MS = 33  # shortest timer period (about the TaskHandler rate)
IDLE_MS = 15  # idle time left after every tick, so the REPL and USB serial still get a turn

# --- Colors -----------------------------------------------------------------------------------
BG = 0x0A0E1A
PEG = 0xB0B4C0
FRAME = 0x5A6890
BALL = 0xE8B020
BALL_SHINE = 0xFFF0A0
EXPECT = 0xFF4040


def _rgb565(c):
    """0xRRGGBB as native RGB565 (no byte swap -- the bus does that)."""
    return ((c >> 8) & 0xF800) | ((c >> 5) & 0x07E0) | ((c & 0xFF) >> 3)


C_BG, C_PEG, C_FRAME = _rgb565(BG), _rgb565(PEG), _rgb565(FRAME)
C_BALL, C_SHINE, C_EXPECT = _rgb565(BALL), _rgb565(BALL_SHINE), _rgb565(EXPECT)

# Disc sprites: (left offset, width) per row.
BALL_SPANS = bytes((2, 2, 1, 4, 0, 6, 0, 6, 1, 4, 2, 2))  # 6x6
PEG_SPANS = bytes((1, 3, 0, 5, 0, 5, 0, 5, 1, 3))  # 5x5

# --- Display ----------------------------------------------------------------------------------
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

# Our own send buffer (DMA), separate from LVGL's frame buffer.
_band = data_bus.allocate_framebuffer(
    DISPLAY_WIDTH * BUFFER_ROWS * 2, lcd_bus.MEMORY_SPIRAM | lcd_bus.MEMORY_DMA
)
_band_mv = memoryview(_band)
gc.collect()
_bg = bytearray(DISPLAY_WIDTH * DISPLAY_HEIGHT * 2)  # never sent directly


# --- Drawing primitives (viper; at most 4 arguments, so parameters go in an array) ------------
@micropython.viper
def _rect(buf, p):
    """Fills a rectangle. p = [x, y, w, h, color, buffer rows]; clipped to the buffer."""
    q = ptr32(p)
    x0 = q[0]
    y0 = q[1]
    x1 = x0 + q[2]
    y1 = y0 + q[3]
    c = q[4]
    rows = q[5]
    if x0 < 0:
        x0 = 0
    if x1 > 320:
        x1 = 320
    if y0 < 0:
        y0 = 0
    if y1 > rows:
        y1 = rows
    b = ptr16(buf)
    y = y0
    while y < y1:
        i = y * 320 + x0
        e = y * 320 + x1
        while i < e:
            b[i] = c
            i += 1
        y += 1


@micropython.viper
def _disc(buf, spans, p):
    """Draws a sprite of horizontal spans. p = [x, y, color, buffer rows, sprite rows]."""
    q = ptr32(p)
    s = ptr8(spans)
    x = q[0]
    y = q[1]
    c = q[2]
    rows = q[3]
    n = q[4]
    b = ptr16(buf)
    r = 0
    while r < n:
        yy = y + r
        if yy >= 0:
            if yy < rows:
                xa = x + s[2 * r]
                xb = xa + s[2 * r + 1]
                if xa < 0:
                    xa = 0
                if xb > 320:
                    xb = 320
                i = yy * 320 + xa
                e = yy * 320 + xb
                while i < e:
                    b[i] = c
                    i += 1
        r += 1


@micropython.viper
def _copy_rows(dst, src, y0: int, n_rows: int):
    """Copies n_rows full-width rows of src, starting at row y0, to the start of dst."""
    d = ptr32(dst)
    s = ptr32(src)
    off = y0 * 160  # 320 px = 160 words per row
    n = n_rows * 160
    i = 0
    while i < n:
        d[i] = s[off + i]
        i += 1


_p = array("i", (0, 0, 0, 0, 0, 0))


def rect(buf, rows, x, y, w, h, color):
    _p[0], _p[1], _p[2], _p[3], _p[4], _p[5] = x, y, w, h, color, rows
    _rect(buf, _p)


def ball(buf, rows, x, y):
    """A shaded ball with its top-left corner at (x, y)."""
    _p[0], _p[1], _p[2], _p[3], _p[4] = x, y, C_BALL, rows, 6
    _disc(buf, BALL_SPANS, _p)
    rect(buf, rows, x + 1, y + 1, 2, 2, C_SHINE)


def peg(buf, cx, cy):
    _p[0], _p[1], _p[2], _p[3], _p[4] = cx - 2, cy - 2, C_PEG, DISPLAY_HEIGHT, 5
    _disc(buf, PEG_SPANS, _p)


# --- Board ------------------------------------------------------------------------------------
def _peg_x(i, j):
    """Center x of peg j (0 .. i) in row i."""
    return CX + (2 * j - i) * SP // 2


def _slot_xy(j, slot):
    """Top-left of a landed ball: bin j, slot-th ball in that bin."""
    layer, col = divmod(slot, COLS)
    return DIV0_X + j * SP + 2 + col * BALL_D, FLOOR_Y - (layer + 1) * BALL_D


def _draw_background():
    rows = DISPLAY_HEIGHT
    rect(_bg, rows, 0, 0, DISPLAY_WIDTH, rows, C_BG)
    # Funnel: two 2px diagonals narrowing to a spout just wider than a ball.
    top, bottom = TOP_ROWS + 4, SPAWN_Y + 10
    for y in range(top, bottom):
        half = 8 + (bottom - y) * 60 // (bottom - top)
        rect(_bg, rows, CX - half - 2, y, 2, 1, C_FRAME)
        rect(_bg, rows, CX + half, y, 2, 1, C_FRAME)
    for i in range(N):
        for j in range(i + 1):
            peg(_bg, _peg_x(i, j), PEG_Y0 + i * ROW_H)
    for k in range(N + 2):  # bin dividers
        rect(_bg, rows, DIV0_X + k * SP - 1, BIN_TOP, 2, FLOOR_Y - BIN_TOP, C_FRAME)
    rect(_bg, rows, DIV0_X - 1, FLOOR_Y, (N + 1) * SP + 2, DISPLAY_HEIGHT - FLOOR_Y, C_FRAME)


# Binomial probabilities C(N, j) / 2^N, for the expected-height ticks.
_P = []
_c = 1
for _j in range(N + 1):
    _P.append(_c / (1 << N))
    _c = _c * (N - _j) // (_j + 1)

# --- LVGL: title and counter only -------------------------------------------------------------
screen = lv.screen_active()
screen.set_style_bg_color(lv.color_hex(BG), 0)
title = lv.label(screen)
title.set_text("Galton Board")
title.set_style_text_color(lv.color_hex(0xFFFFFF), 0)
title.align(lv.ALIGN.TOP_LEFT, 12, 12)
counter = lv.label(screen)
counter.set_text("0 balls")
counter.set_style_text_color(lv.color_hex(0xA0A8C0), 0)
counter.align(lv.ALIGN.TOP_RIGHT, -12, 12)
lv.refr_now(None)

# --- Animation --------------------------------------------------------------------------------
# A ball: [s (progress in rows), xs (center x at each peg row, then its slot's center x),
#          target y (slot's top-left y), bin, slot, drawn x, drawn y]
_balls = []
_reserved = [0] * (N + 1)  # slots handed out per bin
_landed = 0
_shown_landed = -1
_state = 0  # 0 = dropping balls, 1 = draining (a bin is full), 2 = holding the result
_hold_start = 0
_next_spawn = time.ticks_ms()
_last_ms = time.ticks_ms()
_dirty = [(TOP_ROWS, DISPLAY_HEIGHT - 1)]  # screen row ranges to send; everything at first
_tick_ys = []  # y of each bin's expected-height tick, as last drawn
_frame_ms = 0  # time spent in the last tick (for the REPL)


def _mark(y0, y1):
    """Marks screen rows y0 .. y1 for sending."""
    y0 = max(y0, TOP_ROWS)
    y1 = min(y1, DISPLAY_HEIGHT - 1)
    if y0 <= y1:
        _dirty.append((y0, y1))


def _row_runs(spans):
    """Sorts and merges row ranges (bridging gaps of a few rows, since every send has a fixed
    cost), then splits them into runs of at most BUFFER_ROWS rows."""
    spans.sort()
    merged = []
    for a, b in spans:
        if merged and a <= merged[-1][1] + 4:
            if b > merged[-1][1]:
                merged[-1][1] = b
        else:
            merged.append([a, b])
    runs = []
    for a, b in merged:
        while a <= b:
            n = min(BUFFER_ROWS, b - a + 1)
            runs.append((a, n))
            a += n
    return runs


def _update_ticks():
    """Recomputes the expected-height ticks and marks their old and new rows."""
    global _tick_ys
    ys = []
    if _landed:
        for j in range(N + 1):
            ys.append(FLOOR_Y - int(_landed * _P[j] / COLS * BALL_D + 0.5))
    old = _tick_ys
    for j in range(max(len(old), len(ys))):
        y_old = old[j] if j < len(old) else -1
        y_new = ys[j] if j < len(ys) else -1
        if y_old != y_new:  # most landings move only a tick or two, by a pixel
            if y_old >= 0:
                _mark(y_old - 1, y_old)
            if y_new >= 0:
                _mark(y_new - 1, y_new)
    _tick_ys = ys


def _spawn():
    """Starts a new ball. Returns False if its bin is already full."""
    path = random.getrandbits(N)
    xs = []
    j = 0
    for i in range(N):
        xs.append(_peg_x(i, j))
        j += (path >> i) & 1
    slot = _reserved[j]
    if slot >= CAPACITY:
        return False
    _reserved[j] = slot + 1
    sx, sy = _slot_xy(j, slot)
    xs.append(sx + BALL_D // 2)
    _balls.append([-1.0, xs, sy, j, slot, CX - BALL_D // 2, SPAWN_Y - BALL_D // 2])
    return True


def _ball_center(b):
    s = b[0]
    if s < 0:  # spout -> top peg, falling from rest
        f = s + 1
        return CX, SPAWN_Y + (PEG_Y0 - BALL_LIFT - SPAWN_Y) * f * f
    i = int(s)
    if i > N - 1:
        i = N - 1
    f = s - i
    xs = b[1]
    x0, x1 = xs[i], xs[i + 1]
    x = x1 if f >= 1 else x0 + (x1 - x0) * f
    y = PEG_Y0 - BALL_LIFT + ROW_H * i - HOP * f + (ROW_H + HOP) * f * f
    return x, y


def _reset():
    global _landed, _state
    for j in range(N + 1):
        _reserved[j] = 0
    _landed = 0
    _state = 0
    _draw_background()
    _update_ticks()
    _mark(TOP_ROWS, DISPLAY_HEIGHT - 1)


def _compose_and_send(y0, n):
    """Composes screen rows y0 .. y0 + n - 1 (n <= BUFFER_ROWS) in _band and sends them."""
    _copy_rows(_band, _bg, y0, n)
    for j in range(len(_tick_ys)):
        y = _tick_ys[j]
        if y >= BIN_TOP - 2 and y > y0 and y - 1 < y0 + n:
            rect(_band, n, DIV0_X + j * SP + 1, y - 1 - y0, SP - 2, 2, C_EXPECT)
    for bl in _balls:
        by = bl[6]
        if by + BALL_D > y0 and by < y0 + n:
            ball(_band, n, bl[5], by - y0)
    y2 = y0 + n - 1
    data_bus.register_callback(None)  # synchronous tx_color, no LVGL flush_ready callback
    try:
        cmd = display._set_memory_location(0, y0, DISPLAY_WIDTH - 1, y2)  # NOQA
        # Only the n rows: tx_color sends the whole buffer it is given, and extra rows would
        # wrap around the window (and cost ~2.8ms per send even for one row).
        data_bus.tx_color(cmd, _band_mv[:n * DISPLAY_WIDTH * 2], 0, y0, DISPLAY_WIDTH - 1, y2,  # NOQA
                          display._rotation, True)  # NOQA
    finally:
        data_bus.register_callback(display._flush_ready_cb)  # NOQA


def _tick(timer):
    global _last_ms, _next_spawn, _landed, _shown_landed, _state, _hold_start, _frame_ms
    now = time.ticks_ms()
    dt = min(time.ticks_diff(now, _last_ms), 100)
    _last_ms = now

    if _state == 0 and time.ticks_diff(now, _next_spawn) >= 0:
        _next_spawn = time.ticks_add(now, SPAWN_MS)
        if not _spawn():
            _state = 1
    elif _state == 1 and not _balls:
        _state, _hold_start = 2, now
    elif _state == 2 and time.ticks_diff(now, _hold_start) >= HOLD_MS:
        _reset()

    step = dt * ROWS_PER_SEC / 1000
    landed_before = _landed
    for bl in _balls[:]:
        _mark(bl[6], bl[6] + BALL_D - 1)  # where it was drawn
        bl[0] += step
        x, y = _ball_center(bl)
        x = int(x) - BALL_D // 2
        y = int(y) - BALL_D // 2
        if bl[0] > N - 1 and y >= bl[2]:  # reached its slot
            sx, sy = _slot_xy(bl[3], bl[4])
            ball(_bg, DISPLAY_HEIGHT, sx, sy)
            _mark(sy, sy + BALL_D - 1)
            _balls.remove(bl)
            _landed += 1
            continue
        bl[5], bl[6] = x, y
        _mark(y, y + BALL_D - 1)
    if _landed != landed_before:
        _update_ticks()

    runs = _row_runs(_dirty)
    _dirty.clear()
    for y0, n in runs:
        _compose_and_send(y0, n)

    if _landed != _shown_landed:
        _shown_landed = _landed
        counter.set_text("%d balls" % _landed)
    _frame_ms = time.ticks_diff(time.ticks_ms(), now)
    # LVGL measures the period from the start of the last run, so a long frame gets a longer
    # period: the board always idles IDLE_MS between ticks (it hung the REPL without this).
    timer.set_period(max(TICK_MS, _frame_ms + IDLE_MS))


_draw_background()
_timer = lv.timer_create(_tick, TICK_MS, None)
task_handler = TaskHandler()

print("Galton Board running.")
