"""
Bosch BME280 temperature / humidity / pressure sensor driver (I2C).

Wiring on the FNK0104N: the 4-pin "I2C" header (P4): 1=3V3, 2=GND, 3=SCL (GPIO 39),
4=SDA (GPIO 38). Tie CSB to 3V3 (I2C mode) and SDO to GND (address 0x76) or 3V3 (0x77),
unless the breakout already has pull resistors on them.

The bus is shared with the touch controller (0x55) and the ES8311 codec (0x18). In a
script that also uses touch, pass the touch driver's `i2c.I2C.Bus` instead of opening
I2C host 0 again (it can't be opened twice). The driver only needs `readfrom_mem` and
`writeto_mem`, which both `machine.I2C` and `i2c.I2C.Bus` have.

Uses forced mode: each read() starts one measurement (x1 oversampling on all three,
IIR filter off, ~10 ms) and the sensor sleeps in between, which is Bosch's recommended
setting for weather monitoring and keeps self-heating to a minimum.

The compensation math is Bosch's integer reference code from the datasheet
(BME280_compensate_T_int32 / P_int64 / H_int32). MicroPython's float is only 32-bit on
the ESP32, too coarse for the datasheet's double-precision pressure formula, while its
ints are arbitrary precision, so the 64-bit integer version runs exactly.

    from machine import I2C, Pin
    import bme280
    sensor = bme280.BME280(I2C(0, scl=Pin(39), sda=Pin(38), freq=400000))
    t, p, h = sensor.read()        # degC, hPa, %RH
    print(sensor.read_f())         # degF, inHg, %RH
"""

import time

ADDR_SDO_LOW = 0x76
ADDR_SDO_HIGH = 0x77

_CHIP_ID = 0x60

_REG_CALIB_TP = 0x88   # 0x88..0xA1, 26 bytes (T1..P9, then 0xA0 unused, 0xA1 = H1)
_REG_CHIP_ID = 0xD0
_REG_RESET = 0xE0
_REG_CALIB_H = 0xE1    # 0xE1..0xE7, 7 bytes
_REG_CTRL_HUM = 0xF2
_REG_STATUS = 0xF3
_REG_CTRL_MEAS = 0xF4
_REG_CONFIG = 0xF5
_REG_DATA = 0xF7       # 0xF7..0xFE, 8 bytes: press[3], temp[3], hum[2]

_OSRS_X1 = 1
_MODE_SLEEP = 0
_MODE_FORCED = 1


def _u16(b, i):
    return b[i] | (b[i + 1] << 8)


def _s16(b, i):
    v = _u16(b, i)
    return v - 65536 if v & 0x8000 else v


def _s8(v):
    return v - 256 if v & 0x80 else v


def _cdiv(a, b):
    """C-style integer division (truncates toward zero; Python's // floors)."""
    q = abs(a) // abs(b)
    return q if (a < 0) == (b < 0) else -q


