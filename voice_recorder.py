"""
Voice recorder demo for the Freenove FNK0104N: tap RECORD to capture 10 seconds from the
onboard MEMS mic, then tap PLAY to pick any .wav file in the SD card's root from a scrollable
list and hear it over the onboard speaker. Portrait UI on the 3.5" display (ST77922, 320x480,
QSPI) with its integrated touch.

Audio goes through the same ES8311 codec music_player.py uses (see its docstring for wiring,
and es8311.py for the codec config -- enable_mic() is what turns on the ADC/mic path). The mic
is not a separate device: it feeds the ES8311's ADC, whose output comes back to the ESP32 over
I2S on DIN = GPIO16 (docs.freenove.com's FNK0104N Music chapter), sharing BCK=18/WS=21 with
playback. Like Freenove's own "Echo" example this is half-duplex -- I2S is created in RX mode to
record, deinit()ed, then re-created in TX mode to play. Each time I2S starts, the codec is
re-initialized *after* BCLK is running, the same ordering music_player.play() found necessary
(the codec derives its clock from BCLK -- see es8311.py).

Recording is 44100Hz/16-bit mono, the one rate es8311.py's BCLK-clock table is verified for
(ESP32 std-mode I2S still clocks two slots in MONO, so BCLK is identical to stereo playback).
10s of that is 882,000 bytes, recorded into a RAM (PSRAM heap) bytearray.

The codec's I2C control port (addr 0x18) shares host 0 / pins 39/38 with the touch controller
(addr 0x55), and host 0 can't be opened twice in one process, so the codec is driven through
the touch driver's own i2c.I2C.Bus (it wraps machine.I2C and exposes the same writeto_mem()
es8311.py uses) rather than a second machine.I2C(0, ...).

The speaker amp (GPIO 1, active-LOW) is only enabled during playback, so it can't hiss into the
mic while recording. Touch polling is paused while recording too -- its I2C traffic on the bus
shared with the codec put a click into the mic every ~33ms (see start_recording()).

After each recording, the audio is post-processed once (~1s, "Processing..." on screen):
100Hz high-pass + 4kHz low-pass filter, a noise gate that turns pauses down 12dB, then
normalization so the loudest sample is near full scale. See _process() and the constants
above it for the measured values each setting was calibrated against.

The processed recording is then saved to the SD card as /sd/rec_YYYYMMDD_HHMMSS.wav (Mountain
local time; ~2.6s, "Saving..." on screen), in the same 44100Hz/16-bit format music_player.py
plays. The clock comes from one WiFi+NTP sync at startup (local_time.py, credentials from
wifi_secrets.py), after which WiFi is turned off again. If that sync fails, files fall back to
the next free recNNN.wav name instead. Nothing is ever overwritten.

PLAY opens a file picker that lists the SD card's root .wav files, newest recordings first.
Tapping one streams it from the card (so songs far bigger than RAM play fine); only
44100Hz/16-bit PCM files are accepted, mono or stereo -- the one rate es8311.py's clock table
supports. With no SD card inserted, saving is skipped and PLAY replays the last recording from
RAM instead.

Confirmed working on hardware (2026-09-25): voice is clear, playback volume is comfortable, and
touch works normally after each recording.

Requires the custom lvgl_micropython firmware -- see hello_world_display.py / README.md.
Wiring/init pattern (QSPI display bus, then I2C touch after it) is copied from
touch_keyboard_demo.py; see hello_world_display.py's comments for why the ordering matters.
"""

import gc
import time

import micropython

import lvgl as lv
import lcd_bus
from machine import SPI

import st77922
from task_handler import TaskHandler

# i2c/st77922_touch/es8311 are imported further down, after the display's QSPI bus is up --
# see hello_world_display.py's comment for why.

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

# Importing/compiling loose files (st77922_touch.py, es8311.py) here -- after the display's
# esp_lcd panel IO is already up -- rather than at the top of the file keeps that
# compilation's transient memory use out of the way of esp_lcd_new_panel_io_spi's allocation
# above; doing it beforehand was enough to make that allocation fail (ESP_ERR_NO_MEM).
gc.collect()
import i2c
import st77922_touch
from machine import I2S, Pin, SDCard
from es8311 import ES8311

