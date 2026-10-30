# -*- coding: utf-8 -*-
#
# An add the engine refuses (plugin-hostd answering -509 PIN_BINARY_MISMATCH, say) leaves no processor in the chain,
# no busy client behind and the refusal's meaning to show; a snapshot restore that meets one drops that processor,
# finishes, and says what it left out (#18). The chain manager's own add_processor and set_state, on a real chain.
#
# Copyright (C) 2026 Pau Aliagas <linuxnow@gmail.com>
# SPDX-License-Identifier: GPL-2.0-or-later

import sys
import types
import unittest
from unittest import mock

import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_engine_plugin_hostd  # noqa: F401  the stand-ins and the protocol fixture
from zyngine import zynthian_engine_plugin_hostd as ph

# What the chain manager imports beside the engines: stand-ins, as the hardware ones are
_added = []  # the stand-ins put in sys.modules here, taken out again once the chain manager is imported
for _name, _attrs in (("zynlibs", {}), ("zynlibs.zynseq", {}), ("zyngui", {}), ("zyngui.zynthian_gui_config", {}),
                      ("zyngine.zynthian_engine_jalv", {}),
                      ("zyngine.zynthian_engine_pianoteq", {"get_pianoteq_binary_info": lambda: None}),
                      ("zyngine.zynthian_state_manager", {"MAIN_MIXBUS_ID": 17})):
    if _name not in sys.modules:
        _m = types.ModuleType(_name)
        _m.__dict__.update(_attrs)
        sys.modules[_name] = _m
        _added.append(_name)
sys.modules["zyngui"].zynthian_gui_config = sys.modules["zyngui.zynthian_gui_config"]
# the router's limits the chain manager reads when it is imported
_core = sys.modules["zyncoder.zyncore"].lib_zyncore
for _fn, _value in (("zmop_get_num_chains", 17), ("zmip_get_num_devs", 16), ("zmop_get_num_devs", 16),
                    ("zmip_get_seq_index", 17), ("zmip_get_step_index", 18), ("zmip_get_int_index", 19),
                    ("zmip_get_ctrl_index", 20)):
    getattr(_core, _fn).return_value = _value
sys.modules["zyngine"].zynthian_state_manager = sys.modules["zyngine.zynthian_state_manager"]
# the engine classes it names at import, which the real zyngine/__init__ star-imports: a stand-in for each one
# this package stand-in does not already hold (the plugin-hostd engine is the real one)
import os as _os, re as _re  # noqa: E402
_src = open(_os.path.join(test_engine_plugin_hostd.ROOT, "zyngine", "zynthian_chain_manager.py")).read()
for _cls in sorted(set(_re.findall(r"\b(zynthian_engine_\w+)\b", _src))):
    if not hasattr(sys.modules["zyngine"], _cls):
        setattr(sys.modules["zyngine"], _cls, mock.MagicMock(name=_cls))
if not hasattr(sys.modules["zyngine.zynthian_lv2"], "get_engines"):
    sys.modules["zyngine.zynthian_lv2"].get_engines = lambda *a, **k: {"PT": {"NAME": "Pianoteq", "TYPE": "MIDI Synth"}}
from zyngine import zynthian_chain_manager as cm  # noqa: E402
from zyngine.zynthian_chain import zynthian_chain  # noqa: E402
# later test modules (test_startup_imports on a Zynthian) must find the real zyngui, not this stand-in
for _name in _added:
    del sys.modules[_name]

REFUSED = -509


class busy_log:
    """The state manager's busy clients, as start_busy and end_busy keep them."""

    def __init__(self):
        self.busy = set()
        self.zynseq = mock.MagicMock()  # start_engine moves the sequencer's JACK position

    def start_busy(self, clid, message=None, details=None, tts=True):
        self.busy.add(clid)

    def end_busy(self, clid):
        self.busy.discard(clid)

    def is_busy(self, client=None):
        return client in self.busy if client else bool(self.busy)


