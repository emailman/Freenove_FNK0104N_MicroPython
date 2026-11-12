"""
Live flight tracker for the Freenove FNK0104N's onboard 3.5" display (ST77922, shown as 480x320
landscape) and integrated touch.

Shows the aircraft within an adjustable radius of an airport as a radar-style map: the airport
in the center, range rings, and one dot per aircraft, colored by altitude, with two small
dots ahead of it showing its track and its callsign beside it. Tap a dot for details.
The side panel has the airport (tap it to type another code), the radius (- / + step through
5..250 nm) and the status of the last update. The airport and radius are saved to
/flight_tracker.json.

Data: adsb.lol's free ADS-B API, https://api.adsb.lol/v2/point/{lat}/{lon}/{radius_nm}, polled
every POLL_S seconds over WiFi (credentials from wifi_secrets.py, see local_time.py). It
returns each aircraft's distance ("dst", nm) and bearing ("dir") from the point, which is all
the map needs. (airplanes.live has the same API, but turns away clients that haven't
registered by email.)

Airport codes are looked up in /airports.txt, built on the PC with make_airports.py:
    .venv\\Scripts\\python.exe make_airports.py
    mpremote connect COM12 cp airports.txt :airports.txt

Drawing: every update repositions a fixed pool of plain lv.obj dots and labels (nothing is
created or deleted per update) after one map.invalidate(), so an update costs one 320x320
redraw. lv.line isn't used: an experiment with it on this firmware crashed the board.

A fetch takes 2.5-7 s on the board (mostly the TLS handshake and the network), so it runs on a
_thread that only does the network and JSON work and hands back a result; the LVGL timer does
all the drawing, since LVGL isn't thread-safe. MicroPython's requests isn't used: it sends
HTTP/1.0, which adsb.lol rejects with 426 Upgrade Required (see http_get()).

Requires the custom lvgl_micropython firmware -- see hello_world_display.py / README.md.
"""

import gc
import json
import math
import time

import lvgl as lv
import lcd_bus
from machine import SPI

import st77922
from task_handler import TaskHandler

DISPLAY_WIDTH = 480   # logical/landscape size LVGL renders into -- see ST77922Landscape
DISPLAY_HEIGHT = 320
BUFFER_ROWS = 26      # same proven-safe chunk size as ntp_clock.py

API_URL = "https://api.adsb.lol/v2/point/%.4f/%.4f/%d"
# Route (origin/destination) per callsign, from adsb.lol's copy of the VRS standing data.
# These come from schedules, not ADS-B, so a route can be missing or out of date.
ROUTE_URL = "https://vrs-standing-data.adsb.lol/routes/%s/%s.json"
USER_AGENT = "fnk0104n-flight-tracker"
AIRPORTS_FILE = "/airports.txt"
SETTINGS_FILE = "/flight_tracker.json"

RADII = (5, 10, 25, 50, 100, 250)   # nm
POLL_S = 10
MIN_GAP_MS = 1500                   # stay well under the API's rate limit
MAX_AC = 60                         # the closest MAX_AC aircraft are drawn

# Per-update log lines. Off by default: once `mpremote run` exits, nothing reads the USB serial
# port, and a print() that fills its buffer blocks forever. That's the likely cause of a full
# freeze seen on hardware 2026-11-12. Turn on only while a terminal is attached.
DEBUG = False

MAP_SIZE = 320
MAP_C = MAP_SIZE // 2
MAP_R = 150                         # pixels for the full radius

PANEL_X = MAP_SIZE
PANEL_W = DISPLAY_WIDTH - MAP_SIZE

C_BG = 0x000000
C_PANEL = 0x202020
C_RING = 0x1E5A1E
C_TEXT = 0xC0C0C0
C_SELECT = 0xFFFF00
C_GROUND = 0x808080
C_LOW = 0x30D030      # below 10,000 ft
C_MID = 0xF0D020      # 10,000 - 25,000 ft
C_HIGH = 0x30C0FF     # above 25,000 ft

# ---- display + touch ------------------------------------------------------------------------

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
    rgb565_byte_swap=True,
)
display.init()
display.set_power(True)
display.set_backlight(100)

# Loose-file imports after display init -- see CLAUDE.md.
gc.collect()
import i2c
import st77922_touch

touch_i2c_bus = i2c.I2C.Bus(host=0, scl=39, sda=38, freq=100_000)
touch_device = i2c.I2C.Device(
    bus=touch_i2c_bus, dev_id=st77922_touch.I2C_ADDR, reg_bits=st77922_touch.BITS
)
touch = st77922_touch.ST77922LTouch(device=touch_device, reset_pin=48, int_pin=47)

