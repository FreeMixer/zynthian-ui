# -*- coding: utf-8 -*-
#
# The plugin-hostd engine against a fake daemon that follows the protocol of plugin-hostd's README.
# Runs with plain `python3 -m unittest`: zyncore, liblo, pexpect, autoconnect and lilv are stand-ins,
# the engine, zynthian_chain, zynthian_processor and zynthian_controller are the real ones.
#
# Copyright (C) 2026 Pau Aliagas <linuxnow@gmail.com>
# SPDX-License-Identifier: GPL-2.0-or-later

import os
import re
import sys
import json
import time
import types
import signal
import socket
import logging
import tempfile
import threading
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIXTURES = os.path.join(HERE, "fixtures", "plugin_hostd")
FAKE = os.path.join(HERE, "fake_plugin_hostd.py")
START_STUB = os.path.join(FIXTURES, "plugin-hostd-start")  # zynthian-sys's sbin/plugin-hostd-start.sh
sys.path[:0] = [HERE, ROOT]
# plugin-hostd's declared protocol, as its package installs it
os.environ["ZYNTHIAN_PLUGIN_HOSTD_PROTOCOL_DIR"] = os.path.join(FIXTURES, "protocol")

# Hardware and native libraries: stand-ins, so the import needs no Zynthian
for _name in ("pexpect", "pyliblo3", "zynconf", "zynautoconnect", "zyncoder", "zyncoder.zyncore"):
    sys.modules.setdefault(_name, mock.MagicMock())
# The zyngine package without its __init__, which imports every engine and their libraries
if "zyngine" not in sys.modules:
    _pkg = types.ModuleType("zyngine")
    _pkg.__path__ = [os.path.join(ROOT, "zyngine")]
    sys.modules["zyngine"] = _pkg
# zynthian_lv2 needs lilv: a stand-in that knows the ports of one plugin
LV2_PORTS = {"urn:test:amp": {0: {"symbol": "gain", "name": "Gain", "range": {"min": -90, "max": 90}, "value": 0,
                                  "is_integer": False, "is_toggled": False}}}
# One stand-in per process: test_convert_snapshots_to_ph imports this file again under its bare name, and the
# catalogue must see the same registry as every copy of these tests
_lv2 = sys.modules.get("zyngine.zynthian_lv2")
if _lv2 is None:
    _lv2 = types.ModuleType("zyngine.zynthian_lv2")
    _lv2.get_plugin_ports = lambda uri: LV2_PORTS.get(uri, {})
    _lv2.engines_by_type = {}
    sys.modules["zyngine.zynthian_lv2"] = _lv2
sys.modules["zyngine"].zynthian_lv2 = _lv2
# the presets Zynthian's cache holds for an LV2 plugin, by plugin name, as jalv reads them: a bank label to its URL and
# presets; a test sets it, and a plugin it does not name has none
LV2_PRESETS = {}
_lv2.get_plugin_presets_cache = lambda name: LV2_PRESETS.get(name, {})

from zyngine import zynthian_engine_plugin_hostd as ph  # noqa: E402
from zyngine import zynthian_plugin_hostd_catalogue as cat  # noqa: E402
from zyngine import zynthian_plugin_hostd_grouping as grouping  # noqa: E402
from zyngine.zynthian_chain import zynthian_chain  # noqa: E402
from zyngine.zynthian_processor import zynthian_processor  # noqa: E402
from fake_plugin_hostd import fake_plugin_hostd  # noqa: E402
from fake_plugin_hostd import PLUGINS as fake_plugins  # noqa: E402

with open(os.path.join(FIXTURES, "omx-clap-scan.txt")) as _f:
    CLAP_SCAN = _f.read()


CLAP_SCAN_MVERB = """  plugin studio.kx.distrho.MVerb "MVerb" vendor falkTX version 1.0
    features audio-effect reverb stereo
    in  main 2 channels unknown "in" in_1
    out main 2 channels unknown "out" out_1
"""