class refusing_engine:
    """An engine whose add answers what plugin-hostd answered on the Pi."""

    def __init__(self, eng_code, state_manager):
        self.processors = []

    def add_processor(self, processor):
        raise ph.plugin_hostd_error(REFUSED, "add http://moddevices.com/plugins/mda/Piano 1000")

    def remove_processor(self, processor):
        raise AssertionError("the engine never took the processor, so it is not asked to remove it")


class accepting_engine(refusing_engine):

    def add_processor(self, processor):
        self.processors.append(processor)

    def remove_processor(self, processor):
        self.processors.remove(processor)

    def get_name(self, processor):
        return "Accepting"

    def __getattr__(self, name):
        # the rest of an engine's surface the chain's graph rebuild reads (jackname, ports...): stand-ins
        value = mock.MagicMock(name=name)
        setattr(self, name, value)
        return value


class refused_add(unittest.TestCase):

    def setUp(self):
        man = cm.zynthian_chain_manager.__new__(cm.zynthian_chain_manager)
        man.state_manager = busy_log()
        man.chains = {1: zynthian_chain(1, midi_chan=0)}
        man.processors = {}
        man.zyngines = {}
        man.zyngine_counter = 0
        man.add_refusal = None
        info = {"TYPE": "MIDI Synth", "CAT": "Piano", "ENABLED": True, "INDEX": 0, "URL": "", "UI": False}
        man.engine_info = {"PH/Piano": dict(info, NAME="Piano", ENGINE=refusing_engine),
                           "PH/Ok": dict(info, NAME="Ok", TYPE="Audio Effect", ENGINE=accepting_engine)}
        man.rebuild_optimisation_cache = lambda: None
        man.stop_unused_single_proc_engines = lambda: None
        man.remove_all_chains = lambda *a, **k: None
        man.add_chain_from_state = lambda chain_id, state: chain_id
        self.man = man
        self.chain = man.chains[1]
        self.chain.rebuild_graph = lambda: None  # the audio graph needs a mixer strip: not what these tests are about

    def test_the_refused_processor_is_not_left_in_the_chain(self):
        self.assertIsNone(self.man.add_processor(1, "PH/Piano", proc_id=1000))
        self.assertEqual(self.chain.get_processors(), [])
        self.assertNotIn(1000, self.man.processors)
        self.assertNotIn("PH#1000", self.man.zyngines)

    def test_no_busy_client_is_left_behind(self):
        self.man.add_processor(1, "PH/Piano", proc_id=1000)
        self.assertFalse(self.man.state_manager.is_busy(), self.man.state_manager.busy)

    def test_the_refusal_says_what_the_protocol_says_the_code_means(self):
        self.man.add_processor(1, "PH/Piano", proc_id=1000)
        self.assertEqual(self.man.add_refusal, "Piano: " + ph.protocol.meaning(REFUSED))
        self.assertIn("SHA-256", self.man.add_refusal)

    def test_an_accepted_add_has_no_refusal(self):
        self.man.add_processor(1, "PH/Piano", proc_id=1000)
        self.assertIsNotNone(self.man.add_processor(1, "PH/Ok", proc_id=1001))
        self.assertIsNone(self.man.add_refusal)

    def test_a_restore_drops_the_refused_processor_finishes_and_says_so(self):
        state = {1: {"slots": [{"1000": "PH/Piano"}, {"1001": "PH/Ok"}]}}
        with mock.patch.object(cm.zynsigman, "send_queued") as send:
            self.man.set_state(state, {}, merge=False)
        self.assertFalse(self.man.state_manager.is_busy(), self.man.state_manager.busy)
        self.assertEqual([p.id for p in self.chain.get_processors()], [1001])
        messages = [c.kwargs.get("message") for c in send.call_args_list if c.kwargs.get("message")]
        self.assertEqual(len(messages), 1, send.call_args_list)
        self.assertIn("Piano: " + ph.protocol.meaning(REFUSED), messages[0])


if __name__ == "__main__":
    unittest.main()