gc.collect()
import _thread
import network
import socket
import ssl
import local_time

# ---- settings + airport lookup --------------------------------------------------------------


def _load_settings():
    try:
        with open(SETTINGS_FILE) as f:
            s = json.load(f)
        if s.get("radius") in RADII and s.get("code"):
            return s
    except (OSError, ValueError):
        pass
    return {"code": "MSO", "radius": 25}


def _save_settings():
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump({"code": _airport[0], "radius": _radius}, f)
    except OSError as e:
        print("Couldn't save settings:", e)


_airports_data = None


def lookup_airport(code):
    """Returns (code, lat, lon, name) for an IATA, ICAO/ident or local code, or None.

    IATA matches win over ident/local ones (so MSO is Missoula, not some airport whose ident
    happens to be MSO).
    """
    code = code.strip().upper()
    if not code or "," in code:
        return None
    global _airports_data
    if _airports_data is None:
        # ~490 KB, read once (~2.7 s from flash) and kept in PSRAM; bytes.find on it is fast.
        try:
            with open(AIRPORTS_FILE, "rb") as f:
                _airports_data = b"\n" + f.read()
        except OSError:
            print("No", AIRPORTS_FILE, "-- see the module docstring")
            return None
    data = _airports_data
    key = b"," + code.encode() + b","
    fallback = None
    pos = data.find(key)
    while pos >= 0:
        start = data.rfind(b"\n", 0, pos) + 1
        end = data.find(b"\n", pos)
        ident, iata, local, lat, lon, name = data[start:end].decode().split(",", 5)
        if code in (ident, iata, local):
            hit = (code, float(lat), float(lon), name)
            if code == iata:
                return hit
            fallback = fallback or hit
        pos = data.find(key, pos + 1)
    if fallback is None:  # ident is the first field, so it has no leading comma
        pos = data.find(b"\n" + code.encode() + b",")
        if pos >= 0:
            end = data.find(b"\n", pos + 1)
            ident, iata, local, lat, lon, name = data[pos + 1:end].decode().split(",", 5)
            fallback = (code, float(lat), float(lon), name)
    return fallback


def http_get(url, timeout=15, not_found_ok=False):
    """Minimal HTTPS GET returning the body as bytes.

    MicroPython's requests sends HTTP/1.0, which adsb.lol rejects with 426 Upgrade Required,
    so this speaks HTTP/1.1 itself (Connection: close; content-length or chunked bodies).
    Returns None for a 404 when not_found_ok is set.
    """
    _, _, host, path = url.split("/", 3)
    ai = socket.getaddrinfo(host, 443, 0, socket.SOCK_STREAM)[0]
    s = socket.socket(ai[0], socket.SOCK_STREAM)
    try:
        s.settimeout(timeout)
        s.connect(ai[-1])
        s = ssl.wrap_socket(s, server_hostname=host)
        s.write(("GET /%s HTTP/1.1\r\nHost: %s\r\nUser-Agent: %s\r\n"
                 "Accept: application/json\r\nConnection: close\r\n\r\n"
                 % (path, host, USER_AGENT)).encode())
        status = s.readline().split(None, 2)
        code = int(status[1])
        length = None
        chunked = False
        while True:
            line = s.readline()
            if not line or line == b"\r\n":
                break
            k, _, v = line.partition(b":")
            k = k.strip().lower()
            if k == b"content-length":
                length = int(v)
            elif k == b"transfer-encoding" and b"chunked" in v.lower():
                chunked = True
        if code == 404 and not_found_ok:
            return None
        if code != 200:
            raise OSError("HTTP %d" % code)
        if chunked:
            parts = []
            while True:
                n = int(s.readline().split(b";")[0], 16)
                if n == 0:
                    break
                parts.append(_read_exact(s, n))
                s.readline()  # CRLF after each chunk
            return b"".join(parts)
        if length is not None:
            return _read_exact(s, length)
        parts = []
        while True:
            b = s.read(4096)
            if not b:
                return b"".join(parts)
            parts.append(b)
    finally:
        s.close()


def _read_exact(s, n):
    buf = bytearray(n)
    mv = memoryview(buf)
    got = 0
    while got < n:
        k = s.readinto(mv[got:])
        if not k:
            raise OSError("connection closed early")
        got += k
    return buf


