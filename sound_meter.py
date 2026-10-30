"""
Live sound level meter for the Freenove FNK0104N: the onboard MEMS mic's level, in dBFS, as a
~10s sweeping history graph plus a VU bar with a peak-hold marker. Portrait UI on the 3.5"
display (ST77922, 320x480, QSPI).

Audio comes in through the same ES8311 codec / I2S RX path as voice_recorder.py (see its
docstring and es8311.py): DIN = GPIO16, BCK = 18, WS = 21, 44100Hz/16-bit mono, codec
(re)initialized only after BCLK is running. Unlike the recorder, capture never stops: two
FRAME-sized buffers are filled alternately by non-blocking, IRQ-driven readinto() calls, and
each completed frame's RMS level is computed in the callback (_level16, viper).

Touch is deliberately never initialized. Its I2C polling on the bus shared with the codec puts
a click into the mic every ~33ms (see voice_recorder.py's start_recording()), which would show
up on the graph as a raised noise floor. With no touch driver, the codec is driven through a
plain machine.I2C on host 0. The speaker amp (GPIO 1, active-LOW) is held off the whole time.

Level is RMS over one FRAME (2048 samples, ~46ms) with the frame's DC offset removed, as
dBFS (0 = a full-scale square wave). The UI timer runs at 10 Hz and plots the loudest frame
since its previous tick, so short sounds aren't missed between ticks. Measured on hardware
(2026-10-30, quiet room, MIC_GAIN 6): the noise floor reads about -60 dBFS.

Render budget, measured on hardware by timing lv.task_handler() with each element alone (all
at the 10 Hz UI rate, in ms of CPU per second):
  - audio callbacks + analysis (baseline): ~84
  - VU bar: ~40
  - 2x-scaled readout, updated at 5 Hz: ~110 (an unscaled label is ~free; the transform is
    the cost, not the box size)
  - history chart, SHIFT update mode: ~580. Shifting redraws the whole 220x340 chart every
    point, and with everything else this saturated the CPU: the I2S callbacks fell behind,
    and only 10-17 of the 21.5 frames/s arrived (dropped audio).
  - history chart, CIRCULAR update mode: ~70, since only the area around the new point is
    redrawn. That's why the graph sweeps left to right like an ECG monitor, with a cursor at
    the write position, instead of scrolling.

Future spectrum view -- the structure is already split for it:
  - FRAME is a power of two: a 1024- or 2048-point FFT at 44100Hz gives 43 or 21.5 Hz bins.
  - Per-frame work in _rx_cb must stay cheap (it delays the next readinto()); an FFT would
    run in the UI timer on _bufs[_latest] instead, the most recent complete frame.
  - The plan is a Q15 fixed-point radix-2 FFT in @micropython.viper with a precomputed
    twiddle array("h") and a Hann window, log-spaced bands, and an lv.chart BAR series. Check
    help('modules') for ulab first; this firmware almost certainly doesn't include it.
  - The UI for each view is built by its own function on its own container
    (_build_level_view), so a _build_spectrum_view can be swapped in once there are controls.

Requires the custom lvgl_micropython firmware -- see hello_world_display.py / README.md.
"""

import gc
import math
import time
from array import array

import micropython

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
FRAME_BITS = 11
FRAME = 1 << FRAME_BITS  # samples per analysis frame (~46ms)
MIC_GAIN = 6  # 0..7 = 0..42dB, same as voice_recorder.py

# Codec I2C control port (addr 0x18) on host 0, pins 39/38 -- the same bus the touch controller
# uses, but touch is never set up here (see module docstring), so there's nothing to share.
codec = ES8311(I2C(0, scl=Pin(39), sda=Pin(38), freq=100_000))
amp_enable = Pin(AMP_ENABLE_PIN, Pin.OUT, value=1)  # speaker amp off

_bufs = (bytearray(FRAME * 2), bytearray(FRAME * 2))
_cur = 0  # buffer the pending readinto() is filling
_latest = 1  # most recently completed frame (for the future spectrum view)
_i2s = None
_stats = array("i", (0, 0, 0, 0))  # _level16 output: sumsq lo, sumsq hi, peak, mean


@micropython.viper
def _level16(buf: ptr16, nbits: int, out: ptr32):
    """DC-removed sum of squares and peak of 1 << nbits int16 samples.

    Viper ints are 32-bit and the sum of 2048 squared samples can reach 2^41, so each square
    is split into its low and high 16 bits and those are summed separately (each sum fits);
    the caller recombines them as hi * 65536 + lo.
    """
    n = 1 << nbits
    s = 0
    for i in range(n):
        x = buf[i]
        if x & 0x8000:
            x -= 0x10000
        s += x
    mean = s >> nbits
    lo = 0
    hi = 0
    peak = 0
    for i in range(n):
        x = buf[i]
        if x & 0x8000:
            x -= 0x10000
        d = x - mean
        if d < 0:
            d = 0 - d
        if d > 32767:
            d = 32767
        if d > peak:
            peak = d
        sq = d * d
        lo += sq & 0xFFFF
        hi += sq >> 16
    out[0] = lo
    out[1] = hi
    out[2] = peak
    out[3] = mean


