# -*- coding: utf-8 -*-
#
# The engine selection screen's own methods (zyngui/zynthian_gui_engine.py) building a plugin menu
# from the fixture qualification, and toggling a PH entry in its enable view. tkinter and the base
# screens are stand-ins; the screen code is the real one.
#
# Copyright (C) 2026 Pau Aliagas <linuxnow@gmail.com>
# SPDX-License-Identifier: GPL-2.0-or-later

import os
import sys
import types
import logging
import unittest
import importlib.util
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIXTURES = os.path.join(HERE, "fixtures", "qualification")
sys.path[:0] = [HERE, ROOT]

if "zyngine" not in sys.modules:
    _pkg = types.ModuleType("zyngine")
    _pkg.__path__ = [os.path.join(ROOT, "zyngine")]
    sys.modules["zyngine"] = _pkg
from zyngine import zynthian_plugin_qualification as qual  # noqa: E402


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


FAST = "Fast Comp (PH)  live 0fr RT-ok 40ns"
SLOW = "Linear EQ (PH)  studio 1024fr RT-ok cost?"


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
        s.qualification = qual.qualification(os.path.join(FIXTURES, "records"), [os.path.join(FIXTURES, "pins")])
        s.listbox, s.listbox2 = mock.MagicMock(), mock.MagicMock()
        s.lb2_fg = s.lb2_bg = "#c"
        s.cat_index, s.show_all, s.engine_info_dirty, s.index, s.context_index = 0, False, False, 0, {}
        s.list_order, s.hosted_only = "tier", False
        s.update_list = lambda: None
        self.screen = s

    def build(self, show="0"):
        with mock.patch.dict(os.environ, {"ZYNTHIAN_PLUGIN_SHOW_HIDDEN": show}), self.assertLogs(level=logging.WARNING):
            self.screen.qualification.stamp = None
            self.screen.get_engines_by_cat()
        self.screen.fill_list()
        return [row[2] for row in self.screen.filled]

    def test_the_menu_is_tagged_and_ordered_and_hides_the_refused_and_crashed(self):
        self.assertEqual(self.build(), ["Fast Comp (PH)  live 0fr RT-ok 40ns", "Linear EQ (PH)  studio 1024fr RT-ok cost?"])
        self.assertEqual(self.screen.engine_cats, ["Dynamics"], "a category of hidden plugins only is not listed")
        self.screen.listbox.itemconfig.assert_not_called()

    def test_shown_the_hidden_ones_are_greyed(self):
        self.assertEqual(self.build("1"), ["Fast Comp (PH)  live 0fr RT-ok 40ns", "Linear EQ (PH)  studio 1024fr RT-ok cost?",
                                           "Refused Amp (PH)  hidden · refused"])
        self.screen.listbox.itemconfig.assert_called_once_with(2, {'fg': "#grey"})
        self.assertEqual(self.screen.engine_cats, ["Dynamics", "Delay"])

    def test_a_refused_plugins_jalv_row_is_listed_after_the_qualified_ones(self):
        # The catalogue leaves the jalv row of a plugin plugin-hostd refuses in the menu: it is offered, tagged
        self.engines["JV/Refused"] = info("Refused Amp", "urn:q:refused", title="Refused Amp")
        self.by_cat["Dynamics"]["JV/Refused"] = self.engines["JV/Refused"]
        self.assertEqual(self.build(), ["Fast Comp (PH)  live 0fr RT-ok 40ns", "Linear EQ (PH)  studio 1024fr RT-ok cost?",
                                        "Refused Amp  refused"])
        self.screen.listbox.itemconfig.assert_not_called()
        self.assertEqual(self.build("1")[2:], ["Refused Amp  refused", "Refused Amp (PH)  hidden · refused"])
        self.screen.listbox.itemconfig.assert_called_once_with(3, {'fg': "#grey"})

    def test_the_list_options_sort_and_filter_the_menu(self):
        self.engines["JV/Slow"] = info("Linear EQ", "urn:q:slow", title="Linear EQ")
        self.by_cat["Dynamics"]["JV/Slow"] = self.engines["JV/Slow"]
        self.build()
        self.screen.show_menu()
        title, options, cb = self.screen.zyngui.screens['option'].config.call_args.args
        self.assertEqual(title, "Plugin list")
        self.assertEqual(list(options), ["Sort by", "\u2612 Tier", "\u2610 Name", "\u2610 Cost", "\u2610 Latency",
                                         "\u2610 RT safety", "Filter", "\u2610 plugin-hostd plugins only"])
        with self.assertLogs(level=logging.WARNING):
            self.screen.qualification.stamp = None
            cb("\u2610 Latency", options["\u2610 Latency"])
        self.screen.fill_list()
        self.assertEqual([row[2] for row in self.screen.filled],
                         [FAST, "Linear EQ  studio 1024fr RT-ok cost?", SLOW])
        self.screen.show_menu()
        options = self.screen.zyngui.screens['option'].config.call_args.args[1]
        self.assertIn("\u2612 Latency", options)
        cb("\u2610 plugin-hostd plugins only", options["\u2610 plugin-hostd plugins only"])
        self.screen.fill_list()
        self.assertEqual([row[2] for row in self.screen.filled], [FAST, SLOW])
        self.assertEqual((self.screen.list_order, self.screen.hosted_only, self.screen.index), ("latency", True, 0))

    def test_an_unreadable_qualification_leaves_the_plain_menu(self):
        self.screen.qualification = mock.MagicMock()
        self.screen.qualification.refresh.side_effect = ValueError("broken")
        with self.assertLogs(level=logging.ERROR):
            self.screen.get_engines_by_cat()
        self.screen.fill_list()
        self.assertEqual([row[2] for row in self.screen.filled], ["Linear EQ (PH)", "Fast Comp (PH)", "Refused Amp (PH)"])

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
