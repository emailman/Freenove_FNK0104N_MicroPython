"""
Live audio spectrum analyzer for the Freenove FNK0104N: the onboard MEMS mic's spectrum as 32
log-spaced bars (86 Hz to 16 kHz) with peak-hold caps. Portrait UI on the 3.5" display
(ST77922, 320x480, QSPI). A companion to sound_meter.py, which shows the overall level.

Audio capture is the same as sound_meter.py (see its docstring): ES8311 codec / I2S RX at
44100Hz/16-bit mono, two FRAME-sized buffers filled alternately by IRQ-driven readinto(), the
codec (re)initialized only after BCLK is running, a stall watchdog in the UI timer, the speaker
amp held off, and touch never initialized (its I2C polling puts clicks into the mic).

_rx_cb does no analysis at all; it only keeps the reads going. The 10 Hz UI timer runs the FFT
on the most recent complete frame (_bufs[_latest]). That buffer can't be overwritten while the
timer runs: a new readinto() into it is only issued from _rx_cb, which is scheduled on the
same thread.

FFT (no ulab in this firmware; checked with help('modules')): a radix-2 decimation-in-time FFT
of N = 1 << FFT_BITS real samples, in integer math in @micropython.viper, in three steps:
  - _window: DC removal, Q15 Hann window, written into re[] in bit-reversed order.
  - _fft: in-place butterflies with Q15 twiddles. Block floating point: a stage halves its
    outputs only if the previous stage's largest component exceeded 8192. That keeps every
    product inside 32 bits (|x| <= 32767 complex magnitude throughout), and quiet signals
    keep full precision instead of being shifted away to nothing. It returns the number of
    halvings, which the dB conversion adds back as 6.02 dB each.
  - _band_power: sums re^2 + im^2 per band, split into low/high 16-bit halves like
    sound_meter.py's _level16, since a band's sum can pass 2^31.
Band level is in dB relative to a full-scale sine (0 dB = a sine at +-32767). Checked against a
CPython mirror of the same integer code: full-scale sine -> -0.1 dB in the right band, adjacent
leakage below -60 dB, a 32-count sine -> -60.5 dB.

Bands: edges are log-spaced, but each band gets at least one FFT bin, so the lowest bands are
single 21.5 Hz bins (FFT_BITS = 11) and the log spacing takes over above ~400 Hz.

Rendering: the bars bypass LVGL. LVGL only draws the static title and scale labels, once. The
bar area (256x340 at x=40, y=80; 32 bars on an 8 px pitch) lives in its own RGB565 buffer. Each
tick, _draw_rows (viper) redraws just the pixel rows that changed, and _send_rows pushes them
straight to the panel with the same synchronous tx_color() sequence as
ST77922Landscape._flush_cb. The window is 8 px column aligned (see CLAUDE.md).

Why not LVGL bars. All figures were measured on hardware (2026-10-31) by timing
lv.refr_now() with random bar levels:
  - Every dirty area costs ~3ms, however small (one 8x8 area: 3.4ms; 32 of them: 111ms).
  - Drawing costs roughly 1 us per pixel: the whole 256x340 bar area takes 110ms, of which
    21ms is the flush.
  - lv.obj bars, with one dirty rectangle per changed column (invalidation switched off while
    moving them, so there's one area per column): ~270ms per tick.
With any of these, the 10 Hz UI saturated the CPU, and only 4-9 of the 21.5 audio frames/s
arrived. Once the board stopped answering USB serial altogether and needed a RESET press. An
lv.chart BAR series is no better, since it redraws the whole chart on every update.
Direct drawing measured 22-23 frames/s with 0 stalls and about 40% of the CPU idle. A UI tick
takes 35-45ms: ~21ms is the FFT, and the rest is bar logic, drawing and sending ~80-160 rows.

Colors in the bar buffer are native RGB565. The bus applies the driver's rgb565_byte_swap to
tx_color() data itself, so pre-swapped colors come out wrong (see _rgb565).

Scale: DB_TOP (-20) to DB_FLOOR (-80) dB, re full-scale sine. Real per-band levels sit well
below full scale (quiet room: the loudest band reads about -66 dB; voice about -40 dB). The
color zones are by height: green, then yellow above DB_YELLOW, then red above DB_RED.

Requires the custom lvgl_micropython firmware -- see hello_world_display.py / README.md.
"""

