# -*- coding: utf-8 -*-
#
# The one gesture vocabulary of omx-panel (schema/gestures.json) on Zynthian's panel controls. The scripts of
# fixtures/gestures/scripts.json are driven into the controllers of a real engine against the fake daemon; the value
# lands within one declared step of the one worked by hand from the control's own travel, a reset and a tap exact, and
# the daemon is asked for it back. The table, the timings and the thresholds come from the installed omx-panel (or the
# file ZYNTHIAN_OMX_GESTURES names): without it the tests FAIL, there is no copy of it in this tree. A declaration with
# other numbers moves the controls by other numbers, which is how the tests tell a literal from a reading.
#
# Copyright (C) 2026 Pau Aliagas <linuxnow@gmail.com>
# SPDX-License-Identifier: GPL-2.0-or-later

import copy
import json
import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_engine_plugin_hostd import plugin_hostd_case, FIXTURES, ROOT, ph  # noqa: F401  (sets up the stand-ins)
from fake_plugin_hostd import PLUGINS as fake_plugins  # noqa: E402
from zyngine import zynthian_panel_gestures as pg  # noqa: E402

GESTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "gestures")
with open(os.path.join(GESTURES, "scripts.json")) as _f:
    SCRIPTS = json.load(_f)
FILTER = {"urn:test:filter": {
    "params": {"cutoff": ("Hz", "log", 20.0, 20000.0, 1000.0), "gain": ("dB", "linear", -20.0, 20.0, 0.0),
               "mode": ("-", "stepped", 0.0, 3.0, 0.0), "active": ("-", "stepped", 0.0, 1.0, 0.0)},
    "info": True, "presets": None}}
ORDER = ["cutoff", "gain", "mode", "active"]
SOURCE = os.path.join(ROOT, "zyngine", "zynthian_panel_gestures.py")


def declared():
    try:
        return pg.load_table()
    except OSError as e:
        raise AssertionError(f"omx-panel's gesture declaration is absent ({e}): install omx-panel or set {pg.ENV_TABLE}")


class Event:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class panel_case(plugin_hostd_case):

    def setUp(self):
        super().setUp()
        self.table = declared()
        patcher = mock.patch.dict(fake_plugins, FILTER)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.info["JV/Filter"] = {**self.info["JV/Amp"], "ID": "Filter", "NAME": "Filter", "TITLE": "Filter",
                                  "URL": "urn:test:filter"}
        from zyngine import zynthian_plugin_hostd_catalogue as cat
        cat.add_catalogue(self.info, clap=[], binary=os.path.join(ROOT, "tests", "fake_plugin_hostd.py"))
        self.fake = self.serve(descriptors={"urn:test:filter": os.path.join(GESTURES, "filter.descriptor.json")})
        self.proc = self.add(self.chain(), "PH/Filter")
        self.zctrls = self.proc.controllers_dict

    def panel(self, table=None):
        return pg.Panel([self.zctrls[s] for s in ORDER], SCRIPTS["width"], SCRIPTS["height"], 10, table=table or self.table)

    def read_back(self, symbol):
        """What the daemon holds for it."""
        return float(self.fake.params[1000][symbol])

    def drive(self, script, table=None):
        panel = self.panel(table)
        zctrl = self.zctrls[script["symbol"]]
        i = ORDER.index(script["symbol"])
        row = panel.rows[i][1]
        top = row.target[1]
        zctrl.set_value(script.get("start", zctrl.value_default))  # a script begins where the plugin does, unless it says
        words = []
        panel.on_word = lambda z, w: words.append(w)
        for e in script["events"]:
            kind, t = e["type"], e["t"]
            y = top + e.get("y", 0)
            if kind == "down":
                panel.press(e["x"], y, t)
            elif kind == "move":
                panel.motion(e["x"], y, t, fine=e.get("fine", False))
            elif kind == "up":
                panel.release(e["x"], y, t)
            elif kind == "tick":
                panel.tick(t)
            elif kind == "wheel":
                row.step(e["n"], t, coarse=e.get("coarse", False), source="wheel")
            elif kind == "key":
                row.key(e["key"], t, shift=e.get("shift", False))
        return panel, row, zctrl, words


