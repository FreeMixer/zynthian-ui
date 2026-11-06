# -*- coding: utf-8 -*-
#
# MPE is switched on by the plugin, not by the engine that hosts it: Surge XT under jalv (JV/) or plugin-hostd (PH/)
# gets the lower-zone RPN 6 (15 members on ALL channels, 0 on a single one); other plugins get nothing.
#
# Copyright (C) 2026 Pau Aliagas <linuxnow@gmail.com>
# SPDX-License-Identifier: GPL-2.0-or-later

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_refused_add  # noqa: F401  imports the real chain with the hardware stand-ins
from zyngine import zynthian_chain as chain_mod

zynthian_chain = test_refused_add.zynthian_chain


def make_chain(eng_code, midi_chan):
    chain = object.__new__(zynthian_chain)
    chain.zmop_index = 3
    chain.midi_chan = midi_chan
    proc = mock.Mock(eng_code=eng_code)
    chain.synth_slots = [[proc]]
    chain.is_synth = lambda: True
    return chain


class ChainMPE(unittest.TestCase):
    def sent(self, eng_code, midi_chan):
        with mock.patch.object(chain_mod, "lib_zyncore") as lib:
            make_chain(eng_code, midi_chan).init_MPE()
        return [c.args for c in lib.zmop_send_ccontrol_change.call_args_list]

    def rpn(self, members):
        return [(3, 0, 0x65, 0), (3, 0, 0x64, 6), (3, 0, 0x6, members)]

    def test_ph_surge_all_channels(self):
        self.assertEqual(self.sent("PH/Surge XT", 0xFFFF), self.rpn(15))

    def test_ph_surge_single_channel(self):
        self.assertEqual(self.sent("PH/Surge XT", 4), self.rpn(0))

    def test_jv_unchanged(self):
        self.assertEqual(self.sent("JV/OB-Xf", 0xFFFF), self.rpn(15))
        self.assertEqual(self.sent("JV/Surge XT", 2), self.rpn(0))
        self.assertEqual(self.sent("TAL U-No-LX-V2", 0xFFFF), self.rpn(15))

    def test_ph_other_plugin_sends_nothing(self):
        self.assertEqual(self.sent("PH/Odin2", 0xFFFF), [])
        self.assertEqual(self.sent("JV/Odin2", 0xFFFF), [])


if __name__ == "__main__":
    unittest.main()
