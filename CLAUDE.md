# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

MicroPython scripts for the Freenove FNK0104N (ESP32-S3 board, onboard 3.5" 320x480 QSPI
display with integrated I2C touch). Not a conventional software project — no package
manager, build system, linter, or test suite. "Development" means editing a `.py` file here
and running it on physical hardware over a REPL connection (WebREPL or serial/`mpremote`);
there is no way to run or test any of this without the board attached. See `README.md` for
the file-by-file overview, pin reference, and known limitations — this file focuses on the
architecture behind the display/touch drivers, which spans multiple files and isn't obvious
from any one of them.

## Why a custom firmware, and what that constrains

Stock MicroPython has no `lvgl`/`lcd_bus`/`i2c` modules. Display- and touch-related code here
runs on a custom-built `lvgl_micropython` firmware (build command in `README.md`), built from
a separate clone of that project — not part of this repo. Two things follow from that:

- **`st77922.py`, `_st77922_init.py`, and `st77922_touch.py` are loose files on the board's
  filesystem, not frozen into the firmware binary.** MicroPython loads the filesystem copy
  over any frozen module of the same name, so editing these and re-pushing them is enough for
  iteration — no firmware rebuild needed unless something genuinely baked into firmware
  itself needs to change (board config, or a *frozen* framework module).
- **Loose files are compiled at import time, which costs real RAM** — unlike frozen modules
  (`pointer_framework`, `_indev_base`, `i2c`, `touch_cal_data`, etc., which the firmware
  build's `generate_manifest` always freezes, regardless of build flags). `hello_world_display.py`
  deliberately imports `i2c`/`st77922_touch` *after* the display's `esp_lcd` bus is already
  initialized, not at the top of the file — importing them earlier adds enough transient
  memory pressure to starve `esp_lcd_new_panel_io_spi`'s allocation from the ESP32-S3's small
  internal (non-PSRAM) DMA-capable memory pool, which fails with `ESP_ERR_NO_MEM`. Keep this
  ordering (loose-file imports after display init) when adding new hardware-facing code.

## Display driver architecture (`st77922.py`)

Structurally a copy of `lvgl_micropython`'s own `st77916.py` driver (same chip family, same
QSPI wire protocol — 1-wire opcode `0x02` for command/param writes, 4-wire opcode `0x32` for
pixel data). The only genuinely panel-specific piece is the init command table in
`_st77922_init.py`, picked up automatically by
`display_driver_framework.DisplayDriver.init()` via the `f'_{class_name.lower()}_init'`
naming convention — so `_st77922_init.py` and the `ST77922` class name are coupled by that
naming convention, not an explicit import.

Two behaviors are overridden from the generic `display_driver_framework` base class, both
required for correctness on real hardware (not stylistic choices):

- `_set_memory_location`/`_dummy_set_memory_location` are overridden because the generic
  base class sends raw, unmodified CASET/RASET/RAMWR bytes that bypass this panel's quad-SPI
  opcode wrapper — silently corrupting every window-set/pixel push if left as-is.
- The demo passes a **partial** framebuffer (`BUFFER_ROWS = 40` rows, not the full 480) via
  `frame_buffer1=`, matching Freenove's own chunk size. A full 320x480 buffer overflows the
  same internal DMA-transaction resource pool mentioned above. This also switches LVGL to
  `DISPLAY_RENDER_MODE.PARTIAL`, which is why `_set_memory_location` needs to be called fresh
  on every chunk flush rather than once.

Hardware MADCTL rotation doesn't behave correctly on this panel (Freenove's own C++ code works
around this with a manual software pixel-shuffle in `Fill_Colors()` for landscape, rotations
1/3). `ST77922Landscape` (used by `ntp_clock.py`) is this project's own from-scratch port of
that same idea — see "Landscape support" below and the `fnk0104n-landscape` skill.

## Landscape support (`ST77922Landscape`)