import gc
import math
import time
from array import array

import micropython
from micropython import const

import lvgl as lv
import lcd_bus
from machine import SPI

import st77922
from task_handler import TaskHandler

# es8311 is imported further down, after the display's QSPI bus is up -- see
# hello_world_display.py's comment for why.

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

# Loose-file import after the display's esp_lcd panel IO is up -- see hello_world_display.py.
gc.collect()
from machine import I2C, I2S, Pin
from es8311 import ES8311

# --- Audio ---------------------------------------------------------------------------------

I2S_ID = 0
I2S_BCK = 18
I2S_WS = 21
I2S_DIN = 16  # ES8311 ADC (mic) -> ESP32
I2S_IBUF = 40000  # generous, so a late callback doesn't drop samples (see voice_recorder.py)
AMP_ENABLE_PIN = 1  # active-LOW -- see music_player.py

RATE = 44100
FRAME = 2048  # samples per capture buffer (~46ms)
MIC_GAIN = 6  # 0..7 = 0..42dB, same as sound_meter.py

# Codec I2C control port (addr 0x18) on host 0, pins 39/38. Touch is never set up here (see
# the module docstring), so nothing else uses the bus.
codec = ES8311(I2C(0, scl=Pin(39), sda=Pin(38), freq=100_000))
amp_enable = Pin(AMP_ENABLE_PIN, Pin.OUT, value=1)  # speaker amp off

_bufs = (bytearray(FRAME * 2), bytearray(FRAME * 2))
_cur = 0  # buffer the pending readinto() is filling
_latest = 1  # most recently completed frame
_new_frame = False  # set by _rx_cb, cleared when the UI timer analyzes _latest
_i2s = None
_frames = 0  # total frames captured -- diagnostics
_last_frame_ms = 0


def _rx_cb(_arg):
    global _cur, _latest, _new_frame, _frames, _last_frame_ms
    _last_frame_ms = time.ticks_ms()
    filled = _cur
    _cur ^= 1
    _i2s.readinto(_bufs[_cur])
    _latest = filled
    _new_frame = True
    _frames += 1


def _start_i2s():
    """Creates the RX channel, gets BCLK running, then (re)initializes the codec.

    Same ordering as sound_meter.py's _start_i2s(): the codec derives its clock from BCLK, so
    BCLK must already be toggling when it's configured (see es8311.py).
    """
    global _i2s, _last_frame_ms
    _i2s = I2S(
        I2S_ID,
        sck=I2S_BCK,
        ws=I2S_WS,
        sd=I2S_DIN,
        mode=I2S.RX,
        bits=16,
        format=I2S.MONO,
        rate=RATE,
        ibuf=I2S_IBUF,
    )
    _i2s.readinto(_bufs[0])  # one blocking read so BCLK is actually running
    time.sleep_ms(50)  # NOQA -- let BCLK settle before the codec tries to lock onto it
    codec.power_on()
    codec.enable_mic(MIC_GAIN)
    _last_frame_ms = time.ticks_ms()
    _i2s.irq(_rx_cb)  # switches the channel to non-blocking, IRQ-driven mode
    _i2s.readinto(_bufs[_cur])


# I2S callbacks go through micropython.schedule(), whose queue is shared with LVGL; when it's
# full, a callback is silently dropped and the read chain just stops (see voice_recorder.py).
# The UI timer restarts the chain if no frame has arrived for STALL_MS.
STALL_MS = 500
_stalls = 0


def _check_stall():
    global _stalls
    if time.ticks_diff(time.ticks_ms(), _last_frame_ms) >= STALL_MS:
        _stalls += 1
        _rx_cb(None)


# --- FFT -----------------------------------------------------------------------------------

FFT_BITS = 11
N = 1 << FFT_BITS  # <= FRAME; uses the newest N samples of the frame