_settings = _load_settings()
_radius = _settings["radius"]
_airport = lookup_airport(_settings["code"]) or lookup_airport("MSO") \
    or ("MSO", 46.9158, -114.0911, "Missoula Montana Airport")
print("Airport:", _airport, "radius", _radius, "nm")

# ---- UI -------------------------------------------------------------------------------------

FONT_12 = lv.font_montserrat_12
FONT_16 = lv.font_montserrat_16

screen = lv.screen_active()
screen.set_style_bg_color(lv.color_hex(C_BG), 0)
screen.set_style_bg_opa(lv.OPA.COVER, 0)
screen.remove_flag(lv.obj.FLAG.SCROLLABLE)


def _box(parent, x, y, w, h, color, radius=0):
    """A plain filled rectangle (no theme styling)."""
    o = lv.obj(parent)
    o.remove_style_all()
    o.set_style_bg_color(lv.color_hex(color), 0)
    o.set_style_bg_opa(lv.OPA.COVER, 0)
    o.set_style_radius(radius, 0)
    o.set_pos(x, y)
    o.set_size(w, h)
    o.remove_flag(lv.obj.FLAG.CLICKABLE)
    o.remove_flag(lv.obj.FLAG.SCROLLABLE)
    return o


def _label(parent, text, x, y, color=C_TEXT, font=FONT_12):
    lb = lv.label(parent)
    lb.set_text(text)
    lb.set_style_text_color(lv.color_hex(color), 0)
    lb.set_style_text_font(font, 0)
    lb.set_pos(x, y)
    return lb


# Map: rings, airport cross, north marker.
map_obj = _box(screen, 0, 0, MAP_SIZE, MAP_SIZE, C_BG)
map_obj.add_flag(lv.obj.FLAG.CLICKABLE)  # tapping empty map clears the selection

for frac in (1, 2):
    r = MAP_R * frac // 2
    ring = lv.obj(map_obj)
    ring.remove_style_all()
    ring.set_style_border_color(lv.color_hex(C_RING), 0)
    ring.set_style_border_width(1, 0)
    ring.set_style_radius(lv.RADIUS_CIRCLE, 0)
    ring.set_size(2 * r + 1, 2 * r + 1)
    ring.set_pos(MAP_C - r, MAP_C - r)
    ring.remove_flag(lv.obj.FLAG.CLICKABLE)
