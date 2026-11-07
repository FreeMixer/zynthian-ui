# -*- coding: utf-8 -*-
#
# The plugin bar is a setting that lasts: chosen from the admin menu, saved by zynconf.save_config into the
# configuration file, applied at once (no restart), and read back by the next start. Below-bar rows are painted in the
# warning colour, a plugin that does not load stays greyed; qualified-only does not accept a refused plugin's pin.
#
# Copyright (C) 2026 Pau Aliagas <linuxnow@gmail.com>
# SPDX-License-Identifier: GPL-2.0-or-later

import os
import sys
import types
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path[:0] = [HERE, ROOT]

if "psutil" not in sys.modules:
    try:
        import psutil  # noqa: F401
    except ImportError:  # zynconf imports it for the network helpers this test does not use
        sys.modules["psutil"] = types.ModuleType("psutil")

import importlib.util  # noqa: E402
import test_engine_menu as menu  # noqa: E402  the engine screen with its stand-ins
import test_engine_plugin_hostd as engine_tests  # noqa: E402  the engine and its fake daemon
from fake_plugin_hostd import fake_plugin_hostd  # noqa: E402
from zyngine import zynthian_engine_plugin_hostd as ph  # noqa: E402
from zyngine import zynthian_plugin_tiers as tiers  # noqa: E402



