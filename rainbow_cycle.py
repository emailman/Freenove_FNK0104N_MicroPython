"""
Rainbow cycle for the Freenove FNK0104N's 3.5" display (ST77922, 320x480, QSPI): a simpler,
stepped version of rainbow_fade.py. The whole screen shows one solid rainbow color (red, orange,
yellow, green, cyan, blue, indigo, violet) and a matching steady tone for SECONDS_PER_STEP, then
jumps straight to the next color and the next tone. After violet, it starts again at red.

The tone's pitch follows the color like rainbow_fade.py's: red is lowest (TONE_LOW_HZ), violet
highest (TONE_HIGH_HZ), spaced evenly in pitch, with the volume tapered the other way, since the
speaker is far louder at high pitches.

The hardware rules from rainbow_fade.py still apply, so a few of its parts are kept as they were:
- The screen is painted as 12 full-width 320x40 bands, refilled before every send (tx_color()
  byte-swaps the buffer in place), with colors in native RGB565.
- Only BANDS_PER_TICK bands are sent per LVGL timer tick. Painting the whole screen in one go
  starves the tone (see rainbow_fade.py). So each color change wipes down the screen over 4
  ticks (~130ms).
- The tone is a viper phase-accumulator sine, generated in the I2S callback. The phase carries
  over at each step, so the pitch changes without a click.
- The timer restarts the I2S callback chain if it stalls.

The I2S buffer holds about IBUF / 88200 s of sound, so whatever the callback writes is heard that
much later. The callback picks its tone for that later time (AUDIO_LEAD_MS ahead), so each new
tone starts with its color instead of lagging behind it.

Runs from an LVGL timer, so the REPL stays free. Call stop() from the REPL to silence it.
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

DISPLAY_WIDTH = 320
DISPLAY_HEIGHT = 480
BUFFER_ROWS = 40  # matches Freenove's own st77922_lcd_init buffer chunk size

SECONDS_PER_STEP = 1.0  # how long each color and tone lasts
# Bands sent per timer tick. More than ~4 in one go starves the tone (see rainbow_fade.py).
BANDS_PER_TICK = 3
TICK_MS = 10  # LVGL actually runs it ~30 times a second (the TaskHandler rate)

TONE_LOW_HZ = 220  # at red
TONE_HIGH_HZ = 880  # at violet (two octaves up)
# Peak sample value (of 32767) at red and at violet, tapered evenly in dB in between.
TONE_AMPLITUDE_LOW = 15000
TONE_AMPLITUDE_HIGH = 500
VOLUME = 70  # codec speaker volume, 0-100 (75 = 0dB DAC gain)

RAINBOW = (
    0xFF0000,  # red
    0xFF7F00,  # orange
    0xFFFF00,  # yellow
    0x00FF00,  # green
    0x00FFFF,  # cyan
    0x0000FF,  # blue
    0x4B0082,  # indigo
    0x8F00FF,  # violet
)
STEPS = len(RAINBOW)
_STEP_MS = int(SECONDS_PER_STEP * 1000)

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


def _rgb565(c):
    """0xRRGGBB as native RGB565 (no byte swap -- the bus does that)."""
    return ((c >> 8) & 0xF800) | ((c >> 5) & 0x07E0) | ((c >> 3) & 0x001F)


_COLORS = [_rgb565(c) for c in RAINBOW]


def _send_band(y, color):
    """Paints rows y .. y + BUFFER_ROWS - 1 one color, synchronously.

    Safe to do from an LVGL timer: LVGL has waited for its own last flush before timers run.
    """
    _fill(_band, DISPLAY_WIDTH * BUFFER_ROWS, color)  # refill: tx_color() swaps it in place
    y2 = y + BUFFER_ROWS - 1
    data_bus.register_callback(None)  # synchronous tx_color, no LVGL flush_ready callback
    try:
        cmd = display._set_memory_location(0, y, DISPLAY_WIDTH - 1, y2)  # NOQA
        data_bus.tx_color(cmd, _band, 0, y, DISPLAY_WIDTH - 1, y2, display._rotation, True)  # NOQA
    finally:
        data_bus.register_callback(display._flush_ready_cb)  # NOQA


_start = time.ticks_ms()


def _step(lead_ms=0):
    """Index into RAINBOW for lead_ms from now. Shared by the color and the tone."""
    return (time.ticks_diff(time.ticks_ms(), _start) + lead_ms) // _STEP_MS % STEPS


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
IBUF = 32000  # I2S internal buffer, bytes (~360ms)
AUDIO_LEAD_MS = IBUF * 1000 // (RATE * 2)  # how far behind the callback's writes are heard
STALL_MS = 500

# Codec I2C control port on host 0, pins 39/38 (the touch controller's bus; touch isn't used).
codec = ES8311(I2C(0, scl=Pin(39), sda=Pin(38), freq=100_000))
amp_enable = Pin(AMP_ENABLE_PIN, Pin.OUT, value=1)  # off until the codec is set up

# Phase step and peak amplitude for each step's tone, worked out once.
_STEP_PHASE = [
    int(TONE_LOW_HZ * (TONE_HIGH_HZ / TONE_LOW_HZ) ** (i / (STEPS - 1)) * 4294967296 / RATE)
    for i in range(STEPS)
]
_STEP_AMPLITUDE = [
    int(TONE_AMPLITUDE_LOW * (TONE_AMPLITUDE_HIGH / TONE_AMPLITUDE_LOW) ** (i / (STEPS - 1)))
    for i in range(STEPS)
]

# Full-scale sine table; _synth scales it by the current amplitude.
_SINE = array("h", (int(32767 * math.sin(2 * math.pi * i / 1024)) for i in range(1024)))
_buf = bytearray(CHUNK * 2)
_osc = array("i", (0, 0, 0))  # phase (32-bit, wraps), phase step per sample, peak amplitude
_i2s = None  # type: I2S | None
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


def _tx_cb(_arg):
    """I2S says the last chunk has been taken; synthesize and queue the next one."""
    global _last_progress
    if _i2s is None:
        return
    _last_progress = time.ticks_ms()
    s = _step(AUDIO_LEAD_MS)
    _osc[1] = _STEP_PHASE[s]
    _osc[2] = _STEP_AMPLITUDE[s]
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


# --- Display timer ----------------------------------------------------------------------------
_shown_step = -1  # step of the last finished repaint
_paint_step = 0  # step of the repaint in progress
_paint_y = -1  # next band's top row, or -1 when no repaint is in progress


def _tick(_timer):
    global _shown_step, _paint_step, _paint_y, _stalls
    for _ in range(BANDS_PER_TICK):
        if _paint_y < 0:
            s = _step()
            if s == _shown_step:
                break
            _paint_step, _paint_y = s, 0
        _send_band(_paint_y, _COLORS[_paint_step])
        _paint_y += BUFFER_ROWS
        if _paint_y >= DISPLAY_HEIGHT:
            _shown_step, _paint_y = _paint_step, -1
    # Restart the tone's callback chain if a scheduled callback was dropped.
    if _i2s is not None and time.ticks_diff(time.ticks_ms(), _last_progress) > STALL_MS:
        _stalls += 1
        _tx_cb(None)


# Give LVGL's own screen nothing to draw over us:
# it renders the default screen once at start-up,
# and our first tick repaints it right after.
lv.screen_active().set_style_bg_color(lv.color_hex(RAINBOW[0]), 0)

_start_tone()
_timer = lv.timer_create(_tick, TICK_MS, None)
task_handler = TaskHandler()

print("Rainbow cycle running.")
