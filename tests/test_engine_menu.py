# -*- coding: utf-8 -*-
#
# The engine selection screen's own methods (zyngui/zynthian_gui_engine.py): the info panel of a PH entry, and
# toggling a PH entry in its enable view. tkinter and the base screens are stand-ins; the screen code is the real one.
#
# Copyright (C) 2026 Pau Aliagas <linuxnow@gmail.com>
# SPDX-License-Identifier: GPL-2.0-or-later

import os
import sys
import types
import unittest
import importlib.util
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path[:0] = [HERE, ROOT]

if "zyngine" not in sys.modules:
    _pkg = types.ModuleType("zyngine")
    _pkg.__path__ = [os.path.join(ROOT, "zyngine")]
    sys.modules["zyngine"] = _pkg
import test_engine_plugin_hostd  # noqa: E402,F401  the stand-ins of the native libraries, for the engine the screen imports


def stand_in(name, **attrs):
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    return module


class selector:
    def __init__(self, *args):
        pass

    def fill_list(self):
        self.filled = list(self.list_data)


def load_screen():
    gui_config = stand_in("zyngui.zynthian_gui_config", color_tx_off="#grey", color_panel_bg="#bg",
                          color_panel_tx="#tx", layout={"ctrl_order": []})
    modules = {
        "tkinter": stand_in("tkinter", END="end"),
        "zyngui": stand_in("zyngui", __path__=[], zynthian_gui_config=gui_config),
        "zyngui.zynthian_gui_config": gui_config,
        "zyngui.zynthian_gui_base": stand_in("zyngui.zynthian_gui_base", zynthian_gui_base=object),
        "zyngui.zynthian_gui_selector": stand_in("zyngui.zynthian_gui_selector", zynthian_gui_selector=selector),
        "zyngui.zynthian_gui_controller": stand_in("zyngui.zynthian_gui_controller", zynthian_gui_controller=object),
        "zyngine.zynthian_lv2": stand_in("zyngine.zynthian_lv2", engines_by_type={}),
    }
    with mock.patch.dict(sys.modules, modules), \
            mock.patch.object(sys.modules["zyngine"], "zynthian_lv2", modules["zyngine.zynthian_lv2"], create=True):
        spec = importlib.util.spec_from_file_location("zynthian_gui_engine_under_test",
                                                      os.path.join(ROOT, "zyngui", "zynthian_gui_engine.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


screen_module = load_screen()


def info(name, url, enabled=True, title=None):
    return {"NAME": name, "TITLE": title or f"{name} (PH)", "TYPE": "Audio Effect", "CAT": "Dynamics", "URL": url,
            "ENABLED": enabled, "EDIT": 0}


class engine_menu(unittest.TestCase):

    def setUp(self):
        self.engines = {"PH/Slow": info("Linear EQ", "urn:q:slow"), "PH/Fast": info("Fast Comp", "urn:q:fast"),
                        "PH/Refused": info("Refused Amp", "urn:q:refused"),
                        "PH/Crashed": info("Crashy Delay", "urn:q:crashed")}
        self.by_cat = {"Dynamics": dict(self.engines), "Delay": {"PH/Crashed": self.engines["PH/Crashed"]}}
        self.by_cat["Dynamics"].pop("PH/Crashed")
        s = object.__new__(screen_module.zynthian_gui_engine)
        s.chain_manager = mock.MagicMock()
        s.chain_manager.engine_info = self.engines
        s.chain_manager.filtered_engines_by_cat.side_effect = lambda t, all: {c: dict(e) for c, e in self.by_cat.items()}
        s.zyngui = mock.MagicMock()
        s.zyngui.modify_chain_status = {"type": "MIDI Synth"}
        s.zyngui.state_manager.get_jackd_samplerate.return_value = 48000
        s.zyngui.state_manager.get_jackd_blocksize.return_value = 256
        s.listbox, s.listbox2 = mock.MagicMock(), mock.MagicMock()
        s.lb2_fg = s.lb2_bg = "#c"
        s.cat_index, s.show_all, s.engine_info_dirty, s.index, s.context_index = 0, False, False, 0, {}
        s.update_list = lambda: None
        self.screen = s

    def test_the_info_panel_shows_the_format_line_after_the_description(self):
        surge = {"NAME": "Surge XT", "TYPE": "MIDI Synth", "CAT": "Synth", "QUALITY": 0, "COMPLEX": 0,
                 "DESCR": "Plugin by Surge Synth Team",
                 "FORMAT_LINE": "CLAP, run by plugin-hostd (an LV2 build is also installed)"}
        self.assertEqual(self.screen.description_text(surge), "Plugin by Surge Synth Team\n\n"
                         "CLAP, run by plugin-hostd (an LV2 build is also installed)")
        self.assertEqual(self.screen.description_text({"DESCR": "A jalv plugin"}), "A jalv plugin")
        self.assertEqual(self.screen.description_text({"DESCR": "", "FORMAT_LINE": "LV2, run by plugin-hostd"}),
                         "LV2, run by plugin-hostd")
        self.screen.engine_cats = []
        self.screen.get_info = lambda code=None: surge
        self.screen.show_details("PH/surge")
        html = self.screen.zyngui.screens['help'].set_html.call_args.args[0]
        self.assertIn('<div class="format">CLAP, run by plugin-hostd (an LV2 build is also installed)</div>', html)

    def test_the_enable_view_carries_a_ph_flag_to_its_jalv_entry(self):
        self.engines["JV/Linear EQ"] = info("Linear EQ", "urn:q:slow", title="Linear EQ")
        self.engines["PH/Linear EQ"] = self.engines.pop("PH/Slow")
        self.screen.show_all = True
        self.screen.list_data = [("PH/Linear EQ", 0, "", "")]
        self.screen.select_action(0)
        self.assertFalse(self.engines["PH/Linear EQ"]["ENABLED"])
        self.assertFalse(self.engines["JV/Linear EQ"]["ENABLED"])
        self.assertTrue(self.screen.engine_info_dirty)


if __name__ == "__main__":
    unittest.main()
