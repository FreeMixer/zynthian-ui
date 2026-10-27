# -*- coding: utf-8 -*-
#
# The processor options screen (zyngui/zynthian_gui_processor_options.py) lists a Grouping entry for a
# plugin-hostd plugin the daemon's plan left alone, with the text of the reason, and none for any other
# processor. The base screens are stand-ins; the screen code is the real one.
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


class selector_info:
    def __init__(self, *args, **kwargs):
        pass

    def fill_list(self):
        self.filled = list(self.list_data)


def load_screen():
    modules = {
        "zynautoconnect": mock.MagicMock(),
        "zyngine.zynthian_signal_manager": types.SimpleNamespace(zynsigman=mock.MagicMock()),
        "zyngui.zynthian_gui_selector_info": types.SimpleNamespace(zynthian_gui_selector_info=selector_info),
    }
    if "zyngine" not in sys.modules:
        pkg = types.ModuleType("zyngine")
        pkg.__path__ = [os.path.join(ROOT, "zyngine")]
        modules["zyngine"] = pkg
    if "zyngui" not in sys.modules:
        modules["zyngui"] = types.ModuleType("zyngui")
        modules["zyngui"].__path__ = []
    with mock.patch.dict(sys.modules, modules):
        spec = importlib.util.spec_from_file_location("zynthian_gui_processor_options_under_test",
                                                      os.path.join(ROOT, "zyngui", "zynthian_gui_processor_options.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


screen_module = load_screen()


class processor_options_grouping(unittest.TestCase):

    def entries(self, engine):
        screen = screen_module.zynthian_gui_processor_options.__new__(screen_module.zynthian_gui_processor_options)
        screen.last_random = {}
        screen.zyngui = mock.MagicMock()
        screen.processor = mock.MagicMock(type="Audio Effect", eng_code="PH/x", engine=engine)
        screen.processor.is_bypassed.return_value = False
        screen.processor.chain.get_processor_count.return_value = 1
        screen.fill_list()
        return screen, [row for row in screen.filled if row[2] == "Grouping"]

    def test_a_plugin_left_alone_lists_the_reason(self):
        screen, rows = self.entries(types.SimpleNamespace(grouping_text=lambda: "Alone: no neighbour it can share a worker with"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][3], ["Alone: no neighbour it can share a worker with", "info.png"])
        titles = [row[2] for row in screen.filled]
        self.assertEqual(titles.index("Grouping"), titles.index("Info") + 1)
        rows[0][0]()
        screen.zyngui.show_info.assert_called_once_with("Alone: no neighbour it can share a worker with")

    def test_a_plugin_with_no_reason_lists_nothing(self):
        self.assertEqual(self.entries(types.SimpleNamespace(grouping_text=lambda: None))[1], [])

    def test_an_engine_that_does_not_group_lists_nothing(self):
        self.assertEqual(self.entries(types.SimpleNamespace())[1], [])


if __name__ == "__main__":
    unittest.main()
