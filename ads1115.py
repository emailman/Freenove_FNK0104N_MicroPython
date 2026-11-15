"""
Minimal I2C driver for a TI ADS1115 16-bit ADC module.

Wiring: the "I2C" header (P4: 1=3V3 2=GND 3=SCL/GPIO39 4=SDA/GPIO38). With the module's ADDR
pin tied to GND (the usual default on breakout boards) the address is 0x48; ADDR to VDD, SDA
or SCL gives 0x49, 0x4A or 0x4B.

Each read is a single-shot conversion: write the config register with OS=1, wait for the OS
bit to come back 1 (conversion done), then read the signed 16-bit conversion register. Works
with machine.I2C or the touch driver's shared i2c.I2C.Bus (it only uses readfrom_mem and
writeto_mem, like bme280.py).

Note the input range: the PGA setting only scales the measurement. No input may go above VDD
+ 0.3V or below GND - 0.3V, whatever the PGA range. Powered from 3V3, that means 0..3.3V.
"""

import time

_REG_CONVERSION = 0x00
_REG_CONFIG = 0x01

# PGA full-scale range in volts -> config bits 11:9
PGA = {
    6.144: 0x0000,
    4.096: 0x0200,
    2.048: 0x0400,  # power-on default
    1.024: 0x0600,
    0.512: 0x0800,
    0.256: 0x0A00,
}

# Data rate in samples/s -> config bits 7:5
RATE = {8: 0x0000, 16: 0x0020, 32: 0x0040, 64: 0x0060, 128: 0x0080,
        250: 0x00A0, 475: 0x00C0, 860: 0x00E0}

# Single-ended channel -> MUX bits 14:12 (AINx vs GND)
_MUX_SINGLE = (0x4000, 0x5000, 0x6000, 0x7000)

_OS_START = 0x8000
_MODE_SINGLE = 0x0100
_COMP_DISABLE = 0x0003


class ADS1115:
    def __init__(self, i2c, address=0x48, fsr=4.096, rate=128):
        self.i2c = i2c
        self.address = address
        self.fsr = fsr
        self._pga = PGA[fsr]
        self._rate = RATE[rate]
        self._timeout_ms = 2 + 2000 // rate  # a few conversion times
        self._buf = bytearray(2)

    def _write(self, reg, value):
        self._buf[0] = value >> 8
        self._buf[1] = value & 0xFF
        self.i2c.writeto_mem(self.address, reg, self._buf)

    def _read(self, reg):
        b = self.i2c.readfrom_mem(self.address, reg, 2)
        return (b[0] << 8) | b[1]

    def read_raw(self, channel=0):
        """Single-ended reading of AIN<channel> vs GND, as a signed count (-32768..32767)."""
        self._write(_REG_CONFIG, _OS_START | _MUX_SINGLE[channel] | self._pga
                    | _MODE_SINGLE | self._rate | _COMP_DISABLE)
        start = time.ticks_ms()
        while not self._read(_REG_CONFIG) & _OS_START:
            if time.ticks_diff(time.ticks_ms(), start) > self._timeout_ms:
                raise OSError("ADS1115 conversion timeout")
            time.sleep_ms(1)
        raw = self._read(_REG_CONVERSION)
        return raw - 0x10000 if raw & 0x8000 else raw

    def to_volts(self, raw):
        return raw * self.fsr / 32768

    def read_volts(self, channel=0):
        return self.to_volts(self.read_raw(channel))
