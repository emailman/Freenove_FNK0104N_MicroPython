# ST77922 QSPI display driver for lvgl_micropython, ported from Freenove's
# Freenove_ESP32_S3_Display Arduino library (TFT_eSPI_v2.5.43.zip,
# TFT_eSPI/ST77922.h + ST77922.cpp) for the FNK0104N (3.5", 320x480).
#
# Structurally this is a straight copy of the framework's own st77916.py
# driver (same chip family, same QSPI wire protocol: 1-wire opcode 0x02 for
# command/param writes, 4-wire opcode 0x32 for pixel data, RAMWRC at 0x3C).
# The only thing specific to this panel is the init command table, which
# lives in the paired _st77922_init.py (transcribed verbatim from Freenove's
# st77922_lcd_init[] table) and is picked up automatically by
# display_driver_framework.DisplayDriver.init() via the
# f'_{class_name.lower()}_init' naming convention.
#
# Panel wiring (from Freenove's ST77922.h, verified against their source,
# not guessed): QSPI on SPI2_HOST @ 80MHz, mode 0.
#   CS=10  BL=41 (plain GPIO, active-high backlight -- no PWM)
#   SCLK=12  D0=11  D1=13  D2=14  D3=9
#
# NOTE on rotation: Freenove's own Fill_Colors() does a manual, software
# pixel-shuffle for rotation 1/3 (landscape) because MADCTL-based hardware
# rotation on this panel does not behave normally for those orientations --
# their Set_Rotation() leaves MADCTL unchanged (same as rotation 0) and
# instead pre-rotates the framebuffer content in software before sending it.
# This driver does NOT replicate that workaround, so only rotation 0
# (native portrait, 320x480) is verified to match Freenove's behavior.
# Landscape support would need the same manual buffer-flip approach,
# layered on top of (or replacing) the generic flush path -- left as a
# follow-up, not required for the portrait "Hello World" demo.

import display_driver_framework
import rgb_display_framework  # NOQA
from micropython import const  # NOQA
import lcd_bus
import lvgl as lv  # NOQA
import gc


STATE_HIGH = display_driver_framework.STATE_HIGH
STATE_LOW = display_driver_framework.STATE_LOW
STATE_PWM = display_driver_framework.STATE_PWM

BYTE_ORDER_RGB = display_driver_framework.BYTE_ORDER_RGB
BYTE_ORDER_BGR = display_driver_framework.BYTE_ORDER_BGR

_WRITE_CMD = const(0x02)
_WRITE_COLOR = const(0x32)

_MADCTL_MH = const(0x04)  # Refresh 0=Left to Right, 1=Right to Left
_MADCTL_BGR = const(0x08)  # BGR color order
_MADCTL_ML = const(0x10)  # Refresh 0=Top to Bottom, 1=Bottom to Top

_MADCTL_MV = const(0x20)  # 0=Normal, 1=Row/column exchange
_MADCTL_MX = const(0x40)  # 0=Left to Right, 1=Right to Left
_MADCTL_MY = const(0x80)  # 0=Top to Bottom, 1=Bottom to Top

_RASET = const(0x2B)
_CASET = const(0x2A)
_RAMWR = const(0x2C)
_RAMWRC = const(0x3C)
_MADCTL = const(0x36)


