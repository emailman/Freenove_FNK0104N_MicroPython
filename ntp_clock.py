"""
NTP-synced clock for the Freenove FNK0104N's onboard 3.5" display (ST77922, 320x480, QSPI),
rendered in landscape (480x320) via st77922.ST77922Landscape -- see that class's docstring in
st77922.py for how landscape is done (a from-scratch software pixel-shuffle, since neither
this panel's MADCTL rotation nor LVGL's own software rotation path work here).

Connects to WiFi (credentials from the gitignored wifi_secrets.py -- copy
wifi_secrets.py.example and fill it in), syncs the RTC over NTP (ntptime -- a frozen module,
so unlike st77922.py/st77922_touch.py there's no loose-file import-ordering concern), then
displays the current date/time converted to Mountain Time (MST/MDT, auto-switching by US DST
rules computed locally, since MicroPython's RTC has no timezone database). Re-syncs over NTP
periodically so the RTC doesn't drift over a long-running session.

Requires the custom lvgl_micropython firmware -- see hello_world_display.py / README.md.
No touch is used here, so (unlike hello_world_display.py/touch_led_colors.py) this script
never imports st77922_touch/i2c, and the import-ordering concern documented there doesn't
apply.
"""

import gc
import time

import network
import ntptime

import lvgl as lv
import lcd_bus
from machine import SPI

import st77922
from task_handler import TaskHandler
import wifi_secrets

DISPLAY_WIDTH = 480   # logical/landscape width LVGL renders into -- see ST77922Landscape
DISPLAY_HEIGHT = 320  # logical/landscape height
BUFFER_ROWS = 26      # 480*26*2 = 24,960 bytes/chunk -- under the 320*40*2 = 25,600-byte DMA
                       # transaction size the portrait demos already proved safe on hardware

NTP_RESYNC_INTERVAL_MS = 6 * 60 * 60 * 1000  # re-sync every 6 hours


# --- WiFi --------------------------------------------------------------------------------

def connect_wifi(timeout_s=15):
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if not wlan.isconnected():
        print("Connecting to WiFi SSID '%s'..." % wifi_secrets.SSID)
        wlan.connect(wifi_secrets.SSID, wifi_secrets.PASSWORD)
        t0 = time.ticks_ms()
        while not wlan.isconnected():
            if time.ticks_diff(time.ticks_ms(), t0) > timeout_s * 1000:
                raise RuntimeError("WiFi connect timed out")
            time.sleep_ms(200)
    print("WiFi connected:", wlan.ifconfig())
    return wlan


# --- NTP ---------------------------------------------------------------------------------

def sync_time(retries=3):
    for attempt in range(1, retries + 1):
        try:
            ntptime.settime()
            print("NTP sync OK, RTC (UTC) now:", time.localtime())
            return
        except Exception as exc:  # NOQA -- OSError on socket timeout/unreachable host, etc.
            print("NTP sync attempt %d/%d failed: %r" % (attempt, retries, exc))
            time.sleep(1)
    print("NTP sync failed after %d attempts -- RTC may be stale/wrong." % retries)


# --- Mountain Time (MST/MDT) via US DST rules, computed locally ------------------------
#
# MicroPython's RTC holds UTC (set by ntptime.settime()) and time.localtime() applies no
# timezone offset of its own -- there's no tz database on this firmware. US DST runs from the
# 2nd Sunday in March at 2AM local standard time to the 1st Sunday in November at 2AM local
# daylight time; both boundaries are computed here directly from the raw UTC calendar fields,
# comparing (month, day, hour, minute) tuples rather than converting through epoch seconds.

