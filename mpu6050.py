"""
Minimal I2C driver for an InvenSense MPU-6050 accelerometer/gyroscope (the GY-521 module).

Wiring: the "I2C" header (P4: 1=3V3 2=GND 3=SCL/GPIO39 4=SDA/GPIO38), same bus as the codec.
GY-521 pins: VCC, GND, SCL, SDA; AD0 low or open (the module pulls it down) gives address
0x68, AD0 high gives 0x69. XDA/XCL/INT aren't needed.

Works with machine.I2C or the touch driver's shared i2c.I2C.Bus (it only uses readfrom_mem and
writeto_mem, like bme280.py and ads1115.py).
"""

import math
import struct
import time

_SMPLRT_DIV = 0x19
_CONFIG = 0x1A
_GYRO_CONFIG = 0x1B
_ACCEL_CONFIG = 0x1C
_ACCEL_XOUT_H = 0x3B  # 14 bytes: accel X/Y/Z, temp, gyro X/Y/Z, big-endian signed
_PWR_MGMT_1 = 0x6B
_WHO_AM_I = 0x75

# Full-scale range -> (config bits, LSB per unit)
ACCEL_RANGE = {2: (0x00, 16384.0), 4: (0x08, 8192.0), 8: (0x10, 4096.0), 16: (0x18, 2048.0)}
GYRO_RANGE = {250: (0x00, 131.0), 500: (0x08, 65.5), 1000: (0x10, 32.8), 2000: (0x18, 16.4)}


class MPU6050:
    def __init__(self, i2c, address=0x68, accel_g=2, gyro_dps=250, dlpf=3):
        """accel_g: 2/4/8/16. gyro_dps: 250/500/1000/2000. dlpf: digital low-pass setting
        0..6 (3 = ~44 Hz bandwidth)."""
        self.i2c = i2c
        self.address = address
        self.who_am_i = i2c.readfrom_mem(address, _WHO_AM_I, 1)[0]  # 0x68 on genuine parts
        self._w(_PWR_MGMT_1, 0x80)  # device reset
        time.sleep_ms(100)
        self._w(_PWR_MGMT_1, 0x01)  # wake, clock from the X gyro PLL (more stable than internal)
        time.sleep_ms(10)
        self._w(_CONFIG, dlpf & 0x07)
        self._w(_SMPLRT_DIV, 9)  # 1 kHz / (1 + 9) = 100 Hz with the DLPF on
        bits, self._accel_lsb = ACCEL_RANGE[accel_g]
        self._w(_ACCEL_CONFIG, bits)
        bits, self._gyro_lsb = GYRO_RANGE[gyro_dps]
        self._w(_GYRO_CONFIG, bits)
        self.gyro_bias = (0.0, 0.0, 0.0)
        self.accel_bias = (0.0, 0.0, 0.0)
        time.sleep_ms(50)

    def _w(self, reg, val):
        self.i2c.writeto_mem(self.address, reg, bytes((val,)))

    def read_raw(self):
        """(ax, ay, az, temp, gx, gy, gz) as signed counts."""
        return struct.unpack(">7h", self.i2c.readfrom_mem(self.address, _ACCEL_XOUT_H, 14))

    def read(self):
        """((ax, ay, az) in g minus accel_bias, temp in degC, (gx, gy, gz) in deg/s minus
        gyro_bias)."""
        ax, ay, az, t, gx, gy, gz = self.read_raw()
        a = self._accel_lsb
        g = self._gyro_lsb
        ab = self.accel_bias
        gb = self.gyro_bias
        return ((ax / a - ab[0], ay / a - ab[1], az / a - ab[2]), t / 340 + 36.53,
                (gx / g - gb[0], gy / g - gb[1], gz / g - gb[2]))

    def calibrate(self, samples=100, zero_level=True):
        """Average readings at rest (about 1 s at 100 Hz) and store the gyro's zero offset in
        gyro_bias. Keep the module still.

        With zero_level, also store accel_bias so that the current position reads as level:
        X = Y = 0 and Z = exactly +1 g (or -1 g if the module is upside down). Keep the module
        flat as well. This corrects the accelerometer's offsets for that position; it doesn't
        correct gain errors, so readings at large tilts can still be off by a few percent.
        Returns (gyro_bias, accel_bias)."""
        s = [0] * 7
        for _ in range(samples):
            r = self.read_raw()
            for i in range(7):
                s[i] += r[i]
            time.sleep_ms(10)  # one sample period at 100 Hz
        g = self._gyro_lsb * samples
        self.gyro_bias = (s[4] / g, s[5] / g, s[6] / g)
        if zero_level:
            a = self._accel_lsb * samples
            ax, ay, az = s[0] / a, s[1] / a, s[2] / a
            self.accel_bias = (ax, ay, az - (1.0 if az >= 0 else -1.0))
        return self.gyro_bias, self.accel_bias

    @staticmethod
    def tilt(accel):
        """(pitch, roll) in degrees from an accelerometer reading, valid while not
        accelerating. Pitch is rotation about Y (X axis tipping up), roll about X."""
        ax, ay, az = accel
        pitch = math.degrees(math.atan2(-ax, math.sqrt(ay * ay + az * az)))
        roll = math.degrees(math.atan2(ay, az))
        return pitch, roll
