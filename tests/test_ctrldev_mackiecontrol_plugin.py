# -*- coding: utf-8 -*-
#
# A Mackie Control surface (X-Touch and every MCU-compatible desk) on a plugin-hostd processor: the eight V-Pots
# drive the current control page, which is omx-panel's layout page, the LCD shows its names and values, the
# channel buttons page; the faders drive the chains' levels with motor feedback. The driver, the surface pages,
# the engine, the chain, the processor and the controllers are the real ones; zyncore's MIDI output is recorded.
#
# Copyright (C) 2026 Pau Aliagas <linuxnow@gmail.com>
# SPDX-License-Identifier: GPL-2.0-or-later

import os
import sys
import types
import tempfile
import unittest
from unittest import mock

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_engine_plugin_hostd import plugin_hostd_case, FIXTURES, ROOT, ph  # noqa: F401  (sets up the stand-ins)
from zyngine.zynthian_controller import zynthian_controller

# The driver reads its yaml from the config dir, copied there from the UI's default on first use
_config = tempfile.TemporaryDirectory()
os.environ.setdefault("ZYNTHIAN_CONFIG_DIR", _config.name)
os.environ.setdefault("ZYNTHIAN_UI_DIR", ROOT)
sys.modules.setdefault("oyaml", yaml)
if "zynlibs.zynseq" not in sys.modules:
    _zynseq = types.ModuleType("zynlibs.zynseq")
    _zynseq.zynseq = mock.MagicMock()
    sys.modules.setdefault("zynlibs", types.ModuleType("zynlibs"))
    sys.modules["zynlibs.zynseq"] = _zynseq
from zyngine.ctrldev import zynthian_ctrldev_mackiecontrol as mkc  # noqa: E402

NOTE_ON, CC, PITCHBEND = 0x90, 0xB0, 0xE0
PLUGIN_ASSIGN, CHANNEL_LEFT, CHANNEL_RIGHT, FADER_TOUCH_0 = 43, 48, 49, 104
VPOT_0, VPOT_RING_0 = 16, 48


class surface_out:
    """What the surface receives: the 2x56 LCD, the V-Pot rings and the motor faders, decoded from the driver's
    zyncore calls."""

    def __init__(self):
        self.lcd = [" "] * 112
        self.rings = {}
        self.faders = {}

    def dev_send_midi_event(self, idev, msg, n):
        msg = bytes(msg[:n])
        if msg[:6] == bytes.fromhex("F0 00 00 66 14 12"):
            pos, text = msg[6], msg[7:-1]
            for i, c in enumerate(text.decode()):
                self.lcd[pos + i] = c

    def dev_send_ccontrol_change(self, idev, chan, ccnum, value):
        if VPOT_RING_0 <= ccnum < VPOT_RING_0 + 8:
            self.rings[ccnum - VPOT_RING_0] = value

    def dev_send_pitchbend_change(self, idev, chan, value):
        self.faders[chan] = value

    def strip(self, row, i):
        start = row * 56 + i * 7
        return "".join(self.lcd[start:start + 7])


