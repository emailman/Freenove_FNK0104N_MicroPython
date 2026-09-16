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
import micropython


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


# 90-degree-rotation pixel shuffle for ST77922Landscape._flush_cb, as a @micropython.viper
# function rather than plain Python. Measured on hardware (a standalone benchmark script, not
# kept in-repo): the equivalent plain-Python loop -- byte-slice memoryview copies, 2 bytes at a
# time -- took ~8.0 SECONDS for one 480x26 chunk (the buffer size ntp_clock.py actually uses).
# That's the real explanation for "screen updates seemed VERY SLOW": a full-screen redraw fires
# 13+ chunks, so well over a minute per redraw, and the once-a-second _clock_timer refresh in
# ntp_clock.py was re-running a multi-second-per-chunk loop every second indefinitely -- also
# the likely real cause of the separately-noted REPL-unresponsive symptom, competing with REPL
# input for CPU far worse than assumed when that was first written off as a minor concern.
# This viper version does the exact same index arithmetic but as directly-typed pointer
# indexing (ptr16, since this driver only ever runs RGB565 -- px_size == 2) instead of
# memoryview slice objects -- confirmed byte-identical output against the old loop on hardware,
# and ~2200x faster (3.6ms vs 8.0s for the same chunk). Only handles px_size == 2; _flush_cb
# falls back to the old byte-slice loop for anything else (never exercised in practice -- every
# script in this repo constructs ST77922Landscape with color_space=lv.COLOR_FORMAT.RGB565).
#
# Writes directly into the right place inside ST77922Landscape's persistent, full-panel-sized
# staging framebuffer (see that class's docstring) rather than into a small per-chunk buffer --
# `fb_width`/`fb_height` are that staging buffer's physical dimensions (320x480), so the
# destination is strided differently than the source (dest rows are fb_width pixels apart,
# source rows are w_l apart) -- an ordinary blit, not a same-shape copy.
#
# General form: handles a chunk starting at ANY logical (lx1, ly1), not just lx1 == 0. Applies
# the class docstring's px = ly, py = (fb_height - 1) - lx mapping directly, per source pixel
# (r, c) at logical (lx1 + c, ly1 + r) -> physical (px, py) = (ly1 + r, fb_height - 1 - lx1 - c).
# An earlier version hard-assumed every chunk spans the full logical width (lx1 always 0, w_l
# always the full canvas width) -- true for ntp_clock.py's forced-every-tick full-screen
# invalidate, but NOT true for arbitrary widgets' own partial invalidates (confirmed on
# hardware while building touch_keyboard_demo_landscape.py, chasing a brief solid-green flash
# on the textarea/keyboard when a widget triggers its own partial redraw (typing, the
# textarea's blinking cursor, a keyboard mode-switch button). Direct hardware instrumentation
# (logging every _flush_cb call's area, and separately sampling raw pre-rotation pixel data)
# ruled out both this function's old lx1 == 0-only assumption and outright bad source pixel
# data as the cause -- every observed chunk in that app was already full logical width
# (lx1 == 0), and sampled colors always looked like ordinary UI content, never green. The flash
# itself was never fully root-caused (see touch_keyboard_demo_landscape.py's docstring for what
# was ruled out and where that investigation was left). This generalization is kept anyway
# because it's strictly more correct than the old lx1 == 0-only version for any chunk that
# *does* start at a nonzero logical x (verified byte-identical to the old code's output for the
# lx1 == 0, full-width case ntp_clock.py exercises every tick -- no regression there), even
# though it turned out not to explain the flash above.
@micropython.viper
def _rotate_chunk_rgb565(src, fb, w_l: int, h_l: int, lx1: int, ly1: int, fb_width: int, fb_height: int):
    s = ptr16(src)
    o = ptr16(fb)
    for c in range(w_l):
        py = fb_height - 1 - lx1 - c
        dest_row = py * fb_width
        for r in range(h_l):
            o[dest_row + ly1 + r] = s[r * w_l + c]


