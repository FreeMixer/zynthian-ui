# -*- coding: utf-8 -*-
#
# The gestures of omx-panel on a REAL plugin-hostd and a real LV2 on the Pi, by hand (not collected by CI): the engine
# of this tree adds the plugin to a daemon already listening on PORT (a jackd of its own name and a plugin-hostd the
# caller started), a Panel of its controllers is driven by scripted gestures, and the value is read back from the daemon.
#
#   ZYNTHIAN_OMX_GESTURES=gestures.json ZYNTHIAN_OMX_SCALE_VECTORS=scale-vectors.json PHD_PORT=17555 \
#   PHD_URI=http://lsp-plug.in/plugins/lv2/compressor_mono python3 tests/pi/panel_gestures_on_the_pi.py
#
# Copyright (C) 2026 Pau Aliagas <linuxnow@gmail.com>
# SPDX-License-Identifier: GPL-2.0-or-later

import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
from test_engine_plugin_hostd import plugin_hostd_case, ROOT, ph  # noqa: E402
from zyngine import zynthian_panel_gestures as pg  # noqa: E402
from zyngine import zynthian_touch_layer as touch  # noqa: E402
from zyngine import zynthian_plugin_hostd_catalogue as cat  # noqa: E402

PORT = int(os.environ.get("PHD_PORT", "17555"))
URI = os.environ.get("PHD_URI", "http://lsp-plug.in/plugins/lv2/compressor_mono")


class OnThePi(plugin_hostd_case):

    def setUp(self):
        super().setUp()
        os.environ["PATH"] = "/usr/bin:/bin"  # omx-panel and node, as the engine finds them on the Pi
        self.port = PORT
        ph.daemon = ph.plugin_hostd_daemon(PORT)
        ph.daemon.start_script = os.path.join(self.tmp, "not-installed")
        self.info["JV/Real"] = {**self.info["JV/Amp"], "ID": "Real", "NAME": "Real", "TITLE": "Real", "URL": URI}
        cat.add_catalogue(self.info, clap=[], binary="/usr/bin/plugin-hostd")
        self.table = pg.load_table()
        self.proc = self.add(self.chain(), "PH/Real")
        self.z = self.proc.controllers_dict

    def held(self, symbol):
        return float(ph.daemon.call(f"param_get {self.proc.engine.instance} {symbol}")[1].split()[0])

    def test_list(self):
        for s, z in self.z.items():
            print(s, z.value_min, z.value_max, z.value_default, z.value, z.position_law, z.is_toggle, bool(z.ticks), z.is_integer, flush=True)

    def test_drive(self):
        sel = [s for s, z in self.z.items() if z.position_law and not z.readonly][:1]  # bypass is the engine's, not a daemon param
        rows = [self.z[s] for s in sel]
        q = self.z.get("mid_high") or self.z.get("q")  # stands in for a band's Q: a log-free range of its own
        panel = pg.Panel(rows, 400, 100 * len(rows), 10, table=self.table, seconds={rows[0].symbol: q} if q else None)
        words = []
        panel.on_word = lambda z, w: words.append((z.symbol, w))
        a = rows[0]
        row = panel.rows[0][1]
        print("control", a.symbol, a.value_min, a.value_max, a.position_law, "start", a.value, "daemon", self.held(a.symbol), flush=True)
        a.set_value(a.value_default)
        t = 0
        # drag: 90 px right of where the 10 px start was crossed = 90/380 of the travel
        panel.press(100, 10, t); panel.motion(115, 10, t + 10); panel.motion(205, 10, t + 20); panel.release(205, 10, t + 30)
        pos = row.travel.position(a.value)
        print("drag   :", a.value, "daemon", self.held(a.symbol), "words", [g["word"] for g in row.log], flush=True)
        t += 1000
        row.key("Up", t)  # the row is a handle here: its wheel is the Q, its arrow a step
        print("arrow  :", a.value, "daemon", self.held(a.symbol), flush=True)
        t += 1000
        row.key("Down", t)
        print("arrow dn:", a.value, "daemon", self.held(a.symbol), flush=True)
        t += 1000
        panel.press(100, 10, t); panel.release(100, 10, t + 20); panel.press(100, 10, t + 100); panel.release(100, 10, t + 120)
        print("dbl-clk:", a.value, "default", a.value_default, "daemon", self.held(a.symbol), flush=True)
        if len(rows) > 1:
            tg = rows[1]
            tg.set_value(tg.value_min)
            y = panel.rows[1][1].target[1] + 10
            t += 1000
            panel.press(100, y, t); panel.release(100, y, t + 20)
            print("toggle :", tg.symbol, tg.value, "daemon", self.held(tg.symbol), flush=True)
        if q:
            q.set_value(q.value_min)
            q.set_value(2000.0)
            slots = touch.Slots(touch.route(panel), ((0, 4096), (0, 4096)), (400, 100 * len(rows)))
            def frame(t, *fingers):
                for slot, tid, x, y in fingers:
                    slots.feed(touch.EV_ABS, touch.ABS_MT_SLOT, slot)
                    if tid is not None:
                        slots.feed(touch.EV_ABS, touch.ABS_MT_TRACKING_ID, tid)
                    if x is not None:
                        slots.feed(touch.EV_ABS, touch.ABS_MT_POSITION_X, x)
                        slots.feed(touch.EV_ABS, touch.ABS_MT_POSITION_Y, y)
                slots.feed(touch.EV_SYN, touch.SYN_REPORT, 0, t)
            t += 2000
            y = round(20 / (100 * len(rows)) * 4096)
            frame(t, (0, 21, 1000, y)); frame(t + 40, (1, 22, 1600, y)); frame(t + 80, (1, None, 2200, y)); frame(t + 120, (0, -1, None, None), (1, -1, None, None))
            print("pinch  :", q.symbol, "from 2000 with the spread doubled ->", q.value, "daemon", self.held(q.symbol), "words", [g["word"] for g in row.log][-1], flush=True)
            q.set_value(2000.0)
            row.step(1, t + 5000, source="encoder3")
            print("enc3   :", q.symbol, q.value, "daemon", self.held(q.symbol), flush=True)
        print("words", words, flush=True)


if __name__ == "__main__":
    unittest.main()
