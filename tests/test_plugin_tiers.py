# -*- coding: utf-8 -*-
#
# The plugin menus from qualification: the rule table row by row, the fixture records and pins read
# into facts, and the menu entries of a mixed catalogue in order. Plain `python3 -m unittest`.
#
# Copyright (C) 2026 Pau Aliagas <linuxnow@gmail.com>
# SPDX-License-Identifier: GPL-2.0-or-later

import os
import sys
import json
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
# Records and pins copied from the Zynthian Pi 5's /var/lib/omx/qualification, as plugin-qualify-sync wrote them
PI5 = os.path.join(FIXTURES, "pi5")
sys.path[:0] = [HERE, ROOT]

if "zyngine" not in sys.modules:
    _pkg = types.ModuleType("zyngine")
    _pkg.__path__ = [os.path.join(ROOT, "zyngine")]
    sys.modules["zyngine"] = _pkg

from zyngine import zynthian_plugin_tiers as tiers  # noqa: E402
from zyngine import zynthian_plugin_qualification as qual  # noqa: E402
from zyngine.zynthian_plugin_qualification import Facts, NO_FACTS  # noqa: E402


def measured(latency=None, worst="same", rt=None, soaked=None, pin=None, crashed=False, hung=False,
             frames=None, declared=None, cost=None):
    return Facts(True, pin, None, crashed, hung, latency, latency if worst == "same" else worst, rt, soaked,
                 frames, declared, cost)


# (case, facts, live ms, tier, tag, hidden)
RULES = [
    ("no record", NO_FACTS, 5, "unverified", "not qualified", False),
    ("a pin and no record", NO_FACTS._replace(pin="in-process"), 5, "unverified", "not qualified", False),
    ("refused", measured(0.0, rt=True, soaked=True, pin="refused"), 5, None, "hidden · refused", True),
    ("refused with no record", NO_FACTS._replace(pin="refused"), 5, None, "hidden · refused", True),
    ("crashed", measured(0.0, rt=True, crashed=True), 5, None, "hidden · crashed", True),
    ("hung", measured(None, rt=None, hung=True), 5, None, "hidden · hung", True),
    ("refused and crashed", measured(0.0, pin="refused", crashed=True), 5, None, "hidden · refused", True),
    ("isolated pin, fast", measured(1.2, rt=True, soaked=True, pin="isolated", frames=58, cost=19.4238), 5, "live",
     "live · 58 fr · RT clean · 19.4 ns/smp", False),
    ("suitable fast", measured(1.2, rt=True, soaked=True, frames=58, declared=64, cost=120.6), 5, "live",
     "live · 58/64 fr · RT clean · 121 ns/smp", False),
    ("at the threshold", measured(5.0, rt=True, soaked=True, frames=240, declared=240), 5, "live",
     "live · 240/240 fr · RT clean · cost unknown", False),
    ("just over it", measured(5.01, rt=True, soaked=True, frames=241), 5, "studio", "studio · 241 fr · RT clean · cost unknown", False),
    ("suitable slow", measured(21.333, rt=True, soaked=True, frames=1024, cost=40), 5, "studio",
     "studio · 1024 fr · RT clean · 40 ns/smp", False),
    ("slow, under a raised threshold", measured(8.0, rt=True, soaked=True, frames=384), 10, "live",
     "live · 384 fr · RT clean · cost unknown", False),
    ("alloc in callback", measured(0.0, rt=False, soaked=True, frames=0, declared=0), 5, "studio",
     "studio · 0/0 fr · runs alone · cost unknown", False),
    ("soak unmeasured", measured(1.333, rt=True, soaked=False, frames=64), 5, "live",
     "live · 64 fr · RT clean · cost unknown · unsoaked", False),
    ("no latency figure", measured(None, rt=True, soaked=True), 5, "studio", "studio · latency unknown · RT clean · cost unknown", False),
    ("RT-safety unmeasured", measured(1.0, rt=None, soaked=True, frames=48), 5, "studio",
     "studio · 48 fr · RT unmeasured · cost unknown", False),
    ("nothing at this rate, live at the worst", measured(None, worst=1.333, rt=True), 5, "live",
     "live · latency unknown · RT clean · cost unknown", False),
    ("fast here, slow at the worst rate", measured(2.0, worst=21.0, rt=True, frames=96), 5, "studio",
     "studio · 96 fr · RT clean · cost unknown", False),
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
                                          "ZYNTHIAN_QUALIFICATION_PINS": "", "ZYNTHIAN_PLUGIN_HOSTD_CONF": "/absent.conf"}):
            self.assertEqual(qual.qualification_dir(), "/var/lib/omx/qualification")
            self.assertEqual(qual.pin_paths(), ["/usr/share/plugin-hostd/pins.d"])
        with mock.patch.dict(os.environ, {"ZYNTHIAN_QUALIFICATION_DIR": "/q", "ZYNTHIAN_QUALIFICATION_PINS": "/a.pins:/b"}):
            self.assertEqual((qual.qualification_dir(), qual.pin_paths()), ("/q", ["/a.pins", "/b"]))

    def test_unset_the_pins_are_the_ones_plugin_hostd_reads(self):
        # The package's pins.d and the pin_file of plugin-hostd's config, where the qualification sync writes
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        with open(os.path.join(tmp, "plugin-hostd.conf"), "w") as f:
            f.write("# settings\nmod_host /usr/local/bin/mod-host\npin_file /var/lib/omx/qualified.pins\n")
        with mock.patch.dict(os.environ, {"ZYNTHIAN_QUALIFICATION_PINS": "", "ZYNTHIAN_PLUGIN_HOSTD_CONF": "",
                                          "ZYNTHIAN_CONFIG_DIR": tmp}):
            self.assertEqual(qual.pin_paths(), ["/usr/share/plugin-hostd/pins.d", "/var/lib/omx/qualified.pins"])
        with mock.patch.dict(os.environ, {"ZYNTHIAN_QUALIFICATION_PINS": "",
                                          "ZYNTHIAN_PLUGIN_HOSTD_CONF": os.path.join(tmp, "plugin-hostd.conf")}):
            self.assertEqual(qual.pin_paths(), ["/usr/share/plugin-hostd/pins.d", "/var/lib/omx/qualified.pins"])


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