`ST77922Landscape` presents LVGL a logical 480x320 canvas and remaps every flush by hand onto the
physical portrait panel. **Never send a narrow-CASET/full-RASET window to this panel** (it
renders green noise; see the column-alignment rule below). The full history of the three
stacked landscape fixes (viper rotation, the full-panel staging buffer, ghosting, in-place byte
swap) is in the `fnk0104n-landscape` skill: load it before touching `ST77922Landscape` or any
landscape script.

### Write windows must be 8-pixel column aligned (`ST77922._round_area_cb`)

Found with `sound_meter.py`, whose `lv.chart` in CIRCULAR mode only invalidates a thin vertical
strip per point: the chart area filled with snow in plain portrait mode. A raw-window test on
hardware (no LVGL; black windows sent onto a white screen) showed which windows render
correctly:

| Window columns | Width | Result |
|---|---|---|
| 0..319, 0..159, 160..319, 64..127, 0..7 | multiple of 8 | clean |
| various x | 2, 6 or 38 | green, sometimes noisy |
| 0..318 | 319 | green |

Height made no difference: a single 6x40 window failed too. Before this, portrait scripts mostly
got away with it because their redraws were full-width or happened to be aligned.
`ST77922.__init__` now registers an `lv.EVENT.INVALIDATE_AREA` handler that rounds every
invalidated area out to 8-pixel column boundaries (`x1 &= ~7`, `x2 |= 7`) before LVGL renders
it. This fixes every script that uses the driver, and it costs almost nothing. Keep it.

If any of this needs re-testing: `mpremote connect COM12 ...` (used throughout this project's
development) only performs a **soft** reset (Python globals wiped, like Ctrl-D) — it does
*not* free native ESP-IDF resources any more than a script's own soft-reset/re-run does (see
"Hardware-state gotcha" below), so do an explicit `mpremote connect COM12 reset` + a few
seconds' settle delay before any test script that constructs `SPI.Bus`/`SPIBus`/a display
driver from scratch. Also remember the display panel's own GRAM is separate physical memory
that persists across *any* ESP32 reset — leftover content from a previous script can easily be
mistaken for a new test's own output unless the new script fills the entire screen itself.

## Touch driver architecture (`st77922_touch.py`)

Touch is a **separate I2C peripheral** from the QSPI display bus (own pins, own address —
see `README.md`), not exposed through the display's QSPI commands, despite being physically
integrated into the same panel. Built on `lvgl_micropython`'s `pointer_framework.PointerDriver`
base class (the same framework its bundled touch drivers like `cst816s.py` use) — that base
class handles all LVGL indev registration/click-detection/calibration; this driver only needs
to implement `_get_coords()` returning `(state, x, y)` or `None`.

The one non-obvious hardware quirk, worth knowing before touching this file again: **this
chip will not clear its "With Coord." status bit (register `0x0010`, and the physical INT
pin with it) until the host reads through to the last register of the last supported
coordinate slot** — i.e. the *entire* `7 * max_points`-byte point-data block from
`TOUCH_POINT0`, even though this driver only uses point 0. Reading fewer bytes (e.g. just the
one point actually used) leaves the chip reporting "touched" forever, even long after the
finger lifts. This was first found by directly polling raw registers and the INT pin on
hardware, and is now also confirmed against Sitronix's own protocol spec
(`Datasheet/ST77922_TDDI_Interface_Protocol_V01.00.pdf`, pulled from Freenove's GitHub repo,
kept locally but **not committed to this repo** — it's marked confidential, so `.gitignore`
excludes the `Datasheet/` folder — see its Reporting Table section) — `st77922_touch.py`'s
module docstring cites the exact wording. `_get_coords()` always reads the full block for
this reason.

That same datasheet documents one more register bit this driver now acts on: `0x0010` bit 7
is `RstChip` ("notifies host that the touch controller should be reset") — `_get_coords()`
calls `hw_reset()` when it sees this set, which the original Freenove C++ source doesn't
handle either.

