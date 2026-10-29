# -*- coding: utf-8 -*-
#
# The plugin menus show each plugin's qualification figures at the current rate and quantum and sort
# by them: three fixture records (clean, violations, RT safety unmeasured), an unmeasured-record and a
# plugin with no record. Plain `python3 -m unittest`.
#
# Copyright (C) 2026 Pau Aliagas <linuxnow@gmail.com>
# SPDX-License-Identifier: GPL-2.0-or-later

import os
import sys
import time
import types
import shutil
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIGURES = os.path.join(HERE, "fixtures", "qualification", "figures")
sys.path[:0] = [HERE, ROOT]

if "zyngine" not in sys.modules:
    _pkg = types.ModuleType("zyngine")
    _pkg.__path__ = [os.path.join(ROOT, "zyngine")]
    sys.modules["zyngine"] = _pkg

from zyngine import zynthian_plugin_tiers as tiers  # noqa: E402
from zyngine import zynthian_plugin_qualification as qual  # noqa: E402

CLEAN = "clap:/usr/lib/clap/clean.clap#org.example.clean"


def entry(name, url):
    return {"NAME": name, "TITLE": f"{name} (PH)", "TYPE": "Audio Effect", "URL": url}


# One menu in the engine config's order
CATALOGUE = {
    "PH/Untested": entry("Untested Chorus", "urn:fig:none"),
    "PH/Locky": entry("Locky Verb", "urn:fig:violations"),
    "PH/Unprobed": entry("Unprobed Synth", "urn:fig:unprobed"),
    "PH/Analog": entry("Analog Delay", "urn:fig:unmeasured"),
    "PH/" + CLEAN: entry("Clean Comp", CLEAN),
    "JV/Locky": {"NAME": "Locky Verb", "TITLE": "Locky Verb", "URL": "urn:fig:violations"},
}

# At 48 kHz in blocks of 256
ROWS = {
    "PH/" + CLEAN: "Clean Comp (PH)  live 0/0fr RT-ok 85.2ns",
    "PH/Locky": "Locky Verb (PH)  studio 64/64fr RT-alone 30.5ns",
    "JV/Locky": "Locky Verb  studio 64/64fr RT-alone 30.5ns",
    "PH/Analog": "Analog Delay (PH)  studio 32fr RT-? cost?",
    "PH/Unprobed": "Unprobed Synth (PH)  not qualified",
    "PH/Untested": "Untested Chorus (PH)  not qualified",
}


class menu_figures(unittest.TestCase):

    def setUp(self):
        self.q = qual.qualification(FIGURES, []).refresh()

    def entries(self, order="tier", hosted_only=False, rate=48000, quantum=256):
        return tiers.menu_entries(CATALOGUE, lambda url: self.q.facts(url, rate, quantum), 5.0, False, order, hosted_only)

    def keys(self, order, hosted_only=False):
        return [key for key, _, _ in self.entries(order, hosted_only)]

    def test_each_row_shows_the_figures_of_its_record(self):
        self.assertEqual({key: text for key, text, _ in self.entries()}, ROWS)

    def test_the_cost_is_the_one_measured_at_the_current_quantum(self):
        rows = dict((key, text) for key, text, _ in self.entries(quantum=512))
        self.assertEqual(rows["PH/" + CLEAN], "Clean Comp (PH)  live 0/0fr RT-ok 61ns")
        self.assertTrue(rows["PH/Locky"].endswith("RT-alone cost?"), "no other quantum stands in")
        rows = dict((key, text) for key, text, _ in self.entries(rate=96000))
        self.assertEqual(rows["PH/" + CLEAN], "Clean Comp (PH)  live lat? RT-ok cost?")

    def test_sorted_by_name(self):
        self.assertEqual(self.keys("name"), ["PH/Analog", "PH/" + CLEAN, "JV/Locky", "PH/Locky",
                                             "PH/Unprobed", "PH/Untested"])

    def test_sorted_by_cost_the_unknown_after_the_known(self):
        self.assertEqual(self.keys("cost"), ["JV/Locky", "PH/Locky", "PH/" + CLEAN, "PH/Analog",
                                             "PH/Unprobed", "PH/Untested"])

    def test_sorted_by_latency(self):
        self.assertEqual(self.keys("latency"), ["PH/" + CLEAN, "PH/Analog", "JV/Locky", "PH/Locky",
                                                "PH/Unprobed", "PH/Untested"])

    def test_sorted_by_rt_safety_clean_then_runs_alone_then_unmeasured(self):
        self.assertEqual(self.keys("rt"), ["PH/" + CLEAN, "JV/Locky", "PH/Locky", "PH/Analog",
                                           "PH/Unprobed", "PH/Untested"])

    def test_by_tier_the_order_is_unchanged(self):
        self.assertEqual(self.keys("tier"), ["PH/" + CLEAN, "PH/Locky", "PH/Analog", "JV/Locky",
                                             "PH/Untested", "PH/Unprobed"])

    def test_the_rows_with_no_record_sort_last_in_every_order(self):
        for order in tiers.ORDERS:
            with self.subTest(order=order):
                self.assertEqual(sorted(self.keys(order)[-2:]), ["PH/Unprobed", "PH/Untested"])

    def test_the_filter_keeps_the_plugins_plugin_hostd_runs(self):
        engines = dict(CATALOGUE, ZY={"NAME": "ZynAddSubFX", "TITLE": "ZynAddSubFX"})
        rows = tiers.menu_entries(engines, lambda url: self.q.facts(url, 48000, 256), 5.0, False, "cost", True)
        self.assertEqual([key for key, _, _ in rows], ["PH/Locky", "PH/" + CLEAN, "PH/Analog", "PH/Unprobed", "PH/Untested"])


