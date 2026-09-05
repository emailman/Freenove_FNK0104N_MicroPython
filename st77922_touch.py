"""
Driver for the ST77922's integrated capacitive touch controller, as wired on the Freenove
FNK0104N's onboard 3.5" display.

Ported from Freenove's own ST77922_Touch.h/.cpp (Arduino TFT_eSPI library, found in
Libraries/FNK0104N/TFT_eSPI_v2.5.43.zip in github.com/Freenove/Freenove_ESP32_S3_Display) --
same approach as st77922.py's port of the display half. Built on lvgl_micropython's
pointer_framework.PointerDriver, following the pattern of its bundled I2C touch drivers
(e.g. cst816s.py).

Touch is a separate I2C peripheral from the QSPI display bus, with its own reset/interrupt
pins (from Freenove's own ST77922_Touch.h):
    I2C addr=0x55 (16-bit register addresses)  SCL=39  SDA=38  RST=48  INT=47
INT is configured as an input (matching Freenove's own pinMode call) but not used for
edge-triggered reads -- like Freenove's own Get_Touch(), this driver just polls the
touch-info register, which is how LVGL's indev framework drives it anyway.

Register-level behavior below is confirmed against Sitronix's own protocol spec ("Sitronix
TDDI ST77922 Touch Screen Controller Interface Protocol", v01.00, 2023/07/25 -- ships as
Datasheet/ST77922_TDDI_Interface_Protocol_V01.00.pdf in this project, pulled from Freenove's
own GitHub repo's Datasheet/ folder), not just inferred from Freenove's C++ source or
hardware trial-and-error, though it was independently confirmed on hardware first (raw
register + INT-pin polling) before this datasheet turned up.

IMPORTANT: every poll reads the *entire* `7 * max_points`-byte point-data block starting at
TOUCH_POINT0, even though only point 0 is used below. Per the spec's Reporting Table section:
once register 0x0010's "With Coord." bit (0x08) is set, the host "must read the last Reg.
addr. of the last supported coordinate" for the controller to clear that bit and de-assert
INT -- e.g. for max_touches=5, reading through to register 0x0036 (= TOUCH_POINT0 + 7*5 - 1,
the last byte of point 4's slot). Reading fewer bytes leaves "With Coord." set (and INT
asserted) indefinitely, even long after the finger is lifted -- which is exactly the
"permanently latched touched" symptom this was debugged from. Freenove's own Get_Touch()
always reads the full `7 * max_points` block for the same reason.
"""

import time

from micropython import const  # NOQA
import machine  # NOQA

import pointer_framework

I2C_ADDR = const(0x55)  # NOQA
BITS = const(16)  # NOQA -- register address width, for I2C.Device(reg_bits=...)

_STATUS = const(0x0001)  # NOQA
_MAX_TOUCHES = const(0x0009)  # NOQA
_TOUCH_INFO = const(0x0010)  # NOQA
_TOUCH_POINT0 = const(0x0014)  # NOQA

# Register 0x0001's low nibble is a `Device Status` enum (0=Normal, 1=Init, 2=Error,
# 3=SmartWakeup, 4=Idle, 5=Power Down), not a generic busy bitmask -- masking with 0x0F and
# waiting for it to hit 0 works because Normal is enum value 0.
_STATUS_NOT_NORMAL_MASK = const(0x0F)  # NOQA

# Register 0x0010 ("Advanced Touch Info."): bit 7 = RstChip ("notifies host that the touch
# controller should be reset"), bit 3 = "With Coord." (coordinates were updated).
_TOUCH_INFO_RST_CHIP = const(0x80)  # NOQA
_TOUCH_INFO_WITH_COORD = const(0x08)  # NOQA

# TOUCH_POINTn byte 0: bit 7 = Valid_n, bits 5:0 = high bits of X_n.
_POINT_VALID = const(0x80)  # NOQA


class ST77922Touch(pointer_framework.PointerDriver):

    def __init__(
        self,
        device,
        reset_pin=None,
        int_pin=None,
        touch_cal=None,
        startup_rotation=pointer_framework.lv.DISPLAY_ROTATION._0,  # NOQA
        debug=False,
    ):
        self._device = device
        self._status_buf = bytearray(1)
        self._status_mv = memoryview(self._status_buf)

        if reset_pin is None or not isinstance(reset_pin, int):
            self._reset_pin = reset_pin
        else:
            self._reset_pin = machine.Pin(reset_pin, machine.Pin.OUT)

        if isinstance(int_pin, int):
            # Matches Freenove's `pinMode(TOUCH_INT, INPUT)` -- wired but unused by this
            # polling-based driver.
            machine.Pin(int_pin, machine.Pin.IN)

        self.hw_reset()

        # Wait for Device Status to reach Normal (0) -- the spec puts this at 20ms typical,
        # bounded here to 1s so a wiring problem prints instead of hanging forever. Matches
        # Freenove's own `do { Read_Data(STATUS, ...) } while (data & 0x0F)`.
        for _ in range(200):
            self._device.read_mem(_STATUS, buf=self._status_mv)
            if not (self._status_buf[0] & _STATUS_NOT_NORMAL_MASK):
                break
            time.sleep_ms(5)  # NOQA
        else:
            print('ST77922Touch: controller did not report ready, continuing anyway')

        self._device.read_mem(_MAX_TOUCHES, buf=self._status_mv)
        self.max_points = self._status_buf[0] or 1

        # Sized for the *entire* point-data block -- see the module docstring for why this
        # must always be read in full, even though only point 0 (the first 7 bytes) is used.
        self._point_buf = bytearray(7 * self.max_points)
        self._point_mv = memoryview(self._point_buf)

        super().__init__(
            touch_cal=touch_cal, startup_rotation=startup_rotation, debug=debug
        )

    def hw_reset(self):
        if self._reset_pin is None:
            return

        self._reset_pin(0)
        time.sleep_ms(100)  # NOQA
        self._reset_pin(1)
        time.sleep_ms(100)  # NOQA

    def _get_coords(self):
        self._device.read_mem(_TOUCH_INFO, buf=self._status_mv)
        info = self._status_buf[0]

        if info & _TOUCH_INFO_RST_CHIP:
            # Spec: "notifies host that the touch controller should be reset." Expected to
            # be rare -- hw_reset()'s ~200ms of blocking sleep is acceptable here even though
            # this runs from LVGL's indev poll (via TaskHandler's scheduled callback).
            self.hw_reset()
            return None

        if not (info & _TOUCH_INFO_WITH_COORD):
            return None

        # The full block (all max_points contacts) must be read every time -- see the
        # module docstring -- even though only point 0's first 4 bytes are used below.
        self._device.read_mem(_TOUCH_POINT0, buf=self._point_mv)
        data = self._point_buf
        if not (data[0] & _POINT_VALID):
            return None

        x = ((data[0] & 0x3F) << 8) | data[1]
        y = ((data[2] & 0x3F) << 8) | data[3]

        return self.PRESSED, x, y