## Combining audio with the display/touch stack (`voice_recorder.py`)

`voice_recorder.py` is the first script to run audio and LVGL/touch in one process. These
things surfaced on hardware that apply to any future combined script (e.g. a music player UI):

Audio-only gotchas (codec, I2S, mic, SD card, WiFi with the mic) are in the `fnk0104n-audio`
skill: load it before writing or debugging any audio code.

- **Render cost decides whether audio keeps up** (measured for `sound_meter.py`, 2026-10-30,
  by timing `lv.task_handler()` with each widget alone, at a 10 Hz UI rate). Continuous I2S RX
  plus per-frame analysis costs ~84ms of CPU per second. An `lv.chart` in `SHIFT` update mode
  redraws the whole chart for every point: ~58ms per redraw at 220x340, or ~580ms/s. With the
  rest of the UI, that saturated the CPU and the I2S callbacks fell behind, so only 10-17 of
  21.5 frames/s arrived. This showed up as dropped audio, not as watchdog stalls. `CIRCULAR`
  mode only redraws around the new point (~70ms/s). A 2x `transform_scale` label costs ~22ms
  per text change, whatever the size of the box it sits in; an unscaled one is nearly free.
  Budget these before adding a busy widget to an audio script.
- **LVGL itself is too slow for animation across many small areas** (measured 2026-10-31 for
  `spectrum_analyzer.py` by timing `lv.refr_now()`). Each dirty area costs ~3ms, however small:
  1 area of 8x8 took 3.4ms, 32 of them took 111ms. Drawing costs ~1us per pixel: a 256x340 area
  took 110ms, of which the flush was 21ms. 32 animated `lv.obj` bars cut the audio to 4-9 of
  21.5 frames/s, and once hung the board so hard it needed a RESET press (no serial at all). The
  fix: draw the animated region yourself (viper, into an `allocate_framebuffer(SPIRAM|DMA)`
  buffer) and send it with the same synchronous `register_callback(None)` /
  `_set_memory_location` / `tx_color` sequence as `ST77922Landscape._flush_cb`, from an LVGL
  timer (LVGL has finished its own flushes by then). Leave LVGL just the static parts. Two
  rules: keep the window 8 px column aligned (the INVALIDATE_AREA rounding doesn't apply to
  direct writes), and write **native** RGB565, because the bus applies `rgb565_byte_swap` to
  `tx_color()` data itself. Pre-swapped colors came out byte-reversed (0x101010 grey showed
  as purple). That swap happens **in place in your buffer**, so never send the same buffer
  twice without redrawing it: `rainbow_fade.py` filled one 40-row band and sent it 12 times,
  and every other band on screen came out byte-swapped (2026-11-02).
  `tx_color()` also sends **the whole buffer it is given**, whatever the window size. To send
  fewer rows than the buffer holds, pass `memoryview(buf)[:rows * 640]`, as
  `ST77922Landscape._flush_cb` and `galton_board.py` do. Measured on hardware (2026-11-16): a
  1-row send cost 2.9ms when given the full 40-row buffer, and 0.7ms when given a 1-row slice.
  The extra rows should also wrap around inside the window and overwrite it with stale data
  from the end of the buffer (inferred from how RAMWR windows work, not seen on screen).
- **Long synchronous panel writes starve I2S output, even with CPU to spare** (measured
  2026-11-02 for `rainbow_fade.py`). Painting the full screen in one go (12 synchronous 40-row
  `tx_color()` bands, ~39ms of busy-waiting) let a generated tone's TX callbacks fall from the
  43/s it needs to 20/s, heard as static. That happened even at 10 repaints/s with ~40% of the
  CPU idle, and with no stalls, so this isn't the dropped-callback problem described in the `fnk0104n-audio` skill. The
  hardware was simply running out of audio. 1-4 bands per LVGL tick held a steady 43/s, so
  spread big direct writes across ticks.

## HTTPS on the board (`flight_tracker.py`)