def lv2_info(name, uri, etype="Audio Effect"):
    return {"ID": name, "NAME": name, "TITLE": name, "TYPE": etype, "CAT": "Other", "ENABLED": True, "INDEX": 0,
            "URL": uri, "UI": "", "DESCR": "", "QUALITY": 0, "COMPLEX": 0, "EDIT": 0}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(predicate, what, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


class plugin_hostd_case(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        # PATH holds python3 for the fake daemon and nothing else, unless a test adds omx-panel
        self.bin = os.path.join(self.tmp, "bin")
        os.mkdir(self.bin)
        os.symlink(sys.executable, os.path.join(self.bin, "python3"))
        self.starts = os.path.join(self.tmp, "starts")
        patcher = mock.patch.dict(os.environ, {
            "PATH": self.bin, "FAKE_HOSTD_JOURNAL": os.path.join(self.tmp, "journal"),
            "START_STUB_LOG": self.starts, "START_STUB_DIR": self.tmp})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.port = free_port()
        ph.daemon = ph.plugin_hostd_daemon(self.port)
        ph.daemon.start_script = os.path.join(self.tmp, "not-installed")
        ph.daemon.conf = os.path.join(self.tmp, "no.conf")
        self.addCleanup(lambda: (ph.daemon.stop(), ph.daemon.client.close()))
        ph._logged.clear()
        ph.layout_cache.clear()
        ph.probe_cache.clear()
        ph.DESCRIPTOR_WAIT = 0.5
        # The menus' lists by type start empty for every test, in place: the registry is shared
        _lv2.engines_by_type.clear()
        _lv2.engines_by_type.update({"MIDI Synth": {}, "Audio Effect": {}})
        # The catalogue makes the PH entries the tests add, from jalv's LV2 ones and the CLAP scan
        self.info = {"JV/Amp": lv2_info("Amp", "urn:test:amp"), "JV/NoPin": lv2_info("NoPin", "urn:test:nopin"),
                     "JV/Big": lv2_info("Big", "urn:test:big")}
        cat.add_catalogue(self.info, clap=cat.parse_clap_scan(CLAP_SCAN))
        self.sm = mock.MagicMock()
        self.sm.chain_manager.engine_info = self.info
        self.sm.chain_manager.chains = {}
        self.next_id = 1

    def serve(self, **kw):
        fake = fake_plugin_hostd(self.port, **kw).start()
        self.addCleanup(fake.stop)
        return fake

    def chain(self, chain_id=1):
        chain = zynthian_chain(chain_id, midi_chan=0)
        self.sm.chain_manager.chains[chain_id] = chain
        return chain

    def add(self, chain, code, xconfig=None):
        """What chain_manager.add_processor does: insert, start the engine, rebuild the graph."""
        proc = zynthian_processor(code, self.info[code], self.next_id)
        self.next_id += 1
        chain.insert_processor(proc)
        engine = ph.zynthian_engine_plugin_hostd(code, self.sm)
        if xconfig:
            engine.set_extended_config(xconfig)
        proc.set_engine(engine)
        chain.rebuild_graph()
        return proc

    def remove(self, chain, proc):
        """What chain_manager.remove_processor does: out of the chain, then stop the unused engine."""
        chain.remove_processor(proc)
        proc.engine.stop()

    def started(self):
        """The runs of plugin-hostd-start.sh: its arguments and the PHD_CONF it was given."""
        try:
            with open(self.starts) as f:
                return f.read().splitlines()
        except FileNotFoundError:
            return []

    def journal(self):
        try:
            with open(os.environ["FAKE_HOSTD_JOURNAL"]) as f:
                return [line.rstrip("\n").split(" ", 1) for line in f]
        except FileNotFoundError:
            return []


class test_daemon(plugin_hostd_case):

    def test_starts_a_daemon_when_none_answers_and_stops_only_that_one(self):
        ph.daemon.start_script = START_STUB
        chain = self.chain()
        proc = self.add(chain, "PH/Amp")
        started = ph.daemon.pid
        self.assertIsNotNone(started, "no daemon answered, so the engine starts one")
        self.assertEqual(self.started(), [f"{self.port} PHD_CONF={ph.daemon.conf}"],
                         "through zynthian-sys's script, on the engine's port, with its config")
        self.assertEqual({pid for pid, _ in self.journal()}, {str(started)}, "the add reached the started process")
        self.assertIn([str(started), "add urn:test:amp 1000"], self.journal())
        self.remove(chain, proc)
        self.assertIsNone(ph.daemon.pid)
        with self.assertRaises(ProcessLookupError, msg="the daemon it started is stopped with its last instance"):
            os.kill(started, 0)

    def test_reuses_a_running_daemon_and_never_stops_it(self):
        fake = self.serve()
        chain = self.chain()
        proc = self.add(chain, "PH/Amp")
        self.assertIsNone(ph.daemon.pid, "a daemon answered, so none is started")
        self.assertEqual(self.started(), [], "and the start script is not run")
        self.assertIn("add urn:test:amp 1000", fake.lines)
        self.remove(chain, proc)
        ph.daemon.stop()
        self.assertNotIn("quit", fake.lines)
        self.assertEqual(ph.plugin_hostd_client(self.port).call("worker_list"), (0, ""),
                         "the daemon it did not start still answers")

    def test_mod_host_on_the_port_is_not_taken_for_plugin_hostd(self):
        fake = self.serve(mod_host=True)
        with self.assertRaises(ph.plugin_hostd_down):
            self.add(self.chain(), "PH/Amp")
        self.assertFalse([line for line in fake.lines if line.startswith("add")])

    def test_an_instance_number_taken_by_another_controller_is_skipped(self):
        fake = self.serve()
        fake.instances[1000] = "urn:test:amp"
        fake.params[1000] = {"gain": 0.0}
        proc = self.add(self.chain(), "PH/Amp")
        self.assertEqual(proc.jackname, "effect_1001")
        self.assertEqual(fake.instances[1001], "urn:test:amp")

    def test_refusals_are_exceptions_that_carry_their_names(self):
        self.serve()
        client = ph.plugin_hostd_client(self.port)
        with self.assertRaises(ph.plugin_hostd_error) as refused:
            client.call("frobnicate 1")
        self.assertEqual((refused.exception.code, refused.exception.name), (-902, "ERR_INVALID_OPERATION"))
        with self.assertRaises(ph.plugin_hostd_error) as refused:
            client.call("chain_get 7")
        self.assertEqual(refused.exception.name, "PHD_ERR_NO_SUCH_CHAIN")
        self.assertEqual(ph.plugin_hostd_error(-501, "x").name, "PHD_ERR_PLACEMENT_INVALID")
        self.assertEqual(ph.plugin_hostd_error(-531, "x").name, "PHD_ERR_DESCRIPTOR_SCAN_FAILED")

    def test_a_daemon_that_does_not_answer_times_out(self):
        silent = socket.socket()
        silent.bind(("127.0.0.1", self.port))
        silent.listen(1)
        self.addCleanup(silent.close)
        client = ph.plugin_hostd_client(self.port)
        with self.assertRaises(ph.plugin_hostd_timeout):
            client.call("worker_list", timeout=0.2)
        self.assertIsNone(client.sock, "a late reply is never read as the next one")

    def test_a_dead_daemon_is_restarted_and_its_instances_replayed(self):
        ph.daemon.start_script = START_STUB
        chain = self.chain()
        amp1, amp2 = self.add(chain, "PH/Amp"), self.add(chain, "PH/Amp")
        amp2.controllers_dict["gain"].set_value(4.5)
        first = ph.daemon.pid
        os.kill(first, signal.SIGKILL)
        self.assertTrue(ph.daemon.wait_gone(5.0))
        amp1.controllers_dict["gain"].set_value(-2.0)
        self.assertNotEqual(ph.daemon.pid, first)
        second = str(ph.daemon.pid)
        lines = [line for pid, line in self.journal() if pid == second]
        for expected in ("add urn:test:amp 1000", "add urn:test:amp 1001", "param_set 1001 gain 4.5",
                         "chain 1 1000 1001"):
            self.assertIn(expected, lines)
        self.assertEqual(lines[-1], "param_set 1000 gain -2.0", "the command that met the dead daemon is sent last")
        self.assertLess(lines.index("param_set 1001 gain 4.5"), lines.index("chain 1 1000 1001"))


class test_protocol(plugin_hostd_case):
    """The reply codes and the readiness line are read from plugin-hostd's installed files."""

    def declared(self, daemon_errors=None, ready="plugin-hostd ready!", host_errors=None):
        """A protocol directory with the given codes, for a daemon that declares something else."""
        directory = os.path.join(self.tmp, "protocol")
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "protocol.json"), "w") as f:
            json.dump({"errors": daemon_errors or [], "readiness": {"daemon_line": ready}}, f)
        with open(os.path.join(directory, "mod-host.json"), "w") as f:
            json.dump({"host_errors": host_errors or []}, f)
        return directory

    def test_a_code_is_named_by_the_file_not_by_the_engine(self):
        directory = self.declared(
            [{"name": "ODD", "macro": "PHD_ERR_ODD", "code": -777}],
            host_errors=[{"name": "ERR_HOST_INVALID_URI", "value": -101}, {"name": "ERR_LV2_INVALID_URI", "value": -101}])
        p = ph.zynthian_plugin_hostd_protocol.protocol(directory)
        self.assertEqual((p.name(-777), p.code("ODD"), p.name(-101)), ("PHD_ERR_ODD", -777, "ERR_HOST_INVALID_URI"))
        self.assertEqual(p.name(-501), "ERR_501", "a code the files do not declare is a bare number")

    def test_the_installed_files_name_what_the_engine_branches_on(self):
        self.assertEqual(ph.protocol.name(-101), "ERR_HOST_INVALID_URI", "mod-host.json's name, not a retyped one")
        for code, name in ((-2, "ERR_INSTANCE_ALREADY_EXISTS"), (-529, "PHD_ERR_DESCRIPTOR_ABSENT"),
                           (-902, "ERR_INVALID_OPERATION")):
            self.assertEqual(ph.protocol.name(code), name)
        self.assertEqual(ph.CHAIN_FALLBACK_CODES, (-501, -519, -520, -521))
        self.assertEqual((ph.DESCRIPTOR_PENDING, ph.DESCRIPTOR_NONE), (-530, (-529, -531)))

    def test_the_engine_types_no_reply_code(self):
        with open(ph.__file__) as f:
            source = f.read()
        self.assertEqual(re.findall(r"(?<![\w.\[])-(?:[1-9]|[1-9]\d\d)\b(?![.\w\]])", source), [],
                         "a reply code in the engine's source is a retyped one")

    def test_without_the_protocol_files_there_is_no_daemon_to_trust(self):
        with self.assertLogs(level=logging.ERROR) as logs:
            empty = ph.zynthian_plugin_hostd_protocol.protocol(os.path.join(self.tmp, "none"), needs=ph.NEEDS)
        self.assertFalse(empty.loaded)
        self.assertEqual(len(logs.records), 1, "said once, when the files are read")
        self.assertIn(os.path.join(self.tmp, "none", "protocol.json"), logs.output[0])
        with mock.patch.object(ph, "protocol", empty):
            with self.assertRaises(ph.plugin_hostd_down) as down:
                ph.daemon.ensure()
        self.assertIn(os.path.join(self.tmp, "none", "protocol.json"), str(down.exception),
                      "the refusal names the file")

    def test_mod_host_json_missing_is_a_failure_too(self):
        directory = self.declared()
        os.remove(os.path.join(directory, "mod-host.json"))
        with self.assertLogs(level=logging.ERROR):
            p = ph.zynthian_plugin_hostd_protocol.protocol(directory)
        self.assertFalse(p.loaded)
        self.assertIn(os.path.join(directory, "mod-host.json"), p.failure)

    def test_a_name_the_engine_branches_on_and_the_files_do_not_declare_is_a_failure(self):
        declared = [{"name": n, "macro": "PHD_ERR_" + n, "code": -700 - i} for i, n in enumerate(ph.NEEDS)
                    if n != "CHAIN_MIXED_FORMAT" and not n.startswith("ERR_")]
        host = [{"name": n, "value": -800 - i} for i, n in enumerate(ph.NEEDS) if n.startswith("ERR_")]
        with self.assertLogs(level=logging.ERROR):
            p = ph.zynthian_plugin_hostd_protocol.protocol(self.declared(declared, host_errors=host), needs=ph.NEEDS)
        self.assertFalse(p.loaded)
        self.assertIn("do not declare CHAIN_MIXED_FORMAT", p.failure)
        with mock.patch.object(ph, "protocol", p):
            with self.assertRaises(ph.plugin_hostd_down) as down:
                ph.daemon.ensure()
        self.assertIn("CHAIN_MIXED_FORMAT", str(down.exception))

    def test_the_engine_reads_the_files_once_at_import(self):
        with mock.patch.object(ph.zynthian_plugin_hostd_protocol, "read", side_effect=AssertionError("read again")):
            self.serve()
            self.add(self.chain(), "PH/Amp")

    @unittest.skipUnless(os.path.isfile("/usr/share/plugin-hostd/protocol.json"), "plugin-hostd is not installed")
    def test_the_fixture_says_what_the_installed_files_say(self):
        installed = ph.zynthian_plugin_hostd_protocol.protocol("/usr/share/plugin-hostd", needs=ph.NEEDS)
        self.assertTrue(installed.loaded, installed.failure)
        for name, code in ph.protocol.codes.items():
            self.assertEqual(installed.codes.get(name), code, f"the fixture retypes {name}")
        readiness = [ph.zynthian_plugin_hostd_protocol.read("protocol.json", d)[0]["readiness"]
                     for d in ("/usr/share/plugin-hostd", os.path.join(FIXTURES, "protocol"))]
        self.assertEqual(readiness[0], readiness[1], "the start script stand-in waits for the installed line")

    def test_ready_is_the_start_scripts_to_say(self):
        """The engine waits for nothing of its own: zynthian-sys's script returns once the declared line is out."""
        with open(ph.__file__) as f:
            source = f.read()
        self.assertNotIn("ready_line", source)
        self.assertNotIn("readiness", source)
        self.assertNotIn("Popen", source, "no daemon is started but by the script")

    def test_a_start_script_that_fails_is_down_with_its_reason(self):
        ph.daemon.start_script = START_STUB
        os.environ["START_STUB_FAIL"] = "plugin-hostd did not print 'plugin-hostd ready!' in 10 s, see the log"
        with self.assertRaises(ph.plugin_hostd_down) as down:
            ph.daemon.ensure()
        self.assertIn("did not print 'plugin-hostd ready!'", str(down.exception))
        self.assertIsNone(ph.daemon.pid)

    def test_without_the_start_script_there_is_no_daemon(self):
        with self.assertRaises(ph.plugin_hostd_down) as down:
            ph.daemon.ensure()
        self.assertIn("not-installed is not installed", str(down.exception))

    def test_a_daemon_the_script_found_running_is_not_stopped_by_the_engine(self):
        """The script prints no PID for a daemon that was already up (a service, or another UI started it)."""
        self.serve()
        stub = os.path.join(self.bin, "already-running")
        with open(stub, "w") as f:
            f.write("#!/bin/sh\necho \"plugin-hostd is already running on port $1\" >&2\n")
        os.chmod(stub, 0o755)
        ph.daemon.start_script = stub
        with mock.patch.object(ph.daemon, "identify", return_value=False):
            ph.daemon.ensure()
        self.assertIsNone(ph.daemon.pid)


