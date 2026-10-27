# -*- coding: utf-8 -*-
#
# plugin-hostd as the engine the menus offer LV2 plugins through, and ZYNTHIAN_PLUGIN_ENGINE=jalv
# to switch back. Runs with plain `python3 -m unittest`; zynthian_lv2 is a stand-in.
#
# Copyright (C) 2026 Pau Aliagas <linuxnow@gmail.com>
# SPDX-License-Identifier: GPL-2.0-or-later

import os
import sys
import types
import logging
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path[:0] = [HERE, ROOT]

if "zyngine" not in sys.modules:
    _pkg = types.ModuleType("zyngine")
    _pkg.__path__ = [os.path.join(ROOT, "zyngine")]
    sys.modules["zyngine"] = _pkg
from zyngine import zynthian_plugin_hostd_catalogue as cat  # noqa: E402


def lv2_info(name, uri, etype="Audio Effect", enabled=True):
    return {"ID": name, "NAME": name, "TITLE": name, "TYPE": etype, "CAT": "Other", "ENABLED": enabled, "INDEX": 0,
            "URL": uri, "UI": "", "DESCR": "", "QUALITY": 0, "COMPLEX": 0, "EDIT": 0}


class plugin_engine_case(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        # An executable that stands for plugin-hostd
        self.binary = os.path.join(tmp.name, "plugin-hostd")
        with open(self.binary, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(self.binary, 0o755)
        self.engines = {"JV/ZamComp": lv2_info("ZamComp", "urn:zamaudio:ZamComp"),
                        "JV/Surge": lv2_info("Surge", "urn:surge", "MIDI Synth", enabled=False),
                        "JV/Arp": lv2_info("Arp", "urn:test:arp", "MIDI Tool"),
                        "ZY": lv2_info("ZynAddSubFX", "", "MIDI Synth")}
        self.by_type = {"Audio Effect": {}, "MIDI Synth": {}, "MIDI Tool": {}}
        for key, info in self.engines.items():
            self.by_type[info["TYPE"]][key] = info
        # zynthian_lv2 needs lilv: a stand-in holding the menus' lists by type
        lv2 = types.ModuleType("zyngine.zynthian_lv2")
        lv2.engines_by_type = self.by_type
        for patcher in (mock.patch.dict(sys.modules, {"zyngine.zynthian_lv2": lv2}),
                        mock.patch.object(sys.modules["zyngine"], "zynthian_lv2", lv2, create=True)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def catalogue(self, setting=None, binary=None):
        env = {} if setting is None else {"ZYNTHIAN_PLUGIN_ENGINE": setting}
        with mock.patch.dict(os.environ, env):
            if setting is None:
                os.environ.pop("ZYNTHIAN_PLUGIN_ENGINE", None)
            return cat.add_catalogue(self.engines, clap=[], binary=binary or self.binary)

    def menu(self, etype):
        return sorted(self.by_type[etype])

    def test_by_default_the_menus_offer_lv2_plugins_through_plugin_hostd(self):
        self.catalogue()
        self.assertEqual(self.menu("Audio Effect"), ["PH/ZamComp"])
        self.assertEqual(self.menu("MIDI Synth"), ["PH/Surge", "ZY"])
        # The jalv entries stay known, so a chain saved with one still loads
        self.assertIn("JV/ZamComp", self.engines)
        self.assertIn("JV/Surge", self.engines)

    def test_jalv_switches_back(self):
        self.catalogue("jalv")
        self.assertEqual(self.menu("Audio Effect"), ["JV/ZamComp", "PH/ZamComp"])
        self.assertEqual(self.menu("MIDI Synth"), ["JV/Surge", "PH/Surge", "ZY"])

    def test_the_setting_reads_without_case_or_spaces(self):
        self.catalogue(" JALV ")
        self.assertIn("JV/ZamComp", self.by_type["Audio Effect"])

    def test_an_unknown_setting_is_reported_and_plugin_hostd_used(self):
        with self.assertLogs(level=logging.WARNING) as logs:
            self.catalogue("carla")
        self.assertEqual(self.menu("Audio Effect"), ["PH/ZamComp"])
        self.assertIn("ZYNTHIAN_PLUGIN_ENGINE=carla", logs.output[0])

    def test_without_plugin_hostd_installed_the_menus_keep_jalv(self):
        with self.assertLogs(level=logging.WARNING) as logs:
            self.catalogue(binary=os.path.join(os.path.dirname(self.binary), "absent"))
        self.assertEqual(self.menu("Audio Effect"), ["JV/ZamComp", "PH/ZamComp"])
        self.assertIn("not installed", logs.output[0])

    def test_midi_tools_stay_with_jalv(self):
        self.catalogue()
        self.assertEqual(self.menu("MIDI Tool"), ["JV/Arp"])

    def test_enabling_a_plugin_hostd_entry_carries_to_its_jalv_one(self):
        self.catalogue()
        self.assertFalse(self.engines["PH/Surge"]["ENABLED"])
        cat.set_enabled(self.engines, "PH/Surge", True)
        self.assertTrue(self.engines["JV/Surge"]["ENABLED"])
        # The next load makes the PH entry again from the jalv one, enabled
        self.catalogue()
        self.assertTrue(self.engines["PH/Surge"]["ENABLED"])

    def test_enabling_other_entries_touches_nothing_else(self):
        self.catalogue()
        cat.set_enabled(self.engines, "ZY", False)
        cat.set_enabled(self.engines, "JV/Arp", False)
        self.assertFalse(self.engines["ZY"]["ENABLED"])
        self.assertTrue(self.engines["JV/ZamComp"]["ENABLED"])


if __name__ == "__main__":
    unittest.main()