- **MicroPython's `requests` sends HTTP/1.0, and some servers refuse it.** adsb.lol answers
  every HTTP/1.0 request with `426 Upgrade Required` (confirmed with curl's `--http1.0` too).
  `flight_tracker.py` has its own small `http_get()` instead: a socket wrapped with
  `ssl.wrap_socket(..., server_hostname=host)`, an HTTP/1.1 request with `Connection: close`,
  and a body read by `content-length` (with chunked decoding as a fallback). Copy it for any
  other API that rejects `requests`.
- **One HTTPS request takes 2.5-11 s here**, mostly the TLS handshake and network waits. Doing
  it in an LVGL timer freezes touch and drawing for that long. `flight_tracker.py` runs it on a
  `_thread` instead (`_thread.stack_size(32 * 1024)` first, since TLS needs more than the
  default stack). The thread does only network and JSON work, and hands a result tuple to the
  LVGL timer, which does all the drawing, since LVGL isn't thread-safe. UI stalls fell to
  ~0.4 s (~1 s when parsing a ~200 KB response).
- **airplanes.live** (the same `/v2/point` API as adsb.lol) now refuses unregistered clients
  and asks for an email to its operators. adsb.lol needs no key.

## Don't keep printing after `mpremote run` exits

Once `mpremote run` (or `exec`) returns, nothing reads the board's USB serial output, but
timers and threads started by the script keep running. A script that keeps calling `print()`
(e.g. a log line every update) eventually fills the serial output buffer, and the next
`print()` blocks forever. The whole app freezes and mpremote can't get back in
(`could not enter raw repl`) until a physical RESET. Seen with `flight_tracker.py`
(2026-11-12): it froze when started detached, but ran fine for minutes while a logger was
attached, and ran indefinitely once its per-update prints were gated behind `DEBUG = False`.
Printing at startup is fine. Repeated output belongs behind a debug flag, or on screen.

## LVGL Python binding quirks

This binding (generated for this specific firmware build) doesn't always put enums where
other LVGL binding docs/examples suggest. Confirmed by direct inspection of the generated
binding source (`lv_mp.c`) rather than assumption:

- Most enums are top-level `lv` attributes: `lv.EVENT.CLICKED`, `lv.STATE.*`,
  `lv.INDEV_TYPE.*`, `lv.DISPLAY_ROTATION.*`.
- Object flags are the exception — nested under the `obj` class itself, not the module:
  `lv.obj.FLAG.CLICKABLE`, not `lv.OBJ_FLAG.CLICKABLE`.

If a new `lv.SOMETHING` attribute error comes up, don't guess — grep the WSL
`lvgl_micropython` clone's generated
`lib/micropython/ports/esp32/build-ESP32_GENERIC_S3-SPIRAM_OCT/lv_mp.c` for the exact QSTR
registration to find where it actually lives.

## Text sizing: no larger fonts compiled in, scale labels instead

This firmware's LVGL binding only has `font_montserrat_12/14/16` compiled in — confirmed by
grepping the WSL `lvgl_micropython` clone's `lv_mp.c` (same file/technique as the binding
quirks above) for `font_montserrat_[0-9]*`. Default label text is 14px. There is no larger
compiled-in font (e.g. `font_montserrat_28`) to switch to, and adding one means enabling
`LV_FONT_MONTSERRAT_28` in `lv_conf.h` and rebuilding firmware — not a loose-file change.

**To make label text bigger without a firmware rebuild**, apply a render-time scale
transform to the label instead of changing font:

```python
label.set_style_transform_pivot_x(lv.pct(50), 0)
label.set_style_transform_pivot_y(lv.pct(50), 0)
label.set_style_transform_scale(lv.SCALE_NONE * 2, 0)  # 2x; SCALE_NONE (256) = 1x
label.center()
```