DB_FLOOR = -70  # bottom of the graph / VU bar; anything quieter is drawn at the floor


def _to_db(rms):
    if rms <= 0:
        return DB_FLOOR
    return max(DB_FLOOR, 20 * math.log10(rms / 32768))


# Loudest frame since the UI timer last looked (it plots one point per tick, ~2 frames).
_pend_db = None
_frames = 0  # total frames analyzed -- diagnostics
_last_frame_ms = 0


def _on_frame(buf):
    global _pend_db
    _level16(buf, FRAME_BITS, _stats)
    db = _to_db(math.sqrt((_stats[1] * 65536 + _stats[0]) / FRAME))
    if _pend_db is None or db > _pend_db:
        _pend_db = db


def _rx_cb(_arg):
    global _cur, _latest, _frames, _last_frame_ms
    _last_frame_ms = time.ticks_ms()
    filled = _cur
    _cur ^= 1
    # Queue the next read first, so capture continues while this frame is analyzed.
    _i2s.readinto(_bufs[_cur])
    _latest = filled
    _frames += 1
    _on_frame(_bufs[filled])


def _start_i2s():
    """Creates the RX channel, gets BCLK running, then (re)initializes the codec.

    Same ordering as voice_recorder.py's _start_i2s(): the codec derives its clock from BCLK,
    so BCLK must already be toggling when it's configured (see es8311.py).
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
# full a callback is silently dropped and the read chain just stops (see voice_recorder.py).
# The UI timer restarts the chain if no frame has arrived for STALL_MS.
STALL_MS = 500
_stalls = 0


def _check_stall():
    global _stalls
    if time.ticks_diff(time.ticks_ms(), _last_frame_ms) >= STALL_MS:
        _stalls += 1
        _rx_cb(None)


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


def _panel(parent, x, y, w, h, color):
    panel = lv.obj(parent)
    panel.set_size(w, h)
    panel.set_pos(x, y)
    panel.set_style_bg_color(lv.color_hex(color), 0)
    panel.set_style_bg_opa(lv.OPA.COVER, 0)
    panel.set_style_border_width(0, 0)
    panel.set_style_radius(0, 0)
    panel.set_style_pad_all(0, 0)
    panel.remove_flag(lv.obj.FLAG.SCROLLABLE)
    return panel


title = _scaled_label(screen, "Sound Level", 0xFFFFFF, 2)
title.align(lv.ALIGN.TOP_MID, 0, 25)

# Level readout in its own fixed box, so a text change only redraws that box.
readout_box = _panel(screen, (DISPLAY_WIDTH - 160) // 2, 68, 160, 44, 0x000000)
readout = _scaled_label(readout_box, "", 0xFFFFFF, 2)
readout.center()

VIEW_Y = 130
VIEW_H = DISPLAY_HEIGHT - VIEW_Y - 10

CHART_X = 40
CHART_W = 220
HISTORY_POINTS = 100  # one per UI tick: ~10s
VU_X = 272
VU_W = 38
PEAK_H = 3
PEAK_HOLD_MS = 1500
PEAK_DECAY_DB = 2  # per UI tick after the hold: 20 dB/s

GREEN = 0x00C000
YELLOW = 0xE0C000
RED = 0xE00000


def _vu_color(db):
    return RED if db > -6 else YELLOW if db > -20 else GREEN


def _db_to_px(db, h):
    """Height in pixels of a bar reaching db, out of h for the DB_FLOOR..0 range."""
    return max(0, min(h, int((db - DB_FLOOR) * h) // -DB_FLOOR))


class _LevelView:
    pass


def _build_level_view(parent):
    v = _LevelView()
    v.root = _panel(parent, 0, VIEW_Y, DISPLAY_WIDTH, VIEW_H, 0x000000)

    chart = lv.chart(v.root)
    chart.set_size(CHART_W, VIEW_H)
    chart.set_pos(CHART_X, 0)
    chart.set_type(lv.chart.TYPE.LINE)
    # CIRCULAR, not SHIFT: shifting redraws the whole chart every point (see module docstring).
    chart.set_update_mode(lv.chart.UPDATE_MODE.CIRCULAR)
    chart.set_point_count(HISTORY_POINTS)
    chart.set_axis_range(lv.chart.AXIS.PRIMARY_Y, DB_FLOOR, 0)
    chart.set_div_line_count(-DB_FLOOR // 10 + 1, 6)  # a line every 10 dB, every 2s
    chart.set_style_bg_color(lv.color_hex(0x101010), 0)
    chart.set_style_border_width(0, 0)
    chart.set_style_radius(0, 0)
    chart.set_style_pad_all(0, 0)
    chart.set_style_line_color(lv.color_hex(0x303030), 0)  # grid
    chart.set_style_line_width(2, lv.PART.ITEMS)
    chart.set_style_width(0, lv.PART.INDICATOR)  # no point markers, just the line
    chart.set_style_height(0, lv.PART.INDICATOR)
    v.series = chart.add_series(lv.color_hex(0x00E0FF), lv.chart.AXIS.PRIMARY_Y)
    chart.set_all_values(v.series, lv.CHART_POINT_NONE)
    v.chart = chart
    # Marks where the sweep writes next, since circular mode has no other "now" indicator.
    v.cursor = _panel(v.root, CHART_X, 0, 2, VIEW_H, 0xFFFFFF)
    v.next_point = 0

    # dB labels to the left of the chart, centered on their grid lines.
    for db in (0, -20, -40, -60):
        label = lv.label(v.root)
        label.set_text(str(db))
        label.set_style_text_color(lv.color_hex(0x808080), 0)
        label.align(lv.ALIGN.TOP_RIGHT, CHART_X - 4 - DISPLAY_WIDTH,
                    -db * (VIEW_H - 1) // -DB_FLOOR - 8)

    v.vu_track = _panel(v.root, VU_X, 0, VU_W, VIEW_H, 0x202020)
    v.vu_fill = _panel(v.vu_track, 0, VIEW_H, VU_W, 0, GREEN)
    v.vu_peak = _panel(v.vu_track, 0, VIEW_H - PEAK_H, VU_W, PEAK_H, 0xFFFFFF)
    v.fill_color = GREEN
    v.peak_db = DB_FLOOR
    v.peak_ms = 0
    return v


def _update_level_view(v, db):
    v.chart.set_next_value(v.series, int(db))
    v.next_point = (v.next_point + 1) % HISTORY_POINTS
    v.cursor.set_x(CHART_X + v.next_point * (CHART_W - 2) // (HISTORY_POINTS - 1))

    h = _db_to_px(db, VIEW_H)
    v.vu_fill.set_pos(0, VIEW_H - h)
    v.vu_fill.set_height(h)
    color = _vu_color(db)
    if color != v.fill_color:
        v.fill_color = color
        v.vu_fill.set_style_bg_color(lv.color_hex(color), 0)

    now = time.ticks_ms()
    if db >= v.peak_db:
        v.peak_db, v.peak_ms = db, now
    elif time.ticks_diff(now, v.peak_ms) > PEAK_HOLD_MS:
        v.peak_db = max(db, v.peak_db - PEAK_DECAY_DB)
    v.vu_peak.set_y(VIEW_H - max(PEAK_H, _db_to_px(v.peak_db, VIEW_H)))


level_view = _build_level_view(screen)

DEBUG = True  # prints frames/s, stalls, level and UI tick time once a second
_tick = 0
_readout_text = None
_debug_ms = time.ticks_ms()
_debug_frames = 0


def _refresh_ui(_timer):
    global _pend_db, _tick, _readout_text, _debug_ms, _debug_frames
    t0 = time.ticks_us()
    _check_stall()
    db, _pend_db = _pend_db, None
    if db is None:
        return  # no new frame since the last tick
    _tick += 1
    _update_level_view(level_view, db)

    if _tick % 2 == 0:  # 5 Hz, so the number is readable
        text = "%d dB" % round(db) if db > DB_FLOOR else "< %d dB" % DB_FLOOR
        if text != _readout_text:
            _readout_text = text
            readout.set_text(text)
            # set_text() on a transform_scale'd label doesn't invalidate where an earlier,
            # differently sized render sat -- redraw its whole strip (see CLAUDE.md).
            readout_box.invalidate()

    if DEBUG:
        now = time.ticks_ms()
        if time.ticks_diff(now, _debug_ms) >= 1000:
            print("frames/s %d  stalls %d  level %.1f dBFS  peak %d  ui %dus" % (
                _frames - _debug_frames, _stalls, db, _stats[2], time.ticks_diff(time.ticks_us(), t0)))
            _debug_ms, _debug_frames = now, _frames


_start_i2s()
_ui_timer = lv.timer_create(_refresh_ui, 100, None)

# Drives lv.tick_inc()/lv.task_handler() on a background hardware timer, so the meter keeps
# running after this script finishes.
task_handler = TaskHandler()

print("Sound meter running (DEBUG=%s)." % DEBUG)