class isolated_plugins(unittest.TestCase):

    def test_the_filter_keeps_a_plugin_plugin_hostd_isolates(self):
        # RT-safety violations: plugin-hostd runs the plugin alone in its own worker; it is offered, not refused
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        pins = os.path.join(tmp, "q.pins")
        with open(pins, "w") as f:
            f.write("pin_set urn:fig:violations locky.so=1 omx-layout/1:a verdict=isolated\n")
        q = qual.qualification(FIGURES, [pins]).refresh()
        rows = tiers.menu_entries(CATALOGUE, lambda url: q.facts(url, 48000, 256), 5.0, False, "rt", True)
        self.assertEqual([(key, text) for key, text, _ in rows][:2],
                         [("PH/" + CLEAN, ROWS["PH/" + CLEAN]), ("PH/Locky", ROWS["PH/Locky"])])


class record_cache(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        self.dir = os.path.join(tmp, "q")
        shutil.copytree(FIGURES, self.dir)

    def test_a_record_is_read_once_until_its_mtime_changes(self):
        q = qual.qualification(self.dir, [])
        with mock.patch.object(qual, "file_entries", wraps=qual.file_entries) as read:
            q.refresh()
            self.assertEqual(read.call_count, 4)
            q.stamp = None  # a refresh that looks again finds every file unchanged
            q.refresh()
            self.assertEqual(read.call_count, 4)
            path = os.path.join(self.dir, "violations.qualification.json")
            with open(path) as f:
                text = f.read().replace('"locks": 12', '"locks": 0')
            with open(path, "w") as f:
                f.write(text)
            os.utime(path, ns=(time.time_ns() + 10**9,) * 2)
            q.refresh()
            self.assertEqual([c.args[0] for c in read.call_args_list[4:]], [path])
        self.assertIs(q.facts("urn:fig:violations", 48000, 256).rt_safe, True)

    def test_a_record_gone_leaves_the_menu(self):
        q = qual.qualification(self.dir, []).refresh()
        os.remove(os.path.join(self.dir, "clean.qualification.json"))
        q.refresh()
        self.assertFalse(q.facts(CLEAN, 48000, 256).record)
        self.assertNotIn(os.path.join(self.dir, "clean.qualification.json"), q.cache)

    def test_no_plugin_is_loaded_to_show_the_figures(self):
        refuse = mock.Mock(side_effect=AssertionError("a plugin was loaded"))
        with mock.patch("subprocess.Popen", refuse), mock.patch("ctypes.CDLL", refuse):
            q = qual.qualification(self.dir, []).refresh()
            tiers.menu_entries(CATALOGUE, lambda url: q.facts(url, 48000, 256), 5.0, False, "cost")
        refuse.assert_not_called()


if __name__ == "__main__":
    unittest.main()