class test_processor(plugin_hostd_case):

    def test_add_gives_the_jack_client_of_the_instance(self):
        fake = self.serve()
        chain = self.chain()
        a, b = self.add(chain, "PH/Amp"), self.add(chain, "PH/Amp")
        self.assertEqual((a.jackname, b.jackname), ("effect_1000", "effect_1001"))
        self.assertEqual(a.engine.get_jackname(), "effect_1000")
        self.assertEqual(chain.audio_routes["effect_1001"], ["effect_1000"], "the chain connects it like any engine")
        self.assertEqual(sorted(fake.instances), [1000, 1001])

    def test_a_parameter_goes_out_and_comes_back(self):
        fake = self.serve()
        proc = self.add(self.chain(), "PH/Amp")
        gain = proc.controllers_dict["gain"]
        self.assertEqual((gain.value_min, gain.value_max, gain.name), (-20.0, 20.0, "Gain dB"),
                         "range and unit come from param_info, not from the LV2 port")
        gain.set_value(6.0)
        self.assertEqual(fake.params[1000]["gain"], 6.0)
        fake.params[1000]["gain"] = -3.5
        proc.engine.refresh_values()
        self.assertEqual(gain.value, -3.5)
        proc.controllers_dict["bypass"].set_value(1)
        self.assertEqual(fake.bypass[1000], 1)

    def test_presets_are_listed_and_loaded(self):
        fake = self.serve()
        proc = self.add(self.chain(), "PH/clap:/usr/lib/clap/test.clap#org.test.synth")
        presets = proc.engine.get_preset_list(proc.engine.get_bank_list()[0], proc)
        self.assertEqual([(p[0], p[2]) for p in presets], [("factory:bright", "Bright Lead"), ("factory:warm", "Warm Pad")],
                         "sorted by name")
        self.assertTrue(proc.engine.set_preset(proc, presets[1]))
        self.assertIn("preset_load 1000 factory:warm", fake.lines)
        self.assertEqual(proc.controllers_dict["1"].value, 0.25, "the values are read back after the load")

    def test_an_lv2_plugin_with_no_presets_says_so_and_logs_nothing(self):
        self.serve()
        proc = self.add(self.chain(), "PH/Amp")
        with self.assertLogs(level=logging.WARNING) as logs:
            banks = proc.engine.get_bank_list(proc)
            presets = proc.engine.get_preset_list(banks[0], proc)
            logging.warning("end")
        self.assertEqual([(p[0], p[2]) for p in presets], [("", "No presets")], "one row says so")
        self.assertEqual(len(logs.records), 1, "nothing is logged for a plugin that has no presets")
        self.assertFalse(proc.engine.set_preset(proc, presets[0]), "the row loads nothing")

    def test_a_clap_plugin_whose_worker_lists_no_presets_says_so_and_logs_once(self):
        self.serve()
        proc = self.add(self.chain(), "PH/clap:/usr/lib/clap/lsp-plugins.clap#in.lsp-plug.limiter_mono")
        with self.assertLogs(level=logging.WARNING) as logs:
            self.assertEqual(proc.engine.get_preset_list(None, proc), [["", None, ph.zynthian_engine_plugin_hostd.NO_PRESETS, ""]])
            self.assertEqual(proc.engine.get_preset_list(None, proc), [["", None, ph.zynthian_engine_plugin_hostd.NO_PRESETS, ""]])
            logging.warning("end")
        self.assertEqual(len(logs.records), 2, "PHD_ERR_NO_PRESET_LIST is logged once")
        self.assertIn("PHD_ERR_NO_PRESET_LIST", logs.output[0])

    def lv2_amp_presets(self, my_data):
        """The cache Zynthian keeps for the LV2 Amp: two banks of its bundle and one of the user's, saved under jalv."""
        user = f"file://{my_data}/presets/lv2/Amp.presets.lv2/"
        LV2_PRESETS["Amp"] = {
            "Bundle A": {"bank_url": None, "presets": [{"label": "Loud_Lead", "url": "urn:test:amp#loud"}]},
            "Bundle B": {"bank_url": "urn:test:amp#bank-b", "presets": [{"label": "clean", "url": "urn:test:amp#clean"}]},
            "Mine": {"bank_url": user + "Mine", "presets": [{"label": "Gig", "url": user + "Gig.ttl"}]},
        }
        self.addCleanup(LV2_PRESETS.clear)
        amp = fake_plugins["urn:test:amp"]
        self.addCleanup(amp.__setitem__, "presets", amp["presets"])
        amp["presets"] = [("urn:test:amp#loud", "Loud Lead", {"gain": 6.0}), (user + "Gig.ttl", "Gig", {"gain": -2.0})]
        return user

    def test_an_lv2_and_a_clap_plugin_show_the_same_banks_and_menu_shape(self):
        self.serve()
        with tempfile.TemporaryDirectory() as my_data:
            self.lv2_amp_presets(my_data)
            lv2 = self.add(self.chain(), "PH/Amp")
            clap = self.add(self.chain(2), "PH/clap:/usr/lib/clap/test.clap#org.test.synth")
            for proc in (lv2, clap):
                proc.engine.my_data_dir = my_data
                proc.engine.save_preset(None, "zz Saved")
            menus = {}
            for name, proc in (("lv2", lv2), ("clap", clap)):
                banks = proc.engine.get_bank_list(proc)
                rows = [proc.engine.get_preset_list(bank, proc) for bank in banks]
                menus[name] = ([b[2] for b in banks], [[(type(r[0]), r[1], r[3]) for r in bank] for bank in rows],
                               [[r[2] for r in bank] for bank in rows])
                self.assertTrue(proc.auto_save_bank, "the save screen asks for no bank")
            self.assertEqual(menus["lv2"][0], ["Factory", "User"])
            self.assertEqual(menus["lv2"][0], menus["clap"][0], "the same bank names for both formats")
            self.assertEqual([len(b) for b in menus["lv2"][1]], [2, 2])
            self.assertEqual([len(b) for b in menus["clap"][1]], [2, 1])
            self.assertEqual(menus["lv2"][1][0][0], menus["clap"][1][0][0], "a row has the same shape for both formats")
            self.assertEqual(menus["lv2"][2], [["clean", "Loud Lead"], ["Gig", "zz Saved"]],
                             "plain names, sorted by name; the bundle's banks are one Factory, jalv's and ours one User")
            self.assertEqual(menus["clap"][2], [["Bright Lead", "Warm Pad"], ["zz Saved"]], "sorted the same way")

    def test_an_lv2_preset_loads_by_its_uri_whichever_bank_it_is_in(self):
        fake = self.serve()
        with tempfile.TemporaryDirectory() as my_data:
            user = self.lv2_amp_presets(my_data)
            proc = self.add(self.chain(), "PH/Amp")
            proc.engine.my_data_dir = my_data
            factory, mine = (proc.engine.get_preset_list(b, proc) for b in proc.engine.get_bank_list(proc))
            self.assertTrue(proc.engine.set_preset(proc, factory[1]))
            self.assertIn("preset_load 1000 urn:test:amp#loud", fake.lines)
            self.assertEqual(proc.controllers_dict["gain"].value, 6.0, "the values are read back after the load")
            self.assertEqual(mine[0][0], user + "Gig.ttl")
            self.assertTrue(proc.engine.is_preset_user(mine[0]) and not proc.engine.is_preset_user(factory[0]))
            self.assertTrue(proc.engine.set_preset(proc, mine[0]))
            self.assertEqual(proc.controllers_dict["gain"].value, -2.0)
            self.assertNotIn("preset_list 1000", fake.lines, "an LV2 plugin's presets are not asked of the worker")

    def test_a_user_preset_is_saved_through_the_worker_and_loaded_renamed_and_deleted(self):
        fake = self.serve()
        with tempfile.TemporaryDirectory() as my_data:
            proc = self.add(self.chain(), "PH/clap:/usr/lib/clap/test.clap#org.test.synth")
            engine = proc.engine
            engine.my_data_dir = my_data
            user = engine.get_bank_list(proc)[1]
            self.assertEqual([r[2] for r in engine.get_preset_list(user, proc)], [ph.zynthian_engine_plugin_hostd.NO_PRESETS])
            proc.controllers_dict["1"].set_value(0.375)
            self.assertFalse(engine.preset_exists(user, "Gig Pad"))
            path = engine.save_preset(None, "Gig Pad")
            self.assertTrue(path and path.startswith(engine.user_dir() + os.sep) and " " not in path, path)
            self.assertIn(f'preset_save 1000 {path} "Gig Pad"', fake.lines, "saved by the worker, the name quoted")
            self.assertTrue(engine.preset_exists(user, "Gig Pad"))
            self.assertEqual(proc.bank_info[0], "user", "the processor is in User after a save")
            presets = engine.get_preset_list(user, proc)
            self.assertEqual([(p[0], p[2]) for p in presets], [(path, "Gig Pad")])
            self.assertTrue(engine.is_preset_user(presets[0]))
            proc.controllers_dict["1"].set_value(0.9)
            self.assertTrue(engine.set_preset(proc, presets[0]))
            self.assertIn(f"preset_load 1000 {path}", fake.lines, "loaded by its path")
            self.assertEqual(proc.controllers_dict["1"].value, 0.375, "the saved value is back")
            engine.rename_preset(user, presets[0], "Gig Pad 2")
            self.assertEqual([p[2] for p in engine.get_preset_list(user, proc)], ["Gig Pad 2"])
            self.assertEqual(engine.delete_preset(user, engine.get_preset_list(user, proc)[0]), 0)
            self.assertFalse(os.path.exists(path))
            self.assertFalse(hasattr(engine, "create_user_bank"), "no bank is created: there are two")

    def test_remove_takes_the_instance_out_of_the_daemon(self):
        fake = self.serve()
        chain = self.chain()
        a, b = self.add(chain, "PH/Amp"), self.add(chain, "PH/Amp")
        self.remove(chain, a)
        self.assertEqual(sorted(fake.instances), [1001])
        self.assertIsNone(a.jackname)

    def test_a_processor_moved_to_another_chain_keeps_its_instance(self):
        fake = self.serve()
        one, two = self.chain(1), self.chain(2)
        proc = self.add(one, "PH/Amp")
        proc.controllers_dict["gain"].set_value(5.0)
        one.remove_processor(proc)
        two.insert_processor(proc)
        one.rebuild_audio_graph()
        two.rebuild_audio_graph()
        self.assertEqual(proc.jackname, "effect_1000")
        self.assertNotIn("remove 1000", fake.lines)
        self.assertEqual(fake.params[1000]["gain"], 5.0)