class BME280:
    def __init__(self, i2c, addr=ADDR_SDO_LOW):
        self._i2c = i2c
        self._addr = addr
        chip = self._read(_REG_CHIP_ID, 1)[0]
        if chip != _CHIP_ID:
            # 0x56-0x58 is a BMP280, which has no humidity sensor.
            raise OSError('no BME280 at 0x%02x (chip id 0x%02x)' % (addr, chip))

        self._write(_REG_RESET, 0xB6)
        time.sleep_ms(5)
        while self._read(_REG_STATUS, 1)[0] & 0x01:   # im_update: NVM copy in progress
            time.sleep_ms(2)

        self._load_calibration()

        # ctrl_hum only takes effect after a later write to ctrl_meas.
        self._write(_REG_CTRL_HUM, _OSRS_X1)
        self._write(_REG_CONFIG, 0x00)                 # filter off, no SPI 3-wire
        self._ctrl_meas = (_OSRS_X1 << 5) | (_OSRS_X1 << 2)
        self._write(_REG_CTRL_MEAS, self._ctrl_meas | _MODE_SLEEP)

    def _read(self, reg, n):
        return self._i2c.readfrom_mem(self._addr, reg, n)

    def _write(self, reg, val):
        self._i2c.writeto_mem(self._addr, reg, bytes((val,)))

    def _load_calibration(self):
        c = self._read(_REG_CALIB_TP, 26)
        self._T1 = _u16(c, 0)
        self._T2 = _s16(c, 2)
        self._T3 = _s16(c, 4)
        self._P1 = _u16(c, 6)
        self._P2 = _s16(c, 8)
        self._P3 = _s16(c, 10)
        self._P4 = _s16(c, 12)
        self._P5 = _s16(c, 14)
        self._P6 = _s16(c, 16)
        self._P7 = _s16(c, 18)
        self._P8 = _s16(c, 20)
        self._P9 = _s16(c, 22)
        self._H1 = c[25]

        h = self._read(_REG_CALIB_H, 7)
        self._H2 = _s16(h, 0)
        self._H3 = h[2]
        # H4/H5 are signed 12-bit values split across 0xE4..0xE6 (0xE5 holds a nibble of each).
        self._H4 = (_s8(h[3]) << 4) | (h[4] & 0x0F)
        self._H5 = (_s8(h[5]) << 4) | (h[4] >> 4)
        self._H6 = _s8(h[6])

    def read_raw(self):
        """Run one forced measurement; return (adc_T, adc_P, adc_H)."""
        self._write(_REG_CTRL_MEAS, self._ctrl_meas | _MODE_FORCED)
        time.sleep_ms(10)      # max measurement time at x1/x1/x1 is 9.3 ms
        for _ in range(20):
            if not self._read(_REG_STATUS, 1)[0] & 0x08:   # measuring
                break
            time.sleep_ms(2)
        d = self._read(_REG_DATA, 8)
        adc_p = (d[0] << 12) | (d[1] << 4) | (d[2] >> 4)
        adc_t = (d[3] << 12) | (d[4] << 4) | (d[5] >> 4)
        adc_h = (d[6] << 8) | d[7]
        return adc_t, adc_p, adc_h

    def _comp_t(self, adc_t):
        """Return (centi-degC, t_fine)."""
        v1 = (((adc_t >> 3) - (self._T1 << 1)) * self._T2) >> 11
        x = (adc_t >> 4) - self._T1
        v2 = (((x * x) >> 12) * self._T3) >> 14
        t_fine = v1 + v2
        return (t_fine * 5 + 128) >> 8, t_fine

    def _comp_p(self, adc_p, t_fine):
        """Return pressure in Pa * 256 (Q24.8), or 0 if invalid."""
        v1 = t_fine - 128000
        v2 = v1 * v1 * self._P6
        v2 += (v1 * self._P5) << 17
        v2 += self._P4 << 35
        v1 = ((v1 * v1 * self._P3) >> 8) + ((v1 * self._P2) << 12)
        v1 = (((1 << 47) + v1) * self._P1) >> 33
        if v1 == 0:
            return 0
        p = 1048576 - adc_p
        p = _cdiv(((p << 31) - v2) * 3125, v1)
        v1 = (self._P9 * (p >> 13) * (p >> 13)) >> 25
        v2 = (self._P8 * p) >> 19
        return ((p + v1 + v2) >> 8) + (self._P7 << 4)

    def _comp_h(self, adc_h, t_fine):
        """Return relative humidity in %RH * 1024 (Q22.10)."""
        v = t_fine - 76800
        v = ((((adc_h << 14) - (self._H4 << 20) - (self._H5 * v)) + 16384) >> 15) * \
            (((((((v * self._H6) >> 10) * (((v * self._H3) >> 11) + 32768)) >> 10)
               + 2097152) * self._H2 + 8192) >> 14)
        v -= (((((v >> 15) * (v >> 15)) >> 7) * self._H1) >> 4)
        v = min(max(v, 0), 419430400)
        return v >> 12

    def read(self):
        """Return (temperature degC, pressure hPa, humidity %RH)."""
        adc_t, adc_p, adc_h = self.read_raw()
        t100, t_fine = self._comp_t(adc_t)
        p = self._comp_p(adc_p, t_fine)
        h = self._comp_h(adc_h, t_fine)
        return t100 / 100, p / 25600, h / 1024

    def read_f(self):
        """Return (temperature degF, pressure inHg, humidity %RH)."""
        t, p, h = self.read()
        return t * 9 / 5 + 32, p * 0.0295300, h

    @staticmethod
    def sea_level_hpa(p_hpa, altitude_m):
        """Convert station pressure to sea-level pressure (what weather reports show)."""
        return p_hpa / (1 - altitude_m / 44330) ** 5.255
