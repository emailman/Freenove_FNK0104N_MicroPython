"""
WiFi + NTP + Mountain Time helpers, shared by scripts that need a real wall-clock time.

Copied from ntp_clock.py (which keeps its own copy for now -- it's confirmed working on hardware
and hasn't been switched over). WiFi credentials come from the gitignored wifi_secrets.py (copy
wifi_secrets.py.example and fill it in).

The RTC holds UTC after sync_time(); mountain_time() applies the US DST rules locally, since
there's no tz database on this firmware.
"""

import time

import network
import ntptime

import wifi_secrets


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


def sync_time(retries=3):
    """Sets the RTC to UTC over NTP. Returns True on success, False if every attempt failed."""
    for attempt in range(1, retries + 1):
        try:
            ntptime.settime()
            print("NTP sync OK, RTC (UTC) now:", time.localtime())
            return True
        except Exception as exc:  # NOQA -- OSError on socket timeout/unreachable host, etc.
            print("NTP sync attempt %d/%d failed: %r" % (attempt, retries, exc))
            time.sleep(1)
    print("NTP sync failed after %d attempts -- RTC may be stale/wrong." % retries)
    return False


# US DST runs from the 2nd Sunday in March at 2AM local standard time to the 1st Sunday in
# November at 2AM local daylight time; both boundaries are computed directly from the raw UTC
# calendar fields, comparing (month, day, hour, minute) tuples.

def _day_of_week(year, month, day):
    """0=Sunday..6=Saturday, via Zeller's congruence (Gregorian calendar)."""
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
