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
that same idea — see the next section for the real hardware issues that surfaced building it
and how they were fixed.

## Landscape support (`ST77922Landscape`) — a real panel limitation, not a software bug

`ST77922Landscape` presents LVGL a logical 480x320 canvas and remaps every flush by hand onto
the physical, still-portrait-native 320x480 panel (MADCTL is never touched — see its class
docstring for why neither hardware rotation nor LVGL's own software-rotation path
(`LV_DRAW_TRANSFORM_USE_MATRIX=0`) are usable here). Getting this working on real hardware
took three separate, stacked fixes — worth knowing about before touching this class again:

- **The naive per-chunk rotation loop was catastrophically slow: ~8 seconds per chunk**,
  measured directly on hardware (not estimated) — a plain-Python loop doing a 2-byte
  `memoryview` slice-copy per pixel, ~12,480 iterations for one 480x26 chunk. A full-screen
  redraw (13+ chunks) took well over a minute, and `ntp_clock.py`'s once-a-second refresh timer
  was re-running that cost every second, forever — also the real explanation for a separately
  -observed "REPL becomes fully unresponsive" symptom. **Fixed with `@micropython.viper`**
  (confirmed this firmware build supports both `@micropython.viper` and `@micropython.native`
  — tested directly on hardware, not assumed): rewriting the same index arithmetic as a
  `ptr16`-typed pointer-indexing viper function instead of `memoryview` slice objects measured
  **~2200x faster (3.6ms vs. 8.0s for the same chunk)**, confirmed byte-identical output
  against the old loop. See `_rotate_chunk_rgb565` in `st77922.py`. (Also confirmed on hardware
  while chasing this: MicroPython's `array`/`bytearray`/`memoryview` here only support
  `step=1` slicing — no vectorized strided-copy trick was ever on the table.)
