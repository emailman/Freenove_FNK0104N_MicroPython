"""
On-screen touch keyboard demo for the Freenove FNK0104N's onboard 3.5" display (ST77922,
320x480, QSPI), rendered in landscape (480x320) via st77922.ST77922Landscape -- see that
class's docstring in st77922.py for how landscape is done (a from-scratch software
pixel-shuffle, since neither this panel's MADCTL rotation nor LVGL's own software rotation
path work here) -- same display setup ntp_clock.py uses.

Landscape variant of touch_keyboard_demo.py, which only covers portrait. That file's earlier
docstring flagged landscape touch as not yet attempted because st77922_touch.py's raw touch
coordinates have no rotation remapping of their own -- they're always reported in the panel's
native physical orientation. This demo uses st77922_touch.ST77922LTouch (added alongside
ST77922Touch in st77922_touch.py), which remaps those raw physical coordinates into the same
logical 480x320 space ST77922Landscape presents to LVGL, using the documented inverse of
ST77922Landscape's own flush-time rotation formula -- see that class's docstring for the
derivation (and for why it's named "...LTouch", not the more obvious "...TouchLandscape" -- a
real ESP-IDF NVS key-length limit found while testing this on hardware).

`lv.keyboard`/`lv.textarea` are compiled into this firmware (confirmed in
touch_keyboard_demo.py) -- this uses LVGL's built-in keyboard widget rather than a hand-built
one. A textarea near the top of the screen shows what's typed; the keyboard fills the rest.
Tapping keys inserts into the textarea directly -- LVGL's pointer (touch) indev clicks the
keyboard's buttonmatrix without needing any lv.group/encoder-focus setup. The keyboard's own
Enter/OK key fires lv.EVENT.READY; its Hide/Cancel key (only present in some modes) fires
lv.EVENT.CANCEL -- both just print here.

Requires the custom lvgl_micropython firmware -- see hello_world_display.py / README.md.
Wiring/init pattern (QSPI display bus, then I2C touch after it) is copied from
hello_world_display.py; see that file's comments for why the ordering matters.

KNOWN COSMETIC ISSUE, not fully root-caused (confirmed on hardware 2026-09-16): tapping certain
keyboard control buttons -- the mode-switch keys (ABC/abc, 1#) reliably, Enter reliably, and
Backspace intermittently -- produces a brief, solid-green flash on the keyboard (Backspace can
also flash the textarea) before self-correcting. Ordinary character keys, by contrast, are
confirmed clean. This is a DIFFERENT symptom from st77922.py's documented narrow-CASET/
full-RASET panel corruption (which is always green WITH random noise, not solid) -- direct
hardware instrumentation ruled out both bad chunk geometry (every _flush_cb call in this app
is already full logical width, confirmed by logging every call's area) and bad source pixel
data (sampling raw pre-rotation pixel values during a flash showed only ordinary UI colors,
never green) as the cause. A rate limit on real hardware pushes (guarding against two
back-to-back full-panel sends, since a mode-switch's own internal partial redraw is followed
immediately by an app-level full-screen corrective one) was also tried and made no difference,
and was removed again. Typing (character keys) and the textarea's blinking cursor, which showed
the same kind of flash before the fixes below, are confirmed fixed; the control-button cases
above remain, accepted as-is rather than chased further.
Pinning this down further would need hardware-level tracing (e.g. a logic analyzer on the QSPI
lines), the same class of limit this project has hit before (see st77922.py's narrow-CASET/
full-RASET writeup) -- left as a known quirk rather than chased further.
"""

import gc

import lvgl as lv
import lcd_bus
from machine import SPI

import st77922
from task_handler import TaskHandler

# i2c/st77922_touch are imported further down, after the display's QSPI bus is up --
# see hello_world_display.py's comment for why.

DISPLAY_WIDTH = 480   # logical/landscape width LVGL renders into -- see ST77922Landscape
DISPLAY_HEIGHT = 320  # logical/landscape height
BUFFER_ROWS = 26      # 480*26*2 = 24,960 bytes/chunk -- same proven-safe size ntp_clock.py
                       # uses; portrait's BUFFER_ROWS=40 doesn't apply since the row width
                       # (and therefore per-row byte count) differs.

qspi_bus = SPI.Bus(host=2, sck=12, quad_pins=(11, 13, 14, 9))
data_bus = lcd_bus.SPIBus(spi_bus=qspi_bus, dc=-1, cs=10, freq=80_000_000, spi_mode=0, quad=True)

gc.collect()
frame_buffer = data_bus.allocate_framebuffer(
    DISPLAY_WIDTH * BUFFER_ROWS * 2, lcd_bus.MEMORY_SPIRAM | lcd_bus.MEMORY_DMA
)

