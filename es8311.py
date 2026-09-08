"""
Driver for the ES8311 low-power audio codec used for speaker playback on the FNK0104N (see
music_player.py for wiring). DAC/playback path only -- no mic/ADC support.

Register table originally ported from raptor09010's Micropython-ES8311-Library (MIT license,
github.com/raptor09010/Micropython-ES8311-Library), then corrected against Espressif's own
official ES8311 driver (espressif/esp-bsp, components/es8311/es8311.c, Apache-2.0 -- fetched
directly, not paraphrased, specifically to get its `coeff_div[]` clock-coefficient table and
`es8311_clock_config()`/`es8311_sample_frequency_config()` logic right) after on-hardware
testing showed real audio playing for only a few seconds before permanently cutting out with
no software-visible error.

Root cause: this firmware's machine.I2S has no mck= (see music_player.py's docstring), so an
earlier version of this file generated the codec's MCLK via machine.PWM instead -- a
technique that clearly locked onto SOMETHING briefly (real audio was heard) but didn't hold
up, consistent with a PWM-synthesized clock not being clean/stable enough for the codec's
internal PLL. Fixed by dropping the whole MCLK-pin approach and switching the ES8311 to derive
its clock from BCLK (the I2S bit clock) instead -- a real, ESP32-hardware-generated clock, not
software-PWM, and one the ES8311 explicitly supports as an alternate mode for exactly this
"no MCLK line" situation (register 0x01 bit 7, MCLK_SEL: 0 = from MCLK pin, 1 = from BCLK).

Comparing Espressif's real coeff_div[] table entries for 44100Hz against what was already in
the register tables below (tuned for the old MCLK-pin-at-256x-ratio approach) turned up a
lucky simplification: for this project's fixed case (44100Hz, 16-bit), the two configurations
share every clock-manager register (0x03-0x08) except two:
  - register 0x01 (clock source select): 0x3F -> 0xBF (sets bit 7, MCLK_SEL = BCLK)
  - register 0x02 (pre-divider/pre-multiplier): 0x00 -> 0x18 (pre_multi 0->3/8x, compensating
    for BCLK, at sample_rate*16*2 = 1,411,200 Hz, being 1/8th of the 11,289,600 Hz the old
    256x-ratio MCLK-pin table assumed)
This is specific to 44100Hz/16-bit (what mp3_to_wav.py produces) -- Espressif's coeff_div[]
table only tabulates a handful of "nice" MCLK/rate pairs (not every ratio for every rate), so
BCLK-as-source mode isn't guaranteed to have a matching table row at some other sample rate; a
real multi-rate player would need to either recompute/relookup this per file, or resample
everything to 44100Hz on the PC side first.

Confirmed on hardware (2026-09-08): demo1.wav played fully through the speaker with no cutout,
via music_player.py -- register values were already verified against Espressif's own official
driver math (not guessed), and playback now confirms it holds up in practice too. (Getting
audible output at all also required an unrelated fix -- an inverted amp-enable GPIO polarity
in music_player.py, see its module docstring -- worth knowing if this ever needs revisiting,
since a silent/no-output symptom here doesn't necessarily mean the codec/clock config is at
fault.)

power_on() now does a proper chip-level reset (0x1F -> 20ms delay -> 0x00 -> 0x80, matching
Espressif's own es8311_init()) before applying the rest of _POWER_UP, instead of a single
0x80 write. This chip is a separate I2C peripheral whose internal register state persists
across ESP32 resets (only the microcontroller reboots on a board reset/re-flash, not the
codec) -- across this driver's testing history it's been left in several different, some
non-working, configurations without ever going through a real reset in between, so a stuck
internal PLL/state-machine state (not cleared by re-writing only some registers) is a real
possibility worth ruling out.
"""

import time

from micropython import const  # NOQA

I2C_ADDR = const(0x18)  # NOQA

_VOLUME_REG = const(0x32)  # NOQA

# (register, value) pairs, applied in order, ~10ms apart (matches the source library), after
# power_on()'s explicit chip reset below. 0x00 is included here too (redundant with the reset
# dance's final 0x80 write, but harmless) so this table stays a complete, standalone record of
# every register this driver sets. 0x01/0x02 are the two registers changed from the source
# library's values -- see the module docstring for why (BCLK-as-clock-source instead of a
# PWM-driven MCLK pin).
_POWER_UP = (
    (0x00, 0x80), (0x01, 0xBF), (0x02, 0x18), (0x03, 0x10), (0x04, 0x10),
    (0x05, 0x00), (0x06, 0x03), (0x07, 0x00), (0x08, 0xFF), (0x09, 0x0C),
    (0x0A, 0x4C), (0x0B, 0x00), (0x0C, 0x00), (0x0D, 0x01), (0x0E, 0x02),
    (0x0F, 0x00), (0x10, 0x1F), (0x11, 0x7F), (0x12, 0x00), (0x13, 0x10),
    (0x14, 0x1A), (0x15, 0x40), (0x16, 0x24), (0x17, 0xBF), (0x18, 0x00),
    (0x19, 0x00), (0x1A, 0x00), (0x1B, 0x0A), (0x1C, 0x6A),
    (0x32, 0x9F), (0x37, 0x08), (0x44, 0x50),
)
_POWER_DOWN = (
    (0x00, 0x1F), (0x01, 0x00), (0x02, 0x00), (0x03, 0x10), (0x04, 0x10),
    (0x05, 0x00), (0x06, 0x03), (0x07, 0x00), (0x08, 0xFF), (0x09, 0x00),
    (0x0A, 0x00), (0x0B, 0x00), (0x0C, 0x20), (0x0D, 0xFC), (0x0E, 0x6A),
    (0x0F, 0x00), (0x10, 0x13), (0x11, 0x7C), (0x12, 0x02), (0x13, 0x40),
    (0x14, 0x10), (0x15, 0x00), (0x16, 0x04), (0x17, 0x00), (0x18, 0x00),
    (0x19, 0x00), (0x1A, 0x00), (0x1B, 0x0C), (0x1C, 0x4C),
    (0x32, 0x00), (0x37, 0x08), (0x44, 0x00),
)


class ES8311:

    def __init__(self, i2c, addr=I2C_ADDR):
        self._i2c = i2c
        self._addr = addr

    def _write(self, reg, val):
        self._i2c.writeto_mem(self._addr, reg, bytes((val,)))

    def power_on(self):
        # Proper chip-level reset first -- see the module docstring for why this matters here.
        # Matches Espressif's own es8311_init(): full reset, settle, then the power-on command.
        self._write(0x00, 0x1F)
        time.sleep_ms(20)  # NOQA
        self._write(0x00, 0x00)
        time.sleep_ms(10)  # NOQA

        for reg, val in _POWER_UP:
            self._write(reg, val)
            time.sleep_ms(10)  # NOQA

    def power_off(self):
        for reg, val in _POWER_DOWN:
            self._write(reg, val)
            time.sleep_ms(10)  # NOQA

    def set_volume(self, percent):
        percent = max(0, min(100, percent))
        self._write(_VOLUME_REG, int(255 * percent / 100))
