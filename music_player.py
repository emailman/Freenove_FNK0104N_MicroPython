"""
Plays a WAV file from the FNK0104N's microSD card over its onboard ES8311 codec + speaker.
Starting point for a future full SD-card music player (see README) -- for now it just plays
demo1.wav on import/run.

Why WAV and not the original demo1.mp3: this project's firmware has no MP3 decoder.
MicroPython/ESP32 doesn't ship one, and this repo's custom lvgl_micropython build doesn't
bundle one either (checked: no MP3-related source anywhere in that build, and nothing freezes
one into the manifest). Real-time MP3 decoding on-device would need a native C decoder (e.g.
libhelix-mp3) compiled into firmware -- a genuine firmware rebuild, not a loose .py file like
everything else in this project. So demo1.mp3 is decoded to demo1.wav ahead of time on the PC
(see mp3_to_wav.py) and only the WAV is played on-device. Copy demo1.wav onto the SD card's
root (e.g. via a card reader -- much faster than pushing ~4.7MB over WebREPL/serial) before
running this.

Wiring -- pins are FNK0104N-specific values from docs.freenove.com's SD Card and Music
chapters (docs.freenove.com/projects/fnk0104/en/latest/fnk0104/codes/MAIN/6_SD_Card.html and
.../7_Music.html); OTHER FNK0104 variants (AB/S) use different pins entirely, so don't reuse
this file's constants for those boards. Unlike st77922.py/st77922_touch.py, there's no
Freenove FNK0104N C++ source file to cross-check these against -- treat the pin numbers as
the first thing to suspect if playback is silent or garbled. (AMP_ENABLE_PIN's polarity was
one such bad assumption -- see below; now fixed and confirmed on hardware.)
    SD card (SDMMC, 4-bit):  CLK=5  CMD=4  D0=6  D1=7  D2=2  D3=3
    I2S (ESP32-S3 -> ES8311 DIN): BCK=18  WS=21  SD=15  (MCK=17 exists in hardware/Freenove's
                              own wiring but is UNUSED by this driver -- see es8311.py: the
                              codec derives its clock from BCK instead, not a dedicated MCLK
                              line, since this firmware's machine.I2S can't drive one anyway)
    ES8311 control (I2C):    SCL=39  SDA=38, addr 0x18 -- same bus/pins as the touch
                              controller (addr 0x55, see st77922_touch.py); different
                              addresses so sharing the bus should be fine, just not tested
                              with both devices live at once yet.
    Speaker amp enable:      GPIO 1, active-LOW. Originally assumed active-high (docs.freenove.
                              com didn't document polarity) -- that guess was wrong and was the
                              actual root cause of an earlier "codec config verified byte-
                              perfect via full register dump, I2S pipeline verified healthy,
                              still total silence" mystery: the amp was being explicitly
                              disabled the whole time by the old code's `.value(1)`. Found by
                              isolating the amp-enable pin with a synthetic tone generated
                              directly over I2S (bypassing SD/WAV entirely) in a temporary
                              diagnostic script, since removed -- see the
                              fnk0104n-music-player-audio-debug memory for the full debugging
                              history if this area regresses again.

This script opens its own machine.I2C(0, ...) on the touch bus's pins -- fine standalone, but
if this is ever combined with hello_world_display.py/touch_led_colors.py in one running
process, that touch bus should be reused (host 0 can't be opened twice) rather than opened
again here; see hello_world_display.py's touch_i2c_bus for the existing instance to share.

Unlike the display/touch scripts, this one only uses plain machine.I2C/I2S/SDCard/Pin -- no
lvgl/lcd_bus, no frozen modules from this project's custom firmware -- so it should run on
stock MicroPython too, not just this project's custom lvgl_micropython build.

Per the project's general hardware-state gotcha (see CLAUDE.md): hard-reset the board before
re-running this if a previous attempt raised partway through -- SDCard/I2S are native ESP-IDF
resources too, same as the display's QSPI bus.

Confirmed working end-to-end on hardware (2026-09-08): demo1.wav played fully through the
onboard speaker at DEFAULT_VOLUME. Five real, non-obvious issues had to be found and fixed via
on-hardware testing to get here (the last one, amp polarity, is what actually closed it out --
see the wiring section above; the other four are documented in CLAUDE.md too since they're easy
to hit again if this area of the firmware gets touched):
  - machine.SDCard has a NON-STANDARD, project-specific constructor on this firmware:
    lvgl_micropython overlays its own machine_sdcard.c (micropy_updates/esp32/machine_sdcard.c
    in the WSL clone) over upstream MicroPython's. SD/MMC-mode kwargs are `slot`, `width`,
    `clk` (not `sck`), `cmd`, `data_pins` (not `data`) -- all plain ints.
  - machine.I2S has NO mck= support on this firmware -- this ESP32 port is built against
    ports/esp32/machine_i2s.c (an ESP32-specific file, per MICROPY_PY_MACHINE_I2S_INCLUDEFILE
    in mpconfigport.h), not the generic extmod/machine_i2s.c other MicroPython ports use,
    which is the one that has mck=.
  - A PWM-generated MCLK (the initial workaround for the above) let the codec lock onto audio
    briefly, then it silently, permanently lost lock a few seconds in, with the whole software
    stack (I2S IRQ callback, SD reads) confirmed still running fine throughout -- consistent
    with the synthesized clock not being clean/stable enough for the codec's PLL. Fixed by
    switching the ES8311 to derive its clock from BCLK instead of a dedicated MCLK line
    (see es8311.py) -- eliminates the PWM/MCLK pin entirely.
  - Switching to BCLK-as-clock-source made things WORSE at first (total silence, not even the
    old PWM version's brief burst) until play() below was reordered: WavPlayer.play() (which
    creates the I2S channel and starts BCLK actually toggling) now runs BEFORE
    codec.power_on() (which configures the codec's clock manager to derive its clock FROM
    BCLK) instead of after. Makes sense in hindsight -- there's nothing for the codec to lock
    onto if its clock source isn't running yet when it's told to start deriving from it.
"""