class test_chain(plugin_hostd_case):

    def test_insert_reorder_and_remove_declare_the_chain(self):
        fake = self.serve()
        chain = self.chain()
        a, b, c = (self.add(chain, "PH/Amp") for _ in range(3))
        self.assertIn("chain 1 1000 1001", fake.lines)
        self.assertEqual(fake.chains[1], (1000, 1001, 1002))
        sent = fake.lines.count("chain 1 1000 1001 1002")
        chain.rebuild_graph()
        self.assertEqual(fake.lines.count("chain 1 1000 1001 1002"), sent, "an unchanged chain is not sent again")
        chain.nudge_processor(c, True)  # into the slot of b, in parallel: same order
        chain.nudge_processor(c, True)  # out of it, before b
        self.assertEqual(fake.chains[1], (1000, 1002, 1001))
        self.remove(chain, b)
        self.assertEqual(fake.chains[1], (1000, 1002))
        self.assertLess(fake.lines.index("chain 1 1000 1002"), fake.lines.index("remove 1001"))
        self.remove(chain, c)
        self.assertNotIn(1, fake.chains, "a chain of one member is cleared")
        self.assertIn("chain_clear 1", fake.lines)

    def test_a_refused_chain_falls_back_to_separate_workers_and_logs_once(self):
        fake = self.serve()
        chain = self.chain()
        self.add(chain, "PH/Amp")
        self.add(chain, "PH/Amp")
        self.assertEqual(fake.chains[1], (1000, 1001))
        with self.assertLogs(level=logging.WARNING) as logs:
            self.add(chain, "PH/NoPin")
            chain.rebuild_graph()
            self.add(chain, "PH/Amp")
            logging.warning("end")
        refused = [r for r in logs.output if "separate workers" in r]
        self.assertEqual(len(refused), 1)
        self.assertIn("PHD_ERR_PLACEMENT_INVALID", refused[0])
        self.assertNotIn(1, fake.chains, "the chain it had is cleared, each member back in a worker of its own")
        self.assertEqual(fake.lines.count("chain 1 1000 1001 1002"), 1, "refused members are not sent again")

    def test_each_fallback_code_falls_back_and_another_refusal_does_not(self):
        fake = self.serve()
        chain = self.chain()
        self.add(chain, "PH/Amp")
        for code in (-501, -519, -520, -521, -518):
            ph.daemon.chain_refused.clear()
            ph.daemon.chains[1] = (1000, 9999)
            fake.chains[1] = (1000,)
            fake.verb_chain = lambda *args, code=code: f"resp {code}"
            self.add(chain, "PH/Amp") if code == -501 else chain.rebuild_graph()
            fell_back = code in ph.CHAIN_FALLBACK_CODES
            self.assertEqual(1 not in fake.chains, fell_back, f"code {code}")
            self.assertEqual(1 in ph.daemon.chain_refused, fell_back, f"code {code}")


