"""Minimal MicroPythonOS app for the FNK0104N, meant to be copied as a starting point.

Shows the pieces most apps need: building the screen in onCreate, handling a touch
event, and an lv.timer that only runs while the app is in the foreground (started in
onResume, deleted in onPause).
"""
import time

import lvgl as lv
from mpos import Activity


class Template(Activity):

    def onCreate(self):
        self.taps = 0
        self.timer = None
        self.started_ms = time.ticks_ms()

        screen = lv.obj()

        title = lv.label(screen)
        title.set_text('Template app')
        title.align(lv.ALIGN.TOP_MID, 0, 20)

        button = lv.button(screen)
        button.set_size(160, 60)
        button.center()
        button.add_event_cb(self._on_tap, lv.EVENT.CLICKED, None)
        button_label = lv.label(button)
        button_label.set_text('Tap me')
        button_label.center()

        self.count_label = lv.label(screen)
        self.count_label.set_text('Taps: 0')
        self.count_label.align_to(button, lv.ALIGN.OUT_BOTTOM_MID, 0, 16)

        self.uptime_label = lv.label(screen)
        self.uptime_label.align(lv.ALIGN.BOTTOM_MID, 0, -20)
        self._update_uptime(None)

        self.setContentView(screen)

    def onResume(self, screen):
        super().onResume(screen)
        if self.timer is None:
            self.timer = lv.timer_create(self._update_uptime, 1000, None)

    def onPause(self, screen):
        super().onPause(screen)
        if self.timer is not None:
            self.timer.delete()
            self.timer = None

    def _on_tap(self, event):
        self.taps += 1
        self.count_label.set_text('Taps: {}'.format(self.taps))

    def _update_uptime(self, timer):
        seconds = time.ticks_diff(time.ticks_ms(), self.started_ms) // 1000
        self.uptime_label.set_text('Open for {} s'.format(seconds))
