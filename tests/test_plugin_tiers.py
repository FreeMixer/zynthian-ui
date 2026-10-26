# -*- coding: utf-8 -*-
#
# The plugin menus from qualification: the rule table row by row, the fixture records and pins read
# into facts, and the menu entries of a mixed catalogue in order. Plain `python3 -m unittest`.
#
# Copyright (C) 2026 Pau Aliagas <linuxnow@gmail.com>
# SPDX-License-Identifier: GPL-2.0-or-later

import os
import sys
import time
import types
import shutil
import logging
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIXTURES = os.path.join(HERE, "fixtures", "qualification")
RECORDS = os.path.join(FIXTURES, "records")
PINS = os.path.join(FIXTURES, "pins")
sys.path[:0] = [HERE, ROOT]

if "zyngine" not in sys.modules:
    _pkg = types.ModuleType("zyngine")
    _pkg.__path__ = [os.path.join(ROOT, "zyngine")]
    sys.modules["zyngine"] = _pkg

from zyngine import zynthian_plugin_tiers as tiers  # noqa: E402
from zyngine import zynthian_plugin_qualification as qual  # noqa: E402
from zyngine.zynthian_plugin_qualification import Facts, NO_FACTS  # noqa: E402


def measured(latency=None, worst="same", rt=None, soaked=None, pin=None, crashed=False, hung=False):
    return Facts(True, pin, None, crashed, hung, latency, latency if worst == "same" else worst, rt, soaked)


# (case, facts, live ms, tier, tag, hidden)
RULES = [
    ("no record", NO_FACTS, 5, "unverified", "unverified", False),
    ("a pin and no record", NO_FACTS._replace(pin="in-process"), 5, "unverified", "unverified", False),
    ("refused", measured(0.0, rt=True, soaked=True, pin="refused"), 5, None, "hidden · refused", True),
    ("refused with no record", NO_FACTS._replace(pin="refused"), 5, None, "hidden · refused", True),
    ("crashed", measured(0.0, rt=True, crashed=True), 5, None, "hidden · crashed", True),
    ("hung", measured(None, rt=None, hung=True), 5, None, "hidden · hung", True),
    ("refused and crashed", measured(0.0, pin="refused", crashed=True), 5, None, "hidden · refused", True),
    ("isolated pin, fast", measured(1.2, rt=True, soaked=True, pin="isolated"), 5, "live", "live · 1.2 ms · RT", False),
    ("suitable fast", measured(1.2, rt=True, soaked=True), 5, "live", "live · 1.2 ms · RT", False),
    ("at the threshold", measured(5.0, rt=True, soaked=True), 5, "live", "live · 5.0 ms · RT", False),
    ("just over it", measured(5.01, rt=True, soaked=True), 5, "studio", "studio · 5.0 ms · RT", False),
    ("suitable slow", measured(21.333, rt=True, soaked=True), 5, "studio", "studio · 21 ms · RT", False),
    ("slow, under a raised threshold", measured(8.0, rt=True, soaked=True), 10, "live", "live · 8.0 ms · RT", False),
    ("alloc in callback", measured(0.0, rt=False, soaked=True), 5, "studio", "studio · 0.0 ms · not RT", False),
    ("soak unmeasured", measured(1.333, rt=True, soaked=False), 5, "live", "live · 1.3 ms · RT · unsoaked", False),
    ("no latency figure", measured(None, rt=True, soaked=True), 5, "studio", "studio · RT", False),
    ("RT-safety unmeasured", measured(1.0, rt=None, soaked=True), 5, "studio", "studio · 1.0 ms", False),
    ("nothing at this rate, live at the worst", measured(None, worst=1.333, rt=True), 5, "live", "live · RT", False),
    ("fast here, slow at the worst rate", measured(2.0, worst=21.0, rt=True), 5, "studio", "studio · 2.0 ms · RT", False),
]


class rule_table(unittest.TestCase):

    def test_every_row(self):
        for case, facts, live_ms, tier, tag, hidden in RULES:
            with self.subTest(case):
                p = tiers.place(facts, live_ms)
                self.assertEqual((p.tier, p.tag, p.hidden), (tier, tag, hidden))

    def test_the_table_has_no_two_rows_alike(self):
        self.assertEqual(len({(f, ms) for _, f, ms, *_ in RULES}), len(RULES))


