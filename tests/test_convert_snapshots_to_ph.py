# -*- coding: utf-8 -*-
#
# tools/convert_snapshots_to_ph.py on fixture snapshots, and a converted processor loaded by the real
# zynthian_processor into the PH engine against the fake plugin-hostd.
#
# Copyright (C) 2026 Pau Aliagas <linuxnow@gmail.com>
# SPDX-License-Identifier: GPL-2.0-or-later

import io
import os
import sys
import json
import shutil
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIXTURES = os.path.join(HERE, "fixtures", "snapshots")
sys.path[:0] = [HERE, os.path.join(ROOT, "tools")]

import convert_snapshots_to_ph as conv  # noqa: E402
import test_engine_plugin_hostd as phtest  # noqa: E402
import fake_plugin_hostd  # noqa: E402


def load(path):
    with open(path) as f:
        return json.load(f)


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


class converter_case(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = os.path.join(tmp.name, "snapshots")
        shutil.copytree(FIXTURES, self.dir)
        self.config = os.path.join(self.dir, "engine_config.json")

    def run_tool(self, *paths, pins=""):
        out = io.StringIO()
        code = conv.main(["--engine-config", self.config, "--pins", pins, *paths], out=out)
        return code, out.getvalue()

    def test_a_plugin_plugin_hostd_refuses_stays_with_jalv(self):
        pins = os.path.join(self.dir, "pins")
        code, report = self.run_tool(self.dir, pins=pins)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "one-jv-one-zy.ph.zss")))
        self.assertIn("left       processor 2: JV/ZamComp: plugin-hostd refuses urn:zamaudio:ZamComp (pin verdict=refused)",
                      report)
        # verdict=isolated is hosted, in a worker of its own
        self.assertIn("converted  processor 4 JV/Surge XT -> PH/Surge XT", report)

    def test_the_jalv_plugin_becomes_a_ph_one_and_the_rest_stays(self):
        src = os.path.join(self.dir, "one-jv-one-zy.zss")
        before = read_bytes(src)
        code, report = self.run_tool(src)
        self.assertEqual(code, 0)
        self.assertEqual(read_bytes(src), before, "the original is never touched")
        old, new = load(src), load(os.path.join(self.dir, "one-jv-one-zy.ph.zss"))
        self.assertEqual(new["chains"]["1"]["slots"], [{"1": "ZY"}, {"2": "PH/ZamComp"}])
        for zs3 in ("zs3-0", "zs3-1"):
            was, now = old["zs3"][zs3]["processors"]["2"], new["zs3"][zs3]["processors"]["2"]
            self.assertEqual(now["controllers"], was["controllers"], f"{zs3}: the same values and MIDI learn")
            self.assertEqual(now["preset_info"], ["urn:zamaudio:ZamComp#preset002", None, "Gentle", ""])
            self.assertEqual(now["bank_info"], ["", None, "Presets", None])
        self.assertEqual(new["zs3"]["zs3-0"]["processors"]["1"], old["zs3"]["zs3-0"]["processors"]["1"])
        # Everything that is not a JV processor is the same document
        for key in ("schema_version", "last_snapshot_fpath", "gui", "zynseq", "last_zs3_id"):
            self.assertEqual(new[key], old[key])
        self.assertEqual(new["zs3"]["zs3-0"]["chains"], old["zs3"]["zs3-0"]["chains"])
        self.assertIn("converted  processor 2 JV/ZamComp -> PH/ZamComp (urn:zamaudio:ZamComp)", report)
        self.assertIn("left       processor 1: ZY is not a jalv plugin", report)
        self.assertIn("1 snapshots read, 1 written; 1 processors converted, 1 left as they are", report)

    def test_a_directory_and_every_reason_to_stay(self):
        code, report = self.run_tool(self.dir)
        new = load(os.path.join(self.dir, "mixed", "live-set.ph.zss"))
        self.assertEqual(new["chains"]["1"]["slots"], [{"3": "JV/Arpeggiator"}, {"4": "PH/Surge XT"}, {"5": "JV/Gone"}])
        self.assertEqual(new["chains"]["2"]["slots"], [{"6": "MD"}], "a MOD-UI chain keeps its engine")
        self.assertIsNone(new["zs3"]["zs3-0"]["processors"]["4"]["preset_info"], "no preset is made up")
        self.assertEqual(new["zs3"]["zs3-0"]["processors"]["3"]["controllers"], {"rate": {"value": 8}})
        self.assertFalse(os.path.exists(os.path.join(self.dir, "mixed", "old-schema.ph.zss")))
        for line in ("left       processor 3: JV/Arpeggiator is a MIDI Tool: plugin-hostd hosts instruments and effects",
                     "left       processor 5: JV/Gone is not in the engine config, so its plugin URL is unknown",
                     "left       processor 6: MD is not a jalv plugin",
                     "left       schema 4 is not 5: load and save it once in Zynthian, then convert",
                     "3 snapshots read, 2 written; 2 processors converted, 4 left as they are"):
            self.assertIn(line, report)

    def test_nothing_is_overwritten(self):
        target = os.path.join(self.dir, "one-jv-one-zy.ph.zss")
        with open(target, "w") as f:
            f.write("mine")
        code, report = self.run_tool(os.path.join(self.dir, "one-jv-one-zy.zss"))
        self.assertEqual(read_bytes(target).decode(), "mine")
        self.assertIn("exists, left as it is (nothing is overwritten)", report)
        self.assertIn("left       processor 2 JV/ZamComp: would become PH/ZamComp", report)
        self.assertIn("0 written", report)

    def test_a_second_run_reads_no_result_as_a_snapshot(self):
        self.run_tool(self.dir)
        code, report = self.run_tool(self.dir)
        self.assertNotIn(".ph.ph.zss", report)
        self.assertIn("3 snapshots read, 0 written", report)

    def test_a_snapshot_with_no_jalv_plugin_writes_nothing(self):
        path = os.path.join(self.dir, "plain.zss")
        with open(path, "w") as f:
            json.dump({"schema_version": 5, "chains": {"1": {"slots": [{"1": "ZY"}]}}, "zs3": {}}, f)
        code, report = self.run_tool(path)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "plain.ph.zss")))
        self.assertIn("plain.zss: left as it is", report)

    def test_an_unreadable_engine_config_stops_before_any_file(self):
        self.config = os.path.join(self.dir, "absent.json")
        code, report = self.run_tool(self.dir)
        self.assertEqual(code, 2)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "one-jv-one-zy.ph.zss")))