class test_chain_jack_names(plugin_hostd_case):
    """The members of a CLAP chain are one jack client; the processors name the client the daemon says."""

    CLAP = "PH/clap:/usr/lib/clap/lsp-plugins.clap#in.lsp-plug.limiter_mono"

    def test_the_daemons_replies_name_the_chains_client(self):
        # The replies of the issue: three CLAP members added, chain 1 0 1 2, read back
        fake = self.serve()
        fake.verb_chain_get = lambda chain: "resp 0 w4 0,1,2 0,1,2"
        fake.verb_worker_list = lambda: "resp 1 w4:111797:clap:up:chain:1:0,1,2"
        ph.daemon.call("worker_list")
        ph.daemon.engines = {i: types.SimpleNamespace(jackname=f"effect_{i}") for i in (0, 1, 2)}
        self.addCleanup(ph.daemon.engines.clear)
        self.assertEqual(ph.chain_clients(1), {0: "effect_0", 1: "effect_0", 2: "effect_0"})

    def test_a_chain_of_clap_members_is_one_client_named_for_its_head(self):
        fake = self.serve()
        chain = self.chain()
        procs = [self.add(chain, self.CLAP) for _ in range(3)]
        self.assertEqual(fake.chains[1], (1000, 1001, 1002))
        self.assertEqual([p.jackname for p in procs], ["effect_1000"] * 3)
        self.assertEqual([p.engine.jackname for p in procs], ["effect_1000", "effect_1001", "effect_1002"],
                         "each engine still names its own instance")
        # Every connect is made on ports of the one client that exists: nothing is routed to or from
        # effect_1001 or effect_1002, and the client is not routed to itself
        clients = {name for dst, src in chain.audio_routes.items() for name in [dst] + src}
        self.assertNotIn("effect_1001", clients)
        self.assertNotIn("effect_1002", clients)
        self.assertEqual(chain.audio_routes["effect_1000"], [], "the head is fed by the chain's input")
        for out in chain.get_audio_out():
            self.assertEqual(chain.audio_routes[out], ["effect_1000"], "the chain's output is the client's")

    def test_a_mixed_lv2_and_clap_chain_is_one_client_named_for_its_head(self):
        fake = self.serve()
        chain = self.chain()
        a, b = self.add(chain, "PH/Amp"), self.add(chain, self.CLAP)
        self.assertEqual(fake.chains[1], (1000, 1001))
        self.assertIn(":lv2-clap:up:chain:1:1000,1001", fake.verb_worker_list())
        self.assertEqual((a.jackname, b.jackname), ("effect_1000", "effect_1000"))
        self.assertNotIn("effect_1001", {name for dst, src in chain.audio_routes.items() for name in [dst] + src})
        for out in chain.get_audio_out():
            self.assertEqual(chain.audio_routes[out], ["effect_1000"])

    def test_a_chain_of_lv2_members_keeps_a_client_per_member(self):
        self.serve()
        chain = self.chain()
        a, b = self.add(chain, "PH/Amp"), self.add(chain, "PH/Amp")
        self.assertEqual((a.jackname, b.jackname), ("effect_1000", "effect_1001"))

    def test_a_member_the_worker_does_not_run_keeps_its_own_client(self):
        fake = self.serve()
        chain_get = fake.verb_chain_get
        # 1001 is quarantined: declared, not running
        fake.verb_chain_get = lambda c: re.sub(r"(\S+) (\S+) (\S+)$", r"\1 \2 1000,1002", chain_get(c))
        chain = self.chain()
        procs = [self.add(chain, self.CLAP) for _ in range(3)]
        self.assertEqual([p.jackname for p in procs], ["effect_1000", "effect_1001", "effect_1000"])

    def test_a_member_taken_out_goes_back_to_its_own_client_and_the_head_moves(self):
        self.serve()
        chain = self.chain()
        a, b, c = (self.add(chain, self.CLAP) for _ in range(3))
        self.remove(chain, a)
        self.assertEqual((b.jackname, c.jackname), ("effect_1001", "effect_1001"), "the head is now 1001")
        self.remove(chain, c)
        self.assertEqual(b.jackname, "effect_1001")
        self.assertNotIn(1, ph.daemon.clients)

    def test_a_refused_chain_gives_every_member_its_own_client(self):
        self.serve()
        chain = self.chain()
        a, b = self.add(chain, self.CLAP), self.add(chain, self.CLAP)
        self.assertEqual((a.jackname, b.jackname), ("effect_1000", "effect_1000"))
        c = self.add(chain, "PH/NoPin")  # -501: the daemon will not run the three together
        self.assertEqual([p.jackname for p in (a, b, c)], ["effect_1000", "effect_1001", "effect_1002"])

    def test_a_restored_daemon_names_the_clients_again(self):
        fake = self.serve()
        chain = self.chain()
        procs = [self.add(chain, self.CLAP) for _ in range(2)]
        ph.daemon.client.close()
        fake.stop()
        fake2 = self.serve()
        ph.daemon.call("worker_list")
        self.assertEqual(fake2.chains[1], (1000, 1001))
        self.assertEqual([p.jackname for p in procs], ["effect_1000", "effect_1000"])


