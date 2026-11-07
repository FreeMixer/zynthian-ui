# -*- coding: utf-8 -*-
#
# A sample, a kit or an SFZ named by a layer reaches the plugin through plugin-hostd's patch_set, on the plugin's own
# path property (read from its TTL, never typed per plugin), a path with a space in quotation marks; patch_get confirms
# it; a snapshot keeps it and a reload, or a daemon that came back, sends it again.
#
# Copyright (C) 2026 Pau Aliagas <linuxnow@gmail.com>
# SPDX-License-Identifier: GPL-2.0-or-later

import os
import sys
import json
import signal
import logging
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_engine_plugin_hostd as base  # noqa: E402  the stand-ins and the protocol fixture
from test_engine_plugin_hostd import ph, lv2_info, START_STUB, plugin_hostd_case  # noqa: E402

SAMPLE = "http://sampler.test/lv2#P101_SAMPLE_FILE"
KIT = "http://kit.test/lv2#KIT_FILE"
TUNING = "http://kit.test/lv2#TUNING_FILE"
SPACED = "/zynthian/zynthian-data/soundfonts/sfz/Pianos/Zynthian Salamander Grand/Accurate Salamander Grand.sfz"


def path_port(symbol, name, uri, types=None):
    return {"symbol": symbol, "name": name, "range": {"min": None, "max": None}, "value": None, "is_toggled": False,
            "is_integer": False, "is_path": True, "uri": uri, "path_file_types": types, "path_preload": False}


gain = base.LV2_PORTS["urn:test:amp"][0]
base.LV2_PORTS["urn:test:sampler"] = {0: gain, 1: path_port("P101_SAMPLE_FILE", "Sample", SAMPLE, ["wav"])}
base.LV2_PORTS["urn:test:kit"] = {0: gain, 1: path_port("KIT_FILE", "Kit", KIT, ["kit"]),
                                  2: path_port("TUNING_FILE", "Tuning", TUNING)}


class samples_case(plugin_hostd_case):

    def setUp(self):
        super().setUp()
        self.info["JV/Sampler"] = lv2_info("Sampler", "urn:test:sampler", "MIDI Synth")
        self.info["JV/Kit"] = lv2_info("Kit", "urn:test:kit", "MIDI Synth")
        base.cat.add_catalogue(self.info, clap=base.cat.parse_clap_scan(base.CLAP_SCAN), binary=base.FAKE)


class test_the_file_reaches_the_plugin(samples_case):

    def test_a_chosen_file_is_one_patch_set_on_the_property_the_ttl_names(self):
        fake = self.serve()
        proc = self.add(self.chain(), "PH/Sampler")
        zctrl = proc.controllers_dict["P101_SAMPLE_FILE"]
        self.assertTrue(zctrl.is_path, "the file is a path controller: the file browser opens on it")
        self.assertEqual(zctrl.path_file_types, ["wav"], "the types are the TTL's")
        zctrl.set_value("/data/sound.wav")
        self.assertEqual(fake.patches[1000], {SAMPLE: "/data/sound.wav"})
        self.assertIn(f'patch_set 1000 {SAMPLE} "/data/sound.wav"', fake.lines)

    def test_a_path_with_a_space_is_one_word(self):
        fake = self.serve()
        proc = self.add(self.chain(), "PH/Sampler")
        proc.controllers_dict["P101_SAMPLE_FILE"].set_value(SPACED)
        self.assertEqual(fake.patches[1000], {SAMPLE: SPACED}, "the daemon took the whole path as one word")
        self.assertIn(f'patch_set 1000 {SAMPLE} "{SPACED}"', fake.lines)

    def test_a_path_with_a_quote_is_escaped(self):
        fake = self.serve()
        proc = self.add(self.chain(), "PH/Sampler")
        proc.controllers_dict["P101_SAMPLE_FILE"].set_value('/data/the "one".wav')
        self.assertEqual(fake.patches[1000], {SAMPLE: '/data/the "one".wav'})

    def test_every_path_property_of_the_plugin_has_its_own_controller_and_uri(self):
        fake = self.serve()
        proc = self.add(self.chain(), "PH/Kit")
        proc.controllers_dict["KIT_FILE"].set_value("/data/a.kit")
        proc.controllers_dict["TUNING_FILE"].set_value("/data/b scl.scl")
        self.assertEqual(fake.patches[1000], {KIT: "/data/a.kit", TUNING: "/data/b scl.scl"},
                         "no property is typed in the engine: each URI is the plugin's own")

    def test_patch_get_confirms_it(self):
        self.serve()
        proc = self.add(self.chain(), "PH/Sampler")
        engine = proc.engine
        proc.controllers_dict["P101_SAMPLE_FILE"].set_value(SPACED)
        self.assertEqual(engine.read_path("P101_SAMPLE_FILE"), SPACED)

    def test_a_plugin_that_does_not_keep_the_file_is_said(self):
        fake = self.serve()
        proc = self.add(self.chain(), "PH/Sampler")
        fake.verb_patch_get = lambda instance, prop: "resp 0 /somewhere/else.wav"
        with self.assertLogs(level=logging.WARNING) as logs:
            proc.controllers_dict["P101_SAMPLE_FILE"].set_value("/data/sound.wav")
        self.assertTrue(any("/data/sound.wav" in l and "/somewhere/else.wav" in l for l in logs.output), logs.output)

    def test_a_plugin_without_a_path_property_has_no_file_controller(self):
        self.serve()
        proc = self.add(self.chain(), "PH/Amp")
        self.assertEqual([s for s, z in proc.controllers_dict.items() if z.is_path], [])

    def test_the_file_is_on_a_screen_of_the_processor(self):
        self.serve()
        proc = self.add(self.chain(), "PH/Sampler")
        screens = [symbols for _, symbols in proc.engine._ctrl_screens]
        self.assertTrue(any("P101_SAMPLE_FILE" in s for s in screens), screens)


