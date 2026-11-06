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
# One stand-in per process: other test files import this one again under its bare name, and the
# catalogue must see the same registry as every copy of these tests
_lv2 = sys.modules.get("zyngine.zynthian_lv2")
if _lv2 is None:
    _lv2 = types.ModuleType("zyngine.zynthian_lv2")
    _lv2.get_plugin_ports = lambda uri: LV2_PORTS.get(uri, {})
    _lv2.engines_by_type = {}
    sys.modules["zyngine.zynthian_lv2"] = _lv2
sys.modules["zyngine"].zynthian_lv2 = _lv2

from zyngine import zynthian_engine_plugin_hostd as ph  # noqa: E402
from zyngine import zynthian_plugin_hostd_catalogue as cat  # noqa: E402
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
            "START_STUB_LOG": self.starts, "START_STUB_DIR": self.tmp, "ZYNTHIAN_PLUGIN_ENGINE": "plugin-hostd"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.port = free_port()
        ph.daemon = ph.plugin_hostd_daemon(self.port)
        ph.daemon.start_script = os.path.join(self.tmp, "not-installed")
        ph.daemon.conf = os.path.join(self.tmp, "no.conf")
        self.addCleanup(lambda: (ph.daemon.stop(), ph.daemon.client.close()))
        ph._logged.clear()
        ph.layout_cache.clear()
        ph.layout_help.clear()
        os.environ.pop("OMX_PANEL_STUB_OLD", None)
        ph.probe_cache.clear()
        ph.DESCRIPTOR_WAIT = 0.5
        # The menus' lists by type start empty for every test, in place: the registry is shared
        _lv2.engines_by_type.clear()
        _lv2.engines_by_type.update({"MIDI Synth": {}, "Audio Effect": {}})
        # The catalogue makes the PH entries the tests add, from jalv's LV2 ones and the CLAP scan
        self.info = {"JV/Amp": lv2_info("Amp", "urn:test:amp"), "JV/NoPin": lv2_info("NoPin", "urn:test:nopin"),
                     "JV/Big": lv2_info("Big", "urn:test:big")}
        cat.add_catalogue(self.info, clap=cat.parse_clap_scan(CLAP_SCAN), binary=FAKE)
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
            client.call("descriptor_get urn:test:none")
        self.assertEqual(refused.exception.name, "PHD_ERR_DESCRIPTOR_ABSENT")
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
        for expected in ("add urn:test:amp 1000", "add urn:test:amp 1001", "param_set 1001 gain 4.5"):
            self.assertIn(expected, lines)
        self.assertEqual(lines[-1], "param_set 1000 gain -2.0", "the command that met the dead daemon is sent last")


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
        self.assertIn(os.path.join(self.tmp, "none", "plugin-hostd.json"), logs.output[0])
        with mock.patch.object(ph, "protocol", empty):
            with self.assertRaises(ph.plugin_hostd_down) as down:
                ph.daemon.ensure()
        self.assertIn(os.path.join(self.tmp, "none", "plugin-hostd.json"), str(down.exception),
                      "the refusal names the file")

    def test_the_files_are_read_where_libplugin_hostd_protocol0_installs_them(self):
        """plugin-hostd 0.16.0 moved them: /usr/share/plugin-hostd-protocol/plugin-hostd.json, no protocol.json."""
        new_home = self.declared([{"name": "ODD", "macro": "PHD_ERR_ODD", "code": -777}])
        os.rename(os.path.join(new_home, "protocol.json"), os.path.join(new_home, "plugin-hostd.json"))
        old_home = os.path.join(self.tmp, "old")
        os.makedirs(old_home)
        with open(os.path.join(old_home, "protocol.json"), "w") as f:
            json.dump({"errors": [{"name": "ODD", "macro": "PHD_ERR_OLD", "code": -778}]}, f)
        with open(os.path.join(old_home, "mod-host.json"), "w") as f:
            json.dump({"host_errors": []}, f)
        places = ((new_home, "plugin-hostd.json"), (old_home, "protocol.json"))
        with mock.patch.object(ph.zynthian_plugin_hostd_protocol, "PROTOCOL_DIR", None), \
                mock.patch.object(ph.zynthian_plugin_hostd_protocol, "PLACES", places):
            p = ph.zynthian_plugin_hostd_protocol.protocol()
            self.assertTrue(p.loaded, p.failure)
            self.assertEqual(p.code("ODD"), -777, "the new home is read first")
            self.assertEqual(ph.zynthian_plugin_hostd_protocol.protocol(new_home).code("ODD"), -777,
                             "a directory given holds the daemon's file under its new name")
            os.remove(os.path.join(new_home, "plugin-hostd.json"))
            self.assertEqual(ph.zynthian_plugin_hostd_protocol.protocol().code("ODD"), -778,
                             "a plugin-hostd before 0.16.0 still has its files read")

    def test_mod_host_json_missing_is_a_failure_too(self):
        directory = self.declared()
        os.remove(os.path.join(directory, "mod-host.json"))
        with self.assertLogs(level=logging.ERROR):
            p = ph.zynthian_plugin_hostd_protocol.protocol(directory)
        self.assertFalse(p.loaded)
        self.assertIn(os.path.join(directory, "mod-host.json"), p.failure)

    def test_a_name_the_engine_branches_on_and_the_files_do_not_declare_is_a_failure(self):
        declared = [{"name": n, "macro": "PHD_ERR_" + n, "code": -700 - i} for i, n in enumerate(ph.NEEDS)
                    if n != "DESCRIPTOR_PENDING" and not n.startswith("ERR_")]
        host = [{"name": n, "value": -800 - i} for i, n in enumerate(ph.NEEDS) if n.startswith("ERR_")]
        with self.assertLogs(level=logging.ERROR):
            p = ph.zynthian_plugin_hostd_protocol.protocol(self.declared(declared, host_errors=host), needs=ph.NEEDS)
        self.assertFalse(p.loaded)
        self.assertIn("do not declare DESCRIPTOR_PENDING", p.failure)
        with mock.patch.object(ph, "protocol", p):
            with self.assertRaises(ph.plugin_hostd_down) as down:
                ph.daemon.ensure()
        self.assertIn("DESCRIPTOR_PENDING", str(down.exception))

    def test_the_engine_reads_the_files_once_at_import(self):
        with mock.patch.object(ph.zynthian_plugin_hostd_protocol, "read", side_effect=AssertionError("read again")):
            self.serve()
            self.add(self.chain(), "PH/Amp")

    @unittest.skipUnless(any(os.path.isfile(os.path.join(d, n)) for d, n in ph.zynthian_plugin_hostd_protocol.PLACES),
                         "plugin-hostd is not installed")
    def test_the_fixture_says_what_the_installed_files_say(self):
        with mock.patch.object(ph.zynthian_plugin_hostd_protocol, "PROTOCOL_DIR", None):
            home = ph.zynthian_plugin_hostd_protocol.daemon_file()
        installed = ph.zynthian_plugin_hostd_protocol.protocol(home[0], needs=ph.NEEDS)
        self.assertTrue(installed.loaded, installed.failure)
        for name, code in ph.protocol.codes.items():
            self.assertEqual(installed.codes.get(name), code, f"the fixture retypes {name}")
        readiness = [ph.zynthian_plugin_hostd_protocol.read(*reversed(home))[0]["readiness"],
                     ph.zynthian_plugin_hostd_protocol.read("protocol.json", os.path.join(FIXTURES, "protocol"))[0]["readiness"]]
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
        self.assertEqual([(p[0], p[2]) for p in presets], [("factory:warm", "Warm Pad"), ("factory:bright", "Bright Lead")])
        self.assertTrue(proc.engine.set_preset(proc, presets[0]))
        self.assertIn("preset_load 1000 factory:warm", fake.lines)
        self.assertEqual(proc.controllers_dict["1"].value, 0.25, "the values are read back after the load")

    def test_a_plugin_whose_worker_lists_no_presets_has_none(self):
        self.serve()
        proc = self.add(self.chain(), "PH/Amp")
        with self.assertLogs(level=logging.WARNING) as logs:
            self.assertEqual(proc.engine.get_preset_list(None, proc), [])
            self.assertEqual(proc.engine.get_preset_list(None, proc), [])
            logging.warning("end")
        self.assertEqual(len(logs.records), 2, "PHD_ERR_NO_PRESET_LIST is logged once")
        self.assertIn("PHD_ERR_NO_PRESET_LIST", logs.output[0])

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


