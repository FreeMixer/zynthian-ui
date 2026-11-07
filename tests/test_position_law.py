# -*- coding: utf-8 -*-
#
# The position law of a plugin-hostd controller: a Python twin of omx-contract's scalePosition and scaleValue, held to
# the golden vectors the omx-contract-data package installs (/usr/share/omx-contract/scale-vectors.json, or
# ZYNTHIAN_OMX_SCALE_VECTORS). Without that file the tests FAIL: there is no copy of the vectors in this tree.
# Every other engine's controller keeps upstream's is_logarithmic law, and the tests say so by value.
#
# Copyright (C) 2026 Pau Aliagas <linuxnow@gmail.com>
# SPDX-License-Identifier: GPL-2.0-or-later

import math
import os
import sys
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path[:0] = [HERE, ROOT]

for _name in ("pexpect", "pyliblo3", "zynconf", "zynautoconnect", "zyncoder", "zyncoder.zyncore"):
    sys.modules.setdefault(_name, mock.MagicMock())
if "zyngine" not in sys.modules:
    _pkg = types.ModuleType("zyngine")
    _pkg.__path__ = [os.path.join(ROOT, "zyngine")]
    sys.modules["zyngine"] = _pkg
from zyngine import zynthian_position_law as law  # noqa: E402
from zyngine import zynthian_controller as ctrl_mod  # noqa: E402
from zyngine import zynthian_engine_plugin_hostd as ph  # noqa: E402  (imported for the wiring test)


def _import_gui_controller():
    """The drawing code subclasses tkinter.Canvas and reads zynthian_gui_config: stand-ins for the import only, taken
    out again so the other test files see what they saw before."""
    names = ("tkinter", "tkinter.font", "zyngui", "zyngui.zynthian_gui_config", "zyngui.zynthian_gui_controller")
    saved = {n: sys.modules.get(n) for n in names}
    tk = types.ModuleType("tkinter")
    tk.Canvas = type("Canvas", (), {})
    tk.font = types.ModuleType("tkinter.font")
    gui = types.ModuleType("zyngui")
    gui.__path__ = [os.path.join(ROOT, "zyngui")]
    sys.modules.update({"tkinter": tk, "tkinter.font": tk.font, "zyngui": gui,
                        "zyngui.zynthian_gui_config": mock.MagicMock()})
    gui.zynthian_gui_config = sys.modules["zyngui.zynthian_gui_config"]
    try:
        import importlib
        return importlib.import_module("zyngui.zynthian_gui_controller")
    finally:
        for n, m in saved.items():
            if m is None:
                sys.modules.pop(n, None)
            else:
                sys.modules[n] = m


gui_mod = _import_gui_controller()

ULP = 2.220446049250313e-16


def close(a, b):
    """within one ulp, relative"""
    return a == b or abs(a - b) <= ULP * max(abs(a), abs(b), 1e-300)


def vectors():
    # fails loudly, never skips: the installed vectors are the oracle
    try:
        return law.load_vectors()
    except OSError as e:
        raise AssertionError(
            f"the installed scale vectors are absent ({law.VECTORS_PATH}): install omx-contract-data "
            f"or set ZYNTHIAN_OMX_SCALE_VECTORS ({e})")


class TwinAgainstVectors(unittest.TestCase):
    def test_vectors_are_installed_and_complete(self):
        v = vectors()
        self.assertEqual(sorted(v["laws"]), ["linear", "log", "map", "stepped"])
        for name, d in v["laws"].items():
            self.assertGreaterEqual(len(d["rows"]), 6, name)

    def test_position_and_value_both_directions_within_one_ulp(self):
        n = 0
        for name, d in vectors()["laws"].items():
            for row in d["rows"]:
                with self.subTest(law=name, value=row["value"]):
                    self.assertTrue(close(law.scale_position(d["travel"], row["value"]), row["p"]),
                                    (law.scale_position(d["travel"], row["value"]), row["p"]))
                    want = row.get("valueAtP", row["value"])
                    self.assertTrue(close(law.scale_value(d["travel"], row["p"]), want),
                                    (law.scale_value(d["travel"], row["p"]), want))
                    n += 1
        self.assertGreaterEqual(n, 24)

    def test_one_khz_on_twenty_hz_to_twenty_khz_is_0_5663(self):
        p = law.scale_position({"scale": "log", "min": 20, "max": 20000}, 1000)
        self.assertEqual(round(p, 4), 0.5663)
        self.assertNotEqual(round(p, 4), 0.5)

    def test_log_over_a_floor_that_is_not_positive_is_linear(self):
        self.assertEqual(law.scale_position({"scale": "log", "min": 0, "max": 10}, 5), 0.5)


def zctrl(**opts):
    base = {"value_min": 20.0, "value_max": 20000.0, "value": 1000.0, "is_integer": False}
    base.update(opts)
    return ctrl_mod.zynthian_controller(None, "freq", base)


PH_TRAVEL = {"scale": "log", "min": 20.0, "max": 20000.0}


def plot(z):
    g = object.__new__(gui_mod.zynthian_gui_controller)
    g.hidden = False
    g.zctrl = z
    g.selector_counter = False
    g.format_print = None
    g.value_plot = None
    g.value_print = None
    g.calculate_plot_values()
    return g.value_plot


def upstream_log_plot(z):
    return math.log10((9 * z.value - (10 * z.value_min - z.value_max)) / z.value_range)