ZAMCOMP = {"params": {"threshold": ("dB", "linear", -60.0, 0.0, 0.0), "ratio": ("-", "linear", 1.0, 20.0, 2.0),
                      "attack": ("ms", "linear", 0.1, 100.0, 10.0)},
           "info": True, "presets": [("urn:zamaudio:ZamComp#preset002", "Gentle", {"ratio": 3.0, "attack": 5.0})]}
# Its LV2 ports, as the lilv stand-in gives them when the daemon has no descriptor
ZAMCOMP_PORTS = {i: {"symbol": s, "name": s.title(), "range": {"min": lo, "max": hi}, "value": d, "is_integer": False,
                     "is_toggled": False} for i, (s, (_, _, lo, hi, d)) in enumerate(ZAMCOMP["params"].items())}


class converted_snapshot_loads(phtest.plugin_hostd_case):
    """The job: a converted snapshot's processor, loaded the way the state manager loads it, plays the
    plugin with the preset and the values the jalv one had."""

    def test_the_converted_processor_gets_its_preset_and_values_from_plugin_hostd(self):
        lv2 = sys.modules["zyngine.zynthian_lv2"]
        ports = lv2.get_plugin_ports
        for patcher in (mock.patch.dict(fake_plugin_hostd.PLUGINS, {"urn:zamaudio:ZamComp": ZAMCOMP}),
                        mock.patch.object(lv2, "get_plugin_ports", lambda uri: ZAMCOMP_PORTS
                                          if uri == "urn:zamaudio:ZamComp" else ports(uri))):
            patcher.start()
            self.addCleanup(patcher.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        src = os.path.join(tmp.name, "one.zss")
        shutil.copy(os.path.join(FIXTURES, "one-jv-one-zy.zss"), src)
        conv.main(["--engine-config", os.path.join(FIXTURES, "engine_config.json"), "--pins", "", src], out=io.StringIO())
        snapshot = load(os.path.join(tmp.name, "one.ph.zss"))
        code = snapshot["chains"]["1"]["slots"][1]["2"]
        state = snapshot["zs3"]["zs3-0"]["processors"]["2"]

        phtest.cat.add_catalogue(self.info, clap=[])  # no PH/ZamComp without its jalv entry
        self.assertNotIn(code, self.info)
        self.info["JV/ZamComp"] = phtest.lv2_info("ZamComp", "urn:zamaudio:ZamComp")
        phtest.cat.add_catalogue(self.info, clap=[])

        fake = self.serve()
        proc = self.add(self.chain(), code)
        proc.set_state(state)
        self.assertIn("add urn:zamaudio:ZamComp 1000", fake.lines)
        self.assertIn("preset_load 1000 urn:zamaudio:ZamComp#preset002", fake.lines)
        self.assertEqual(fake.params[1000], {"threshold": -18.5, "ratio": 4.0, "attack": 10.0},
                         "the saved values win over the preset's, as they did under jalv")


if __name__ == "__main__":
    unittest.main()