class ST77922Landscape(ST77922):
    # Landscape variant of ST77922, done entirely in software rather than via
    # MADCTL. Checked directly against the actual firmware build (WSL
    # lvgl_micropython clone) before writing this rather than guessing:
    #   - display_driver_framework.DisplayDriver.set_rotation()/_on_size_change()
    #     only ever drives hardware MADCTL via _ORIENTATION_TABLE -- the same
    #     mechanism this module's own docstring already documents as not
    #     behaving correctly on this panel for rotation 1/3.
    #   - LVGL's own software-rotation path (lv_display_set_matrix_rotation,
    #     matrix-transform drawing) is compiled out: LV_DRAW_TRANSFORM_USE_MATRIX
    #     is 0 in this project's lv_conf.h (confirmed against
    #     lv_conf_internal.h's fallback -- there's no override).
    #   - lcd_bus.SPIBus.tx_color() does receive a `rotation` argument, but
    #     tracing modlcd_bus.c -> lcd_types.c -> spi_bus.c shows it's LCD_UNUSED
    #     for the SPI bus type; the rotation-compensation code in
    #     esp32_src/rgb_bus_rotation.c is wired up only for the RGB parallel
    #     bus, not SPI/QSPI.
    #
    # So this class leaves the panel's MADCTL at its native rotation-0 setting
    # (never calls set_rotation()/touches _ORIENTATION_TABLE) and instead
    # presents LVGL with a logical 480x320 canvas (construct with
    # display_width=480, display_height=320), remapping every flush by hand
    # onto the physical, still-portrait 320x480 panel -- the same kind of
    # manual pixel-shuffle Freenove's own Fill_Colors() does for landscape,
    # just written fresh (their C++ source for it wasn't available to port
    # directly).
    #
    # Rotation direction: a 90-degree-clockwise rotation of the physical
    # (320-wide x 480-tall) image maps physical pixel (px, py) to logical
    # pixel (lx, ly) = (479 - py, px) -- the standard "rotate W x H image 90
    # CW" formula (new_x = old_height-1-old_y, new_y = old_x) applied with
    # the physical image as input. Inverting gives px = ly, py = 479 - lx.
    # NOT yet confirmed on hardware which physical corner this actually lands
    # logical (0, 0) on -- expect this may need flipping/adjusting once
    # tested for real (see ntp_clock.py's verification notes).
    #
    # Assumes offset_x=offset_y=0 (unlike the base class's _flush_cb, this one doesn't add
    # self._offset_x/_offset_y to the logical area) -- fine for ntp_clock.py, which doesn't
    # set either, but worth knowing if this class is ever reused with a nonzero offset.
    #
    # IMPORTANT HARDWARE FINDING (2026-09-12): this panel does not correctly handle a
    # narrow-CASET/full-RASET window write -- the shape a naive per-chunk rotate-and-send
    # implementation is architecturally forced into (see _flush_cb's own comment below for why).
    # Confirmed by direct hardware testing, isolating every other variable one at a time
    # (throwaway diagnostic scripts, not kept in-repo): a *single*, isolated, all-black
    # narrow-CASET/full-RASET write -- the very first QSPI content write after display.init(),
    # nothing sent before it -- renders GREEN with random white-pixel noise instead of black.
    # Repeating it (tiled, matching a real redraw's 13 chunks), synchronizing every write so
    # each one's DMA transfer completes before the next begins, and substituting RAMWR for
    # RAMWRC all made no difference -- still green. A full-CASET/narrow-RASET write (portrait
    # mode's shape) of the exact same color, on the exact same panel, is always clean. So this
    # is neither a race, a buffer-lifetime bug, a command-semantics bug, nor rotation-math --
    # it's this specific window shape, full stop, that this panel's controller cannot handle,
    # for reasons no amount of software-level testing without a logic analyzer can pin down
    # further. The fix below works around it rather than solving it: never send that shape to
    # the panel at all.
    _PHYSICAL_WIDTH = 320   # native panel width
    _PHYSICAL_HEIGHT = 480  # native panel height -- this class is specific to this one panel

    # Real hardware writes only ever happen in this many-row, full-CASET/narrow-RASET shape --
    # identical to portrait mode's own proven-safe chunk size (see the module docstring).
    _SEND_CHUNK_ROWS = 40

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Persistent, full-panel-sized staging buffer (320x480x2 = 307,200 bytes -- comfortably
        # inside PSRAM) that every flush's rotated chunk gets blitted into. Nothing is ever sent
        # to the panel from a narrow-CASET/full-RASET window again -- only this buffer, read
        # back out in safe, full-CASET/narrow-RASET bands, on the last flush of each redraw (see
        # _flush_cb). MEMORY_DMA because it's used directly as tx_color's source buffer for
        # those real writes.
        fb_size = self._PHYSICAL_WIDTH * self._PHYSICAL_HEIGHT * 2  # RGB565 always, 2 bytes/px
        data_bus = self._data_bus  # NOQA
        try:
            self._landscape_fb = data_bus.allocate_framebuffer(
                fb_size, lcd_bus.MEMORY_SPIRAM | lcd_bus.MEMORY_DMA
            )
        except MemoryError:
            self._landscape_fb = data_bus.allocate_framebuffer(fb_size, lcd_bus.MEMORY_SPIRAM)

    def _flush_cb(self, _, area, color_p):
        lx1, ly1, lx2, ly2 = area.x1, area.y1, area.x2, area.y2
        w_l = lx2 - lx1 + 1  # logical chunk width (columns)
        h_l = ly2 - ly1 + 1  # logical chunk height (rows)

        px_size = lv.color_format_get_size(self._color_space)  # NOQA -- 2 for RGB565
        size = w_l * h_l * px_size
        src = color_p.__dereference__(size)  # NOQA

        # Blit this chunk's rotated pixels directly into the persistent staging framebuffer at
        # its physical position -- see _rotate_chunk_rgb565's comment and __init__ above. No
        # window is set and nothing is sent to the panel here; that only happens once per
        # redraw, below, on the last chunk. _rotate_chunk_rgb565 handles an arbitrary (lx1, ly1)
        # chunk origin directly, not just the lx1 == 0, full-logical-width case a naive
        # per-chunk rotation is usually forced into by row-based partial-buffer chunking of a
        # *full-screen* invalidate -- see that function's comment for why this generalization
        # is correct but turned out not to be what a separately-observed corruption traced back
        # to (see touch_keyboard_demo_landscape.py's docstring).
        if px_size == 2:
            _rotate_chunk_rgb565(
                src, self._landscape_fb, w_l, h_l, lx1, ly1,
                self._PHYSICAL_WIDTH, self._PHYSICAL_HEIGHT
            )
        else:
            # Never exercised in practice (every script here uses RGB565) -- kept only so a
            # future non-RGB565 use doesn't hit a hard type error in _rotate_chunk_rgb565's
            # ptr16 cast. Slower (plain Python), and still correct -- same general (lx1, ly1)
            # -aware mapping as the viper version above.
            fb = memoryview(self._landscape_fb)
            for c in range(w_l):
                py = self._PHYSICAL_HEIGHT - 1 - lx1 - c
                dest_row = (py * self._PHYSICAL_WIDTH) * px_size
                col_src = c * px_size
                for r in range(h_l):
                    src_off = r * w_l * px_size + col_src
                    dest_off = dest_row + (ly1 + r) * px_size
                    fb[dest_off:dest_off + px_size] = src[src_off:src_off + px_size]

        if not self._disp_drv.flush_is_last():
            # Nothing was sent to hardware -- just a RAM copy above -- so there's no async
            # transfer for _flush_ready_cb to signal completion of. Tell LVGL this flush is
            # done ourselves, immediately; a synchronous flush_ready() call from within the
            # flush callback is an explicitly-supported LVGL pattern (this is exactly what
            # every "instant" framebuffer-backed LVGL port does).
            self._disp_drv.flush_ready()
            return

        # Last chunk of this redraw: the staging buffer now holds the complete, correctly
        # rotated 320x480 physical image. Send it to the panel for real, but only ever in the
        # full-CASET(0..319)/narrow-RASET shape proven safe by portrait mode (and now doubly
        # confirmed by hardware testing to be the *only* shape this panel handles correctly --
        # see the class docstring). register_callback(None) forces tx_color() back to its
        # synchronous busy-wait behavior (see modlcd_bus.c's mp_lcd_bus_tx_color) for this
        # internal loop, so each band's transfer completes before the next begins, without
        # spuriously re-triggering _flush_ready_cb/flush_ready() once per internal band.
        fb_mv = memoryview(self._landscape_fb)
        row_bytes = self._PHYSICAL_WIDTH * 2
        self._data_bus.register_callback(None)
        try:
            y = 0
            while y < self._PHYSICAL_HEIGHT:
                y2 = min(y + self._SEND_CHUNK_ROWS, self._PHYSICAL_HEIGHT) - 1
                is_last_band = y2 == self._PHYSICAL_HEIGHT - 1
                cmd = self._set_memory_location(0, y, self._PHYSICAL_WIDTH - 1, y2)
                band = fb_mv[y * row_bytes:(y2 + 1) * row_bytes]
                self._data_bus.tx_color(cmd, band, 0, y, self._PHYSICAL_WIDTH - 1, y2,
                                         self._rotation, is_last_band)
                y = y2 + 1
        finally:
            self._data_bus.register_callback(self._flush_ready_cb)

        # Real flush is fully complete (synchronous above), so it's safe to unblock LVGL now.
        self._disp_drv.flush_ready()