def _day_of_week(year, month, day):
    """0=Sunday..6=Saturday, via Zeller's congruence (Gregorian calendar). Verified against
    2000-01-01 (a known Saturday) while writing this."""
    m, y = month, year
    if m < 3:
        m += 12
        y -= 1
    k = y % 100
    j = y // 100
    h = (day + (13 * (m + 1)) // 5 + k + k // 4 + j // 4 + 5 * j) % 7  # 0=Saturday..6=Friday
    return (h + 6) % 7  # shift to 0=Sunday..6=Saturday


def _nth_sunday(year, month, n):
    for day in range(1, 8):
        if _day_of_week(year, month, day) == 0:
            return day + 7 * (n - 1)
    raise AssertionError  # unreachable -- every 7-day span contains exactly one Sunday


def mountain_time():
    """Returns (local_time_tuple, abbreviation) for the current UTC time."""
    utc = time.localtime()
    year, month, day, hour, minute = utc[0], utc[1], utc[2], utc[3], utc[4]

    dst_start = (3, _nth_sunday(year, 3, 2), 9, 0)   # 2AM MST (UTC-7) = 9AM UTC
    dst_end = (11, _nth_sunday(year, 11, 1), 8, 0)   # 2AM MDT (UTC-6) = 8AM UTC
    now_key = (month, day, hour, minute)

    if dst_start <= now_key < dst_end:
        offset_s, abbr = -6 * 3600, "MDT"
    else:
        offset_s, abbr = -7 * 3600, "MST"

    return time.localtime(time.time() + offset_s), abbr


# --- Display -----------------------------------------------------------------------------

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

screen = lv.screen_active()
screen.set_style_bg_color(lv.color_hex(0x000000), 0)
screen.set_style_bg_opa(lv.OPA.COVER, 0)

# --- Fixed-width character-grid label rows -----------------------------------------------
#
# Montserrat-14's digit advance widths aren't equal (measured directly from
# lv_font_montserrat_14.c's glyph_dsc table -- its adv_w field is 8.4 fixed-point, so real
# pixel width = adv_w / 16), so a single auto-fit label re-centers its *whole string* every
# tick and individual digits visibly shift whenever a digit's value changes -- worst whenever
# a '1' (5px wide) appears/disappears next to a '0'/'4'/'6'/'8'/'9' (9px wide). Fix: one
# lv.label per character position, laid out once in a fixed-pitch row and never repositioned
# again -- only set_text() changes after creation. Each label still uses this project's usual
# pivot+scale enlargement trick (see CLAUDE.md's "Text sizing" section / touch_led_colors.py),
# just applied per character instead of per whole string. A firmware rebuild to add a real
# monospace font (LV_FONT_UNSCII_16) was considered and declined in favor of this same-day,
# no-rebuild fix.
#
# Cell widths below are *pre-scale* (native 14px-font) pixel widths, measured from
# lv_font_montserrat_14.c directly, rounded up ~1px for margin. Digit/punctuation/space
# glyphs are identical metrics regardless of row, so those constants are shared; the letter
# cell width is kept separate per row since each string's widest actual letter differs
# ('M' at 13px for AM/PM/MDT/MST vs 'W' at 16px for "Wed").
_DIGIT_W = 10
_PUNCT_W = 4    # ':' and ','
_SPACE_W = 4
_TIME_LETTER_W = 14
_DATE_LETTER_W = 16

# One entry per character position of "%02d:%02d:%02d %s %s" (hour12, minute, second, am_pm, abbr)
TIME_SLOT_WIDTHS = (
    _DIGIT_W, _DIGIT_W, _PUNCT_W,                       # HH:
    _DIGIT_W, _DIGIT_W, _PUNCT_W,                       # MM:
    _DIGIT_W, _DIGIT_W, _SPACE_W,                       # SS_
    _TIME_LETTER_W, _TIME_LETTER_W, _SPACE_W,           # AM/PM_
    _TIME_LETTER_W, _TIME_LETTER_W, _TIME_LETTER_W,     # MDT/MST
)  # 15 slots -- always exactly 15 chars, no padding ever needed (every field is fixed-width)

# One entry per character position of "%s, %s %2d %d" (weekday, month, day, year)
DATE_SLOT_WIDTHS = (
    _DATE_LETTER_W, _DATE_LETTER_W, _DATE_LETTER_W, _PUNCT_W, _SPACE_W,   # "Sat, "
    _DATE_LETTER_W, _DATE_LETTER_W, _DATE_LETTER_W, _SPACE_W,             # "Sep "
    _DIGIT_W, _DIGIT_W, _SPACE_W,                                        # "12 " (day, %2d-padded)
    _DIGIT_W, _DIGIT_W, _DIGIT_W, _DIGIT_W,                               # "2026"
)  # 16 slots


def _make_char_row(parent, slot_widths, scale, color, dy):
    """Creates one lv.label per character position under `parent`, laid out in a fixed-pitch
    row and never repositioned again -- only set_text() (via _set_row_text) changes after this.
    Each label's own CENTER (not its left edge) is pinned at its slot's (dx, dy), computed
    once here from slot_widths; LVGL re-applies that alignment automatically whenever a later
    set_text() changes the label's auto-fit size (same mechanism that made the ghost-text bug
    possible in the first place -- see the comment on screen.invalidate() in _refresh), so
    each character re-centers within its own fixed slot as its glyph width changes (e.g. '1'
    vs '8') without ever drifting into a neighboring slot.

    slot_widths are *pre-scale* pixel widths (native 14px-font metrics); align()'s x_ofs/y_ofs
    aren't affected by the label's own transform_scale (scale only enlarges the rendered glyph
    around its pivot, not its position bookkeeping), so the pitch between slot centers has to
    be scaled up here explicitly to keep pace with the enlarged glyphs.
    """
    total_w = scale * sum(slot_widths)
    labels = []
    x = 0  # running left edge, in on-screen (post-scale) pixels
    for w in slot_widths:
        visual_w = w * scale
        center_x = x + visual_w // 2 - total_w // 2
        label = lv.label(parent)
        label.set_text(" ")  # placeholder -- real content lands via the first _refresh(None)
        label.set_style_text_color(lv.color_hex(color), 0)
        # Same pivot+scale enlargement as every other label in this project (see CLAUDE.md) --
        # applied per character here instead of once per whole string.
        label.set_style_transform_pivot_x(lv.pct(50), 0)
        label.set_style_transform_pivot_y(lv.pct(50), 0)
        label.set_style_transform_scale(lv.SCALE_NONE * scale, 0)
        label.align(lv.ALIGN.CENTER, center_x, dy)
        labels.append(label)
        x += visual_w
    return labels


def _set_row_text(labels, text):
    """Applies `text` to a fixed-width character row, one lv.label per character. Callers are
    expected to pre-format `text` to exactly len(labels) characters (e.g. "%2d" for day-of-month,
    which is otherwise 1 or 2 digits depending on the date) -- every field in both format
    strings below is fixed-width for this reason, so this never has to guess how to pad.
    Defensively right-pads/truncates anyway so a mismatched length degrades instead of raising.
    MicroPython's str has no .ljust()/.rjust() (CPython-only), hence the manual padding.
    """
    n = len(labels)
    if len(text) < n:
        text = text + " " * (n - len(text))
    elif len(text) > n:
        text = text[:n]
    for label, ch in zip(labels, text):
        label.set_text(ch)


time_chars = _make_char_row(screen, TIME_SLOT_WIDTHS, 3, 0xFFFFFF, -30)
date_chars = _make_char_row(screen, DATE_SLOT_WIDTHS, 2, 0xAAAAAA, 60)

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")  # time.localtime() weekday: 0=Mon
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _refresh(_timer):
    local, abbr = mountain_time()
    year, month, day, hour, minute, second, weekday = local[0:7]
    hour12 = hour % 12 or 12  # 0 (midnight) and 12 (noon) both display as 12, not 0
    am_pm = "AM" if hour < 12 else "PM"
    _set_row_text(time_chars, "%02d:%02d:%02d %s %s" % (hour12, minute, second, am_pm, abbr))
    # "%2d" (not "%d") for day -- day-of-month is 1 or 2 digits depending on the date, and this
    # keeps it a fixed 2-character, right-justified field (" 2" vs "12") matching DATE_SLOT_WIDTHS.
    _set_row_text(date_chars, "%s, %s %2d %d" % (_WEEKDAYS[weekday], _MONTHS[month - 1], day, year))

    # Force a full-screen invalidation on every tick rather than trusting LVGL's own per-widget
    # dirty-rect tracking for these labels. Confirmed on hardware: a stale fragment of the very
    # first render (before WiFi/NTP finished, a different width/position than every per-second
    # update since) stayed on screen indefinitely -- each subsequent set_text() only invalidated
    # its own label's *current* box, never the union with wherever the previous render happened
    # to sit, so the old pixels outside that box never got repainted. Root cause looks like an
    # LVGL invalidation-vs-transform_scale interaction (these labels use a static render-time
    # scale transform, not a real font size -- see the comment above), not anything in
    # ST77922Landscape -- but chasing that further isn't worth it when a full redraw is now
    # ~100ms (was well over a minute before the viper/staging-buffer fixes) for a clock that
    # only updates once a second. Still needed with the per-character grid above: each small
    # label's box now varies by at most one glyph's width difference instead of a whole
    # string's, but the same underlying mechanism applies, just at smaller magnitude.
    screen.invalidate()


connect_wifi()
sync_time()
_refresh(None)

# Drives lv.tick_inc()/lv.task_handler() on a background hardware timer, so the clock keeps
# rendering/updating after this script finishes running -- same pattern as every other display
# demo in this repo.
task_handler = TaskHandler()

_resync_timer = lv.timer_create(lambda _t: sync_time(), NTP_RESYNC_INTERVAL_MS, None)
_clock_timer = lv.timer_create(_refresh, 1000, None)

print("NTP landscape clock running.")