class test_the_file_is_kept(samples_case):

    def test_a_snapshot_keeps_the_path_and_a_reload_sends_it_again(self):
        fake = self.serve()
        chain = self.chain()
        proc = self.add(chain, "PH/Sampler")
        proc.controllers_dict["P101_SAMPLE_FILE"].set_value(SPACED)
        state = json.loads(json.dumps(proc.get_state()))
        self.assertEqual(state["controllers"]["P101_SAMPLE_FILE"]["value"], SPACED)
        again = self.add(chain, "PH/Sampler")
        self.assertEqual(fake.patches[1001], {}, "a new instance has no file")
        again.set_state(state)
        self.assertEqual(fake.patches[1001], {SAMPLE: SPACED}, "the reload sent the saved path to the new instance")

    def test_a_snapshot_with_a_state_blob_still_sends_the_path(self):
        fake = self.serve()
        chain = self.chain()
        proc = self.add(chain, "PH/Sampler")
        proc.controllers_dict["P101_SAMPLE_FILE"].set_value("/data/sound.wav")
        state = json.loads(json.dumps(proc.get_state()))
        xconfig = json.loads(json.dumps(proc.engine.get_extended_config()))
        again = self.add(chain, "PH/Sampler", xconfig=xconfig)
        again.set_state(state)
        self.assertEqual(fake.patches[1001], {SAMPLE: "/data/sound.wav"})

    def test_a_daemon_that_came_back_is_given_the_file_again(self):
        ph.daemon.start_script = START_STUB
        proc = self.add(self.chain(), "PH/Sampler")
        proc.controllers_dict["P101_SAMPLE_FILE"].set_value(SPACED)
        first = ph.daemon.pid
        os.kill(first, signal.SIGKILL)
        self.assertTrue(ph.daemon.wait_gone(5.0))
        proc.controllers_dict["gain"].set_value(1.0)  # the command that meets the dead daemon
        second = str(ph.daemon.pid)
        self.assertNotEqual(second, str(first))
        lines = [line for pid, line in self.journal() if pid == second]
        self.assertIn(f'patch_set 1000 {SAMPLE} "{SPACED}"', lines)
        self.assertLess(lines.index("add urn:test:sampler 1000"), lines.index(f'patch_set 1000 {SAMPLE} "{SPACED}"'))

    def test_replay_lines_carry_no_patch_set_for_a_file_never_chosen(self):
        self.serve()
        proc = self.add(self.chain(), "PH/Sampler")
        self.assertFalse([l for l in proc.engine.replay_lines() if l.startswith("patch_set")])


class test_the_user_preset_keeps_the_file(samples_case):

    def saved(self, chain, name="Piano"):
        fake = self.serve()
        proc = self.add(chain, "PH/Sampler")
        proc.engine.my_data_dir = self.tmp
        proc.controllers_dict["P101_SAMPLE_FILE"].set_value(SPACED)
        path = proc.engine.save_preset(None, name)
        return fake, proc, path

    def test_a_saved_preset_names_the_file_and_loading_it_sends_it(self):
        chain = self.chain()
        fake, proc, path = self.saved(chain)
        with open(path) as f:
            self.assertEqual(json.load(f)["patch"], {SAMPLE: SPACED})
        other = self.add(chain, "PH/Sampler")
        other.engine.my_data_dir = self.tmp
        self.assertTrue(other.engine.set_preset(other, [path, None, "Piano", "user"]))
        self.assertEqual(fake.patches[1001], {SAMPLE: SPACED})
        self.assertEqual(other.controllers_dict["P101_SAMPLE_FILE"].value, SPACED, "and the controller shows it")


if __name__ == "__main__":
    unittest.main()