class test_plan(plugin_hostd_case):
    """sync_chain hands the chain to the daemon's planner and takes its segments."""

    CLAP = "PH/clap:/usr/lib/clap/lsp-plugins.clap#in.lsp-plug.limiter_mono"
    CLAP_URI = "clap:/usr/lib/clap/lsp-plugins.clap#in.lsp-plug.limiter_mono"
    ISOLATED = {"urn:test:amp": "isolated"}  # the Amp is a plugin the pin says to isolate

    def five(self, fake, order=("clap", "clap", "amp", "clap", "clap")):
        chain = self.chain()
        return chain, [self.add(chain, self.CLAP if kind == "clap" else "PH/Amp") for kind in order]

    def test_the_segments_are_the_daemons_and_a_lone_member_stays_its_own_client(self):
        # plan 1 1000 1001 1002 1003 1004 answered resp 0 1000,1001;1002;1003,1004
        fake = self.serve(planner=True, grouping=True, verdicts=self.ISOLATED)
        chain, procs = self.five(fake)
        self.assertEqual(fake.lines.count("plan 1 1000 1001 1002 1003 1004"), 1)
        self.assertEqual(fake.plans[1][0], [[1000, 1001], [1002], [1003, 1004]])
        self.assertNotIn("chain 1 1000 1001 1002 1003 1004", fake.lines, "the chain verb is not sent over a plan")
        self.assertEqual([p.jackname for p in procs],
                         ["effect_1000", "effect_1000", "effect_1002", "effect_1003", "effect_1003"])
        self.assertEqual([p.engine.jackname for p in procs], [f"effect_{i}" for i in range(1000, 1005)],
                         "each engine still names its own instance")
        clients = {name for dst, src in chain.audio_routes.items() for name in [dst] + src}
        self.assertEqual(clients & {f"effect_{i}" for i in (1001, 1004)}, set(), "no connect to a client that is not there")
        workers = fake.verb_worker_list()
        self.assertIn(":clap:up:chain:1.0:1000,1001", workers)
        self.assertIn(":clap:up:chain:1.2:1003,1004", workers)
        self.assertNotIn("chain:1.1:", workers, "a lone segment has no worker of its own to name")

    def test_a_segment_worker_of_the_lv2_row_keeps_a_client_per_member(self):
        fake = self.serve(planner=True, grouping=True)
        chain = self.chain()
        a, b = self.add(chain, "PH/Amp"), self.add(chain, "PH/Amp")
        self.assertIn(":lv2:up:chain:1.0:1000,1001", fake.verb_worker_list())
        self.assertEqual((a.jackname, b.jackname), ("effect_1000", "effect_1001"))

    def test_the_reason_of_every_member_left_alone_is_what_plan_get_says(self):
        fake = self.serve(planner=True, grouping=True, verdicts=self.ISOLATED)
        chain, procs = self.five(fake, ("clap", "amp", "clap", "clap", "amp"))
        self.assertEqual(fake.verb_plan_get(1), "resp 0 1000;1001;1002,1003;1004 1000:no-neighbour,1001:verdict-isolated,"
                                                "1004:verdict-isolated")
        self.assertEqual([p.engine.grouping_reason() for p in procs],
                         ["no-neighbour", "verdict-isolated", None, None, "verdict-isolated"])
        self.assertEqual([p.engine.grouping_text() for p in procs],
                         ["Alone: no neighbour it can share a worker with",
                          "Not grouped: it was qualified to run isolated", None, None,
                          "Not grouped: it was qualified to run isolated"])

    def test_a_member_leaving_replans_and_the_reasons_follow(self):
        fake = self.serve(planner=True, grouping=True, verdicts=self.ISOLATED)
        chain, procs = self.five(fake)
        self.remove(chain, procs[2])  # the isolated one
        self.assertEqual(fake.plans[1][0], [[1000, 1001, 1003, 1004]])
        self.assertEqual([p.jackname for p in procs if p is not procs[2]], ["effect_1000"] * 4)
        self.assertIsNone(ph.daemon.reasons.get(1002), "the reason of a removed instance is forgotten")
        self.assertEqual(ph.daemon.reasons, {})
        self.remove(chain, procs[1])
        self.remove(chain, procs[0])
        self.remove(chain, procs[3])
        self.assertNotIn(1, fake.plans, "a chain left with one member is cleared")
        self.assertIn("plan_clear 1", fake.lines)
        self.assertEqual(procs[4].jackname, "effect_1004")

    def test_an_unchanged_chain_is_not_planned_again(self):
        fake = self.serve(planner=True, grouping=True)
        chain, procs = self.five(fake, ("clap",) * 3)
        sent = fake.lines.count("plan 1 1000 1001 1002")
        chain.rebuild_graph()
        self.assertEqual(fake.lines.count("plan 1 1000 1001 1002"), sent)
        self.assertEqual(sent, 1)

    def test_a_member_the_worker_does_not_run_keeps_its_own_client(self):
        fake = self.serve(planner=True, grouping=True)
        worker_list = fake.verb_worker_list
        # 1001 is quarantined: it has a worker of its own and the segment's worker no longer runs it
        fake.verb_worker_list = lambda: worker_list().replace("1000,1001,1002", "1000,1002") \
            .replace("resp 1", "resp 2") + " w1:1:clap:up:own:1001"
        chain, procs = self.five(fake, ("clap",) * 3)
        self.assertEqual([p.jackname for p in procs], ["effect_1000", "effect_1001", "effect_1000"])

    def test_a_member_the_daemon_lists_in_the_segment_and_in_a_worker_of_its_own_keeps_its_own_client(self):
        fake = self.serve(planner=True, grouping=True)
        worker_list = fake.verb_worker_list
        fake.verb_worker_list = lambda: worker_list().replace("resp 1", "resp 2") + " w1:1:clap:up:own:1001"
        chain, procs = self.five(fake, ("clap",) * 3)
        self.assertEqual([p.jackname for p in procs], ["effect_1000", "effect_1001", "effect_1000"])

    def test_a_daemon_that_does_not_know_plan_gets_the_whole_chain_and_one_log_line(self):
        fake = self.serve(planner=False)
        with self.assertLogs(level=logging.WARNING) as logs:
            chain, procs = self.five(fake, ("clap",) * 3)
            chain.rebuild_graph()
            logging.warning("end")
        said = [r for r in logs.output if "does not answer plan" in r]
        self.assertEqual(len(said), 1, "the fallback is told once, not at every rebuild")
        self.assertEqual(fake.chains[1], (1000, 1001, 1002))
        self.assertEqual([p.jackname for p in procs], ["effect_1000"] * 3)
        self.assertEqual(fake.lines.count("plan 1 1000 1001"), 1, "the plan is asked once of such a daemon")
        self.assertEqual(ph.daemon.reasons, {})
        self.assertFalse(ph.daemon.plan_verbs)

    def test_a_refusal_of_chain_still_falls_back_with_the_old_daemon(self):
        fake = self.serve(planner=False)
        chain = self.chain()
        self.add(chain, "PH/Amp")
        self.add(chain, "PH/Amp")
        self.add(chain, "PH/NoPin")
        self.assertNotIn(1, fake.chains)
        self.assertIn(1, ph.daemon.chain_refused)

    def test_with_grouping_off_the_chain_is_declared_whole_as_before(self):
        fake = self.serve(planner=True, grouping=False, verdicts=self.ISOLATED)
        chain, procs = self.five(fake, ("clap",) * 3)
        self.assertEqual(fake.chains[1], (1000, 1001, 1002))
        self.assertEqual(fake.lines.count("plan 1 1000 1001"), 1)
        self.assertEqual(fake.lines.count("plan_clear 1"), 1, "the answered plan is not left kept")
        self.assertEqual(fake.lines.count("plan_get 1"), 1)
        self.assertEqual([p.jackname for p in procs], ["effect_1000"] * 3)
        self.assertEqual(ph.daemon.reasons, {}, "nothing is said of plugins that run together")
        self.assertIs(ph.daemon.grouping, False)
        sent = len(fake.lines)
        self.add(chain, "PH/Amp")
        self.assertEqual([l for l in fake.lines[sent:] if l.startswith("plan")], [],
                         "once grouping is known to be off the plan is not asked again")
        self.assertEqual(fake.chains[1], (1000, 1001, 1002, 1003))

    def test_a_refused_plan_leaves_every_member_alone_with_the_refusal_as_the_reason(self):
        fake = self.serve(planner=True, grouping=True)
        fake.verb_plan = lambda *args: "resp -501"
        with self.assertLogs(level=logging.WARNING) as logs:
            chain, procs = self.five(fake, ("clap",) * 3)
            logging.warning("end")
        self.assertEqual([p.jackname for p in procs], ["effect_1000", "effect_1001", "effect_1002"])
        self.assertEqual([p.engine.grouping_reason() for p in procs], ["plan-refused:-501"] * 3)
        self.assertEqual(procs[0].engine.grouping_text(), "Not grouped: the daemon refused the plan (PHD_ERR_PLACEMENT_INVALID)")
        self.assertEqual(len([r for r in logs.output if "every member runs alone" in r]), 2, "one per change of members")

    def test_a_malformed_reply_is_logged_and_leaves_the_members_as_they_were(self):
        fake = self.serve(planner=True, grouping=True)
        fake.verb_plan = lambda *args: "resp 0 x;y"
        with self.assertLogs(level=logging.ERROR):
            chain, procs = self.five(fake, ("clap",) * 2)
        self.assertEqual([p.jackname for p in procs], ["effect_1000", "effect_1001"])

    def test_a_restored_daemon_is_planned_again(self):
        fake = self.serve(planner=True, grouping=True, verdicts=self.ISOLATED)
        chain, procs = self.five(fake)
        ph.daemon.client.close()
        fake.stop()
        fake2 = self.serve(planner=True, grouping=True, verdicts=self.ISOLATED)
        ph.daemon.call("worker_list")
        self.assertEqual(fake2.plans[1][0], [[1000, 1001], [1002], [1003, 1004]])
        self.assertEqual([p.jackname for p in procs],
                         ["effect_1000", "effect_1000", "effect_1002", "effect_1003", "effect_1003"])
        self.assertEqual(procs[2].engine.grouping_reason(), "verdict-isolated")