class settings(unittest.TestCase):

    def test_live_threshold(self):
        with mock.patch.dict(os.environ, {"ZYNTHIAN_PLUGIN_LIVE_MS": "8"}):
            self.assertEqual(tiers.live_max_ms(), 8.0)
        with mock.patch.dict(os.environ, {"ZYNTHIAN_PLUGIN_LIVE_MS": ""}):
            self.assertEqual(tiers.live_max_ms(), 5.0)
        with mock.patch.dict(os.environ, {"ZYNTHIAN_PLUGIN_LIVE_MS": "fast"}), self.assertLogs(level=logging.WARNING):
            self.assertEqual(tiers.live_max_ms(), 5.0)

    def test_show_hidden(self):
        for value, shown in (("1", True), ("yes", True), ("0", False), ("", False)):
            with mock.patch.dict(os.environ, {"ZYNTHIAN_PLUGIN_SHOW_HIDDEN": value}):
                self.assertIs(tiers.show_hidden(), shown, value)

    def test_paths(self):
        with mock.patch.dict(os.environ, {"ZYNTHIAN_QUALIFICATION_DIR": "", "OMX_QUALIFICATION_DIR": "",
                                          "ZYNTHIAN_QUALIFICATION_PINS": ""}):
            self.assertEqual(qual.qualification_dir(), "/var/lib/omx/qualification")
            self.assertEqual(qual.pin_paths(), ["/usr/share/plugin-hostd/pins.d"])
        with mock.patch.dict(os.environ, {"ZYNTHIAN_QUALIFICATION_DIR": "/q", "ZYNTHIAN_QUALIFICATION_PINS": "/a.pins:/b"}):
            self.assertEqual((qual.qualification_dir(), qual.pin_paths()), ("/q", ["/a.pins", "/b"]))


class fact_readers(unittest.TestCase):

    def test_rt_safe_only_on_a_count_that_is_a_measurement(self):
        clean = {"interposer": True, "swept": True, "allocationsInRun": 0, "locksInRun": 0, "syscallsInRun": 0}
        cases = [(clean, True), (dict(clean, swept=False), None), (dict(clean, swept=False, sweepApplicable=False), True),
                 (dict(clean, interposer=False), None), (dict(clean, swept=False, locksInRun=2), False),
                 ({k: v for k, v in clean.items() if k != "syscallsInRun"}, None), ({}, None)]
        for rt, expected in cases:
            with self.subTest(rt=rt):
                self.assertIs(qual.rt_safe({"rtSafety": rt}), expected)

    def test_latency_at_a_rate_comes_from_a_reading_or_the_scaling_law_only(self):
        def plugin(scaling, **readings):
            return {"latency": {"scalingClass": scaling, "perRate": {r[1:]: v for r, v in readings.items()}}}
        cases = [(plugin("fixed-frame", r48000={"frames": 480}), 96000, 5.0),
                 (plugin("fixed-time", r48000={"frames": 480}), 96000, 10.0),
                 (plugin("zero", r48000={"frames": 0}), 44100, 0.0),
                 (plugin("single-rate", r48000={"frames": 480}), 44100, None),
                 (plugin("nonconforming", r48000={"frames": 480}, r96000={"frames": 2000}), 44100, None),
                 (plugin("fixed-frame", r48000={"frames": 480, "excludedReason": "impulse-onset"}), 48000, None),
                 (plugin("fixed-frame"), 48000, None),
                 (plugin("fixed-frame", r48000={"frames": 480}), None, None)]
        for p, rate, expected in cases:
            with self.subTest(p=p, rate=rate):
                self.assertEqual(qual.latency_at(p, rate), expected)
        self.assertEqual(qual.worst_latency(plugin("x", r48000={"frames": 480}, r96000={"frames": 1920})), 20.0)


# The fixture records and pins, at 48 kHz and at 44.1 kHz: what each plugin's tag reads
FIXTURE_TAGS = {
    "urn:q:fast": ("live · 0.0 ms · RT", "live · 0.0 ms · RT"),
    "urn:q:slow": ("studio · 21 ms · RT", "studio · 23 ms · RT"),            # fixed-frame: 1024 frames
    "urn:q:alloc": ("studio · 0.0 ms · not RT", "studio · not RT"),          # single-rate: nothing at 44.1k
    "urn:q:refused": ("hidden · refused", "hidden · refused"),
    "urn:q:crashed": ("hidden · crashed", "hidden · crashed"),
    "urn:q:hung": ("hidden · hung", "hidden · hung"),
    "urn:q:unsoaked": ("live · 1.3 ms · RT · unsoaked", "live · RT · unsoaked"),
    "urn:q:none": ("unverified", "unverified"),
    "clap:/usr/lib/clap/test.clap#org.test.synth": ("hidden · refused", "hidden · refused"),
}