- **This specific ST77922 panel cannot handle a narrow-CASET/full-RASET window write, under
  any tested condition** — this is the real explanation for what looked like generic visual
  "corruption" (green background instead of black, garbled text) for a long stretch of this
  feature's development. Isolated via a series of throwaway hardware-only diagnostic scripts
  that stripped away LVGL, rotation math, and Python entirely: a *single*, isolated, all-black
  narrow-CASET(≤`BUFFER_ROWS`px)/full-RASET(0-479) write — the very first QSPI content write
  after `display.init()`, nothing sent before it — renders green with random noise instead of
  black. Repeating it, synchronizing every write so each DMA transfer completes before the
  next begins, and substituting RAMWR for RAMWRC all made no difference — still green. A
  full-CASET/narrow-RASET write (portrait mode's shape) of the identical color is always clean.
  This rules out rotation math, buffer lifetime, timing/races, and command semantics — it's
  this specific window shape, full stop, that the panel's controller cannot handle, for reasons
  no amount of software-level testing without a logic analyzer could pin down further.
  Landscape's row-based LVGL chunking combined with a 90-degree rotation architecturally forces
  exactly this window shape for any naive per-chunk send (chunked logical rows always span the
  full logical width, which becomes the full physical Y range after rotation, while the
  chunked logical dimension becomes the narrow physical X range) — so the fix works around it
  rather than solving it: `_flush_cb` now blits each chunk's rotated pixels into a persistent,
  full-panel-sized (320x480x2 byte, PSRAM+DMA) staging framebuffer instead of sending anything
  to hardware, and only on `flush_is_last()` does it write to the panel for real — by
  re-slicing the *complete* accumulated image into 40-row, full-CASET/narrow-RASET bands
  (matching portrait mode's own proven-safe chunk size exactly) and sending those,
  synchronously. Never send a narrow-CASET/full-RASET window to this panel again.
- **A stale-text "ghosting" bug**, found only after the two fixes above (a full redraw is fast
  enough now that this could actually be observed cleanly): `ntp_clock.py`'s per-second
  `set_text()` on a `transform_scale`-enlarged label only invalidates that label's *current*
  box, never the union with wherever an earlier, differently-sized/positioned render happened
  to sit — so a stale fragment of the very first render stayed on screen indefinitely. Likely
  an LVGL invalidation-vs-`transform_scale` interaction (see "Text sizing" below), not anything
  in `ST77922Landscape` itself. Fixed pragmatically in `ntp_clock.py` with an unconditional
  `screen.invalidate()` every tick rather than root-causing the LVGL interaction further —
  cheap now that a full redraw is ~100ms instead of 60+ seconds.

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

## `machine.SDCard` has a non-standard, project-specific constructor

Don't trust upstream MicroPython's `machine.SDCard` docs, or the file at
`lib/micropython/ports/esp32/machine_sdcard.c` in the WSL `lvgl_micropython` clone, for this
project's firmware — **`lvgl_micropython`'s build overlays its own replacement file,
`micropy_updates/esp32/machine_sdcard.c`, on top of the upstream one**, and that overlay has a
different constructor signature. This cost real debugging time (`music_player.py`'s SD/MMC
mount call, working from the upstream file's kwargs, failed twice with different errors before
the overlay file turned up — `micropy_updates/` also holds a similarly-overlaid
`machine_hw_spi.c`, worth checking first for the same reason if `machine.SPI`/`SPI.Bus`
behavior ever looks off from upstream docs too).

The overlay's SD/MMC-mode (`slot=0/1`) kwargs, confirmed by reading that file directly: `slot`,
`width`, `clk` (**not** `sck`), `cmd`, `data_pins` (**not** `data`) — all plain ints, with
`data_pins` a tuple of ints (`mp_obj_get_int()` directly, not `machine_pin_get_id()`, so
`machine.Pin` objects don't work here — pass raw GPIO numbers). `cd`/`wp` also exist as ints
(`-1` = unused). SPI mode (`spi_bus=` + `cs=`) takes an already-constructed `SPI.Bus` object
(this project's own custom SPI class, same one `hello_world_display.py` uses for the display's
QSPI bus) rather than raw `sck`/`mosi`/`miso` pins like upstream's SPI-mode `machine.SDCard`
does. `machine.I2C`/`machine.I2S` are **not** overlaid — those remain the unmodified upstream
implementations (which accept either a `Pin` object or a raw int for pin arguments).

## `machine.I2S` has no MCLK (`mck=`) support on this firmware — and the PWM workaround for it didn't hold up

Confirmed by reading `lib/micropython/ports/esp32/machine_i2s.c` in the WSL `lvgl_micropython`
clone directly (not overlaid — see above — this is genuinely the file this firmware is built
from): it only recognizes `sck`/`ws`/`sd`, no `mck`. Easy to get backwards, because MicroPython
*does* have `mck=` support for `machine.I2S` — just not on the ESP32 port specifically. Other
ports (rp2, stm32, mimxrt) share a generic `extmod/machine_i2s.c` that has it;
`mpconfigport.h`'s `MICROPY_PY_MACHINE_I2S_INCLUDEFILE` points the ESP32 port at its own
`ports/esp32/machine_i2s.c` instead, which predates/lacks that addition.

The first fix tried was generating the ES8311's MCLK in software via `machine.PWM`
(`freq=sample_rate*256`), matching what the community driver `es8311.py` was ported from does
on boards with the same limitation. **This did not work reliably on hardware**: real audio
played for a few seconds, then permanently cut out with zero software-visible error —
confirmed (by wrapping the WAV player's I2S IRQ callback with a counter) that the SD-card
reads and I2S non-blocking writes kept running correctly and continuously the entire time, so
the failure was downstream of all of that, consistent with the codec's PLL briefly locking
onto the synthesized clock and then losing lock.

**Fix confirmed on hardware (2026-09-08): don't use MCLK at all.** The ES8311 has a documented alternate mode where it
derives its internal clock from BCLK (the I2S bit clock — a real, ESP32-hardware-generated
clock, not synthesized) instead of a dedicated MCLK line — register `0x01` bit 7 (`MCLK_SEL`:
0 = MCLK pin, 1 = BCLK). Confirmed against Espressif's own official driver
(`espressif/esp-bsp`, `components/es8311/es8311.c`, fetched directly rather than trusting a
paraphrase) for the exact clock-coefficient math. For this project's fixed case
(44100Hz/16-bit, what `mp3_to_wav.py` produces), that driver's `coeff_div[]` table turned up a
convenient fact: BCLK-source mode and the old 256×-ratio MCLK-pin mode need *identical*
clock-manager register values (0x03–0x08) at 44100Hz — only two registers actually differ:
`0x01` (`0x3F`→`0xBF`, the mode-select bit above) and `0x02` (`0x00`→`0x18`, a pre-multiplier
compensating for BCLK being 1/8th the frequency the old table assumed). See `es8311.py` for
the corrected table and full writeup. This eliminates the PWM/MCLK pin entirely — GPIO17
(Freenove's documented `I2S_MCK` pin) goes unused by this driver as a result.

One caveat worth remembering if a different sample rate is ever needed: Espressif's
`coeff_div[]` table only tabulates a handful of specific MCLK/rate pairs, not every possible
ratio for every rate — BCLK-as-source mode only has a matching table row where `rate × 32`
happens to be one of those tabulated values (44100Hz's is, luckily). A real multi-rate player
would need to either look up/recompute the right row per file, or resample everything to
44100Hz ahead of time (already the plan, via `mp3_to_wav.py`) to sidestep the issue.

Switching to BCLK-as-source made things *worse* at first (total silence, not even the old
PWM version's brief burst) — because BCLK-as-source means the codec has to lock onto BCLK,
and `music_player.py`'s `play()` was configuring the codec (`ES8311.power_on()`) **before**
`WavPlayer.play()` ever created the I2S channel and started BCLK actually toggling. There was
nothing for the codec to derive a clock from yet. Fixed by reordering `play()`: start I2S
first (`WavPlayer.play()`), then power on the codec. Obvious in hindsight, but easy to get
backwards when porting code that assumed an always-present external MCLK, which doesn't have
this ordering dependency the same way.

With the clock/sequencing fixes above all correctly applied, playback was *still* silent —
because a separate, unrelated bug was stacked on top of all of it: `music_player.py`'s speaker
amp enable pin (GPIO1) was being driven the wrong polarity. docs.freenove.com never documented
which level enables the amp, so it was guessed active-high; on-hardware testing (isolating the
amp-enable pin with a synthetic tone generated directly over I2S, bypassing SD/WAV entirely,
so the codec's byte-perfect I2C register writes and healthy I2S pipeline could be trusted and
GPIO1 treated as the only remaining variable) showed it's actually active-LOW. This alone was
enough to fully explain an earlier round of "codec config verified byte-perfect, I2S pipeline
verified healthy, still total silence" debugging that had otherwise pointed toward a downstream
amp hardware fault — the amp was simply being told to stay off the whole time. Worth
remembering if silent/no-output symptoms come up again here: don't assume a hardware fault
before double-checking enable-pin polarity assumptions sourced from docs.freenove.com, since at
least one of them (this one) was wrong.

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