class ScriptedGestures(panel_case):

    def test_every_script_lands_within_one_step_and_the_daemon_holds_it(self):
        for script in SCRIPTS["scripts"]:
            with self.subTest(script["name"]):
                panel, row, zctrl, words = self.drive(script)
                want = script["expect"]["value"]
                if script["expect"]["exact"]:
                    self.assertEqual(zctrl.value, want)
                    self.assertEqual(self.read_back(script["symbol"]), float(want), "the daemon holds it exactly")
                else:
                    step = row.travel.step_of(want)
                    self.assertLessEqual(abs(zctrl.value - want), step, f"within one step ({step}) of {want}")
                    self.assertLessEqual(abs(self.read_back(script["symbol"]) - want), step + 1e-4)
                self.assertEqual(sorted({g["word"] for g in row.log}), script["expect"]["words"], "the gestures it was read as")

    def test_the_declared_step_is_the_gain_controls_not_one_percent(self):
        _, row, zctrl, _ = self.drive(next(s for s in SCRIPTS["scripts"] if s["name"].startswith("gain: one wheel")))
        self.assertEqual(zctrl.position_law["step"], 0.5)
        self.assertAlmostEqual(row.travel.step_norm(), 0.5 / 40)

    def test_a_tap_selects_and_a_long_press_is_more_and_neither_moves_a_value(self):
        for name in ("a tap selects", "a long press is more"):
            script = next(s for s in SCRIPTS["scripts"] if name in s["name"] and s["symbol"] == "cutoff")
            _, _, zctrl, words = self.drive(script)
            self.assertEqual(zctrl.value, script["start"])
            self.assertEqual(words, ["select"] if "tap" in name else ["more"])

    def test_a_read_only_control_takes_no_gesture(self):
        panel = self.panel()
        zctrl = self.zctrls["cutoff"]
        zctrl.set_value(2000.0)
        zctrl.readonly = True
        panel = self.panel()
        panel.press(100, 25, 0)
        panel.motion(200, 25, 20)
        panel.release(200, 25, 40)
        panel.rows[0][1].step(1, 100)
        self.assertEqual(zctrl.value, 2000.0)
        self.assertEqual({r["code"] for r in panel.rows[0][1].refused}, {"GESTURE_READ_ONLY"})

    def test_a_press_while_another_holds_the_control_is_refused(self):
        row = self.panel().rows[0][1]
        row.press(100, 25, 0, "a")
        row.press(120, 25, 50, "b")
        self.assertEqual([r["code"] for r in row.refused], ["GESTURE_CONTROL_OWNED"])

    def test_a_step_stays_the_steppers_through_the_claim(self):
        row = self.panel().rows[0][1]
        row.step(1, 0, source="wheel")
        row.step(1, 100, source="wheel")  # within the claim: one gesture
        row.step(1, 150, source="detent")  # another source in the claim: refused
        self.assertEqual(len(row.log), 1)
        self.assertEqual(len(row.log[0]["changes"]), 2)
        self.assertEqual([r["code"] for r in row.refused], ["GESTURE_CONTROL_OWNED"])
        row.step(1, 1000, source="detent")  # past the claim
        self.assertEqual(len(row.log), 2)


class ReadFromTheDeclaration(panel_case):
    """A declaration with other numbers moves the controls by other numbers: nothing here keeps its own."""

    def declaration(self, **changes):
        table = copy.deepcopy(self.table)
        for k, v in changes.items():
            if k in table["thresholds"]:
                table["thresholds"][k] = v
            else:
                table["timings"][k]["default"] = v
        return table

    def script(self, start):
        return next(s for s in SCRIPTS["scripts"] if s["name"].startswith(start))

    def test_the_fine_ratio(self):
        a, b = self.script("cutoff: drag 100"), self.script("cutoff: fine drag")
        base = self.drive(b)[2].value
        self.zctrls["cutoff"].set_value(1000.0)
        other = self.drive(b, self.declaration(fineRatio=0.6))[2].value
        self.assertGreater(other, base * 1.5, "twice the ratio, a longer move")

    def test_the_drag_start(self):
        s = self.script("cutoff: a press that stays")
        self.assertEqual(self.drive(s)[2].value, 1000.0)
        # with a start of 2 px the 8 px of that script is a drag, and it moves the value
        panel, row, zctrl, words = self.drive(s, self.declaration(dragStartPx=2))
        self.assertNotEqual(zctrl.value, 1000.0)

    def test_the_step_fraction_and_coarse_steps(self):
        s = self.script("cutoff: one wheel")
        self.drive(s)
        one = self.zctrls["cutoff"].value
        self.zctrls["cutoff"].set_value(1000.0)
        self.drive(s, self.declaration(stepFraction=0.05))
        self.assertGreater(self.zctrls["cutoff"].value, one * 1.3)

    def test_the_default_travel(self):
        s = self.script("cutoff: drag 100")
        # the drawn length of a row is the surface's; the declaration's 200 px applies where the surface gives none
        row = pg.Gestures("fader", pg.Travel.of(self.zctrls["cutoff"]), lambda: 1000.0, lambda v: None, (0, 0, 400, 50),
                          table=self.declaration(defaultTravelPx=100))
        got = []
        row.put = got.append
        row.press(100, 25, 0)
        row.motion(100, 5, 10)
        row.motion(100, -15, 20)
        row2 = pg.Gestures("fader", pg.Travel.of(self.zctrls["cutoff"]), lambda: 1000.0, lambda v: None, (0, 0, 400, 50),
                           table=self.table)
        got2 = []
        row2.put = got2.append
        row2.press(100, 25, 0)
        row2.motion(100, 5, 10)
        row2.motion(100, -15, 20)
        self.assertGreater(got[-1], got2[-1], "half the travel, a longer move for the same pixels")

    def test_the_double_click_time(self):
        s = self.script("cutoff: a double click")
        slow = copy.deepcopy(s)
        slow["events"][2]["t"] = 100  # unchanged: the second press at 100, the first release at 30
        self.assertEqual(self.drive(slow)[2].value, 1000.0)
        self.zctrls["cutoff"].set_value(8000.0)
        late = self.drive(slow, self.declaration(doubleTapMs=40))[2].value
        self.assertEqual(late, 8000.0, "70 ms apart is two taps when the declaration says 40")

    def test_the_long_press_time(self):
        s = self.script("cutoff: a long press")
        _, _, _, words = self.drive(s)
        self.assertEqual(words, ["more"])
        _, _, _, words = self.drive(s, self.declaration(longPressMs=700))
        self.assertNotIn("more", words, "held 650 ms is a tap when the declaration says 700")

    def test_no_declaration_means_no_gestures_not_a_copy(self):
        env = {k: v for k, v in os.environ.items() if k != pg.ENV_TABLE}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(pg.GestureTableAbsent):
                pg.table_path()

    def test_the_module_keeps_none_of_the_declarations_numbers(self):
        with open(SOURCE) as f:
            code = re.sub(r"#.*", "", re.sub(r'""".*?"""', "", f.read(), flags=re.S))
        t = self.table
        numbers = {v for v in t["thresholds"].values()} | {v["default"] for v in t["timings"].values()}
        for n in numbers:
            self.assertIsNone(re.search(rf"(?<![\w.\"]){re.escape(str(n))}(?![\w.])", code), f"a literal {n} in the module")