display = st77922.ST77922Landscape(
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

# Importing/compiling st77922_touch.py (a loose, uncompiled file) here -- after the display's
# esp_lcd panel IO is already up -- rather than at the top of the file keeps that
# compilation's transient memory use out of the way of esp_lcd_new_panel_io_spi's allocation
# above; doing it beforehand was enough to make that allocation fail (ESP_ERR_NO_MEM).
gc.collect()
import i2c
import st77922_touch

# Touch is a separate I2C peripheral from the QSPI display bus (see st77922_touch.py for
# wiring/register details, ported from Freenove's own ST77922_Touch.h/.cpp).
touch_i2c_bus = i2c.I2C.Bus(host=0, scl=39, sda=38, freq=100_000)
touch_device = i2c.I2C.Device(
    bus=touch_i2c_bus, dev_id=st77922_touch.I2C_ADDR, reg_bits=st77922_touch.BITS
)
# ST77922LTouch (not the base ST77922Touch) -- remaps raw physical touch coordinates into
# the logical 480x320 space ST77922Landscape presents to LVGL. See its docstring in
# st77922_touch.py.
touch = st77922_touch.ST77922LTouch(device=touch_device, reset_pin=48, int_pin=47)

screen = lv.screen_active()
screen.set_style_bg_color(lv.color_hex(0x000000), 0)
screen.set_style_bg_opa(lv.OPA.COVER, 0)

# Shorter than portrait's TA_HEIGHT=80 -- landscape's total height (320) is much less than
# portrait's (480), so the textarea is kept slim to leave the keyboard comfortable room below.
TA_HEIGHT = 50

textarea = lv.textarea(screen)
textarea.set_size(DISPLAY_WIDTH, TA_HEIGHT)
textarea.set_pos(0, 0)
textarea.set_placeholder_text("Tap keys below")
textarea.set_one_line(False)

keyboard = lv.keyboard(screen)
keyboard.set_size(DISPLAY_WIDTH, DISPLAY_HEIGHT - TA_HEIGHT)
keyboard.set_pos(0, TA_HEIGHT)
keyboard.set_textarea(textarea)


def _on_kb_event(event):
    code = event.get_code()
    if code == lv.EVENT.READY:
        print("Keyboard READY (Enter/OK) -- text so far:", textarea.get_text())
    elif code == lv.EVENT.CANCEL:
        print("Keyboard CANCEL (Hide) -- text so far:", textarea.get_text())


keyboard.add_event_cb(_on_kb_event, lv.EVENT.READY, None)
keyboard.add_event_cb(_on_kb_event, lv.EVENT.CANCEL, None)

# LVGL's normal incremental redraws (as opposed to ntp_clock.py's forced full-screen
# invalidate every tick) let a single typed character or the textarea's own blinking cursor
# invalidate only their own small sub-region. Confirmed on hardware: without working around
# this, typing and the idle cursor blink made the textarea flash a corruption pattern on this
# panel. Fixed the same way ntp_clock.py works around its own (different) invalidation gotcha:
# force a full screen.invalidate() around anything that can trigger a partial redraw here --
# immediately on every textarea text change and every keyboard button event, and periodically
# to catch the cursor blink, which invalidates on its own timer independent of any button
# press. This does NOT fully fix every case -- see the module docstring's "KNOWN COSMETIC
# ISSUE" note for the keyboard mode-switch buttons (ABC/abc, 1#), which still produce a brief
# flash despite also being covered by the keyboard event hook below.
textarea.add_event_cb(lambda _e: screen.invalidate(), lv.EVENT.VALUE_CHANGED, None)
# CLICKED, not VALUE_CHANGED -- confirmed on hardware that VALUE_CHANGED does NOT fire on the
# keyboard object for mode-switch buttons (ABC/abc, 1#): LVGL only refires VALUE_CHANGED at the
# keyboard level when the linked textarea's actual text value changes, which a mode switch
# never does. CLICKED fires for every button in the underlying button matrix regardless of what
# it does, so it also covers mode-switch buttons (and Enter/Hide, redundantly with _on_kb_event
# below -- harmless).
keyboard.add_event_cb(lambda _e: screen.invalidate(), lv.EVENT.CLICKED, None)
_cursor_blink_timer = lv.timer_create(lambda _t: screen.invalidate(), 400, None)

# Drives lv.tick_inc()/lv.task_handler() on a background hardware timer, so the demo keeps
# rendering/responding to touch after this script finishes running.
task_handler = TaskHandler()

print("Landscape touch keyboard demo running. Tap keys to type into the textarea above.")