class ST77922(display_driver_framework.DisplayDriver):

    # Only index 0 (no MADCTL bits) is verified against Freenove's driver --
    # see the module docstring note on rotation above.
    _ORIENTATION_TABLE = (
        0,
        _MADCTL_MV,
        _MADCTL_MX | _MADCTL_MY,
        _MADCTL_MV | _MADCTL_MX | _MADCTL_MY
    )

    @staticmethod
    def __quad_spi_cmd_modifier(cmd):
        cmd <<= 8
        cmd |= _WRITE_CMD << 24
        return cmd

    @staticmethod
    def __quad_spi_color_cmd_modifier(cmd):
        cmd <<= 8
        cmd |= _WRITE_COLOR << 24
        return cmd

    @staticmethod
    def __dummy_cmd_modifier(cmd):
        return cmd

    def __init__(
        self,
        data_bus,
        display_width,
        display_height,
        frame_buffer1=None,
        frame_buffer2=None,
        reset_pin=None,
        reset_state=STATE_HIGH,
        power_pin=None,
        power_on_state=STATE_HIGH,
        backlight_pin=None,
        backlight_on_state=STATE_HIGH,
        offset_x=0,
        offset_y=0,
        color_byte_order=BYTE_ORDER_RGB,
        color_space=lv.COLOR_FORMAT.RGB888,  # NOQA
        rgb565_byte_swap=False,  # NOQA
    ):
        num_lanes = data_bus.get_lane_count()

        if isinstance(data_bus, lcd_bus.SPIBus) and num_lanes == 4:
            self.__cmd_modifier = self.__quad_spi_cmd_modifier
            self.__color_cmd_modifier = self.__quad_spi_color_cmd_modifier
            _cmd_bits = 32

            # NOTE: pass a partial frame_buffer1 yourself (see the demo
            # script -- Freenove's own driver uses a 320x40-row buffer, i.e.
            # 1/12th of the screen). A single full-screen (320x480) buffer
            # was tried and confirmed on hardware to overflow the SPI
            # transaction/DMA-descriptor resources for one giant tx_color()
            # call (ESP_ERR_NO_MEM), so this auto-allocation path (which
            # only runs when frame_buffer1 is left as None) is kept for
            # API-compatibility with st77916.py but is NOT what the demo
            # script actually uses.
            buf_size = display_width * display_height * lv.color_format_get_size(color_space)

            if frame_buffer1 is None:
                gc.collect()

                for flags in (
                    lcd_bus.MEMORY_INTERNAL | lcd_bus.MEMORY_DMA,
                    lcd_bus.MEMORY_SPIRAM | lcd_bus.MEMORY_DMA,
                    lcd_bus.MEMORY_INTERNAL,
                    lcd_bus.MEMORY_SPIRAM
                ):
                    try:
                        frame_buffer1 = (
                            data_bus.allocate_framebuffer(buf_size, flags)
                        )

                        if (flags | lcd_bus.MEMORY_DMA) == flags:
                            frame_buffer2 = (
                                data_bus.allocate_framebuffer(buf_size, flags)
                            )

                        break
                    except MemoryError:
                        frame_buffer1 = data_bus.free_framebuffer(frame_buffer1)  # NOQA

                if frame_buffer1 is None:
                    raise MemoryError(
                        f'Unable to allocate memory for frame buffer ({buf_size})'  # NOQA
                    )

                if len(frame_buffer1) != buf_size:
                    raise ValueError('incorrect framebuffer size')
        else:
            self.__cmd_modifier = self.__dummy_cmd_modifier
            self.__color_cmd_modifier = self.__dummy_cmd_modifier
            _cmd_bits = 8

        super().__init__(
            data_bus,
            display_width,
            display_height,
            frame_buffer1,
            frame_buffer2,
            reset_pin,
            reset_state,
            power_pin,
            power_on_state,
            backlight_pin,
            backlight_on_state,
            offset_x,
            offset_y,
            color_byte_order,
            color_space,  # NOQA
            rgb565_byte_swap=rgb565_byte_swap,
            _cmd_bits=_cmd_bits,
            _param_bits=8,
            _init_bus=True
        )

    def set_params(self, cmd, params=None):
        cmd = self.__cmd_modifier(cmd)
        self._data_bus.tx_param(cmd, params)

    # The base DisplayDriver._set_memory_location()/_flush_cb() send the raw
    # module-level _CASET/_RASET/_RAMWR command bytes straight to
    # data_bus.tx_param()/tx_color(), bypassing set_params() and therefore
    # bypassing __cmd_modifier()/__color_cmd_modifier() entirely. On this
    # quad-SPI panel that means every CASET/RASET window write -- and every
    # pixel push -- go out with the wrong 32-bit command framing (no QSPI
    # write opcode embedded), which scrambles the panel's write window and
    # was confirmed on hardware to produce a screen full of static/noise.
    # Freenove's own driver (Set_Windows()/Fill_Colors() in ST77922.cpp)
    # always sends CASET/RASET through the same 1-wire "write reg" opcode
    # (0x02) used for every other register, and pixel data through the
    # 4-wire "write color" opcode (0x32) at RAMWRC (0x3C) -- so replicate
    # that explicitly here instead of relying on the generic base path.
    def _set_memory_location(self, x1, y1, x2, y2):
        # Called from _flush_cb() with x2/y2 already inclusive (LVGL area
        # coordinates), one call per flush -- this driver uses a partial
        # frame buffer (see the demo script), never the base class's
        # one-time full-buffer window-priming path, so no end-coordinate
        # adjustment is needed here.
        param_buf = self._param_buf  # NOQA
        param_mv = self._param_mv  # NOQA

        param_buf[0] = (x1 >> 8) & 0xFF
        param_buf[1] = x1 & 0xFF
        param_buf[2] = (x2 >> 8) & 0xFF
        param_buf[3] = x2 & 0xFF
        self.set_params(_CASET, param_mv[:4])

        param_buf[0] = (y1 >> 8) & 0xFF
        param_buf[1] = y1 & 0xFF
        param_buf[2] = (y2 >> 8) & 0xFF
        param_buf[3] = y2 & 0xFF
        self.set_params(_RASET, param_mv[:4])

        return self.__color_cmd_modifier(_RAMWRC)

    # Full-frame quad-SPI displays (see the frame_buffer1 handling above) get
    # _set_memory_location replaced with this dummy after the first call --
    # see DisplayDriver.init()'s "keep memory consumption low" block -- so
    # it needs the same opcode-modified RAMWRC command as above, not the
    # base class's raw, un-modified _RAMWR.
    def _dummy_set_memory_location(self, *_, **__):  # NOQA
        return self.__color_cmd_modifier(_RAMWRC)