class fixture_records(unittest.TestCase):

    def setUp(self):
        self.q = qual.qualification(RECORDS, [PINS])
        with self.assertLogs(level=logging.WARNING) as logs:
            self.q.refresh()
        self.warnings = logs.output

    def test_each_fixture_at_two_rates(self):
        for uri, tags in FIXTURE_TAGS.items():
            for rate, tag in zip((48000, 44100), tags):
                with self.subTest(uri=uri, rate=rate):
                    self.assertEqual(tiers.place(self.q.facts(uri, rate)).tag, tag)

    def test_the_facts_hold_only_what_was_measured(self):
        hung = self.q.facts("urn:q:hung", 48000)
        self.assertEqual((hung.latency_ms, hung.worst_latency_ms, hung.rt_safe, hung.soaked), (None, None, None, None))
        self.assertEqual(self.q.facts("urn:q:none", 48000), NO_FACTS)
        alloc = self.q.facts("urn:q:alloc", 48000)
        self.assertEqual((alloc.rt_safe, alloc.deciding), (False, "hosting.rt-safety.violations-in-run"))
        self.assertAlmostEqual(self.q.facts("urn:q:slow", 48000).latency_ms, 21.333, places=3)

    def test_the_latest_run_decides(self):
        self.assertFalse(self.q.facts("urn:q:fast", 48000).crashed, "the 2026-10-01 crash is superseded")

    def test_a_file_that_is_not_a_measurement_document_is_not_read(self):
        # The per-plugin record beside the documents says unsuitable, and a newer file of another
        # format says it crashed; only the measurement documents are read
        self.assertEqual(tiers.place(self.q.facts("urn:q:fast", 48000)).tier, "live")

    def test_a_verdict_word_plugin_hostd_does_not_know_is_reported_and_not_read(self):
        self.assertIsNone(self.q.pin_verdicts["urn:q:slow"])
        self.assertTrue(any("verdict=accepted" in w for w in self.warnings))
        self.assertEqual(self.q.pin_verdicts["urn:q:refused"], "refused")
        self.assertIsNone(self.q.pin_verdicts["urn:q:unsoaked"])

    def test_no_qualification_dir_is_not_qualified_yet(self):
        q = qual.qualification(os.path.join(RECORDS, "absent"), [os.path.join(PINS, "absent")]).refresh()
        self.assertEqual(q.facts("urn:q:fast", 48000), NO_FACTS)

    def test_a_changed_file_is_read_again(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        pins = os.path.join(tmp.name, "my.pins")
        shutil.copy(os.path.join(PINS, "omx.pins"), pins)
        q = qual.qualification(RECORDS, [pins])
        with self.assertLogs(level=logging.WARNING):
            q.refresh()
        self.assertEqual(tiers.place(q.facts("urn:q:fast", 48000)).tier, "live")
        with open(pins, "a") as f:
            f.write("pin_set urn:q:fast fast.so=1 omx-layout/1:a verdict=refused\n")
        os.utime(pins, ns=(time.time_ns() + 10**9,) * 2)
        with self.assertLogs(level=logging.WARNING):
            q.refresh()
        self.assertTrue(tiers.place(q.facts("urn:q:fast", 48000)).hidden)


def entry(name, url, etype="Audio Effect"):
    return {"NAME": name, "TITLE": f"{name} (PH)", "TYPE": etype, "URL": url}


# One menu as filtered_engines_by_cat gives it, in the engine config's order
CATALOGUE = {
    "PH/None": entry("Untested Chorus", "urn:q:none"),
    "AP": {"NAME": "Audio Player", "TITLE": "Audio Player", "URL": ""},
    "PH/Slow": entry("Linear EQ", "urn:q:slow"),
    "PH/Crashed": entry("Crashy Delay", "urn:q:crashed"),
    "PH/Fast": entry("Fast Comp", "urn:q:fast"),
    "PH/Alloc": entry("Alloc Verb", "urn:q:alloc"),
    "PH/Refused": entry("Refused Amp", "urn:q:refused"),
    "PH/Unsoaked": entry("New Gate", "urn:q:unsoaked"),
    "PH/Hung": entry("Stuck Pitch", "urn:q:hung"),
    "JV/Fast": {"NAME": "Fast Comp", "TITLE": "Fast Comp", "URL": "urn:q:fast"},
}


class menu_order(unittest.TestCase):

    def setUp(self):
        with self.assertLogs(level=logging.WARNING):
            self.q = qual.qualification(RECORDS, [PINS]).refresh()

    def entries(self, show):
        return tiers.menu_entries(CATALOGUE, lambda url: self.q.facts(url, 48000), 5.0, show)

    def test_live_first_then_studio_then_the_rest_and_no_hidden_ones(self):
        self.assertEqual(self.entries(False), [
            ("PH/Fast", "Fast Comp (PH)  live · 0.0 ms · RT", False),
            ("PH/Unsoaked", "New Gate (PH)  live · 1.3 ms · RT · unsoaked", False),
            ("JV/Fast", "Fast Comp  live · 0.0 ms · RT", False),
            ("PH/Slow", "Linear EQ (PH)  studio · 21 ms · RT", False),
            ("PH/Alloc", "Alloc Verb (PH)  studio · 0.0 ms · not RT", False),
            ("PH/None", "Untested Chorus (PH)  unverified", False),
            ("AP", "Audio Player", False),
        ])

    def test_shown_the_hidden_ones_come_last_greyed_with_their_reason(self):
        entries = self.entries(True)
        self.assertEqual([key for key, _, _ in entries[:7]], [key for key, _, _ in self.entries(False)])
        self.assertEqual(entries[7:], [
            ("PH/Crashed", "Crashy Delay (PH)  hidden · crashed", True),
            ("PH/Refused", "Refused Amp (PH)  hidden · refused", True),
            ("PH/Hung", "Stuck Pitch (PH)  hidden · hung", True),
        ])

    def test_without_any_record_the_order_is_the_engine_configs(self):
        q = qual.qualification(os.path.join(RECORDS, "absent"), []).refresh()
        entries = tiers.menu_entries(CATALOGUE, lambda url: q.facts(url, 48000), 5.0, False)
        self.assertEqual([key for key, _, _ in entries], list(CATALOGUE))


if __name__ == "__main__":
    unittest.main()
