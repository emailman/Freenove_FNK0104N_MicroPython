"""
Rainbow fade for the Freenove FNK0104N's 3.5" display (ST77922, 320x480, QSPI): the whole screen
is one solid color that glides slowly through the rainbow (red, orange, yellow, green, cyan,
blue, indigo, violet) and then back again, blending smoothly from each color into the next.

How the color is chosen. Position along the rainbow follows a cosine, so the fade eases to a
stop at red and at violet and turns around, instead of bouncing off the ends. Between two
neighboring rainbow colors, the red, green, and blue channels are blended linearly.
SECONDS_PER_PASS sets how long one trip from red to violet takes.

How it's drawn. LVGL isn't used for drawing (it would take ~150ms per full-screen repaint; see
CLAUDE.md). The screen is painted as 12 full-width 320x40 bands, top to bottom: each band is
filled with the color (viper) and pushed with the same synchronous tx_color() sequence as
spectrum_analyzer.py's _send_rows. Full-width writes are 8 px column aligned. Colors are native
RGB565, because the bus applies rgb565_byte_swap itself -- in place, in the buffer, so the band
is refilled before every send (resending one fill showed as alternating horizontal bands, every
other one byte-swapped). A repaint only starts when the RGB565 value actually changes.

Only BANDS_PER_TICK bands are sent per LVGL timer tick, so a repaint is spread over 4 ticks.
Measured on hardware (2026-11-02): painting all 12 bands in one tick (~39ms of busy-waiting on
the panel) starved the tone, even at 10 repaints/s with ~40% of the CPU idle. Its I2S callbacks
fell from the 43/s it needs to as low as 20/s whenever the color was changing quickly, which
sounded like static everywhere but near violet, where the color changes least. The likely cause
is that the task feeding the I2S hardware can't run during a long synchronous send, so the
hardware runs dry. Splitting it up fixed it: 1, 2 or 4 bands per tick all held 43/s with no
stalls. 3 sits between them, with margin, and gives ~7.5 repaints/s. The band edges are never
visible, since each repaint changes the color by only a step or two.

The tone. A sine wave plays through the speaker, and its pitch follows the color, like light
itself: red is lowest (TONE_LOW_HZ) and violet highest (TONE_HIGH_HZ), spaced evenly in pitch
(exponentially in Hz). It's generated on the fly: _tx_cb, the I2S completion callback, fills
CHUNK samples with _synth (viper, a phase accumulator over a 1024-entry sine table) at the
frequency for the current position, then writes them. The phase carries over from chunk to
chunk, so the pitch glides without clicks. The volume tapers the other way, from
TONE_AMPLITUDE_LOW at red to TONE_AMPLITUDE_HIGH at violet (evenly in dB), since the speaker is
far louder at high pitches. Audio setup is the same as voice_recorder.py: the I2S
channel and BCLK start before the codec is configured (it clocks from BCLK), and the speaker amp
(GPIO 1, active-LOW) is switched on after that. The display timer also restarts the callback
chain if it stalls (micropython.schedule() can drop I2S callbacks; see CLAUDE.md). The I2S
buffer delays the sound by about IBUF / 88200 s behind the color, which is too little to notice
at this pace. Call stop() from the REPL to silence it.

Runs from an LVGL timer, so the REPL stays free. Requires the custom lvgl_micropython firmware --
see hello_world_display.py / README.md.
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

DISPLAY_WIDTH = 320
DISPLAY_HEIGHT = 480
BUFFER_ROWS = 40  # matches Freenove's own st77922_lcd_init buffer chunk size

SECONDS_PER_PASS = 30  # red -> violet; the trip back takes as long again
# Bands sent per timer tick. More than ~4 in one go starves the tone (see the module docstring).
BANDS_PER_TICK = 3
TICK_MS = 10  # LVGL actually runs it ~30 times a second (the TaskHandler rate)

TONE_LOW_HZ = 220  # at red
TONE_HIGH_HZ = 880  # at violet (two octaves up)
# Peak sample value (of 32767) at red and at violet. The speaker sounds much louder at high
# pitches, so the volume tapers from one to the other, evenly in dB along the way.
TONE_AMPLITUDE_LOW = 15000
TONE_AMPLITUDE_HIGH = 500
VOLUME = 70  # codec speaker volume, 0-100 (75 = 0dB DAC gain)

RAINBOW = (
    0xFF0000,  # red
    0xFF7F00,  # orange
    0xFFFF00,  # yellow
    0x00FF00,  # green
    0x00FFFF,  # cyan -- without it, green -> blue dims through a dark teal halfway
    0x0000FF,  # blue
    0x4B0082,  # indigo
    0x8F00FF,  # violet
)

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

# Our own band buffer, separate from LVGL's frame buffer.
_band = data_bus.allocate_framebuffer(
    DISPLAY_WIDTH * BUFFER_ROWS * 2, lcd_bus.MEMORY_SPIRAM | lcd_bus.MEMORY_DMA
)


@micropython.viper
def _fill(buf, n: int, color: int):
    """Sets the first n (even) 16-bit pixels of buf to color, two pixels per 32-bit write."""
    p = ptr32(buf)
    pair = color | (color << 16)
    i = 0
    n2 = n >> 1
    while i < n2:
        p[i] = pair
        i += 1


def _rgb565(r, g, b):
    """8-bit channels as native RGB565 (no byte swap -- the bus does that)."""
    return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)


def _rainbow_color(pos):
    """pos in 0.0 .. len(RAINBOW) - 1; blends the two neighboring colors linearly."""
    i = int(pos)
    if i >= len(RAINBOW) - 1:
        i = len(RAINBOW) - 2
    f = pos - i
    c1, c2 = RAINBOW[i], RAINBOW[i + 1]
    r = int(((c1 >> 16) & 0xFF) * (1 - f) + ((c2 >> 16) & 0xFF) * f + 0.5)
    g = int(((c1 >> 8) & 0xFF) * (1 - f) + ((c2 >> 8) & 0xFF) * f + 0.5)
    b = int((c1 & 0xFF) * (1 - f) + (c2 & 0xFF) * f + 0.5)
    return _rgb565(r, g, b)


def _send_band(y, color):
    """Paints rows y .. y + BUFFER_ROWS - 1 one color, synchronously.

    Safe to do from an LVGL timer: LVGL has waited for its own last flush before timers run.
    """
    # Refill before every send: tx_color() byte-swaps the buffer in place (rgb565_byte_swap),
    # so resending one fill gives alternately swapped bands.
    _fill(_band, DISPLAY_WIDTH * BUFFER_ROWS, color)
    y2 = y + BUFFER_ROWS - 1
    data_bus.register_callback(None)  # synchronous tx_color, no LVGL flush_ready callback
    try:
        cmd = display._set_memory_location(0, y, DISPLAY_WIDTH - 1, y2)  # NOQA
        data_bus.tx_color(cmd, _band, 0, y, DISPLAY_WIDTH - 1, y2, display._rotation, True)  # NOQA
    finally:
        data_bus.register_callback(display._flush_ready_cb)  # NOQA


_start = time.ticks_ms()
_shown_color = -1  # color of the last finished repaint
_paint_color = 0  # color of the repaint in progress
_paint_y = -1  # next band's top row, or -1 when no repaint is in progress
_PERIOD_MS = SECONDS_PER_PASS * 2 * 1000  # there and back


def _position():
    """Where we are on the rainbow right now: 0.0 (red) .. 1.0 (violet) .. 0.0, easing at both
    ends. Shared by the color and the tone so they stay in step."""
    t = time.ticks_diff(time.ticks_ms(), _start) % _PERIOD_MS
    return (1 - math.cos(2 * math.pi * t / _PERIOD_MS)) / 2


# --- Tone -------------------------------------------------------------------------------------
# Loose-file import after the display's esp_lcd panel IO is up -- see hello_world_display.py.
gc.collect()
from machine import I2C, I2S, Pin
from es8311 import ES8311

I2S_ID = 0
I2S_BCK = 18
I2S_WS = 21
I2S_DOUT = 15  # ESP32 -> ES8311 DAC (speaker)
AMP_ENABLE_PIN = 1  # active-LOW -- see music_player.py
RATE = 44100
CHUNK = 1024  # samples per callback (~23ms)
IBUF = 32000  # I2S internal buffer, bytes (~360ms) -- also the sound's lag behind the color
STALL_MS = 500

# Codec I2C control port on host 0, pins 39/38 (the touch controller's bus; touch isn't used).
codec = ES8311(I2C(0, scl=Pin(39), sda=Pin(38), freq=100_000))
amp_enable = Pin(AMP_ENABLE_PIN, Pin.OUT, value=1)  # off until the codec is set up

# Full-scale sine table; _synth scales it by the current amplitude.
_SINE = array("h", (int(32767 * math.sin(2 * math.pi * i / 1024)) for i in range(1024)))
_buf = bytearray(CHUNK * 2)
_osc = array("i", (0, 0, 0))  # phase (32-bit, wraps), phase step per sample, peak amplitude
_i2s = None
_last_progress = time.ticks_ms()
_stalls = 0


@micropython.viper
def _synth(buf, n: int, osc, table):
    """Writes n sine samples into buf, continuing osc's phase at osc's step, scaled to osc's
    peak amplitude."""
    out = ptr16(buf)
    o = ptr32(osc)
    t = ptr16(table)
    phase = o[0]
    step = o[1]
    amp = o[2]
    i = 0
    while i < n:
        v = t[(phase >> 22) & 0x3FF]
        if v > 32767:  # ptr16 reads are unsigned
            v -= 65536
        out[i] = (v * amp) >> 15
        phase += step
        i += 1
    o[0] = phase


def _tone_hz(pos):
    return TONE_LOW_HZ * (TONE_HIGH_HZ / TONE_LOW_HZ) ** pos


def _tone_amplitude(pos):
    return int(TONE_AMPLITUDE_LOW * (TONE_AMPLITUDE_HIGH / TONE_AMPLITUDE_LOW) ** pos)


def _tx_cb(_arg):
    """I2S says the last chunk has been taken; synthesize and queue the next one."""
    global _last_progress
    if _i2s is None:
        return
    _last_progress = time.ticks_ms()
    pos = _position()
    _osc[1] = int(_tone_hz(pos) * 4294967296 / RATE)
    _osc[2] = _tone_amplitude(pos)
    _synth(_buf, CHUNK, _osc, _SINE)
    _i2s.write(_buf)


def _start_tone():
    global _i2s
    _i2s = I2S(I2S_ID, sck=I2S_BCK, ws=I2S_WS, sd=I2S_DOUT, mode=I2S.TX, bits=16,
               format=I2S.MONO, rate=RATE, ibuf=IBUF)
    _i2s.write(bytearray(CHUNK * 2))  # one blocking write of silence, so BCLK is running
    time.sleep_ms(50)  # NOQA -- let BCLK settle before the codec locks onto it
    codec.power_on()
    codec.set_volume(VOLUME)
    amp_enable.value(0)  # amp on
    _i2s.irq(_tx_cb)  # non-blocking, IRQ-driven from here on
    _tx_cb(None)


def stop():
    """Silences the tone (the colors keep going). For the REPL."""
    global _i2s
    if _i2s is not None:
        i2s, _i2s = _i2s, None
        amp_enable.value(1)
        i2s.deinit()


def _tick(_timer):
    global _shown_color, _paint_color, _paint_y, _stalls
    for _ in range(BANDS_PER_TICK):
        if _paint_y < 0:
            color = _rainbow_color(_position() * (len(RAINBOW) - 1))
            if color == _shown_color:
                break
            _paint_color, _paint_y = color, 0
        _send_band(_paint_y, _paint_color)
        _paint_y += BUFFER_ROWS
        if _paint_y >= DISPLAY_HEIGHT:
            _shown_color, _paint_y = _paint_color, -1
    # Restart the tone's callback chain if a scheduled callback was dropped.
    if _i2s is not None and time.ticks_diff(time.ticks_ms(), _last_progress) > STALL_MS:
        _stalls += 1
        _tx_cb(None)


# Give LVGL's own screen nothing to draw over us: it renders the default screen once at start-up,
# and our first tick repaints it right after.
lv.screen_active().set_style_bg_color(lv.color_hex(RAINBOW[0]), 0)

_start_tone()
_timer = lv.timer_create(_tick, TICK_MS, None)
task_handler = TaskHandler()

print("Rainbow fade running.")