class Controllers(unittest.TestCase):
    def test_plugin_hostd_controller_draws_1khz_at_the_law_position(self):
        z = zctrl(is_logarithmic=True, position_law=PH_TRAVEL)
        self.assertEqual(round(plot(z), 4), 0.5663)

    def test_jalv_controller_draws_as_upstream(self):
        z = zctrl(is_logarithmic=True)
        self.assertIsNone(z.position_law)
        self.assertEqual(plot(z), upstream_log_plot(z))
        self.assertNotEqual(round(plot(z), 4), 0.5663)

    def test_jalv_linear_controller_draws_as_upstream(self):
        z = zctrl(value_min=0.0, value_max=10.0, value=2.5)
        self.assertEqual(plot(z), 0.25)

    def test_nudge_follows_the_law_for_plugin_hostd_and_upstream_for_the_rest(self):
        z = zctrl(is_logarithmic=True, position_law=PH_TRAVEL)
        z.nudge(1, send=False)
        want = law.scale_value(PH_TRAVEL, law.scale_position(PH_TRAVEL, 1000.0) + z.nudge_factor)
        self.assertEqual(z.value, want)
        j = zctrl(is_logarithmic=True)
        j.nudge(1, send=False)
        lv = math.log10((9 * 1000.0 - (10 * 20.0 - 20000.0)) / 19980.0)
        self.assertEqual(j.value, (math.pow(10, min(1, lv + j.nudge_factor)) * 19980.0 + (10 * 20.0 - 20000.0)) / 9)
        self.assertNotEqual(z.value, j.value)

    def test_midi_law_of_a_jalv_controller_is_untouched(self):
        j = zctrl(is_logarithmic=True, midi_cc_val1=20.0, midi_cc_val2=20000.0)
        self.assertEqual(j.get_ctrl_midi_val(), int(127 * upstream_log_plot(j)))
        j.set_value(20.0, send=False)
        j.midi_cc_mode = 0
        j.midi_control_change(64, send=False)
        self.assertEqual(j.value, 20.0 + 19980.0 * (math.pow(10, 64 / 127) - 1) / 9)

    def test_cc_sweep_lands_where_the_knob_draws(self):
        for cc in (0, 1, 32, 64, 96, 126, 127):
            z = zctrl(is_logarithmic=True, position_law=PH_TRAVEL, midi_cc_val1=20.0, midi_cc_val2=20000.0)
            z.midi_cc_mode = 0
            z.midi_control_change(cc, send=False)
            self.assertAlmostEqual(plot(z), cc / 127, places=12)
            self.assertEqual(z.get_ctrl_midi_val(), cc)

    def test_cc_of_a_linear_plugin_hostd_controller_lands_where_it_draws(self):
        t = {"scale": "linear", "min": -12.0, "max": 12.0}
        z = zctrl(value_min=-12.0, value_max=12.0, value=0.0, position_law=t, midi_cc_val1=-12.0, midi_cc_val2=12.0)
        z.midi_cc_mode = 0
        z.midi_control_change(64, send=False)
        self.assertAlmostEqual(plot(z), 64 / 127, places=12)


class EngineWiring(unittest.TestCase):
    def controllers(self, rows, info):
        eng = object.__new__(ph.zynthian_engine_plugin_hostd)
        eng.instance, eng.type, eng.labels = 1000, "MIDI Synth", {}
        with mock.patch.object(eng, "pages", return_value=[("Page 1", rows)]), \
                mock.patch.object(eng, "read_values", return_value={}), \
                mock.patch.object(eng, "send_quiet", side_effect=lambda line: info.get(line.split()[-1])):
            return eng.build_controllers()

    def test_its_controllers_carry_the_travel_by_scale(self):
        rows = [("freq", "Freq", "Hz", {"travel": {"scale": "log", "min": 20, "max": 20000}}),
                ("gain", "Gain", "dB", {"travel": {"scale": "linear", "min": -12, "max": 12}}),
                ("wave", "Wave", "", {"travel": {"scale": "stepped", "min": 0, "max": 3}}),
                ("on", "On", "", {"kind": "toggle", "default": 0})]
        z = self.controllers(rows, {})
        self.assertEqual(z["freq"].position_law, {"scale": "log", "min": 20, "max": 20000})
        self.assertEqual(round(plot(self._at(z["freq"], 1000.0)), 4), 0.5663)
        self.assertEqual(z["gain"].position_law["scale"], "linear")
        self.assertIsNone(z["wave"].position_law)
        self.assertIsNone(z["on"].position_law)

    def _at(self, z, v):
        z.value = v
        return z

    def test_param_info_decides_the_scale(self):
        rows = [("freq", "Freq", "Hz", {"travel": {"scale": "linear", "min": 0, "max": 1}})]
        z = self.controllers(rows, {"freq": "Hz log 20 20000 1000"})
        self.assertEqual(z["freq"].position_law, {"scale": "log", "min": 20.0, "max": 20000.0})

    def test_no_other_engine_names_the_law(self):
        for name in os.listdir(os.path.join(ROOT, "zyngine")):
            if name.endswith(".py") and name not in ("zynthian_engine_plugin_hostd.py", "zynthian_position_law.py",
                                                      "zynthian_controller.py",
                                                      "zynthian_panel_gestures.py"):  # the gestures move a value ON the law
                with open(os.path.join(ROOT, "zyngine", name)) as f:
                    self.assertNotIn("position_law", f.read(), name)


if __name__ == "__main__":
    unittest.main()
