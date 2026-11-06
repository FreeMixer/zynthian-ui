# -*- coding: utf-8 -*-
#
# The words of plugin-hostd's lines: quoted words, percent-encoded paths, and the pin lines and the pin_file key
# the qualification reader takes through them. "Surge XT.clap" is written by plugin-hostd-protocol 0.4 percent-encoded
# (Surge%20XT.clap) or in quotation marks, and plugin-hostd keys it by the path decoded.
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
if "zyngine" not in sys.modules:
    _pkg = types.ModuleType("zyngine")
    _pkg.__path__ = [os.path.join(ROOT, "zyngine")]
    sys.modules["zyngine"] = _pkg

from zyngine import zynthian_plugin_hostd_protocol as grammar  # noqa: E402
from zyngine import zynthian_plugin_qualification as qual  # noqa: E402

SHA = "1" * 64
LAYOUT = "omx-layout/1:" + "a" * 64
KEY = "clap:/usr/lib/clap/Surge XT.clap#org.surge-synth-team.surge-xt"


class test_words(unittest.TestCase):

    def test_bare_and_quoted_words(self):
        self.assertEqual(grammar.words('pin_set "clap:/p/My Plugins/x.clap#id" f=1  v'), ["pin_set", "clap:/p/My Plugins/x.clap#id", "f=1", "v"])
        self.assertEqual(grammar.words(r'a "b \"c\" d" e'), ["a", 'b "c" d', "e"])

    def test_the_only_escape_is_the_quote(self):
        self.assertEqual(grammar.words(r'x "a\b"'), ["x", "a\\b"], "a backslash is a character of the word")
        self.assertEqual(grammar.quote("a\\b"), '"a\\b"')
        self.assertEqual(grammar.quote('a"b'), '"a\\"b"')
        # (a value that ends in a backslash cannot be quoted: the grammar reads \" there as a quote, never a closing one)
        for value in ("a\\b", 'a"b', "a b", "a\\b\\c d"):
            self.assertEqual(grammar.words(grammar.quote(value)), [value])

    def test_a_path_is_percent_encoded_and_back(self):
        self.assertEqual(grammar.encode_path("/c/Surge XT.clap"), "/c/Surge%20XT.clap")
        self.assertEqual(grammar.encode_path("/c/a,b=c%d\"e\\f\tg"), "/c/a%2Cb%3Dc%25d%22e%5Cf%09g")
        for path in ("/c/Surge XT.clap", "/c/100%.clap", "/c/é ü.clap", "/c/a\nb"):
            self.assertEqual(grammar.decode_path(grammar.encode_path(path)), path)
        with self.assertRaises(ValueError):
            grammar.decode_path("/c/50%.clap")

    def test_every_spelling_of_a_uri_is_one_key(self):
        for spelling in ("clap:/usr/lib/clap/Surge%20XT.clap#org.surge-synth-team.surge-xt", KEY):
            self.assertEqual(grammar.pin_key(spelling), KEY)
        self.assertEqual(grammar.pin_key("urn:amp"), "urn:amp")
        self.assertEqual(grammar.uri_word(KEY), "clap:/usr/lib/clap/Surge%20XT.clap#org.surge-synth-team.surge-xt")
        self.assertEqual(grammar.uri_word("urn:amp"), "urn:amp")


class test_pin_lines(unittest.TestCase):

    def read(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "x.pins")
            with open(path, "w") as f:
                f.write(text)
            return qual.read_pins([path])

    def test_surge_xt_in_each_form_plugin_hostd_writes_it(self):
        forms = {
            "encoded": f"pin_set clap:/usr/lib/clap/Surge%20XT.clap#org.surge-synth-team.surge-xt Surge%20XT.clap={SHA} {LAYOUT} verdict=isolated",
            "quoted": f'pin_set "{KEY}" "Surge XT.clap={SHA}" {LAYOUT} verdict=isolated',
            "quoted store form": f'pin_set "{KEY}" Surge%20XT.clap={SHA} {LAYOUT} verdict=isolated',
        }
        for name, line in forms.items():
            with self.subTest(name):
                self.assertEqual(self.read(line + "\n"), {KEY: "isolated"})

    def test_the_last_line_of_a_key_wins_whatever_the_spelling(self):
        text = (f"pin_set clap:/usr/lib/clap/Surge%20XT.clap#org.surge-synth-team.surge-xt a={SHA} {LAYOUT} verdict=in-process\n"
                f'pin_set "{KEY}" a={SHA} {LAYOUT} verdict=refused\n')
        self.assertEqual(self.read(text), {KEY: "refused"})

    def test_the_menus_find_the_verdict_of_a_spaced_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "x.pins")
            with open(path, "w") as f:
                f.write(f'pin_set "{KEY}" a={SHA} {LAYOUT} verdict=refused\n')
            facts = qual.qualification(os.path.join(tmp, "records"), [path]).refresh().facts(KEY, None)
        self.assertEqual(facts.pin, "refused")

    def test_the_pin_file_of_the_config_may_be_quoted(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf = os.path.join(tmp, "plugin-hostd.conf")
            with open(conf, "w") as f:
                f.write('pin_mode first-use\npin_file "/var/lib/my data/pins file.pins"\n')
            with mock.patch.dict(os.environ, {"ZYNTHIAN_PLUGIN_HOSTD_CONF": conf}):
                self.assertEqual(qual.conf_pin_file(), "/var/lib/my data/pins file.pins")


if __name__ == "__main__":
    unittest.main()