class test_mackiecontrol_plugin(plugin_hostd_case):

    def setUp(self):
        super().setUp()
        # The surface has eight encoders: its pages are omx-panel's pages of eight, which an omx-panel that
        # lays out pages of four (--per-page) would split in halves
        os.environ["OMX_PANEL_STUB_OLD"] = "1"
        os.symlink(os.path.join(FIXTURES, "omx-panel"), os.path.join(self.bin, "omx-panel"))
        self.fake = self.serve(descriptors={"urn:test:big": os.path.join(FIXTURES, "big.descriptor.json")})
        self.chain_ = self.chain()
        self.proc = self.add(self.chain_, "PH/Big")
        self.chain_.current_processor = self.proc
        self.level = zynthian_controller(None, "level", {"value_min": 0.0, "value_max": 1.0, "value": 0.8})
        self.chain_.zynmixer_proc = mock.MagicMock(controllers_dict={"level": self.level})
        cm = self.sm.chain_manager
        main_level = zynthian_controller(None, "level", {"value_min": 0.0, "value_max": 1.0, "value": 0.8})
        cm.chains[0] = mock.MagicMock(chain_id=0, zynmixer_proc=mock.MagicMock(controllers_dict={"level": main_level}))
        cm.get_active_chain.return_value = self.chain_
        cm.get_chain_ids_filtered.return_value = [1]
        cm.get_chain_id_by_mixer_chan.return_value = 1
        self.out = surface_out()
        lib = mock.MagicMock()
        lib.dev_send_midi_event.side_effect = self.out.dev_send_midi_event
        lib.dev_send_ccontrol_change.side_effect = self.out.dev_send_ccontrol_change
        lib.dev_send_pitchbend_change.side_effect = self.out.dev_send_pitchbend_change
        patcher = mock.patch.object(mkc, "lib_zyncore", lib)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.dev = mkc.zynthian_ctrldev_mackiecontrol(self.sm, 1, 2)
        self.dev.refresh()

    def press(self, note):
        self.dev.midi_event(bytes([NOTE_ON, note, 127]))
        self.dev.midi_event(bytes([NOTE_ON, note, 0]))

    def top(self):
        return [self.out.strip(0, i) for i in range(8)]

    def test_the_v_pots_show_the_layout_page_of_the_selected_plugin(self):
        self.press(PLUGIN_ASSIGN)
        self.assertEqual([name for name, _ in self.dev.surface_pages.pages()], ["Main 1", "Main 2"],
                         "the surface's pages are omx-panel's pages of the plugin")
        self.assertEqual(self.top(), [f"Freq {i} "[:7] for i in range(8)], "the LCD names the page's eight controls")
        p0 = self.proc.controllers_dict["p0"]
        self.assertEqual((p0.value, p0.value_min, p0.value_max), (10, 0, 100))
        self.assertEqual(self.out.strip(1, 0).strip(), "10", "and shows their values")
        self.assertEqual(self.out.rings[0], 0x22, "a tenth of its travel lights the second LED")

    def test_a_v_pot_turn_moves_the_plugin_parameter_and_its_ring(self):
        self.press(PLUGIN_ASSIGN)
        p2 = self.proc.controllers_dict["p2"]
        self.dev.midi_event(bytes([CC, VPOT_0 + 2, 5]))  # five steps clockwise
        moved = p2.value
        self.assertGreater(moved, 10)
        self.assertEqual(self.fake.params[1000]["p2"], moved, "the value reaches plugin-hostd")
        self.assertEqual(self.out.rings[2], 0x20 | (1 + round(moved / 100 * 10)))
        self.assertEqual(self.out.strip(1, 2).strip(), f"{moved:.4g}")
        self.dev.midi_event(bytes([CC, VPOT_0 + 2, 0x40 | 2]))  # two steps counterclockwise
        self.assertLess(p2.value, moved)

    def test_the_channel_buttons_page_and_the_control_screen_follows(self):
        self.press(PLUGIN_ASSIGN)
        self.press(CHANNEL_RIGHT)
        self.assertEqual(self.top()[:3], ["Freq 8 ", "Freq 9 ", "       "], "page 2 of the layout, two controls")
        self.assertEqual(self.out.rings[2], 0, "an empty slot's ring is dark")
        screens = list(self.proc.get_ctrl_screens())
        self.assertEqual(screens[self.proc.get_current_screen_index()], "Main 2", "the GUI's page is the surface's")
        self.dev.refresh()
        self.assertEqual(self.top()[0], "Freq 8 ", "a refresh keeps the page")
        self.press(CHANNEL_RIGHT)
        self.assertEqual(self.top()[0], "Freq 8 ", "past the last page nothing moves")
        self.press(CHANNEL_LEFT)
        self.assertEqual(self.top()[0], "Freq 0 ")

    def test_out_of_plugin_assign_the_rings_are_dark_and_the_encoders_leave_the_plugin_alone(self):
        self.press(PLUGIN_ASSIGN)
        self.press(PLUGIN_ASSIGN)  # back to the global view
        self.assertEqual(set(self.out.rings.values()), {0})
        self.dev.midi_event(bytes([CC, VPOT_0 + 6, 5]))
        self.assertEqual(self.proc.controllers_dict["p6"].value, 10)

    def test_a_fader_move_lands_on_the_chain_level_and_the_motor_follows_the_ui(self):
        self.assertEqual(self.out.faders[0], int(0.8 * 16383), "the motor shows the chain's level")
        self.dev.midi_event(bytes([NOTE_ON, FADER_TOUCH_0, 127]))
        self.dev.midi_event(bytes([PITCHBEND, 0x7F, 0x3F]))  # 8191 of 16383
        self.dev.midi_event(bytes([NOTE_ON, FADER_TOUCH_0, 0]))
        self.assertAlmostEqual(self.level.value, 8191 / 16383, places=4)
        self.dev.update_mixer_strip(0, "level", 0.25)  # a change made on the UI
        self.assertEqual(self.out.faders[0], int(0.25 * 16383))


class test_vpot_codec(unittest.TestCase):

    def test_steps(self):
        self.assertEqual(mkc.zynthian_ctrldev_mackiecontrol.vpot_steps(0x01), 1)
        self.assertEqual(mkc.zynthian_ctrldev_mackiecontrol.vpot_steps(0x41), -1)
        self.assertEqual(mkc.zynthian_ctrldev_mackiecontrol.vpot_steps(0x47), -7)

    def test_ring(self):
        ring = mkc.zynthian_ctrldev_mackiecontrol.vpot_ring
        self.assertEqual([ring(None), ring(0.0), ring(0.5), ring(1.0), ring(2.0)], [0, 0x21, 0x26, 0x2B, 0x2B])


if __name__ == "__main__":
    unittest.main()