class BarAndHandleFromTheTable(panel_case):
    """A bar and a handle are read from the table by their key, as a fader is: drag, fine, step and reset move the
    value; a handle's wheel is its Q, which Zynthian does not take; nothing moves on a key the table gives it no word."""

    def row(self, widget):
        z = self.zctrls["gain"]
        z.set_value(0.0)
        return pg.Gestures(widget, pg.Travel.of(z), lambda: z.value, z.set_value, (0, 0, 400, 50),
                           orientation="horizontal", travel_px=380, table=self.table), z

    def test_a_bar_moves_as_declared(self):
        row, z = self.row("bar")
        row.step(1, 0)
        self.assertAlmostEqual(z.value, 0.5)  # the declared step of the gain
        row.key("BackSpace", 1000)
        self.assertEqual(z.value, 0.0)
        self.assertEqual([g["word"] for g in row.log], ["step", "reset"])

    def test_a_handles_wheel_is_its_q_and_moves_no_value(self):
        row, z = self.row("handle")
        row.step(1, 0)
        self.assertEqual(z.value, 0.0)
        self.assertEqual([r["code"] for r in row.refused], ["GESTURE_NOT_TAKEN"])
        row.key("Up", 1000)  # the arrow is a step
        self.assertAlmostEqual(z.value, 0.5)

    def test_a_handle_drags_and_a_bar_is_not_a_switch(self):
        row, z = self.row("handle")
        row.press(100, 25, 0)
        row.motion(200, 25, 20)
        row.motion(300, 25, 40)
        row.release(300, 25, 60)
        self.assertGreater(z.value, 0.0)
        self.assertEqual(row.log[0]["word"], "drag")


class OnTheTkEvents(panel_case):
    """Tk's events through the helpers the widget binds."""

    def test_shift_drag_is_fine_the_wheel_is_a_step_and_shift_wheel_is_five(self):
        panel = self.panel()
        row = panel.rows[0][1]
        pg.tk_press(panel, Event(x=100, y=25, time=0))
        pg.tk_motion(panel, Event(x=120, y=25, time=20, state=pg.SHIFT))
        pg.tk_motion(panel, Event(x=220, y=25, time=40, state=pg.SHIFT))
        pg.tk_release(panel, Event(x=220, y=25, time=60))
        self.assertEqual(row.log[0]["word"], "fine")
        pg.tk_wheel(row, Event(num=4, state=0, time=5000))
        pg.tk_wheel(row, Event(num=5, delta=0, state=pg.SHIFT, time=9000))
        self.assertEqual([g["input"] for g in row.log], ["dragFine", "wheel", "wheelCoarse"])


if __name__ == "__main__":
    unittest.main()