class test_grouping_texts(unittest.TestCase):

    def test_every_word_the_daemon_declares_has_a_text(self):
        declared = grouping.declared_words(FIXTURES)
        self.assertEqual(len(declared), 13)
        self.assertEqual(declared - {grouping.PLAN_REFUSED}, set(grouping.TEXTS))
        for word in declared - {grouping.PLAN_REFUSED}:
            self.assertEqual(grouping.text(word), grouping.TEXTS[word])
            self.assertLess(len(grouping.text(word)), 64, word)

    def test_the_text_of_a_refusal_names_the_code_by_the_daemons_name(self):
        self.assertEqual(grouping.text("plan-refused:-518", ph.protocol.name),
                         "Not grouped: the daemon refused the plan (PHD_ERR_CHAIN_UNKNOWN_INSTANCE)")
        self.assertEqual(grouping.text("plan-refused:-1234", ph.protocol.name),
                         "Not grouped: the daemon refused the plan (ERR_1234)")
        self.assertEqual(grouping.text("plan-refused:x"), "Not grouped: the daemon refused the plan (x)")

    def test_a_word_it_has_no_text_for_reads_as_the_word(self):
        self.assertEqual(grouping.text("brand-new"), "Not grouped: brand-new")

    def test_an_unreadable_declaration_is_none(self):
        self.assertIsNone(grouping.declared_words("/nonexistent"))


class test_pages(plugin_hostd_case):

    def big(self):
        return self.serve(descriptors={"urn:test:big": os.path.join(FIXTURES, "big.descriptor.json")})

    def test_more_than_eight_controls_split_in_pages_of_eight(self):
        os.symlink(os.path.join(FIXTURES, "omx-panel"), os.path.join(self.bin, "omx-panel"))
        runs = os.path.join(self.tmp, "omx-panel.log")
        os.environ["OMX_PANEL_STUB_LOG"] = runs
        self.big()
        proc = self.add(self.chain(), "PH/Big")
        self.assertEqual(proc.engine._ctrl_screens, [
            ["Main 1: Freq 0 Freq 1 Freq 2 Freq 3", ["p0", "p1", "p2", "p3"]],
            ["Main 1: Freq 4 Freq 5 Freq 6 Freq 7", ["p4", "p5", "p6", "p7"]], ["Main 2", ["p8", "p9"]]])
        self.assertEqual([z.symbol for z in proc.get_ctrl_screen("Main 2")], ["p8", "p9"])
        p0 = proc.controllers_dict["p0"]
        self.assertEqual((p0.name, p0.value_min, p0.value_max, p0.group_name), ("Freq 0 Hz", 0, 100, "Main 1"))
        self.assertNotIn("gr", proc.controllers_dict, "a meter is not a control")
        self.add(self.chain(2), "PH/Big")
        with open(runs) as f:
            self.assertEqual(len(f.readlines()), 2, "the layout is cached per descriptor")
        os.utime(os.path.join(FIXTURES, "big.descriptor.json"))
        self.add(self.chain(3), "PH/Big")
        with open(runs) as f:
            self.assertEqual(len(f.readlines()), 4, "a changed descriptor is laid out again")

    def test_a_clap_parameter_is_addressed_by_its_number_not_by_the_layouts_name(self):
        # omx-panel names a CLAP parameter p<number> in its layout; plugin-hostd knows it by the number alone
        os.symlink(os.path.join(FIXTURES, "omx-panel"), os.path.join(self.bin, "omx-panel"))
        uri = "clap:/usr/lib/clap/test.clap#org.test.synth"
        fake = self.serve(descriptors={uri: os.path.join(FIXTURES, "synth.descriptor.json")})
        proc = self.add(self.chain(), "PH/" + uri)
        self.assertEqual([z.symbol for z in proc.get_ctrl_screen("Controls")], ["1", "2"])
        proc.controllers_dict["2"].set_value(3)
        self.assertEqual(fake.params[1000]["2"], 3.0, "the value reaches the plugin")

    def test_a_group_of_five_controls_names_its_screens_by_their_content(self):
        with open(os.path.join(FIXTURES, "big.descriptor.json")) as f:
            descriptor = json.load(f)
        labels = {"p0": "Freq", "p1": "Gain", "p2": "Q", "p3": "Type", "p4": "On"}
        for c in descriptor["controls"]:
            c["group"] = ["Band 1"] if c["id"] in labels else ["Filter"]
            c["name"] = labels.get(c["id"], c["name"])
        path = os.path.join(self.tmp, "band.descriptor.json")
        with open(path, "w") as f:
            json.dump(descriptor, f)
        os.symlink(os.path.join(FIXTURES, "omx-panel"), os.path.join(self.bin, "omx-panel"))
        self.serve(descriptors={"urn:test:big": path})
        proc = self.add(self.chain(), "PH/Big")
        self.assertEqual(proc.engine._ctrl_screens, [
            ["Band 1: Freq Gain Q Type", ["p0", "p1", "p2", "p3"]], ["Band 1: On", ["p4"]],
            ["Filter: Freq 5 Freq 6 Freq 7 Freq 8", ["p5", "p6", "p7", "p8"]], ["Filter: Freq 9", ["p9"]]])

    def test_without_omx_panel_the_pages_follow_the_parameter_order(self):
        self.big()
        with self.assertLogs(level=logging.WARNING) as logs:
            proc = self.add(self.chain(), "PH/Big")
            self.add(self.chain(2), "PH/Big")
            logging.warning("end")
        self.assertEqual([s[0] for s in proc.engine._ctrl_screens], [
            "Page 1: Freq 0 Freq 1 Freq 2 Freq 3", "Page 1: Freq 4 Freq 5 Freq 6 Freq 7", "Page 2"])
        self.assertEqual(proc.engine._ctrl_screens[2][1], ["p8", "p9"])
        self.assertEqual(len([r for r in logs.output if "omx-panel" in r]), 1)

    SYNTH = "PH/clap:/usr/lib/clap/test.clap#org.test.synth"
    SYNTH_URI = "clap:/usr/lib/clap/test.clap#org.test.synth"

    def probe_stub(self, fail=False):
        os.symlink(os.path.join(FIXTURES, "plugin-hostd-probe"), os.path.join(self.bin, "plugin-hostd-probe"))
        log = os.path.join(self.tmp, "probe.log")
        os.environ["PROBE_STUB_LOG"] = log
        os.environ["PROBE_STUB_DESCRIPTOR"] = os.path.join(FIXTURES, "synth.descriptor.json")
        if fail:
            os.environ["PROBE_STUB_FAIL"] = "1"
        return log

    def test_without_a_descriptor_the_catalogue_names_the_parameters_and_says_nothing_of_steps(self):
        self.serve()
        proc = self.add(self.chain(), self.SYNTH)
        self.assertEqual([s[1] for s in proc.engine._ctrl_screens], [["1", "2"]])
        wave = proc.controllers_dict["2"]
        self.assertEqual((wave.name, wave.value_max, wave.is_integer), ("Wave", 3.0, False),
                         "stepped is the daemon's to say, not the engine's guess from the scan")

    def test_a_stepped_parameter_comes_from_the_daemons_descriptor(self):
        self.serve(descriptors={self.SYNTH_URI: os.path.join(FIXTURES, "synth.descriptor.json")})
        proc = self.add(self.chain(), self.SYNTH)
        wave = proc.controllers_dict["2"]
        self.assertEqual((wave.name, wave.is_integer, wave.value_max), ("Wave", True, 3))
        self.assertFalse(proc.controllers_dict["1"].is_integer)

    def test_a_stepped_parameter_comes_from_param_info_when_the_daemon_answers_it(self):
        plugins = {**fake_plugins, self.SYNTH_URI: {**fake_plugins[self.SYNTH_URI], "info": True}}
        with mock.patch.dict(fake_plugins, plugins):
            self.serve()
            proc = self.add(self.chain(), self.SYNTH)
        self.assertTrue(proc.controllers_dict["2"].is_integer)

    def probed(self, descriptors):
        log = self.probe_stub()
        self.serve(descriptors=descriptors)
        proc = self.add(self.chain(), self.SYNTH)
        with open(log) as f:
            self.assertEqual(f.read().split(), ["describe", self.SYNTH_URI])
        self.assertEqual((proc.controllers_dict["2"].name, proc.controllers_dict["2"].is_integer), ("Wave", True))

    def test_the_probe_describes_a_plugin_the_daemon_has_no_descriptor_for(self):
        self.probed({})  # -529

    def test_the_probe_describes_a_plugin_whose_scan_failed(self):
        self.probed({self.SYNTH_URI: None})  # -531

    def test_the_probe_runs_once_per_plugin(self):
        log = self.probe_stub()
        self.serve()
        self.add(self.chain(), self.SYNTH)
        self.add(self.chain(2), self.SYNTH)
        with open(log) as f:
            self.assertEqual(len(f.readlines()), 1)

    def test_a_probe_that_refuses_leaves_the_catalogue_parameters_and_says_so_once(self):
        self.probe_stub(fail=True)
        self.serve()
        with self.assertLogs(level=logging.WARNING) as logs:
            proc = self.add(self.chain(), self.SYNTH)
            self.add(self.chain(2), self.SYNTH)
        self.assertFalse(proc.controllers_dict["2"].is_integer)
        self.assertEqual(len([r for r in logs.output if "the probe made none" in r]), 1)

    def test_a_pending_scan_is_waited_for_not_probed(self):
        log = self.probe_stub()
        fake = self.serve(descriptors={self.SYNTH_URI: os.path.join(FIXTURES, "synth.descriptor.json")})
        answers = iter(["resp -530", "resp -530"])
        real = fake.verb_descriptor_get
        fake.verb_descriptor_get = lambda uri: next(answers, None) or real(uri)
        proc = self.add(self.chain(), self.SYNTH)
        self.assertTrue(proc.controllers_dict["2"].is_integer)
        self.assertFalse(os.path.exists(log), "-530 is ask again, not a missing descriptor")