import os
import time

import machine

from es8311 import ES8311
from wavplayer import WavPlayer

SD_CLK = 5
SD_CMD = 4
SD_DATA = (6, 7, 2, 3)  # D0, D1, D2, D3
SD_MOUNT = "/sd"

I2S_ID = 0
I2S_BCK = 18
I2S_WS = 21
I2S_SD = 15
I2S_IBUF = 20000

CODEC_I2C_SCL = 39
CODEC_I2C_SDA = 38
AMP_ENABLE_PIN = 1

DEFAULT_WAV = "demo1.wav"
DEFAULT_VOLUME = 70  # 0-100


def _mount_sd():
    if SD_MOUNT.strip("/") in os.listdir("/"):
        return  # already mounted (e.g. re-running from the REPL without a reset)
    sd = machine.SDCard(
        slot=1,
        width=4,
        clk=SD_CLK,
        cmd=SD_CMD,
        data_pins=SD_DATA,
    )
    os.mount(sd, SD_MOUNT)


def play(path=DEFAULT_WAV, volume=DEFAULT_VOLUME, loop=False):
    """Mounts the SD card (if not already), starts I2S, then powers up the codec.

    Non-blocking -- playback continues via I2S IRQ after this returns. Returns the
    (WavPlayer, ES8311) pair so the caller can pause()/stop()/set_volume() etc.
    """
    _mount_sd()

    amp_enable = machine.Pin(AMP_ENABLE_PIN, machine.Pin.OUT)
    amp_enable.value(0)  # active-LOW -- confirmed via _tone_test.py, see module docstring

    player = WavPlayer(
        id=I2S_ID,
        sck_pin=I2S_BCK,
        ws_pin=I2S_WS,
        sd_pin=I2S_SD,
        ibuf=I2S_IBUF,
        root=SD_MOUNT,
    )
    # Start I2S -- and so BCLK actually toggling -- BEFORE configuring the codec, not after.
    # es8311.py's BCLK-as-clock-source mode needs a real clock present to lock onto; the
    # codec's power_on() has nothing to derive a clock from until this has run. See
    # es8311.py's module docstring.
    player.play(path, loop=loop)
    time.sleep_ms(50)  # NOQA -- let BCLK settle before the codec tries to lock onto it

    # Raw ints here too, though machine.I2C (unlike SDCard) is unmodified upstream and
    # accepts machine.Pin objects just as well -- see the module docstring.
    i2c_bus = machine.I2C(0, scl=CODEC_I2C_SCL, sda=CODEC_I2C_SDA, freq=100_000)
    # es8311.py's register table is specific to 44100Hz/16-bit (what mp3_to_wav.py produces --
    # DEFAULT_WAV matches) -- see its module docstring for why a different rate would need a
    # different table, now that the codec's clock is derived from BCLK instead of a
    # dynamically-configurable MCLK.
    codec = ES8311(i2c_bus)
    codec.power_on()
    codec.set_volume(volume)

    return player, codec


_player, _codec = play()
print("Playing %s -- call music_player.play(<other file>) to switch tracks." % DEFAULT_WAV)