touch_i2c_bus = i2c.I2C.Bus(host=0, scl=39, sda=38, freq=100_000)
touch_device = i2c.I2C.Device(
    bus=touch_i2c_bus, dev_id=st77922_touch.I2C_ADDR, reg_bits=st77922_touch.BITS
)
touch = st77922_touch.ST77922Touch(device=touch_device, reset_pin=48, int_pin=47)

# --- Audio ---------------------------------------------------------------------------------

I2S_ID = 0
I2S_BCK = 18
I2S_WS = 21
I2S_DOUT = 15  # ESP32 -> ES8311 DAC (speaker)
I2S_DIN = 16  # ES8311 ADC (mic) -> ESP32
# The IRQ callbacks below run via micropython.schedule(), so they wait behind LVGL rendering;
# if a callback is late by more than ibuf's worth of audio, samples are silently dropped (seen
# on hardware: with a 20000-byte ibuf / 4096-byte chunks and a full-screen redraw every 100ms,
# a 10s recording took ~40s and played back choppy). Generous buffering fixes that.
I2S_IBUF = 40000
AMP_ENABLE_PIN = 1  # active-LOW -- see music_player.py

RATE = 44100
RECORD_SECONDS = 10
CHUNK = 8192
VOLUME = 75  # speaker, 0-100 (75 = 0dB DAC gain; 80 was +6.5dB, a bit loud with normalization)
# 0..7 = 0..42dB (see ES8311.enable_mic). Measured on hardware in a quiet room: the noise floor
# peak roughly doubles per step (29 at 4, 106 at 6, 253 at 7, of 32767) -- 6 leaves plenty of
# headroom for speech at arm's length.
MIC_GAIN = 6

codec = ES8311(touch_i2c_bus)  # shares the touch bus -- see module docstring
amp_enable = Pin(AMP_ENABLE_PIN, Pin.OUT, value=1)  # off until playback

gc.collect()
_audio = bytearray(RECORD_SECONDS * RATE * 2)  # 16-bit mono
_audio_mv = memoryview(_audio)
_silence = bytearray(CHUNK)

IDLE = 0
RECORDING = 1
PLAYING = 2
FLUSHING = 3
PROCESSING = 4
SAVING = 5

_state = IDLE
_pos = 0
_recorded_len = 0
_nflush = 0
_i2s = None

# Playback source: an open WAV file on the SD card (or _RamSource for the in-RAM recording),
# streamed CHUNK bytes at a time through _play_buf by _tx_cb.
_play_src = None
_play_len = 0  # bytes of sample data
_play_rate = RATE * 2  # bytes/second, for the countdown
_play_name = ""
_play_buf = bytearray(CHUNK)
_play_mv = memoryview(_play_buf)

# --- SD card: each processed recording is saved as /sd/rec_YYYYMMDD_HHMMSS.wav --------------
# Same SDMMC wiring and non-standard machine.SDCard kwargs as music_player.py (see its
# docstring / CLAUDE.md). 44100Hz/16-bit mono, so music_player.play() can play them back too.
# If no card is present, saving is just skipped -- the recorder still works.
import os
import struct

SD_CLK = 5
SD_CMD = 4
SD_DATA = (6, 7, 2, 3)  # D0, D1, D2, D3
SD_MOUNT = "/sd"


def _mount_sd():
    if SD_MOUNT.strip("/") in os.listdir("/"):
        return True  # already mounted (e.g. by music_player.py in the same session)
    try:
        sd = SDCard(slot=1, width=4, clk=SD_CLK, cmd=SD_CMD, data_pins=SD_DATA)
        os.mount(sd, SD_MOUNT)
        return True
    except OSError as e:
        print("No SD card -- recordings won't be saved:", e)
        return False


_sd_ok = _mount_sd()
_last_saved = None
_unsaved = False  # last recording exists only in RAM (save failed / not yet saved)