# Inner ring (radius/2) first, then outer (radius); each label just inside the bottom of its
# ring, clear of the N marker at the top.
ring_labels = [_label(map_obj, "", MAP_C + 3, MAP_C + MAP_R * f // 2 - 16, C_RING) for f in (1, 2)]
_box(map_obj, MAP_C - 5, MAP_C, 11, 1, 0xFFFFFF)
_box(map_obj, MAP_C, MAP_C - 5, 1, 11, 0xFFFFFF)
_label(map_obj, "N", MAP_C - 4, 0, C_RING, FONT_16)

# Aircraft pool: dot, two heading ticks, callsign label.
_pool = []
for _i in range(MAX_AC):
    dot = _box(map_obj, 0, 0, 7, 7, C_LOW, lv.RADIUS_CIRCLE)
    dot.add_flag(lv.obj.FLAG.CLICKABLE)
    dot.set_ext_click_area(10)
    t1 = _box(map_obj, 0, 0, 3, 3, C_LOW)
    t2 = _box(map_obj, 0, 0, 2, 2, C_LOW)
    lb = _label(map_obj, "", 0, 0)
    for o in (dot, t1, t2, lb):
        o.add_flag(lv.obj.FLAG.HIDDEN)
    _pool.append((dot, t1, t2, lb))

# Side panel.
panel = _box(screen, PANEL_X, 0, PANEL_W, DISPLAY_HEIGHT, C_PANEL)

airport_btn = lv.button(panel)
airport_btn.set_pos(5, 5)
airport_btn.set_size(PANEL_W - 10, 40)
airport_btn_label = lv.label(airport_btn)
airport_btn_label.set_style_text_font(FONT_16, 0)
airport_btn_label.center()
airport_name = _label(panel, "", 5, 50)
airport_name.set_width(PANEL_W - 10)

minus_btn = lv.button(panel)
minus_btn.set_pos(5, 88)
minus_btn.set_size(40, 36)
_l = lv.label(minus_btn)
_l.set_text("-")
_l.set_style_text_font(FONT_16, 0)
_l.center()
plus_btn = lv.button(panel)
plus_btn.set_pos(PANEL_W - 45, 88)
plus_btn.set_size(40, 36)
_l = lv.label(plus_btn)
_l.set_text("+")
_l.set_style_text_font(FONT_16, 0)
_l.center()
radius_label = lv.label(panel)
radius_label.set_style_text_font(FONT_16, 0)
radius_label.set_style_text_color(lv.color_hex(0xFFFFFF), 0)
radius_label.align(lv.ALIGN.TOP_MID, 0, 97)

status_label = _label(panel, "Starting...", 5, 132)
status_label.set_width(PANEL_W - 10)
_box(panel, 5, 168, PANEL_W - 10, 1, 0x505050)
details_label = _label(panel, "Tap a plane\nfor details", 5, 175, 0xFFFFFF)
details_label.set_width(PANEL_W - 10)

# Altitude legend.
for _n, (_c, _t) in enumerate(((C_GROUND, "ground"), (C_LOW, "< 10k ft"),
                               (C_MID, "10-25k ft"), (C_HIGH, "> 25k ft"))):
    _y = 258 + _n * 15
    _box(panel, 8, _y + 4, 7, 7, _c, lv.RADIUS_CIRCLE)
    _label(panel, _t, 22, _y, C_TEXT)

# ---- state ----------------------------------------------------------------------------------

_gen = 0                      # bumped when the airport/radius changes; stale results are dropped
_want_fetch = True            # fetch as soon as possible
_busy = False                 # a fetch thread is running
_result = None                # handed from the fetch thread to the LVGL timer
_last_fetch = time.ticks_add(time.ticks_ms(), -MIN_GAP_MS)
_last_ok = None               # ticks of the last successful update
_error = None
_aircraft = []                # list of (hex, dst, dir, record) drawn on the map
_selected = None              # hex of the selected aircraft
_routes = {}                  # callsign -> "DEN > ONT", "no route", or None while looking up
_route_want = None            # callsign waiting for a route lookup
_route_result = None          # (callsign, text) handed from the route thread
_status_text = ""


def _log(*args):
    if DEBUG:
        print(*args)


def _alt_color(alt):
    if alt is None or alt == "ground":
        return C_GROUND
    if alt < 10000:
        return C_LOW
    if alt < 25000:
        return C_MID
    return C_HIGH


def _set_status(text):
    global _status_text
    if text != _status_text:  # set_text always invalidates; skip the redraw when unchanged
        _status_text = text
        status_label.set_text(text)


def _update_status():
    lines = []
    if _last_ok is not None:
        lines.append("%d aircraft" % len(_aircraft))
    if _busy:
        lines.append("Updating...")
    elif _error:
        lines.append(_error)
    elif _last_ok is not None:
        lines.append("updated %ds ago" % (time.ticks_diff(time.ticks_ms(), _last_ok) // 1000))
    _set_status("\n".join(lines))


def _show_airport():
    airport_btn_label.set_text(_airport[0])
    airport_name.set_text(_airport[3])
    radius_label.set_text("%d nm" % _radius)
    for f, lb in zip((1, 2), ring_labels):
        v = _radius * f / 2
        lb.set_text(("%d" % v if v == int(v) else "%.1f" % v) + " nm")


def _show_details():
    for hx, dst, brg, ac in _aircraft:
        if hx == _selected:
            alt = ac.get("alt_baro")
            alt_s = "ground" if alt == "ground" else ("%d ft" % alt if alt is not None else "?")
            trk = ac.get("track", ac.get("true_heading"))
            cs = _flight(ac)
            route = _routes.get(cs, "") if cs else "no callsign"
            details_label.set_text(
                "%s  %s\n%s\n%s\n%s  %s kt\ntrk %s  %.1f nm" % (
                    _callsign(ac), ac.get("t", ""),
                    route if route is not None else "route...",
                    ac.get("r", hx),
                    alt_s,
                    "%d" % ac["gs"] if "gs" in ac else "?",
                    "%d" % trk if trk is not None else "?",
                    dst,
                ))
            return
    details_label.set_text("Tap a plane\nfor details")


def _flight(ac):
    """The broadcast callsign (e.g. DAL354), or "" if there isn't one."""
    return (ac.get("flight") or "").strip()


def _callsign(ac):
    return (ac.get("flight") or "").strip() or ac.get("r") or ac.get("hex", "?")


def _draw_aircraft():
    scale = MAP_R / _radius
    map_obj.invalidate()  # one big dirty area; the per-object ones below merge into it
    for i, (dot, t1, t2, lb) in enumerate(_pool):
        if i >= len(_aircraft):
            for o in (dot, t1, t2, lb):
                o.add_flag(lv.obj.FLAG.HIDDEN)
            continue
        hx, dst, brg, ac = _aircraft[i]
        b = math.radians(brg)
        x = int(MAP_C + dst * scale * math.sin(b) + 0.5)
        y = int(MAP_C - dst * scale * math.cos(b) + 0.5)
        color = lv.color_hex(_alt_color(ac.get("alt_baro")))
        dot.set_pos(x - 3, y - 3)
        dot.set_style_bg_color(color, 0)
        dot.remove_flag(lv.obj.FLAG.HIDDEN)
        trk = ac.get("track", ac.get("true_heading"))
        if trk is None:
            t1.add_flag(lv.obj.FLAG.HIDDEN)
            t2.add_flag(lv.obj.FLAG.HIDDEN)
        else:
            sx, sy = math.sin(math.radians(trk)), -math.cos(math.radians(trk))
            t1.set_pos(int(x + 7 * sx + 0.5) - 1, int(y + 7 * sy + 0.5) - 1)
            t2.set_pos(int(x + 11 * sx + 0.5) - 1, int(y + 11 * sy + 0.5) - 1)
            t1.set_style_bg_color(color, 0)
            t2.set_style_bg_color(color, 0)
            t1.remove_flag(lv.obj.FLAG.HIDDEN)
            t2.remove_flag(lv.obj.FLAG.HIDDEN)
        lb.set_text(_callsign(ac))
        lb.set_pos(x + 6, y - 2)
        lb.set_style_text_color(lv.color_hex(C_SELECT if hx == _selected else C_TEXT), 0)
        lb.remove_flag(lv.obj.FLAG.HIDDEN)
    _show_details()


def _fetch_worker(gen, lat, lon, radius):
    """Runs on its own thread: network + JSON only, never LVGL (it isn't thread-safe)."""
    global _result, _busy
    t0 = time.ticks_ms()
    try:
        wlan = network.WLAN(network.STA_IF)
        if not wlan.isconnected():
            local_time.connect_wifi()
        body = http_get(API_URL % (lat, lon, radius))
        t1 = time.ticks_ms()
        data = json.loads(body)
        body = None
        acs = []
        for ac in data.get("ac", ()):
            dst, brg = ac.get("dst"), ac.get("dir")
            if dst is None or brg is None or dst > radius:
                continue
            acs.append((ac.get("hex", "?"), dst, brg, ac))
        data = None
        acs.sort(key=lambda a: a[1])
        _result = (gen, acs, None, time.ticks_diff(t1, t0), time.ticks_diff(time.ticks_ms(), t1))
    except Exception as e:  # NOQA -- network errors of every kind, shown and retried
        _result = (gen, None, ("Error: %s" % e)[:40], 0, 0)
    gc.collect()
    _busy = False


def _route_worker(cs):
    """Runs on its own thread, like _fetch_worker: looks up one callsign's route."""
    global _route_result, _busy
    try:
        body = http_get(ROUTE_URL % (cs[:2], cs), not_found_ok=True)
        text = "no route"
        if body:
            r = json.loads(body)
            codes = r.get("_airport_codes_iata") or r.get("airport_codes") or ""
            if codes:
                text = " > ".join(codes.split("-"))
        _route_result = (cs, text)
    except Exception as e:  # NOQA -- shown, and retried on the next tap
        _route_result = (cs, "route error")
        _log("Route lookup failed:", cs, repr(e))
    gc.collect()
    _busy = False


def _apply_result():
    global _result, _aircraft, _last_ok, _error
    gen, acs, err, fetch_ms, parse_ms = _result
    _result = None
    if gen != _gen:
        return  # the airport or radius changed while this was in flight
    if err:
        _error = err
        _log("Fetch failed:", err)
        return
    t0 = time.ticks_ms()
    _aircraft = acs[:MAX_AC]
    _last_ok = t0
    _error = None
    _draw_aircraft()
    _log("%d aircraft (%d shown), fetch %dms, parse %dms, draw %dms, free %d"
          % (len(acs), len(_aircraft), fetch_ms, parse_ms,
             time.ticks_diff(time.ticks_ms(), t0), gc.mem_free()))


def _request_fetch():
    global _gen, _want_fetch
    _gen += 1
    _want_fetch = True


def _tick(_t):
    global _busy, _want_fetch, _last_fetch, _route_want, _route_result
    if _route_result is not None:
        cs, text = _route_result
        _route_result = None
        _routes[cs] = text
        _show_details()
    if _result is not None:
        _apply_result()
        # Time the next poll from when this result arrived, not when the fetch started:
        # fetches can take 8-11 s, which would otherwise start the next one immediately.
        _last_fetch = time.ticks_ms()
    now = time.ticks_ms()
    since = time.ticks_diff(now, _last_fetch)
    # One network thread at a time (each TLS connection needs its own internal RAM); a route
    # lookup goes first, since someone is waiting on it.
    if not _busy and _route_want:
        _busy = True
        cs, _route_want = _route_want, None
        _thread.start_new_thread(_route_worker, (cs,))
    elif not _busy and since >= MIN_GAP_MS and (_want_fetch or since >= POLL_S * 1000):
        _busy = True
        _want_fetch = False
        _last_fetch = now
        _thread.start_new_thread(_fetch_worker, (_gen, _airport[1], _airport[2], _radius))
    _update_status()


# ---- events ---------------------------------------------------------------------------------


def _on_dot(e):
    global _selected, _route_want
    dot = e.get_target_obj()
    for i, p in enumerate(_pool):
        if p[0] == dot and i < len(_aircraft):
            _selected = _aircraft[i][0]
            cs = _flight(_aircraft[i][3])
            if cs and _routes.get(cs, "route error") == "route error":
                _routes[cs] = None  # shows "route..." until the lookup finishes
                _route_want = cs
            break
    _draw_aircraft()


def _on_map(_e):
    global _selected
    if _selected is not None:
        _selected = None
        _draw_aircraft()


def _change_radius(step):
    global _radius
    i = RADII.index(_radius) + step
    if 0 <= i < len(RADII):
        _radius = RADII[i]
        _save_settings()
        _show_airport()
        _aircraft.clear()
        _draw_aircraft()
        _request_fetch()


def _open_keyboard(_e):
    overlay = _box(screen, 0, 0, DISPLAY_WIDTH, DISPLAY_HEIGHT, C_BG)
    overlay.add_flag(lv.obj.FLAG.CLICKABLE)
    ta = lv.textarea(overlay)
    ta.set_size(300, 46)
    ta.set_pos(5, 2)
    ta.set_one_line(True)
    ta.set_max_length(4)
    ta.set_placeholder_text("Airport code")
    msg = _label(overlay, "e.g. MSO, DEN, KSEA", 315, 16)
    kb = lv.keyboard(overlay)
    kb.set_size(DISPLAY_WIDTH, DISPLAY_HEIGHT - 50)
    kb.set_pos(0, 50)
    kb.set_textarea(ta)
    kb.set_mode(lv.keyboard.MODE.TEXT_UPPER)

    closed = [False]

    def _close():
        closed[0] = True
        overlay.delete_async()

    def _on_kb(e):
        global _airport, _selected
        if closed[0]:  # the OK key sends READY to both the keyboard and the textarea
            return
        if e.get_code() == lv.EVENT.CANCEL:
            _close()
            return
        found = lookup_airport(ta.get_text())
        if found is None:
            msg.set_text("Unknown code")
            return
        _airport = found
        _selected = None
        _log("Airport:", _airport)
        _save_settings()
        _show_airport()
        _aircraft.clear()
        _draw_aircraft()
        _request_fetch()
        _close()

    # Enter on a one-line textarea sends READY to the textarea only, not the keyboard; the OK
    # (checkmark) key sends it to both.
    ta.add_event_cb(_on_kb, lv.EVENT.READY, None)
    kb.add_event_cb(_on_kb, lv.EVENT.READY, None)
    kb.add_event_cb(_on_kb, lv.EVENT.CANCEL, None)


for _p in _pool:
    _p[0].add_event_cb(_on_dot, lv.EVENT.CLICKED, None)
map_obj.add_event_cb(_on_map, lv.EVENT.CLICKED, None)
minus_btn.add_event_cb(lambda _e: _change_radius(-1), lv.EVENT.CLICKED, None)
plus_btn.add_event_cb(lambda _e: _change_radius(1), lv.EVENT.CLICKED, None)
airport_btn.add_event_cb(_open_keyboard, lv.EVENT.CLICKED, None)

_show_airport()

_thread.stack_size(32 * 1024)  # TLS needs more than the default thread stack
task_handler = TaskHandler()
_timer = lv.timer_create(_tick, 200, None)

print("Flight tracker running.")
