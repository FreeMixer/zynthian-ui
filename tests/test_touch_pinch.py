# -*- coding: utf-8 -*-
#
# The pinch of omx-panel's gesture table on a multi-touch screen: synthetic two-slot streams of the evdev multi-touch
# protocol go through zynthian_touch_layer.Slots into a panel whose gain row has the cutoff as its Q; the Q lands within
# one declared step of the one worked by hand (the first Q times the spread's ratio), the daemon holds it, and every
# number (palm guard, scaling) is the table's. Also the encoder 3 and Shift+arrow paths of the same word.
#
# Copyright (C) 2026 Pau Aliagas <linuxnow@gmail.com>
# SPDX-License-Identifier: GPL-2.0-or-later

import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_panel_gestures import panel_case, SCRIPTS, ORDER, Event, pg  # noqa: E402,F401
from zyngine import zynthian_touch_layer as touch  # noqa: E402

DEV = 4096  # the device's range on both axes
A, X, Y, TID, SLOT = touch.EV_ABS, touch.ABS_MT_POSITION_X, touch.ABS_MT_POSITION_Y, touch.ABS_MT_TRACKING_ID, touch.ABS_MT_SLOT


class PinchOnTouch(panel_case):

    def setUp(self):
        super().setUp()
        self.panel = pg.Panel([self.zctrls[s] for s in ORDER], SCRIPTS["width"], SCRIPTS["height"], 10, table=self.table,
                              seconds={"gain": self.zctrls["cutoff"]})
        self.row = self.panel.rows[1][1]
        self.w, self.h = SCRIPTS["width"], SCRIPTS["height"]
        self.slots = touch.Slots(touch.route(self.panel), ((0, DEV), (0, DEV)), (self.w, self.h))
        self.zctrls["cutoff"].set_value(1000.0)

    def dev(self, x, y):
        """A canvas point of the gain row in device units."""
        ty = self.row.target[1]
        return round(x / self.w * DEV), round((ty + y) / self.h * DEV)

    def frame(self, t, *fingers):
        """One SYN_REPORT frame: (slot, tracking id or None, x, y) of each finger that changes."""
        for slot, tid, x, y in fingers:
            self.slots.feed(A, SLOT, slot)
            if tid is not None:
                self.slots.feed(A, TID, tid)
            if x is not None:
                self.slots.feed(A, X, x)
                self.slots.feed(A, Y, y)
        self.slots.feed(touch.EV_SYN, touch.SYN_REPORT, 0, t)

    def pinch(self, t1=50, dx0=60, dx1=120):
        (xa, ya), (xb, yb) = self.dev(100, 20), self.dev(100 + dx0, 20)
        self.frame(0, (0, 7, xa, ya))
        self.frame(t1, (1, 8, xb, yb))
        (xb2, _) = self.dev(100 + dx1, 20)
        self.frame(t1 + 40, (1, None, xb2, yb))
        self.frame(t1 + 80, (0, -1, None, None), (1, -1, None, None))

    def test_a_spread_scales_q_by_the_ratio_of_the_spread(self):
        self.pinch(dx0=60, dx1=120)
        q = self.zctrls["cutoff"]
        step = self.row.second[0].step_of(2000.0)
        self.assertLessEqual(abs(q.value - 2000.0), step, "twice the spread, twice the Q")
        self.assertLessEqual(abs(float(self.fake.params[1000]["cutoff"]) - 2000.0), step + 1e-3, "the daemon holds it")
        self.assertEqual([g["word"] for g in self.row.log], ["second"])
        self.assertEqual(self.row.log[0]["input"], "pinch")
        self.assertEqual(self.zctrls["gain"].value, 0.0, "the gain is where it was")

    def test_a_pinch_in_halves_it_and_a_release_ends_it(self):
        self.pinch(dx0=120, dx1=60)
        self.assertLessEqual(abs(self.zctrls["cutoff"].value - 500.0), self.row.second[0].step_of(500.0))
        self.assertIsNone(self.row.owner)
        self.assertEqual(self.row.pointers, {})

    def test_q_stays_in_its_own_range(self):
        self.pinch(dx0=10, dx1=380)
        self.assertEqual(self.zctrls["cutoff"].value, self.zctrls["cutoff"].value_max)
        self.assertEqual(max(self.row.log[0]["changes"]), self.zctrls["cutoff"].value_max, "asked for no more than the range")

    def test_a_second_finger_after_the_palm_guard_is_refused(self):
        guard = self.table["timings"]["palmGuardMs"]["default"]
        self.pinch(t1=guard + 100)
        self.assertEqual(self.zctrls["cutoff"].value, 1000.0)
        self.assertIn("GESTURE_CONTROL_OWNED", [r["code"] for r in self.row.refused])

    def test_a_mouse_and_a_finger_are_not_a_pinch(self):
        self.panel.press(100, self.row.target[1] + 20, 0, "mouse1", "mouse")
        self.panel.press(160, self.row.target[1] + 20, 10, "touch9", "touch")
        self.panel.motion(300, self.row.target[1] + 20, 20, "touch9")
        self.assertEqual(self.zctrls["cutoff"].value, 1000.0)

    def test_the_palm_guard_is_the_declarations(self):
        import copy
        table = copy.deepcopy(self.table)
        table["timings"]["palmGuardMs"]["default"] = 20
        panel = pg.Panel([self.zctrls[s] for s in ORDER], self.w, self.h, 10, table=table, seconds={"gain": self.zctrls["cutoff"]})
        slots = touch.Slots(touch.route(panel), ((0, DEV), (0, DEV)), (self.w, self.h))
        self.slots = slots
        self.pinch(t1=50)  # 50 ms is late for a guard of 20
        self.assertEqual(self.zctrls["cutoff"].value, 1000.0)

    def test_encoder_3_and_shift_arrows_and_the_wheel_step_q(self):
        row = self.row
        one = row.second[0].step_of(1000.0)
        row.step(1, 0, source="encoder3")
        up = self.zctrls["cutoff"].value
        self.assertGreater(up, 1000.0)
        self.assertLessEqual(abs(up - 1000.0), one * 1.5)
        row.key("Up", 1000, shift=True)
        self.assertGreater(self.zctrls["cutoff"].value, up)
        row.step(-1, 2000, source="wheel")
        self.assertLess(self.zctrls["cutoff"].value, self.zctrls["cutoff"].value_max)
        self.assertEqual(self.zctrls["gain"].value, 0.0, "none of them moves the row's own value")
        self.assertEqual([g["input"] for g in row.log], ["encoder3", "arrowSecond", "wheel"])

    def test_a_pointer_that_is_one_finger_is_a_drag_of_the_row(self):
        (xa, ya) = self.dev(100, 20)
        (xb, _) = self.dev(200, 20)
        self.frame(0, (0, 3, xa, ya))
        self.frame(30, (0, None, self.dev(130, 20)[0], ya))  # crosses the drag start
        self.frame(45, (0, None, xb, ya))
        self.frame(60, (0, -1, None, None))
        self.assertNotEqual(self.zctrls["gain"].value, 0.0)
        self.assertEqual(self.zctrls["cutoff"].value, 1000.0)


class EvdevRefusals(unittest.TestCase):

    def fake_evdev(self, devices):
        m = types.ModuleType("evdev")
        m.list_devices = lambda: list(devices)

        def InputDevice(p):
            d = devices[p]
            if isinstance(d, Exception):
                raise d
            return d
        m.InputDevice = InputDevice
        return m

    def test_no_device_is_said(self):
        with mock.patch.dict(sys.modules, {"evdev": self.fake_evdev({})}):
            with self.assertRaisesRegex(touch.NoTouchDevice, "no input device reports multi-touch slots"):
                touch.EvdevSource()

    def test_a_refused_device_is_named_with_the_systems_words(self):
        err = PermissionError(13, "Permission denied")
        with mock.patch.dict(sys.modules, {"evdev": self.fake_evdev({"/dev/input/event5": err})}):
            with self.assertRaisesRegex(touch.NoTouchDevice, r"/dev/input/event5: .*Permission denied"):
                touch.EvdevSource()


if __name__ == "__main__":
    unittest.main()