def _sync_clock():
    """One WiFi+NTP sync for the filename timestamps, then WiFi off again.

    The disconnect() before active(False) is required, not tidiness. Measured on hardware
    (2026-09-26): after a connect + active(False) alone, the mic recorded only its noise floor
    (100ms-window peaks median 91, max 166 while talking), while playback still worked. With
    disconnect() first, recordings were normal (median 363, max 1742, matching a run with no
    WiFi at all). Any failure (no wifi_secrets.py, no network, NTP timeout) just means
    recordings fall back to recNNN.wav names.
    """
    wlan = None
    try:
        import local_time
        wlan = local_time.connect_wifi()
        return local_time.sync_time()
    except Exception as e:  # NOQA -- ImportError, RuntimeError (timeout), OSError, ...
        print("Clock sync failed -- recordings will be named recNNN.wav:", e)
        return False
    finally:
        if wlan is None:
            import network
            wlan = network.WLAN(network.STA_IF)
        try:
            wlan.disconnect()
        except OSError:
            pass  # wasn't connected
        time.sleep_ms(200)
        wlan.active(False)


_clock_ok = _sd_ok and _sync_clock()


def _next_filename():
    if _clock_ok:
        import local_time
        t = local_time.mountain_time()[0]
        base = "rec_%04d%02d%02d_%02d%02d%02d" % t[:6]
        names = os.listdir(SD_MOUNT)
        name, n = base + ".wav", 2
        while name in names:  # two saves within the same second
            name = "%s_%d.wav" % (base, n)
            n += 1
        return name
    used = set()
    for name in os.listdir(SD_MOUNT):
        if name.startswith("rec") and name.endswith(".wav") and name[3:-4].isdigit():
            used.add(int(name[3:-4]))
    n = 1
    while n in used:
        n += 1
    return "rec%03d.wav" % n


def _wav_header(data_len):
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + data_len, b"WAVE",
        b"fmt ", 16, 1, 1, RATE, RATE * 2, 2, 16,  # PCM, mono, rate, byte rate, align, bits
        b"data", data_len,
    )


def _save():
    global _last_saved, _unsaved
    name = _next_filename()
    path = SD_MOUNT + "/" + name
    with open(path, "wb") as f:
        f.write(_wav_header(_recorded_len))
        f.write(_audio_mv[:_recorded_len])
    # Seen on hardware (2026-09-26): a card whose writes had started failing still returned
    # full byte counts from write() and raised nothing on close, leaving 0-byte files behind.
    # Check the size that actually landed on the card instead of trusting write().
    size = os.stat(path)[6]
    if size != 44 + _recorded_len:
        try:
            os.remove(path)
        except OSError:
            pass
        raise OSError("wrote %d of %d bytes" % (size, 44 + _recorded_len))
    _last_saved = name
    _unsaved = False
    print("Saved", SD_MOUNT + "/" + name)


def _list_wavs():
    """Root .wav files on the SD card, reverse-sorted so the newest rec_YYYYMMDD_* is first."""
    return sorted((n for n in os.listdir(SD_MOUNT) if n.lower().endswith(".wav")), reverse=True)


def _open_wav(name):
    """Opens /sd/<name> positioned at its sample data; returns (file, channels, data_len).

    Walks the RIFF chunks (skipping LIST/etc. that converters add) rather than assuming a
    44-byte header. Raises ValueError with a short, on-screen-sized message for anything that
    isn't 44100Hz/16-bit PCM -- the only format es8311.py's BCLK clock table supports.
    """
    f = open(SD_MOUNT + "/" + name, "rb")
    try:
        riff = f.read(12)
        if len(riff) < 12 or riff[:4] != b"RIFF" or riff[8:] != b"WAVE":
            raise ValueError("Not a WAV file")
        channels = None
        while True:
            hdr = f.read(8)
            if len(hdr) < 8:
                raise ValueError("No audio data")
            cid, size = hdr[:4], struct.unpack("<I", hdr[4:])[0]
            if cid == b"fmt ":
                fmt = f.read(size + (size & 1))
                audio_fmt, channels, rate = struct.unpack("<HHI", fmt[:8])
                bits = struct.unpack("<H", fmt[14:16])[0]
                if audio_fmt != 1 or bits != 16:
                    raise ValueError("Unsupported format")
                if rate != RATE:
                    raise ValueError("Unsupported: %dHz" % rate)
                if channels not in (1, 2):
                    raise ValueError("Unsupported format")
            elif cid == b"data":
                if channels is None:
                    raise ValueError("Not a WAV file")
                return f, channels, size
            else:
                f.seek(size + (size & 1), 1)  # chunks are word-aligned
    except Exception:
        f.close()
        raise