def real_zynconf():
    """The real configuration module: other test files put a stand-in for zynconf in sys.modules."""
    spec = importlib.util.spec_from_file_location("zynconf_under_test", os.path.join(ROOT, "zynconf", "zynthian_config.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def install_real_zynconf(case):
    """zynconf is the real one for this case, its configuration file the case's own (the admin menu and the engine
    screen save without naming a file)."""
    global zynconf
    zynconf = real_zynconf()
    zynconf.config_fpath = case.fpath
    patcher = mock.patch.dict(sys.modules, {"zynconf": zynconf})
    patcher.start()
    case.addCleanup(patcher.stop)


zynconf = None
ENVARS = 'export ZYNTHIAN_UI_COLOR_BG="#000"\nexport ZYNTHIAN_PLUGIN_BAR="load-all"\n# Directory Paths\n'


class plugin_bar_setting(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.fpath = os.path.join(self.dir.name, "zynthian_envars.sh")
        with open(self.fpath, "w") as f:
            f.write(ENVARS)
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("ZYNTHIAN_PLUGIN_BAR", None)
        install_real_zynconf(self)

    def restart(self):
        """What the next start does: the environment is gone, the configuration file is read again."""
        os.environ.pop("ZYNTHIAN_PLUGIN_BAR", None)
        zynconf.load_config(True, self.fpath)

    def test_the_bar_is_saved_and_read_back_after_a_restart(self):
        self.assertEqual(tiers.plugin_bar(), tiers.DEFAULT_BAR)
        tiers.set_plugin_bar("qualified-only", self.fpath)
        self.restart()
        self.assertEqual(tiers.plugin_bar(), "qualified-only")
        with open(self.fpath) as f:
            text = f.read()
        self.assertEqual(text.count("ZYNTHIAN_PLUGIN_BAR"), 1, text)
        self.assertIn('export ZYNTHIAN_PLUGIN_BAR="qualified-only"', text)
        self.assertIn('export ZYNTHIAN_UI_COLOR_BG="#000"', text, "the rest of the file is kept")

    def test_a_bar_that_was_never_set_is_added_to_the_file(self):
        with open(self.fpath, "w") as f:
            f.write('export ZYNTHIAN_UI_COLOR_BG="#000"\n')
        tiers.set_plugin_bar("load-all", self.fpath)
        self.restart()
        self.assertEqual(tiers.plugin_bar(), "load-all")

    def test_the_bar_applies_at_once_without_a_restart(self):
        tiers.set_plugin_bar("load-all", self.fpath)
        self.assertEqual(tiers.plugin_bar(), "load-all")
        tiers.set_plugin_bar("qualified-only", self.fpath)
        self.assertEqual(tiers.plugin_bar(), "qualified-only")

    def test_a_value_that_is_no_bar_is_refused_and_nothing_is_saved(self):
        with open(self.fpath) as f:
            before = f.read()
        with self.assertRaises(ValueError):
            tiers.set_plugin_bar("never", self.fpath)
        with open(self.fpath) as f:
            self.assertEqual(f.read(), before)

    def test_the_admin_menu_lists_the_bars_and_ticks_the_one_in_force(self):
        tiers.set_plugin_bar("qualified-only", self.fpath)
        self.assertEqual(tiers.bar_menu_options(), {"\u2612 Qualified only": "qualified-only",
                                                    "\u2610 Warn on crashes": "warn-on-crashes",
                                                    "\u2610 Load all": "load-all"})
        tiers.set_plugin_bar("load-all", self.fpath)
        self.assertEqual([t for t in tiers.bar_menu_options() if t.startswith("\u2612")], ["\u2612 Load all"])


class plugin_bar_screen(unittest.TestCase):
    """The engine screen, with its plugin list, on the saved bar."""

    build = menu.engine_menu.build

    def setUp(self):
        menu.engine_menu.setUp(self)
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.fpath = os.path.join(self.dir.name, "zynthian_envars.sh")
        with open(self.fpath, "w") as f:
            f.write(ENVARS)
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        install_real_zynconf(self)
        self.screen.shown = True

    def test_a_choice_in_the_plugin_list_is_saved_and_the_next_start_reads_it(self):
        self.build()
        self.screen.show_menu()
        options = self.screen.zyngui.screens['option'].config.call_args.args[1]
        cb = self.screen.zyngui.screens['option'].config.call_args.args[2]
        cb("\u2610 Qualified only", options["\u2610 Qualified only"])
        os.environ.pop("ZYNTHIAN_PLUGIN_BAR")
        zynconf.load_config(True, self.fpath)
        self.assertEqual(tiers.plugin_bar(), "qualified-only")

    def test_the_admin_choice_reaches_the_open_list_at_once(self):
        self.build()
        tiers.set_plugin_bar("qualified-only", self.fpath)
        self.screen.refresh_bar()
        self.assertEqual(self.screen.bar, "qualified-only")
        self.screen.fill_list()
        self.assertNotIn("Refused Amp (PH)  below bar · refused", [row[2] for row in self.screen.filled])

    def test_the_rows_below_the_bar_are_painted_warning_and_a_plugin_that_does_not_load_is_greyed(self):
        self.engines["PH/Slow"].update(NOT_WORKING="does not load")
        self.build()
        self.screen.listbox.itemconfig.assert_any_call(1, {'fg': "#warn"})
        self.screen.listbox.itemconfig.assert_any_call(2, {'fg': "#grey"})
        self.assertEqual(self.screen.listbox.itemconfig.call_count, 2)


class plugin_bar_reaches_the_daemon_after_a_restart(engine_tests.plugin_hostd_case):
    """The daemon keeps a plugin the user accepted across the UI's restarts; the saved bar decides, at each add, whether
    that choice stands."""

    def setUp(self):
        super().setUp()
        self.dir2 = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir2.cleanup)
        self.fpath = os.path.join(self.dir2.name, "zynthian_envars.sh")
        with open(self.fpath, "w") as f:
            f.write(ENVARS)
        install_real_zynconf(self)
        os.environ.pop("ZYNTHIAN_PLUGIN_BAR", None)
        self.qualify("pin_set urn:test:amp amp.so=00 omx-layout/1:00 verdict=refused\n")

    qualify = engine_tests.test_below_bar.qualify

    def restart(self):
        """What the next start of the UI does: the environment is gone, the configuration file is read again."""
        os.environ.pop("ZYNTHIAN_PLUGIN_BAR", None)
        path = os.environ["PATH"]
        os.environ["PATH"] = "/usr/bin:/bin"  # the shell that reads the file needs its tools
        try:
            zynconf.load_config(True, self.fpath)
        finally:
            os.environ["PATH"] = path

    def test_a_bar_raised_in_the_menu_is_still_in_force_after_a_restart_of_the_ui(self):
        with mock.patch.object(fake_plugin_hostd, "REFUSED", {"urn:test:amp"}):
            fake = self.serve()
            tiers.set_plugin_bar("load-all", self.fpath)
            self.restart()
            self.add(self.chain(), "PH/Amp")  # accepted: the daemon now holds the user's choice
            tiers.set_plugin_bar("qualified-only", self.fpath)
            self.restart()
            self.assertEqual(tiers.plugin_bar(), "qualified-only")
            with self.assertRaises(ph.plugin_hostd_error) as refused:
                self.add(self.chain(2), "PH/Amp")
        self.assertEqual(refused.exception.code, -516)
        self.assertEqual([l for l in fake.lines if l.split()[0] in ("pin_accept", "pin_clear", "pin_reload")],
                         ["pin_accept urn:test:amp", "pin_clear urn:test:amp", "pin_reload"])


if __name__ == "__main__":
    unittest.main()
