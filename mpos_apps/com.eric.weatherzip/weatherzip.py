"""Weather Zip: sets the US zip code whose outside temperature the top bar shows.

On Save, the zip is looked up once on zippopotam.us, and the zip, latitude,
longitude and place name are saved in the com.eric.weatherzip preferences.
The top bar (lib/fnk0104n_topbar.py) reads lat/lon from there and fetches the
temperature from open-meteo.com; refresh_weather() makes it fetch right away.
"""
import ujson

import lvgl as lv
from mpos import Activity, DownloadManager, SharedPreferences, TaskManager

PREFS = "com.eric.weatherzip"
LOOKUP_URL = "https://api.zippopotam.us/us/"


class WeatherZip(Activity):

    def onCreate(self):
        self.task = None
        prefs = SharedPreferences(PREFS)

        screen = lv.obj()
        screen.set_style_pad_all(8, 0)

        title = lv.label(screen)
        title.set_text('Zip code for the outside temperature')
        title.align(lv.ALIGN.TOP_LEFT, 0, 0)

        self.status = lv.label(screen)
        self.status.align(lv.ALIGN.TOP_LEFT, 0, 24)
        zip_code, place = prefs.get_string("zip"), prefs.get_string("place")
        if zip_code:
            self.status.set_text('Saved: %s (%s)' % (zip_code, place or '?'))
        else:
            self.status.set_text('No zip code set')

        self.entry = lv.textarea(screen)
        self.entry.set_one_line(True)
        self.entry.set_accepted_chars('0123456789')
        self.entry.set_max_length(5)
        self.entry.set_placeholder_text('Zip code')
        self.entry.set_text(zip_code or '')
        self.entry.set_width(160)
        self.entry.align(lv.ALIGN.TOP_LEFT, 0, 50)

        save = lv.button(screen)
        save.set_size(100, 40)
        save.align_to(self.entry, lv.ALIGN.OUT_RIGHT_MID, 16, 0)
        save.add_event_cb(lambda e: self._save(), lv.EVENT.CLICKED, None)
        save_label = lv.label(save)
        save_label.set_text('Save')
        save_label.center()

        keyboard = lv.keyboard(screen)
        keyboard.set_mode(lv.keyboard.MODE.NUMBER)
        keyboard.set_textarea(self.entry)
        keyboard.set_size(lv.pct(100), lv.pct(55))
        keyboard.align(lv.ALIGN.BOTTOM_MID, 0, 0)
        keyboard.add_event_cb(lambda e: self._save(), lv.EVENT.READY, None)

        self.setContentView(screen)

    def onPause(self, screen):
        super().onPause(screen)
        if self.task is not None and not self.task.done():
            self.task.cancel()
        self.task = None

    def _save(self):
        zip_code = self.entry.get_text()
        if len(zip_code) != 5:
            self.status.set_text('Enter a 5-digit zip code')
            return
        if self.task is not None and not self.task.done():
            return  # a lookup is already running
        self.status.set_text('Looking up %s...' % zip_code)
        self.task = TaskManager.create_task(self._lookup(zip_code))

    async def _lookup(self, zip_code):
        try:
            data = await DownloadManager.download_url(LOOKUP_URL + zip_code)
        except Exception as e:
            if '404' in str(e):
                self.status.set_text('Unknown zip code: %s' % zip_code)
            else:
                self.status.set_text('Lookup failed - check WiFi')
            return
        try:
            place = ujson.loads(data)["places"][0]
            lat, lon = place["latitude"], place["longitude"]
            name = '%s, %s' % (place["place name"], place["state abbreviation"])
        except Exception:
            self.status.set_text('Unexpected reply for %s' % zip_code)
            return

        editor = SharedPreferences(PREFS).edit()
        editor.put_string("zip", zip_code)
        editor.put_string("lat", lat)
        editor.put_string("lon", lon)
        editor.put_string("place", name)
        editor.commit()
        self.status.set_text('Saved: %s (%s)' % (zip_code, name))

        try:
            import fnk0104n_topbar
            fnk0104n_topbar.refresh_weather()
        except Exception:
            pass  # the top bar picks the new location up within a minute anyway