class _RamSource:
    """File-like readinto()/close() over the in-RAM recording, so _tx_cb has one code path
    for SD files and (with no SD card) the last recording."""

    def __init__(self, n):
        self._pos = 0
        self._n = n

    def readinto(self, buf):
        n = min(len(buf), self._n - self._pos)
        buf[:n] = _audio_mv[self._pos:self._pos + n]
        self._pos += n
        return n

    def close(self):
        pass


def _start_i2s(mode, sd_pin, fmt=None):
    """Creates the I2S channel, gets BCLK running, then (re)initializes the codec.

    BCLK must already be toggling when the codec is configured -- it derives its clock from
    BCLK (see es8311.py / music_player.py). The codec init (~350ms of register writes) also
    happens here, before any real capture/playback starts, so none of it lands in the audio.
    fmt is I2S.MONO (default) or I2S.STEREO; in std mode BCLK is the same for both, so the
    codec's clock setup doesn't change.
    """
    global _i2s
    _i2s = I2S(
        I2S_ID,
        sck=I2S_BCK,
        ws=I2S_WS,
        sd=sd_pin,
        mode=mode,
        bits=16,
        format=I2S.MONO if fmt is None else fmt,
        rate=RATE,
        ibuf=I2S_IBUF,
    )
    # One short blocking transfer so the channel (and BCLK) is actually running.
    if mode == I2S.RX:
        _i2s.readinto(_silence)
    else:
        _i2s.write(_silence)
    time.sleep_ms(50)  # NOQA -- let BCLK settle before the codec tries to lock onto it
    codec.power_on()
    codec.enable_mic(MIC_GAIN)
    codec.set_volume(VOLUME)


# --- Post-processing, run once after each recording (see _process) --------------------------
# 1. Band-pass: one-pole high-pass (~100Hz, removes rumble/hum) + 2nd-order Butterworth
#    low-pass (~4kHz, removes hiss above the speech band).
# 2. Noise gate: 10ms windows whose peak stays under GATE_THRESHOLD (with GATE_HOLD_WINDOWS
#    of hold after, and one window of look-ahead before, anything louder) are attenuated to
#    GATE_FLOOR -- turned down, not muted -- with a linear gain ramp across each window so
#    there are no clicks at the transitions.
# 3. Normalize, below.
# All per-sample loops are @micropython.viper: 441,000 samples in plain Python would take
# seconds. Viper functions take at most 4 args, hence the coefficient/parameter arrays.
import math
from array import array

HPF_HZ = 100
LPF_HZ = 4000
GATE_WINDOW = RATE // 100  # 10ms
# Raw (pre-normalization, post-filter) window peak. Calibrated on hardware: a silent,
# filtered recording's 10ms window peaks were median 72, max 98 (speech peaks ~2400) --
# ~2x the noise ceiling keeps quiet syllables above the gate.
GATE_THRESHOLD = 200
GATE_HOLD_WINDOWS = 20  # 200ms
GATE_FLOOR = 64  # Q8 gain for gated stretches: 1/4 = -12dB


def _filter_coeffs():
    # One-pole HPF pole in Q15; Butterworth LPF biquad (RBJ bilinear) in Q13.
    r = int((1 - 2 * math.pi * HPF_HZ / RATE) * 32768)
    k = math.tan(math.pi * LPF_HZ / RATE)
    norm = 1 / (1 + k * math.sqrt(2) + k * k)
    b0 = k * k * norm
    a1 = 2 * (k * k - 1) * norm
    a2 = (1 - k * math.sqrt(2) + k * k) * norm
    q = 1 << 13
    return array("i", (r, round(b0 * q), round(2 * b0 * q), round(b0 * q),
                       round(a1 * q), round(a2 * q)))


_coeffs = _filter_coeffs()


@micropython.viper
def _filter16(buf: ptr16, n: int, c: ptr32):
    r = c[0]
    b0 = c[1]
    b1 = c[2]
    b2 = c[3]
    a1 = c[4]
    a2 = c[5]
    xp = 0
    hp = 0
    h1 = 0
    h2 = 0
    y1 = 0
    y2 = 0
    for i in range(n):
        x = buf[i]
        if x & 0x8000:
            x -= 0x10000
        h = x - xp + ((r * hp) >> 15)
        xp = x
        hp = h
        y = (b0 * h + b1 * h1 + b2 * h2 - a1 * y1 - a2 * y2) >> 13
        h2 = h1
        h1 = h
        y2 = y1
        y1 = y
        if y > 32767:
            y = 32767
        elif y < -32768:
            y = -32768
        buf[i] = y & 0xFFFF