The two pivot lines are required, not cosmetic: LVGL's default transform pivot is the
object's top-left corner (0, 0), so scaling without them grows the label away from wherever
`.center()` placed it (drifting toward bottom-right) instead of growing in place. Setting the
pivot to `lv.pct(50)` on both axes anchors the scale to the label's own center, confirmed
against `lv_obj_pos.c`'s pivot/transform handling (LVGL 9.4).

This upscales the already-rendered 14px glyphs rather than drawing from higher-resolution
ones, so text reads a bit softer than a native larger font would — acceptable for this
project's touch-target labels (used in `touch_led_colors.py`), but worth remembering if
crisper large text is ever needed, which would require the firmware-rebuild route instead.

**No monospace font is compiled in either** (only proportional `font_montserrat_12/14/16`;
LVGL ships genuine monospace bitmap fonts — `LV_FONT_UNSCII_8`/`LV_FONT_UNSCII_16` — but both
are disabled in `lv_conf.h`, so enabling one is a firmware rebuild, same class of change as a
larger font). This matters for anything that periodically re-renders changing numeric text
(a clock, a counter): Montserrat-14's glyph advance widths are **not** equal — read directly
from `lv_font_montserrat_14.c`'s glyph table (its `adv_w` field is 8.4 fixed-point, so real
pixel width = `adv_w / 16`) — e.g. `'1'` is 5px wide vs. 9px for `'0'`/`'4'`/`'6'`/`'8'`/`'9'`.
A single auto-fit label re-centers its whole string on every `set_text()`, so digits visibly
shift horizontally whenever a digit's value changes. Without a firmware rebuild, the fix is a
**fixed-width character grid**: one `lv.label` per character position, laid out once in a
fixed-pitch row (each character's own center pinned via `align(lv.ALIGN.CENTER, dx, dy)`,
never repositioned again — only `set_text()` on each single-character label changes
thereafter), still using the pivot+scale trick above per character instead of per whole
string. `ntp_clock.py`'s `_make_char_row`/`_set_row_text` implement this; see there for the
measured per-glyph cell widths. (`set_style_text_letter_space` was checked and ruled out first
— confirmed in `lv_mp.c` that it only adds a constant gap on top of each glyph's own advance
width, which can't produce true monospacing.)

This combination — a `transform_scale`-enlarged label whose *text content* (not its
transform) changes periodically — also surfaced a separate LVGL invalidation gotcha: a stale
fragment of an earlier, differently-sized/positioned render can stay on screen indefinitely,
since `set_text()` only invalidates the label's *current* box, never the union with wherever a
prior render sat. `ntp_clock.py` works around this with an unconditional `screen.invalidate()`
every tick rather than relying on any label's own dirty-rect tracking — see the "Landscape
support" section above for the full story (it was originally mistaken for a display-driver
bug).

## Hardware-state gotcha (not a code bug)

If a script run raises an exception or is interrupted before finishing, **hard-reset the
board (physical RESET button / power-cycle) before the next run**, not just after editing
code. The display's QSPI bus and `esp_lcd` panel-IO handle are native ESP-IDF resources that
a soft-reset/re-run (or a REPL Ctrl-D) does not reliably free. An aborted run can leave enough
of the small internal DMA-capable memory pool allocated that the *next* run's display init
fails with `ESP_ERR_NO_MEM` (`ValueError: 257`) even though the code itself is correct — this
has been mistaken for a real regression more than once during this project's development.

**`mpremote connect COM12 ...` only performs this same soft reset**, confirmed directly
(a variable set in one `mpremote` invocation is gone in the next) — it does not perform a real
hardware reset either, so it doesn't reliably free these native resources any better than a
plain re-run would. When driving the board via `mpremote` for iterative testing, do an
explicit `mpremote connect COM12 reset` (this one does force a real hardware reset) plus a few
seconds' settle delay before any script that constructs `SPI.Bus`/`SPIBus`/a display driver
from scratch — otherwise expect confusing failures (e.g. `SPI.Bus()` construction raising a
plain `TypeError` instead of anything display-related) from the previous run's still-held
resources, not a real regression in the new code.