class test_snapshot(plugin_hostd_case):

    def test_state_is_saved_and_loaded_into_a_new_instance(self):
        fake = self.serve()
        chain = self.chain()
        proc = self.add(chain, "PH/Amp")
        proc.controllers_dict["gain"].set_value(3.0)
        xconfig = json.loads(json.dumps(proc.engine.get_extended_config()))
        self.assertEqual(xconfig["plugin"], "urn:test:amp")
        fake.params[1000]["gain"] = 0.0
        again = self.add(chain, "PH/Amp", xconfig=xconfig)
        self.assertEqual(again.jackname, "effect_1001")
        self.assertEqual(fake.params[1001]["gain"], 3.0, "the saved state lands on the new instance number")
        self.assertEqual(fake.params[1000]["gain"], 0.0, "and on no other")
        self.assertEqual(again.controllers_dict["gain"].value, 3.0)

    def test_without_state_save_the_snapshot_keeps_parameters_only(self):
        fake = self.serve()
        fake.verb_state_save = lambda directory: "resp -902"
        proc = self.add(self.chain(), "PH/Amp")
        with self.assertLogs(level=logging.WARNING) as logs:
            self.assertIsNone(proc.engine.get_extended_config())
            self.assertIsNone(proc.engine.get_extended_config())
            logging.warning("end")
        self.assertEqual(len(logs.output), 2)
        self.assertIn("parameters and presets only", logs.output[0])
        self.assertEqual(proc.get_state()["controllers"]["gain"]["value"], 0.0)


class test_catalogue(plugin_hostd_case):

    def test_clap_files_that_share_a_plugin_id_are_separate_entries(self):
        # DPF-built files report one id for several files; the pin URI names the file too
        scan = "".join(f'file /usr/lib/clap/{f}.clap\n' + CLAP_SCAN_MVERB for f in ("3BandEQ", "3BandSplitter"))
        added = cat.add_catalogue({}, clap=cat.parse_clap_scan(scan), binary=FAKE)
        self.assertEqual(sorted(added), ["PH/clap:/usr/lib/clap/3BandEQ.clap#studio.kx.distrho.MVerb",
                                         "PH/clap:/usr/lib/clap/3BandSplitter.clap#studio.kx.distrho.MVerb"])
        self.assertEqual(len({info["ID"] for info in added.values()}), 2)
        for key, info in added.items():
            self.assertEqual(key, "PH/" + info["URL"])
            self.assertEqual(cat.clap_plugins[key]["path"], info["URL"].split("#")[0][5:])

    def test_titles_say_the_format_and_one_clap_id_in_two_directories_is_listed_once(self):
        surge = CLAP_SCAN_MVERB.replace("studio.kx.distrho.MVerb", "org.surge-synth-team.surge-xt")
        scan = "file /root/.clap/SurgeXT.clap\n" + surge + "file /usr/local/lib/clap/Surge XT.clap\n" + surge
        added = cat.add_catalogue({"JV/Surge": lv2_info("Surge", "urn:surge")}, clap=cat.parse_clap_scan(scan),
                                  binary=FAKE)
        self.assertEqual(sorted(added), ["PH/Surge", "PH/clap:/root/.clap/SurgeXT.clap#org.surge-synth-team.surge-xt"])
        titles = sorted(info["TITLE"] for info in added.values())
        self.assertEqual(len(titles), len(set(titles)))
        self.assertTrue(added["PH/Surge"]["TITLE"].endswith("(LV2 PH)"))
        self.assertTrue(added["PH/clap:/root/.clap/SurgeXT.clap#org.surge-synth-team.surge-xt"]["TITLE"]
                        .endswith("(CLAP PH)"))

    def test_clap_scan_text_is_parsed(self):
        plugins = cat.parse_clap_scan(CLAP_SCAN)
        self.assertEqual([p["id"] for p in plugins], ["in.lsp-plug.limiter_mono", "org.test.synth", "org.test.arp"])
        synth = plugins[1]
        self.assertEqual((synth["name"], synth["vendor"], synth["path"]),
                         ('Test "Pad" Synth', "Test Vendor Inc", "/usr/lib/clap/test.clap"))
        self.assertEqual(synth["params"][1], {"id": "2", "name": "Wave", "min": 0.0, "max": 3.0, "default": 0.0,
                                              "stepped": True})

    def test_instruments_and_effects_enter_the_catalogue(self):
        engines = {"JV/ZamComp": lv2_info("ZamComp", "urn:zamaudio:ZamComp"),
                   "JV/Arp": lv2_info("Arp", "urn:test:arp", "MIDI Tool"),
                   "PH/clap:org.test.gone": lv2_info("Gone", "clap:/gone.clap#org.test.gone")}
        _lv2.engines_by_type["Audio Effect"]["PH/clap:org.test.gone"] = engines["PH/clap:org.test.gone"]
        added = cat.add_catalogue(engines, clap=cat.parse_clap_scan(CLAP_SCAN), binary=FAKE)
        self.assertNotIn("PH/clap:org.test.gone", engines, "a plugin no longer installed leaves the catalogue")
        self.assertNotIn("PH/clap:org.test.gone", _lv2.engines_by_type["Audio Effect"])
        self.assertEqual(sorted(added), ["PH/ZamComp", "PH/clap:/usr/lib/clap/lsp-plugins.clap#in.lsp-plug.limiter_mono", "PH/clap:/usr/lib/clap/test.clap#org.test.synth"])
        self.assertEqual(added["PH/ZamComp"]["URL"], "urn:zamaudio:ZamComp")
        synth = added["PH/clap:/usr/lib/clap/test.clap#org.test.synth"]
        self.assertEqual((synth["TYPE"], synth["CAT"], synth["URL"], synth["ENABLED"]),
                         ("MIDI Synth", "Synth", "clap:/usr/lib/clap/test.clap#org.test.synth", True))
        self.assertEqual(added["PH/clap:/usr/lib/clap/lsp-plugins.clap#in.lsp-plug.limiter_mono"]["CAT"], "Dynamics")
        self.assertIs(_lv2.engines_by_type["MIDI Synth"]["PH/clap:/usr/lib/clap/test.clap#org.test.synth"], synth)
        self.assertTrue(set(added) <= set(engines))

    def test_without_the_daemon_installed_the_entries_are_disabled(self):
        added = cat.add_catalogue({"JV/ZamComp": lv2_info("ZamComp", "urn:zamaudio:ZamComp")},
                                  clap=cat.parse_clap_scan(CLAP_SCAN), binary=os.path.join(self.tmp, "absent"))
        self.assertFalse(any(info["ENABLED"] for info in added.values()))


if __name__ == "__main__":
    unittest.main()