# The fixture records and pins, at 48 kHz and at 44.1 kHz: what each plugin's tag reads. A rate the
# document did not measure shows no latency, whatever its scaling class says
FIXTURE_TAGS = {
    "urn:q:fast": ("live · 0 fr · RT clean · cost unknown", "live · latency unknown · RT clean · cost unknown"),
    "urn:q:slow": ("studio · 1024 fr · RT clean · cost unknown", "studio · latency unknown · RT clean · cost unknown"),
    "urn:q:alloc": ("studio · 0 fr · runs alone · cost unknown", "studio · latency unknown · runs alone · cost unknown"),
    "urn:q:refused": ("hidden · refused", "hidden · refused"),
    "urn:q:crashed": ("hidden · crashed", "hidden · crashed"),
    "urn:q:hung": ("hidden · hung", "hidden · hung"),
    "urn:q:unsoaked": ("live · 64 fr · RT clean · cost unknown · unsoaked",
                       "live · latency unknown · RT clean · cost unknown · unsoaked"),
    "urn:q:none": ("not qualified", "not qualified"),
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

    def test_a_record_with_no_figure_says_nothing(self):
        # The per-plugin record beside the documents says unsuitable but holds no measurement, and a
        # newer file of another format says it crashed; neither moves the document's figures
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


CLAP_EQ = "clap:/usr/lib/clap/3BandEQ.clap#studio.kx.distrho.MVerb"
CLAP_SPLIT = "clap:/usr/lib/clap/3BandSplitter.clap#studio.kx.distrho.MVerb"
CLAP_DELAY = "clap:/usr/lib/clap/lsp-plugins.clap#in.lsp-plug.art_delay_stereo"
LV2_DELAY = "http://lsp-plug.in/plugins/lv2/art_delay_stereo"
LV2_TIMEOUT = "http://kxstudio.linuxaudio.org/plugins/FluidPlug_Black_Pearl_4B"


class pi5_records(unittest.TestCase):
    """The per-plugin records plugin-qualify-sync wrote on the Pi 5, read as the menus read them."""

    def setUp(self):
        self.q = qual.qualification(os.path.join(PI5, "records"), [os.path.join(PI5, "pins")]).refresh()

    def test_a_record_is_read_by_its_pin_uri(self):
        self.assertEqual(sorted(self.q.documents), sorted([CLAP_EQ, CLAP_SPLIT, CLAP_DELAY, LV2_DELAY, LV2_TIMEOUT]))

    def test_a_clean_clap_record_carries_latency_rt_and_the_live_tier(self):
        facts = self.q.facts(CLAP_DELAY, 48000)
        self.assertEqual((facts.record, facts.pin, facts.rt_safe, facts.soaked), (True, "in-process", True, False))
        self.assertAlmostEqual(facts.latency_ms, 1 * 1000 / 48000)   # measuredFrames 1 at 48 kHz
        self.assertEqual(tiers.place(facts).tag, "live · 1/0 fr · RT clean · cost unknown · unsoaked")
        self.assertEqual(tiers.place(facts).tier, "live")
        # the grid measured the cost at 48 kHz in blocks of 512: 47.9 ns per sample (median)
        self.assertEqual(tiers.place(self.q.facts(CLAP_DELAY, 48000, 512)).tag,
                         "live · 1/0 fr · RT clean · 47.9 ns/smp · unsoaked")
        self.assertIsNone(self.q.facts(CLAP_DELAY, 48000, 256).cost_ns, "no other quantum stands in")

    def test_a_latency_is_not_made_up_at_a_rate_the_record_did_not_measure(self):
        facts = self.q.facts(CLAP_DELAY, 96000)
        self.assertIsNone(facts.latency_ms)
        self.assertAlmostEqual(facts.worst_latency_ms, 1 * 1000 / 48000)
        self.assertEqual(tiers.place(facts).tag, "live · latency unknown · RT clean · cost unknown · unsoaked")

    def test_an_lv2_record_without_a_latency_reading_is_studio_and_says_no_latency(self):
        facts = self.q.facts(LV2_DELAY, 48000)
        self.assertEqual((facts.latency_ms, facts.worst_latency_ms, facts.rt_safe), (None, None, True))
        self.assertEqual(tiers.place(facts).tag, "studio · latency unknown · RT clean · cost unknown · unsoaked")

    def test_the_interposer_counts_decide_rt_not_the_pin(self):
        facts = self.q.facts(CLAP_EQ, 48000)      # 512 locks in the run
        self.assertEqual((facts.rt_safe, facts.deciding), (False, "hosting.rt-safety.violations-in-run"))

    def test_a_refused_pin_still_hides_a_record_that_has_figures(self):
        for uri in (CLAP_EQ, CLAP_SPLIT):
            with self.subTest(uri=uri):
                p = tiers.place(self.q.facts(uri, 48000))
                self.assertEqual((p.tag, p.hidden, p.reason), ("hidden · refused", True, "refused"))

    def test_a_probe_that_timed_out_is_hung(self):
        facts = self.q.facts(LV2_TIMEOUT, 48000)
        self.assertEqual((facts.record, facts.hung, facts.latency_ms, facts.rt_safe), (True, True, None, None))
        self.assertEqual(tiers.place(facts).tag, "hidden · hung")

    def test_no_record_is_unverified(self):
        self.assertEqual(tiers.place(self.q.facts("clap:/usr/lib/clap/none.clap#x", 48000)).tag, "not qualified")

    def test_the_menu_lists_the_two_clap_files_that_share_an_id_apart(self):
        engines = {"PH/" + u: {"TITLE": t, "URL": u} for u, t in
                   ((CLAP_EQ, "3 Band EQ (PH)"), (CLAP_SPLIT, "3 Band Splitter (PH)"), (CLAP_DELAY, "Art Delay (PH)"),
                    (LV2_DELAY, "Art Delay LV2 (PH)"), ("clap:/usr/lib/clap/none.clap#x", "None (PH)"))}
        rows = tiers.menu_entries(engines, lambda url: self.q.facts(url, 48000))
        self.assertEqual([text for _, text, _ in rows],
                         ["Art Delay (PH)  live · 1/0 fr · RT clean · cost unknown · unsoaked",
                          "Art Delay LV2 (PH)  studio · latency unknown · RT clean · cost unknown · unsoaked",
                          "None (PH)  not qualified"])
        shown = tiers.menu_entries(engines, lambda url: self.q.facts(url, 48000), show=True)
        self.assertEqual([(text, grey) for _, text, grey in shown][-2:],
                         [("3 Band EQ (PH)  hidden · refused", True), ("3 Band Splitter (PH)  hidden · refused", True)])

    def test_a_refused_plugins_jalv_row_is_listed_after_the_qualified_ones(self):
        engines = {"JV/Refused": {"TITLE": "Refused", "URL": "urn:r"}, "PH/Refused": {"TITLE": "Refused (PH)", "URL": "urn:r"},
                   "ZY": {"TITLE": "ZynAddSubFX"}, "PH/Fast": {"TITLE": "Fast (PH)", "URL": "urn:f"}}
        facts = {"urn:r": NO_FACTS._replace(pin="refused"), "urn:f": measured(1.0, rt=True, soaked=True, frames=48)}
        rows = tiers.menu_entries(engines, lambda url: facts.get(url, NO_FACTS))
        self.assertEqual([(text, grey) for _, text, grey in rows],
                         [("Fast (PH)  live · 48 fr · RT clean · cost unknown", False), ("ZynAddSubFX", False),
                          ("Refused  refused", False)])
        shown = tiers.menu_entries(engines, lambda url: facts.get(url, NO_FACTS), show=True)
        self.assertEqual([text for _, text, _ in shown][-2:], ["Refused  refused", "Refused (PH)  hidden · refused"])


class record_reading(unittest.TestCase):

    def read(self, *records):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for n, rec in enumerate(records):
            with open(os.path.join(tmp.name, f"r{n}.qualification.json"), "w") as f:
                json.dump(rec, f)
        return qual.read_documents(tmp.name)

    def record(self, at, frames, **extra):
        rec = {"schema": qual.RECORD_SCHEMA, "plugin": {"format": "clap", "id": "org.x"}, "binary": {"path": "/a.clap"},
               "verdicts": [], "measuredAt": at, "probe": {"outcome": "probe.ok"},
               "measurements": {"grid": [{"rate": 48000, "quantum": 512, "outcome": "probe.ok", "latency": {"measuredFrames": frames}}]}}
        rec.update(extra)
        return rec

    def test_the_latest_record_of_a_plugin_wins(self):
        found = self.read(self.record("2026-10-27T10:00:00+00:00", 96), self.record("2026-10-26T10:00:00+00:00", 480))
        self.assertEqual(qual.latency_at(found["clap:/a.clap#org.x"][1], 48000), 2.0)

    def test_a_clap_record_without_a_path_or_an_lv2_one_without_an_id_is_not_keyed(self):
        self.assertEqual(self.read(self.record("t", 1, binary={})), {})
        self.assertEqual(self.read(self.record("t", 1, plugin={"format": "lv2"})), {})

    def test_a_failed_point_is_no_reading(self):
        rec = self.record("t", 1)
        rec["measurements"]["grid"][0]["outcome"] = "probe.crashed"
        self.assertEqual(self.read(rec), {}, "no ok point and a run that is not a crash or a timeout")

    def test_the_other_record_schemas_are_not_read(self):
        self.assertEqual(self.read(self.record("t", 1, schema="plugin-qualify.unmeasured-record/1")), {})

    def test_a_crashed_run_is_crashed(self):
        rec = self.record("t", 1, probe={"outcome": "probe.crashed"})
        rec["measurements"]["grid"] = []
        facts = qual.facts_of(self.read(rec)["clap:/a.clap#org.x"], None, 48000)
        self.assertTrue(facts.crashed)

    def test_both_formats_are_read_from_one_directory(self):
        found = qual.read_documents(RECORDS)
        self.assertIn("urn:q:fast", found)
        found = qual.read_documents(PI5 + "/records")
        self.assertIn(CLAP_DELAY, found)


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
            ("PH/Fast", "Fast Comp (PH)  live · 0 fr · RT clean · cost unknown", False),
            ("PH/Unsoaked", "New Gate (PH)  live · 64 fr · RT clean · cost unknown · unsoaked", False),
            ("JV/Fast", "Fast Comp  live · 0 fr · RT clean · cost unknown", False),
            ("PH/Slow", "Linear EQ (PH)  studio · 1024 fr · RT clean · cost unknown", False),
            ("PH/Alloc", "Alloc Verb (PH)  studio · 0 fr · runs alone · cost unknown", False),
            ("PH/None", "Untested Chorus (PH)  not qualified", False),
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