# Window + bit-reversal table: (bit-reversed index << 16) | Q15 Hann coefficient.
_wr = array("i", bytes(4 * N))
for _i in range(N):
    _r = 0
    for _b in range(FFT_BITS):
        _r |= ((_i >> _b) & 1) << (FFT_BITS - 1 - _b)
    _w = min(32767, round(32767 * (0.5 - 0.5 * math.cos(2 * math.pi * _i / N))))
    _wr[_i] = (_r << 16) | _w

# Twiddles W^k = cos - i*sin (2*pi*k/N), interleaved cos, sin, Q15, k < N/2.
_tw = array("i", bytes(4 * N))
for _i in range(N // 2):
    _a = 2 * math.pi * _i / N
    _tw[2 * _i] = min(32767, round(32767 * math.cos(_a)))
    _tw[2 * _i + 1] = min(32767, round(32767 * math.sin(_a)))

_re = array("i", bytes(4 * N))
_im = array("i", bytes(4 * N))
# Each frame's newest N samples, as a ready-made view (no allocation per tick).
_views = tuple(memoryview(b)[(FRAME - N) * 2:] for b in _bufs)


@micropython.viper
def _window(src: ptr16, re: ptr32, wr: ptr32, nbits: int):
    """re[rev(i)] = Hann(i) * (src[i] - mean), for 1 << nbits int16 samples."""
    n = 1 << nbits
    s = 0
    for i in range(n):
        x = src[i]
        if x & 0x8000:
            x -= 0x10000
        s += x
    mean = s >> nbits
    for i in range(n):
        x = src[i]
        if x & 0x8000:
            x -= 0x10000
        x -= mean
        if x > 32767:
            x = 32767
        if x < -32767:
            x = -32767
        e = wr[i]
        re[e >> 16] = (x * (e & 0xFFFF)) >> 15


@micropython.viper
def _fft(re: ptr32, im: ptr32, tw: ptr32, nbits: int) -> int:
    """In-place radix-2 DIT FFT of real input re[] (bit-reversed order); im[] is cleared here.

    Returns how many stages halved their outputs (block floating point -- see the module
    docstring): the true DFT is the result times 2 ** returned value.
    """
    n = 1 << nbits
    mx = 0
    i = 0
    while i < n:
        im[i] = 0
        v = re[i]
        if v < 0:
            v = 0 - v
        if v > mx:
            mx = v
        i += 1
    shifts = 0
    size = 2
    step = n << 1  # index step into tw (cos/sin pairs): 2 * n / size, halved per stage below
    while size <= n:
        half = size >> 1
        step >>= 1
        sh = 0
        if mx > 8192:
            sh = 1
            shifts += 1
        mx = 0
        start = 0
        while start < n:
            k = 0
            a = start
            end = start + half
            while a < end:
                c = tw[k]
                s = tw[k + 1]
                b = a + half
                xr = re[b]
                xi = im[b]
                tr = (xr * c + xi * s) >> 15
                ti = (xi * c - xr * s) >> 15
                ur = re[a]
                ui = im[a]
                v = (ur + tr) >> sh
                re[a] = v
                if v < 0:
                    v = 0 - v
                if v > mx:
                    mx = v
                v = (ui + ti) >> sh
                im[a] = v
                if v < 0:
                    v = 0 - v
                if v > mx:
                    mx = v
                v = (ur - tr) >> sh
                re[b] = v
                if v < 0:
                    v = 0 - v
                if v > mx:
                    mx = v
                v = (ui - ti) >> sh
                im[b] = v
                if v < 0:
                    v = 0 - v
                if v > mx:
                    mx = v
                k += step
                a += 1
            start += size
        size <<= 1
    return shifts


@micropython.viper
def _band_power(re: ptr32, im: ptr32, edges: ptr32, out: ptr32):
    """Per band b (edges[0] bands, bins edges[b+1]..edges[b+2]-1): sum of re^2 + im^2, as
    out[2b] = sum of low 16 bits, out[2b+1] = sum of high bits (caller: hi * 65536 + lo)."""
    nb = edges[0]
    b = 0
    while b < nb:
        lo = 0
        hi = 0
        k = edges[b + 1]
        end = edges[b + 2]
        while k < end:
            r = re[k]
            q = im[k]
            p = r * r + q * q
            lo += p & 0xFFFF
            hi += p >> 16
            k += 1
        out[2 * b] = lo
        out[2 * b + 1] = hi
        b += 1


BANDS = 32
F_LO = 86
F_HI = 16000
BIN_HZ = RATE / N


def _make_edges():
    e = [max(1, round(F_LO / BIN_HZ))]
    for b in range(1, BANDS + 1):
        f = F_LO * (F_HI / F_LO) ** (b / BANDS)
        e.append(max(e[-1] + 1, min(N // 2, round(f / BIN_HZ))))
    return array("i", [BANDS] + e)


_edges = _make_edges()
_power = array("i", bytes(8 * BANDS))
_band_db = [0.0] * BANDS

DB_FLOOR = -80  # bottom of the bars; quieter bands are drawn at the floor
# Total power of a full-scale Hann-windowed sine in one band: 3 * A^2 * N^2 / 32.
_DB_REF = 10 * math.log10(3 * 32767 * 32767 * N * N / 32)
_DB_PER_SHIFT = 20 * math.log10(2)
_fft_us = 0


def _analyze():
    """FFT of the newest frame into _band_db (dB re full-scale sine, floored at DB_FLOOR)."""
    global _fft_us
    t0 = time.ticks_us()
    _window(_views[_latest], _re, _wr, FFT_BITS)
    shifts = _fft(_re, _im, _tw, FFT_BITS)
    _band_power(_re, _im, _edges, _power)
    offset = shifts * _DB_PER_SHIFT - _DB_REF
    log10 = math.log10
    for b in range(BANDS):
        p = _power[2 * b + 1] * 65536 + _power[2 * b]
        _band_db[b] = max(DB_FLOOR, 10 * log10(p) + offset) if p > 0 else DB_FLOOR
    _fft_us = time.ticks_diff(time.ticks_us(), t0)


# --- UI ------------------------------------------------------------------------------------

screen = lv.screen_active()
screen.set_style_bg_color(lv.color_hex(0x000000), 0)


def _scaled_label(parent, text, color, scale):
    # No font larger than 16px is compiled into this firmware -- scale the label instead,
    # pivoting on its own center so it grows in place (see CLAUDE.md "Text sizing").
    label = lv.label(parent)
    label.set_text(text)
    label.set_style_text_color(lv.color_hex(color), 0)
    label.set_style_transform_pivot_x(lv.pct(50), 0)
    label.set_style_transform_pivot_y(lv.pct(50), 0)
    label.set_style_transform_scale(int(lv.SCALE_NONE * scale), 0)
    return label


def _small_label(text):
    label = lv.label(screen)
    label.set_text(text)
    label.set_style_text_color(lv.color_hex(0x808080), 0)
    return label


title = _scaled_label(screen, "Spectrum", 0xFFFFFF, 2)
title.align(lv.ALIGN.TOP_MID, 0, 25)

# Bar area, drawn by _draw_rows straight into _fb and sent to the panel by _send_rows -- LVGL
# never draws here (see the module docstring). The viper code has these sizes built in.
_PITCH = const(8)  # one 8 px aligned column per bar
_BAR_W = const(6)
_CAP_H = const(2)
_FB_W = const(256)  # BANDS * _PITCH
VIEW_X = 40  # multiple of 8, so the write window stays column aligned (see CLAUDE.md)
VIEW_Y = 80
VIEW_H = 340
SEND_ROWS = 50  # 256 x 50 x 2 = 25.6KB per tx_color, the size of a 320 x 40 LVGL flush

PEAK_HOLD_MS = 1000
PEAK_DECAY_DB = 2  # per UI tick after the hold: 20 dB/s
BAR_FALL_DB = 4  # per UI tick: bars rise at once and fall at 40 dB/s


def _rgb565(rgb):
    """0xRRGGBB as native RGB565. No byte swap here: confirmed on hardware that the bus applies
    the driver's rgb565_byte_swap to tx_color() data itself (pre-swapped colors came out with
    the bytes reversed: the 0x101010 background showed as purple)."""
    return ((rgb >> 8) & 0xF800) | ((rgb >> 5) & 0x07E0) | ((rgb & 0xFF) >> 3)


# Top of the bars. Per-band levels of real sounds sit far below full scale: in a quiet room the
# loudest band reads about -66 dB, and voice at arm's length about -40 dB. On a 0 dB top, only
# the bottom half of the bars was ever used. The color zones are bar heights, so loud sounds
# reach yellow and red.
DB_TOP = -20
DB_YELLOW = -45
DB_RED = -30
_DB_RANGE = DB_TOP - DB_FLOOR


def _db_y(db):
    """Pixel row of a dB level (0 = top of the bars)."""
    return (DB_TOP - db) * VIEW_H // _DB_RANGE


def _row_db(y):
    return DB_TOP - _DB_RANGE * (y + 0.5) / VIEW_H


def _db_to_px(db):
    return max(0, min(VIEW_H, int((db - DB_FLOOR) * VIEW_H) // _DB_RANGE))


# Per pixel row: (bar color << 16) | background color. Bars are zoned by height like a classic
# analyzer: red above DB_RED, yellow above DB_YELLOW, green below. Grid lines every 10 dB.
_grid_rows = set(_db_y(_g) for _g in range(DB_TOP - 10, DB_FLOOR, -10))
_rows = array("i", bytes(4 * VIEW_H))
for _y in range(VIEW_H):
    _d = _row_db(_y)
    _fg = 0xE00000 if _d > DB_RED else 0xE0C000 if _d > DB_YELLOW else 0x00C000
    _bg = 0x303030 if _y in _grid_rows else 0x101010
    _rows[_y] = (_rgb565(_fg) << 16) | _rgb565(_bg)

# Per bar: fill top row (VIEW_H = empty), peak cap top row.
_bar_rows = array("i", bytes(8 * BANDS))
for _b in range(BANDS):
    _bar_rows[2 * _b] = VIEW_H
    _bar_rows[2 * _b + 1] = VIEW_H - _CAP_H
_bar_db = [float(DB_FLOOR)] * BANDS
_peak_db = [float(DB_FLOOR)] * BANDS
_peak_ms = [0] * BANDS

gc.collect()
_fb = data_bus.allocate_framebuffer(_FB_W * VIEW_H * 2, lcd_bus.MEMORY_SPIRAM | lcd_bus.MEMORY_DMA)
_fb_mv = memoryview(_fb)


@micropython.viper
def _draw_rows(fb: ptr16, rows: ptr32, bars: ptr32, span: int):
    """Redraws pixel rows (span >> 16) .. (span & 0xFFFF) - 1 of every bar column."""
    y = span >> 16
    y_end = span & 0xFFFF
    white = 0xFFFF
    while y < y_end:
        rc = rows[y]
        fg = (rc >> 16) & 0xFFFF
        bg = rc & 0xFFFF
        o = y * _FB_W
        b = 0
        while b < _FB_W // _PITCH:
            cap = bars[2 * b + 1]
            if y >= cap and y < cap + _CAP_H:
                c = white
            elif y >= bars[2 * b]:
                c = fg
            else:
                c = bg
            end = o + _BAR_W
            while o < end:
                fb[o] = c
                o += 1
            end = o + _PITCH - _BAR_W
            while o < end:
                fb[o] = bg
                o += 1
            b += 1
        y += 1


def _send_rows(y1, y2):
    """Sends rows y1 .. y2 - 1 of _fb to the panel, synchronously, in SEND_ROWS bands.

    Same direct-write sequence as ST77922Landscape._flush_cb. It's safe between LVGL renders:
    this runs from an LVGL timer, and LVGL has waited for its own last flush before timers run.
    """
    data_bus.register_callback(None)  # synchronous tx_color, no LVGL flush_ready callback
    try:
        x1, x2 = VIEW_X, VIEW_X + _FB_W - 1
        y = y1
        while y < y2:
            ye = min(y + SEND_ROWS, y2)
            cmd = display._set_memory_location(x1, VIEW_Y + y, x2, VIEW_Y + ye - 1)  # NOQA
            data_bus.tx_color(cmd, _fb_mv[y * _FB_W * 2:ye * _FB_W * 2], x1, VIEW_Y + y, x2,
                              VIEW_Y + ye - 1, display._rotation, ye == y2)  # NOQA
            y = ye
    finally:
        data_bus.register_callback(display._flush_ready_cb)  # NOQA


def _update_bars(full=False):
    """Applies _band_db to the bars and peak caps, then redraws and sends only the rows that
    changed. Returns the number of rows sent."""
    now = time.ticks_ms()
    y_lo, y_hi = VIEW_H, 0
    for b in range(BANDS):
        db = max(_band_db[b], _bar_db[b] - BAR_FALL_DB)
        _bar_db[b] = db
        if db >= _peak_db[b]:
            _peak_db[b], _peak_ms[b] = db, now
        elif time.ticks_diff(now, _peak_ms[b]) > PEAK_HOLD_MS:
            _peak_db[b] = max(db, _peak_db[b] - PEAK_DECAY_DB)
        top = VIEW_H - _db_to_px(db)
        cap = VIEW_H - max(_CAP_H, _db_to_px(_peak_db[b]))
        old_top, old_cap = _bar_rows[2 * b], _bar_rows[2 * b + 1]
        if top == old_top and cap == old_cap:
            continue
        y_lo = min(y_lo, top, old_top, cap, old_cap)
        y_hi = max(y_hi, top, old_top, cap + _CAP_H, old_cap + _CAP_H)
        _bar_rows[2 * b], _bar_rows[2 * b + 1] = top, cap
    if full:
        y_lo, y_hi = 0, VIEW_H
    if y_lo >= y_hi:
        return 0
    _draw_rows(_fb, _rows, _bar_rows, (y_lo << 16) | y_hi)
    _send_rows(y_lo, y_hi)
    return y_hi - y_lo


# Static labels, drawn by LVGL: dB scale on the left, frequencies below the bars.
for _db in range(DB_TOP, DB_FLOOR - 1, -20):
    _l = _small_label(str(_db))
    _l.align(lv.ALIGN.TOP_RIGHT, VIEW_X - 4 - DISPLAY_WIDTH,
             VIEW_Y + min(VIEW_H - 1, _db_y(_db)) - 8)


def _band_of(freq):
    for b in range(BANDS):
        if _edges[b + 2] * BIN_HZ > freq:
            return b
    return BANDS - 1


for _f, _t in ((100, "100"), (300, "300"), (1000, "1k"), (3000, "3k"), (10000, "10k")):
    _l = _small_label(_t)
    _l.align(lv.ALIGN.TOP_MID, VIEW_X + _band_of(_f) * _PITCH + _BAR_W // 2 - DISPLAY_WIDTH // 2,
             VIEW_Y + VIEW_H + 6)
_l = _small_label("Hz")
_l.align(lv.ALIGN.TOP_LEFT, VIEW_X + _FB_W + 4, VIEW_Y + VIEW_H + 6)

# Let LVGL draw the screen (labels, black background) once, then put the empty bars on it.
lv.refr_now(None)
_update_bars(full=True)

DEBUG = False  # prints frames/s, stalls, FFT time, rows sent and UI tick time once a second
_debug_ms = time.ticks_ms()
_debug_frames = 0


def _refresh_ui(_timer):
    global _new_frame, _debug_ms, _debug_frames
    t0 = time.ticks_us()
    _check_stall()
    if not _new_frame:
        return
    _new_frame = False
    _analyze()
    rows = _update_bars()

    if DEBUG:
        now = time.ticks_ms()
        if time.ticks_diff(now, _debug_ms) >= 1000:
            loudest = max(range(BANDS), key=lambda b: _band_db[b])
            print("frames/s %d  stalls %d  fft %dus  rows %d  ui %dus  loudest band %d (%.1f dB)" % (
                _frames - _debug_frames, _stalls, _fft_us, rows,
                time.ticks_diff(time.ticks_us(), t0), loudest, _band_db[loudest]))
            _debug_ms, _debug_frames = now, _frames


_start_i2s()
_ui_timer = lv.timer_create(_refresh_ui, 100, None)

# Drives lv.tick_inc()/lv.task_handler() on a background hardware timer, so the analyzer keeps
# running after this script finishes.
task_handler = TaskHandler()

print("Spectrum analyzer running (DEBUG=%s)." % DEBUG)