class test_pages(plugin_hostd_case):

    def big(self):
        return self.serve(descriptors={"urn:test:big": os.path.join(FIXTURES, "big.descriptor.json")})

    def test_without_per_page_support_more_than_four_controls_are_screens_named_by_content(self):
        os.environ["OMX_PANEL_STUB_OLD"] = "1"
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

    def test_with_per_page_support_omx_panel_names_the_pages_of_four(self):
        os.symlink(os.path.join(FIXTURES, "omx-panel"), os.path.join(self.bin, "omx-panel"))
        runs = os.path.join(self.tmp, "omx-panel.log")
        os.environ["OMX_PANEL_STUB_LOG"] = runs
        self.big()
        proc = self.add(self.chain(), "PH/Big")
        self.assertEqual(proc.engine._ctrl_screens, [
            ["Main 1", ["p0", "p1", "p2", "p3"]], ["Main 2", ["p4", "p5", "p6", "p7"]], ["Main 3", ["p8", "p9"]]])
        with open(runs) as f:
            self.assertEqual(f.read().splitlines()[1].split()[-4:], ["--pages", "--per-page", "4", "--easy-first"])

    REMOTE = "clap:/usr/lib/clap/test.clap#org.test.remote"

    def remote(self, pages=True):
        scan = ('file /usr/lib/clap/test.clap\n  plugin org.test.remote "Remote" vendor Test version 1\n'
                '    features instrument synthesizer stereo\n    out main 1 channels unknown "out" out_1\n')
        cat.add_catalogue(self.info, clap=cat.parse_clap_scan(scan), binary=FAKE)
        descriptor = {"origin": "scanned", "plugin": {"id": "org.test.remote", "format": "clap"}, "controls": [
            {"id": i, "name": f"P{i}", "kind": "travel", "travel": {"min": 0, "max": 1, "default": 0.5}}
            for i in range(1, 13)]}
        path = os.path.join(self.tmp, "remote.descriptor.json")
        with open(path, "w") as f:
            json.dump(descriptor, f)
        fake = self.serve(descriptors={self.REMOTE: path})
        if not pages:
            fake_plugins[self.REMOTE]["remote"], saved = [], fake_plugins[self.REMOTE]["remote"]
            self.addCleanup(fake_plugins[self.REMOTE].__setitem__, "remote", saved)
        return fake

    def test_the_plugins_remote_pages_come_first_and_the_layout_after_them(self):
        self.remote()
        proc = self.add(self.chain(), "PH/" + self.REMOTE)
        pages = proc.engine.pages()
        self.assertEqual([(name, [r[0] for r in rows]) for name, rows in pages], [
            ("Macros", ["3", "1", "2"]), ("Mixer", ["12"]),
            ("Page 1", ["4", "5", "6", "7", "8"]), ("Page 2", ["9", "10", "11"])])
        self.assertEqual([z.symbol for z in proc.get_ctrl_screen("Macros")], ["3", "1", "2"])
        self.assertEqual(proc.engine._ctrl_screens[0][1], ["3", "1", "2"], "the first screen is the first remote page")

    def test_a_plugin_without_remote_pages_keeps_its_layout(self):
        self.remote(pages=False)
        proc = self.add(self.chain(), "PH/" + self.REMOTE)
        self.assertEqual([(n, len(r)) for n, r in proc.engine.pages()], [("Page 1", 8), ("Page 2", 4)])

    def test_a_clap_parameter_is_addressed_by_its_number_not_by_the_layouts_name(self):
        # omx-panel names a CLAP parameter p<number> in its layout; plugin-hostd knows it by the number alone
        os.symlink(os.path.join(FIXTURES, "omx-panel"), os.path.join(self.bin, "omx-panel"))
        uri = "clap:/usr/lib/clap/test.clap#org.test.synth"
        fake = self.serve(descriptors={uri: os.path.join(FIXTURES, "synth.descriptor.json")})
        proc = self.add(self.chain(), "PH/" + uri)
        self.assertEqual([z.symbol for z in proc.get_ctrl_screen("Controls")], ["1", "2"])
        proc.controllers_dict["2"].set_value(3)
        self.assertEqual(fake.params[1000]["2"], 3.0, "the value reaches the plugin")

    def five_controls_band(self, old):
        with open(os.path.join(FIXTURES, "big.descriptor.json")) as f:
            descriptor = json.load(f)
        labels = {"p0": "Freq", "p1": "Gain", "p2": "Q", "p3": "Type", "p4": "On"}
        for c in descriptor["controls"]:
            c["group"] = ["Band 1"] if c["id"] in labels else ["Filter"]
            c["name"] = labels.get(c["id"], c["name"])
        path = os.path.join(self.tmp, "band.descriptor.json")
        with open(path, "w") as f:
            json.dump(descriptor, f)
        if old:
            os.environ["OMX_PANEL_STUB_OLD"] = "1"
        os.symlink(os.path.join(FIXTURES, "omx-panel"), os.path.join(self.bin, "omx-panel"))
        self.serve(descriptors={"urn:test:big": path})
        proc = self.add(self.chain(), "PH/Big")
        return proc.engine._ctrl_screens

    def test_a_band_of_five_controls_is_two_pages_named_by_omx_panel(self):
        self.assertEqual(self.five_controls_band(old=False), [
            ["Band 1 1", ["p0", "p1", "p2", "p3"]], ["Band 1 2", ["p4"]],
            ["Filter 1", ["p5", "p6", "p7", "p8"]], ["Filter 2", ["p9"]]])

    def test_without_per_page_support_a_band_of_five_names_its_screens_by_their_content(self):
        self.assertEqual(self.five_controls_band(old=True), [
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


class test_easy(plugin_hostd_case):
    # The Easy page, whose rule is omx-panel's: here, only that the engine reaches it

    def easy_plugin(self, easy=("p3", "p1")):
        with open(os.path.join(FIXTURES, "big.descriptor.json")) as f:
            descriptor = json.load(f)
        if easy:
            descriptor["easy"] = list(easy)
        path = os.path.join(self.tmp, "easy.descriptor.json")
        with open(path, "w") as f:
            json.dump(descriptor, f)
        os.symlink(os.path.join(FIXTURES, "omx-panel"), os.path.join(self.bin, "omx-panel"))
        fake = self.serve(descriptors={"urn:test:big": path})
        return fake, self.add(self.chain(), "PH/Big")

    def test_the_first_controller_page_is_easy_and_no_control_repeats(self):
        fake, proc = self.easy_plugin()
        pages = proc.engine.pages()
        self.assertEqual((pages[0][0], [r[0] for r in pages[0][1]]), ("Easy", ["p3", "p1"]))
        symbols = [r[0] for _, rows in pages for r in rows]
        self.assertEqual(len(symbols), len(set(symbols)))
        self.assertEqual(proc.engine._ctrl_screens[0][1], ["p3", "p1"], "the first screen is the Easy page")

    def test_easy_comes_before_the_plugins_remote_pages(self):
        fake, proc = self.easy_plugin()
        proc.engine.remote_pages = lambda dcontrols: [("Macros", [("p3", "L3", None, {}), ("p5", "L5", None, {})])]
        self.assertEqual([(n, [r[0] for r in rows]) for n, rows in proc.engine.pages()][:2],
                         [("Easy", ["p3", "p1"]), ("Macros", ["p5"])])

    def test_an_omx_panel_without_easy_first_keeps_the_pages_it_gives(self):
        os.environ["OMX_PANEL_STUB_NO_EASY"] = "1"
        self.addCleanup(os.environ.pop, "OMX_PANEL_STUB_NO_EASY", None)
        fake, proc = self.easy_plugin()
        self.assertNotEqual(proc.engine.pages()[0][0], "Easy")


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

    def test_one_clap_id_in_two_directories_is_listed_once(self):
        surge = CLAP_SCAN_MVERB.replace("studio.kx.distrho.MVerb", "org.surge-synth-team.surge-xt")
        scan = "file /root/.clap/SurgeXT.clap\n" + surge + "file /usr/local/lib/clap/Surge XT.clap\n" + surge
        added = cat.add_catalogue({"JV/Surge": lv2_info("Surge", "urn:surge")}, clap=cat.parse_clap_scan(scan),
                                  binary=FAKE)
        self.assertEqual(sorted(added), ["PH/Surge", "PH/clap:/root/.clap/SurgeXT.clap#org.surge-synth-team.surge-xt"])

    SURGE_SCAN = "file /usr/lib/clap/Surge XT.clap\n" + CLAP_SCAN_MVERB.replace(
        'studio.kx.distrho.MVerb "MVerb"', 'org.surge-synth-team.surge-xt "Surge XT"')
    SURGE_CLAP = "PH/clap:/usr/lib/clap/Surge XT.clap#org.surge-synth-team.surge-xt"

    def test_titles_keep_the_engine_mark_and_leave_the_format_out(self):
        added = cat.add_catalogue({"JV/Obxd": lv2_info("Obxd", "urn:obxd"), "JV/surge": lv2_info("Surge XT", "urn:surge")},
                                  clap=cat.parse_clap_scan(self.SURGE_SCAN + "file /usr/lib/clap/mverb.clap\n" + CLAP_SCAN_MVERB),
                                  binary=FAKE)
        self.assertEqual(added["PH/Obxd"]["TITLE"], "Obxd (PH)")
        self.assertEqual(added[self.SURGE_CLAP]["TITLE"], "Surge XT (PH)")
        for info in added.values():
            self.assertTrue(info["TITLE"].endswith(" (PH)"), info["TITLE"])
            self.assertNotRegex(info["TITLE"], r"\b(LV2|CLAP)\b")
            self.assertNotRegex(info["DESCR"], r"\b(LV2|CLAP)\b", "the format is said once, in the format line")

    def test_the_detail_line_names_the_format_and_the_other_installed_build(self):
        added = cat.add_catalogue({"JV/Obxd": lv2_info("Obxd", "urn:obxd"), "JV/surge": lv2_info("surge  XT", "urn:surge")},
                                  clap=cat.parse_clap_scan(self.SURGE_SCAN + "file /usr/lib/clap/mverb.clap\n" + CLAP_SCAN_MVERB),
                                  binary=FAKE)
        self.assertEqual(added[self.SURGE_CLAP]["FORMAT_LINE"],
                         "CLAP, run by plugin-hostd (an LV2 build is also installed)")
        self.assertEqual(added["PH/surge"]["FORMAT_LINE"], "LV2, run by plugin-hostd (a CLAP build is also installed)")
        self.assertEqual(added["PH/Obxd"]["FORMAT_LINE"], "LV2, run by plugin-hostd")
        self.assertEqual(added["PH/clap:/usr/lib/clap/mverb.clap#studio.kx.distrho.MVerb"]["FORMAT_LINE"],
                         "CLAP, run by plugin-hostd")

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

    def test_without_the_daemon_installed_the_entries_are_known_and_not_listed(self):
        with self.assertLogs(level=logging.WARNING):
            added = cat.add_catalogue({"JV/ZamComp": lv2_info("ZamComp", "urn:zamaudio:ZamComp")},
                                      clap=cat.parse_clap_scan(CLAP_SCAN), binary=os.path.join(self.tmp, "absent"))
        self.assertTrue(added)
        self.assertIn("PH/ZamComp", added)
        self.assertNotIn("PH/ZamComp", _lv2.engines_by_type["Audio Effect"], "the menus keep jalv")


if __name__ == "__main__":
    unittest.main()
