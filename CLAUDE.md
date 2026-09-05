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

Only rotation 0 (native portrait) is implemented. Freenove's own C++ code does a manual
software pixel-shuffle in `Fill_Colors()` for landscape (rotations 1/3), because hardware
MADCTL rotation doesn't behave correctly on this panel — that workaround has not been ported.

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
(`Datasheet/ST77922_TDDI_Interface_Protocol_V01.00.pdf`, pulled from Freenove's GitHub repo —
see its Reporting Table section) — `st77922_touch.py`'s module docstring cites the exact
wording. `_get_coords()` always reads the full block for this reason.

That same datasheet documents one more register bit this driver now acts on: `0x0010` bit 7
is `RstChip` ("notifies host that the touch controller should be reset") — `_get_coords()`
calls `hw_reset()` when it sees this set, which the original Freenove C++ source doesn't
handle either.

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

## Hardware-state gotcha (not a code bug)

If a script run raises an exception or is interrupted before finishing, **hard-reset the
board (physical RESET button / power-cycle) before the next run**, not just after editing
code. The display's QSPI bus and `esp_lcd` panel-IO handle are native ESP-IDF resources that
a soft-reset/re-run (or a REPL Ctrl-D) does not reliably free. An aborted run can leave enough
of the small internal DMA-capable memory pool allocated that the *next* run's display init
fails with `ESP_ERR_NO_MEM` (`ValueError: 257`) even though the code itself is correct — this
has been mistaken for a real regression more than once during this project's development.