@micropython.viper
def _window_peaks(buf: ptr16, nw: int, w: int, out: ptr16):
    i = 0
    for k in range(nw):
        p = 0
        for j in range(i, i + w):
            x = buf[j]
            if x & 0x8000:
                x = 0x10000 - x
            if x > p:
                p = x
        out[k] = p
        i += w


@micropython.viper
def _apply_gains(buf: ptr16, g: ptr16, nw: int, p: ptr32):
    w = p[0]
    inv_w = p[1]  # 65536 // w -- avoids a division per sample
    gp = g[0]
    i = 0
    for k in range(nw):
        gt = g[k]
        d = gt - gp
        for j in range(w):
            gain = gp + ((d * j * inv_w) >> 16)
            x = buf[i]
            if x & 0x8000:
                x -= 0x10000
            buf[i] = ((x * gain) >> 8) & 0xFFFF
            i += 1
        gp = gt


def _gate(n):
    nw = n // GATE_WINDOW
    if nw == 0:
        return
    peaks = array("H", bytes(2 * nw))
    _window_peaks(_audio, nw, GATE_WINDOW, peaks)
    gains = array("H", bytes(2 * nw))
    hold = 0
    for k in range(nw):
        if peaks[k] > GATE_THRESHOLD or (k + 1 < nw and peaks[k + 1] > GATE_THRESHOLD):
            hold = GATE_HOLD_WINDOWS
        if hold > 0:
            gains[k] = 256
            hold -= 1
        else:
            gains[k] = GATE_FLOOR
    _apply_gains(_audio, gains, nw, array("i", (GATE_WINDOW, 65536 // GATE_WINDOW)))


def _process():
    n = _recorded_len // 2
    _filter16(_audio, n, _coeffs)
    _gate(n)
    _normalize()


# Recordings are normalized after capture: scaled so their loudest sample lands near
# NORMALIZE_TARGET. Speech at arm's length measured only ~2400/32767 peak at MIC_GAIN 6 --
# audible but quiet on playback. Doing it in software after the fact (rather than more analog/
# ADC gain) adapts to each recording, can't clip, and doesn't raise the noise floor of loud
# recordings. MAX_GAIN caps the boost so a near-silent recording doesn't become loud hiss.
NORMALIZE_TARGET = 30000
MAX_GAIN = 16


@micropython.viper
def _peak16(buf: ptr16, n: int) -> int:
    peak = 0
    for i in range(n):
        v = buf[i]
        if v & 0x8000:
            v = 0x10000 - v  # abs() of the sign-extended int16
        if v > peak:
            peak = v
    return peak


@micropython.viper
def _scale16(buf: ptr16, n: int, gain_q8: int):
    for i in range(n):
        v = buf[i]
        if v & 0x8000:
            v -= 0x10000
        v = (v * gain_q8) >> 8
        if v > 32767:
            v = 32767
        elif v < -32768:
            v = -32768
        buf[i] = v & 0xFFFF


def _normalize():
    n = _recorded_len // 2
    peak = _peak16(_audio, n)
    if peak == 0:
        return
    gain_q8 = min((NORMALIZE_TARGET << 8) // peak, MAX_GAIN << 8)
    if gain_q8 > 256:
        _scale16(_audio, n, gain_q8)


# The I2S completion callbacks are delivered via micropython.schedule(), whose queue is small
# and shared with LVGL's task handler and touch polling. Seen on hardware: when full, the
# callback is silently dropped and the transfer chain simply stops (playback stuck at 0,
# nothing raised). _refresh_ui acts as a watchdog -- if audio is active but hasn't progressed
# for STALL_MS (several chunks' worth), it re-invokes the callback to restart the chain.
STALL_MS = 500
_last_progress = 0
_stalls = 0  # REPL diagnostic: how many times the watchdog had to restart the chain


def _rx_cb(_arg):
    global _state, _pos, _recorded_len, _last_progress, _unsaved
    _last_progress = time.ticks_ms()
    _pos = min(_pos + CHUNK, len(_audio))
    if _pos < len(_audio):
        _i2s.readinto(_audio_mv[_pos:_pos + CHUNK])
    else:
        _i2s.deinit()
        touch._indev_drv.enable(True)
        _recorded_len = _pos
        _unsaved = True
        _state = PROCESSING  # _refresh_ui runs _process() once "Processing..." is on screen


def _tx_cb(_arg):
    global _state, _pos, _nflush, _last_progress
    _last_progress = time.ticks_ms()
    if _state == PLAYING:
        # _play_buf is safe to refill here: this callback means the previous write of it
        # completed (same single-buffer pattern as wavplayer.py).
        n = _play_src.readinto(_play_mv[:min(CHUNK, _play_len - _pos)]) if _pos < _play_len else 0
        if n:
            _pos += n
            _i2s.write(_play_mv[:n])
            return
        # Let the samples still queued in the I2S internal buffer drain before stopping, so
        # the end of the recording isn't cut off (same idea as wavplayer.py's FLUSH state).
        _play_src.close()
        _state = FLUSHING
        _nflush = I2S_IBUF // CHUNK + 1
    if _nflush > 0:
        _nflush -= 1
        _i2s.write(_silence)
    else:
        _i2s.deinit()
        amp_enable.value(1)
        _state = IDLE


def _check_stall():
    global _stalls
    if _state in (IDLE, PROCESSING, SAVING) or time.ticks_diff(time.ticks_ms(), _last_progress) < STALL_MS:
        return
    _stalls += 1
    if _state == RECORDING:
        _rx_cb(None)
    else:
        _tx_cb(None)


def start_recording():
    global _state, _pos, _last_progress
    if _state != IDLE:
        return
    _pos = 0
    # Pause touch polling for the duration of the recording. Each poll is an I2C read on the
    # bus shared with the codec (pins 38/39), and that traffic couples into the mic input:
    # measured on hardware, a silent recording had a ~0.5ms click every ~33ms (LVGL's indev
    # read period) peaking ~750 raw -- only ~9dB below speech, and heard as "static" once
    # normalized. With polling paused: zero clicks, peak 162. Re-enabled in _rx_cb.
    # (RECORD/PLAY taps are ignored while recording anyway.)
    touch._indev_drv.enable(False)
    _start_i2s(I2S.RX, I2S_DIN)
    _last_progress = time.ticks_ms()
    _state = RECORDING
    _i2s.irq(_rx_cb)  # switches the channel to non-blocking, IRQ-driven mode
    _i2s.readinto(_audio_mv[0:CHUNK])


def start_playback(name=None):
    """Streams /sd/<name>, or with name=None the last recording from RAM (no-SD-card case)."""
    global _state, _pos, _last_progress, _play_src, _play_len, _play_rate, _play_name
    global _idle_text, _idle_name
    if _state != IDLE:
        return
    if name is None:
        if _recorded_len == 0:
            return
        src, channels, length = _RamSource(_recorded_len), 1, _recorded_len
        name = "last recording"
    else:
        try:
            src, channels, length = _open_wav(name)
        except (OSError, ValueError) as e:
            print("Can't play", name, "-", e)
            _idle_text = str(e) if isinstance(e, ValueError) else "Can't open file"
            _idle_name = name
            return
    n = src.readinto(_play_mv[:min(CHUNK, length)])
    if n == 0:
        src.close()
        _idle_text, _idle_name = "Empty file", name
        return
    _play_src, _play_len, _play_name = src, length, name
    _play_rate = RATE * 2 * channels
    _pos = n
    amp_enable.value(0)
    _start_i2s(I2S.TX, I2S_DOUT, I2S.STEREO if channels == 2 else I2S.MONO)
    _last_progress = time.ticks_ms()
    _state = PLAYING
    _i2s.irq(_tx_cb)
    _i2s.write(_play_mv[:n])


def peak_level():
    """Peak absolute sample value of the last recording (0..32768) -- REPL diagnostic."""
    return _peak16(_audio, _recorded_len // 2)


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


title = _scaled_label(screen, "Voice Recorder", 0xFFFFFF, 2)
title.align(lv.ALIGN.TOP_MID, 0, 30)

# Status text lives in its own fixed-size strip so a text change only needs that strip
# redrawn (see _refresh_ui), not the whole screen.
status_box = _panel(screen, 0, 80, DISPLAY_WIDTH, 50, 0x000000)
status = _scaled_label(status_box, "Tap RECORD", 0xC0C0C0, 1.5)
status.center()

BAR_X = 20
BAR_Y = 150
BAR_W = DISPLAY_WIDTH - 2 * BAR_X
BAR_H = 20
bar_track = _panel(screen, BAR_X, BAR_Y, BAR_W, BAR_H, 0x303030)
bar_fill = _panel(bar_track, 0, 0, 0, BAR_H, 0xFFFFFF)

# Name of the file playing / last saved, unscaled (file names are too long for the 1.5x
# status line), in its own strip under the bar.
name_box = _panel(screen, 0, BAR_Y + BAR_H + 4, DISPLAY_WIDTH, 22, 0x000000)
name_label = lv.label(name_box)
name_label.set_style_text_color(lv.color_hex(0x808080), 0)
name_label.set_text("")
name_label.center()

BUTTON_Y = 200
BUTTON_H = DISPLAY_HEIGHT - BUTTON_Y
REC_COLOR = 0xC00000
PLAY_COLOR = 0x00A000
DIM_COLOR = 0x303030

rec_button = _panel(screen, 0, BUTTON_Y, DISPLAY_WIDTH // 2, BUTTON_H, REC_COLOR)
rec_button.add_flag(lv.obj.FLAG.CLICKABLE)
rec_button.add_event_cb(lambda _e: start_recording(), lv.EVENT.CLICKED, None)
_scaled_label(rec_button, "RECORD", 0xFFFFFF, 2).center()

play_button = _panel(screen, DISPLAY_WIDTH // 2, BUTTON_Y, DISPLAY_WIDTH // 2, BUTTON_H, DIM_COLOR)
play_button.add_flag(lv.obj.FLAG.CLICKABLE)
play_button.add_event_cb(lambda _e: open_picker(), lv.EVENT.CLICKED, None)
_scaled_label(play_button, "PLAY", 0xFFFFFF, 2).center()

# --- File picker: full-screen overlay listing the SD card's .wav files ---------------------
# Built on demand and deleted on close. Its text uses the native 16px font rather than the
# transform_scale trick: scaled labels inside a scrolling container would hit the stale-
# render invalidation problem described in CLAUDE.md.
PICKER_HEADER_H = 60
PICKER_ROW_H = 56
_picker = None


def open_picker():
    global _picker, _idle_text, _last_ui
    if _state != IDLE or _picker is not None:
        return
    if not _sd_ok:
        start_playback()  # no card: replay the last recording from RAM
        return
    try:
        names = _list_wavs()
    except OSError as e:
        print("Listing SD card failed:", e)
        _idle_text, _last_ui = "SD card error", None
        return

    _picker = _panel(screen, 0, 0, DISPLAY_WIDTH, DISPLAY_HEIGHT, 0x000000)
    _picker.set_style_text_font(lv.font_montserrat_16, 0)

    back = _panel(_picker, 0, 0, DISPLAY_WIDTH, PICKER_HEADER_H, DIM_COLOR)
    back.add_flag(lv.obj.FLAG.CLICKABLE)
    back.add_event_cb(lambda _e: close_picker(), lv.EVENT.CLICKED, None)
    for text, align, dx in (("<  Back", lv.ALIGN.LEFT_MID, 16),
                            ("Choose a file", lv.ALIGN.RIGHT_MID, -16)):
        label = lv.label(back)
        label.set_text(text)
        label.set_style_text_color(lv.color_hex(0xFFFFFF), 0)
        label.align(align, dx, 0)

    file_list = lv.list(_picker)
    file_list.set_size(DISPLAY_WIDTH, DISPLAY_HEIGHT - PICKER_HEADER_H)
    file_list.set_pos(0, PICKER_HEADER_H)
    file_list.set_style_bg_color(lv.color_hex(0x000000), 0)
    file_list.set_style_border_width(0, 0)
    file_list.set_style_radius(0, 0)
    file_list.set_style_pad_all(0, 0)
    file_list.set_style_pad_row(0, 0)
    if _unsaved and _recorded_len:
        # A failed save leaves the recording only in RAM -- still let it be played.
        row = file_list.add_button(None, "(unsaved) last recording")
        row.set_height(PICKER_ROW_H)
        row.set_style_bg_color(lv.color_hex(0x101010), 0)
        row.set_style_text_color(lv.color_hex(0xFFC000), 0)
        row.add_event_cb(lambda _e: _pick(None), lv.EVENT.CLICKED, None)
    elif not names:
        file_list.add_text("No .wav files on the SD card")
    for name in names:
        row = file_list.add_button(None, name)
        row.set_height(PICKER_ROW_H)
        row.set_style_bg_color(lv.color_hex(0x101010), 0)
        row.set_style_text_color(lv.color_hex(0xFFFFFF), 0)
        row.set_style_border_color(lv.color_hex(0x303030), 0)
        row.add_event_cb(lambda _e, n=name: _pick(n), lv.EVENT.CLICKED, None)


def close_picker():
    global _picker, _last_ui
    if _picker is None:
        return
    # delete_async: this runs inside an event callback of one of the picker's own children.
    _picker.delete_async()
    _picker = None
    _last_ui = None  # redraw the main screen's status/buttons next tick


def _pick(name):
    close_picker()
    start_playback(name)


_last_ui = None
PROCESSING_TEXT = "Processing..."
SAVING_TEXT = "Saving..."
_idle_text = "Tap RECORD"
_idle_name = ""  # file name shown under the bar while idle (last saved / last played)


def _refresh_ui(_timer):
    global _last_ui, _state, _idle_text, _idle_name
    _check_stall()
    if _picker is not None:
        return  # the picker covers the whole screen; nothing here is visible
    # Processing and saving each block the UI for ~1s, so each is deferred to here (rather than
    # run at the end of _rx_cb) and only runs once its status text was drawn on a previous tick.
    shown = _last_ui[0] if _last_ui is not None else None
    if _state == PROCESSING and shown == PROCESSING_TEXT:
        _process()
        _idle_text, _idle_name = "Ready", ""
        _state = SAVING if _sd_ok else IDLE
    elif _state == SAVING and shown == SAVING_TEXT:
        try:
            _save()
            _idle_text, _idle_name = "Saved", _last_saved
        except OSError as e:
            print("Saving recording failed:", e)
            _idle_text = "Save failed"
        _state = IDLE
    state, pos = _state, _pos
    name = _idle_name
    if state == PROCESSING:
        text = PROCESSING_TEXT
        fill = BAR_W
    elif state == SAVING:
        text = SAVING_TEXT
        fill = BAR_W
    elif state == RECORDING:
        remaining = (len(_audio) - pos) // (RATE * 2) + 1
        text = "Recording... %ds" % min(remaining, RECORD_SECONDS)
        fill = pos * BAR_W // len(_audio)
        name = ""
    elif state in (PLAYING, FLUSHING):
        remaining = (_play_len - pos) // _play_rate + 1
        text = "Playing... %d:%02d" % (remaining // 60, remaining % 60)
        fill = pos * BAR_W // _play_len
        name = _idle_name = _play_name
        _idle_text = "Played"
    else:
        text = _idle_text
        fill = 0
    idle = state == IDLE
    ui = (text, fill, idle, name)
    if ui == _last_ui:
        return
    last, _last_ui = _last_ui, ui
    bar_fill.set_width(fill)
    if last is None or last[3] != name:
        name_label.set_text(name)
        name_box.invalidate()
    if last is None or last[0] != text:
        status.set_text(text)
        # set_text() on a transform_scale'd label doesn't invalidate where an earlier,
        # differently sized render sat -- redraw its whole strip to avoid stale text (see
        # CLAUDE.md, ntp_clock.py). Only the strip, not the screen: full-screen redraws every
        # tick starve the audio IRQ callbacks (see I2S_IBUF).
        status_box.invalidate()
    if last is None or last[2] != idle:
        rec_button.set_style_bg_color(lv.color_hex(REC_COLOR if idle else DIM_COLOR), 0)
        play_button.set_style_bg_color(
            lv.color_hex(PLAY_COLOR if idle and (_sd_ok or _recorded_len) else DIM_COLOR), 0
        )


_ui_timer = lv.timer_create(_refresh_ui, 100, None)

# Drives lv.tick_inc()/lv.task_handler() on a background hardware timer, so the demo keeps
# rendering/responding to touch after this script finishes running.
task_handler = TaskHandler()

print("Voice recorder running. Tap RECORD to capture %ds, PLAY to pick a file." % RECORD_SECONDS)
