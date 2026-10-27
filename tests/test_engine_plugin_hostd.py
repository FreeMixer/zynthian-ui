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
_lv2 = types.ModuleType("zyngine.zynthian_lv2")
_lv2.get_plugin_ports = lambda uri: LV2_PORTS.get(uri, {})
_lv2.engines_by_type = {"MIDI Synth": {}, "Audio Effect": {}}
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
        patcher = mock.patch.dict(os.environ, {"PATH": self.bin, "FAKE_HOSTD_JOURNAL": os.path.join(self.tmp, "journal")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.port = free_port()
        ph.daemon = ph.plugin_hostd_daemon(self.port)
        ph.daemon.binary = os.path.join(self.tmp, "not-installed")
        ph.daemon.conf = os.path.join(self.tmp, "no.conf")
        self.addCleanup(lambda: (ph.daemon.stop(), ph.daemon.client.close()))
        ph._logged.clear()
        ph.layout_cache.clear()
        ph.probe_cache.clear()
        ph.DESCRIPTOR_WAIT = 0.5
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

    def journal(self):
        try:
            with open(os.environ["FAKE_HOSTD_JOURNAL"]) as f:
                return [line.rstrip("\n").split(" ", 1) for line in f]
        except FileNotFoundError:
            return []


class test_daemon(plugin_hostd_case):

    def test_starts_a_daemon_when_none_answers_and_stops_only_that_one(self):
        ph.daemon.binary = FAKE
        chain = self.chain()
        proc = self.add(chain, "PH/Amp")
        started = ph.daemon.proc
        self.assertIsNotNone(started, "no daemon answered, so the engine starts one")
        pids = {pid for pid, _ in self.journal()}
        self.assertEqual(len(pids), 1)
        self.assertNotEqual(pids, {str(os.getpid())}, "the add reached the started process")
        self.assertIn([pids.pop(), "add urn:test:amp 1000"], self.journal())
        self.remove(chain, proc)
        self.assertIsNone(ph.daemon.proc)
        self.assertIsNotNone(started.poll(), "the daemon it started is stopped with its last instance")

    def test_reuses_a_running_daemon_and_never_stops_it(self):
        fake = self.serve()
        chain = self.chain()
        proc = self.add(chain, "PH/Amp")
        self.assertIsNone(ph.daemon.proc, "a daemon answered, so none is started")
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
        ph.daemon.binary = FAKE
        chain = self.chain()
        amp1, amp2 = self.add(chain, "PH/Amp"), self.add(chain, "PH/Amp")
        amp2.controllers_dict["gain"].set_value(4.5)
        first = ph.daemon.proc
        first.kill()
        first.wait()
        amp1.controllers_dict["gain"].set_value(-2.0)
        self.assertIsNot(ph.daemon.proc, first)
        second = str(ph.daemon.proc.pid)
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
        self.assertEqual(ph.protocol.ready_line, "plugin-hostd ready!")

    def test_the_engine_types_no_reply_code(self):
        with open(ph.__file__) as f:
            source = f.read()
        self.assertEqual(re.findall(r"(?<![\w.\[])-(?:[1-9]|[1-9]\d\d)\b(?![.\w\]])", source), [],
                         "a reply code in the engine's source is a retyped one")

    def test_without_the_protocol_files_there_is_no_daemon_to_trust(self):
        empty = ph.zynthian_plugin_hostd_protocol.protocol(os.path.join(self.tmp, "none"))
        self.assertFalse(empty.loaded)
        with mock.patch.object(ph, "protocol", empty):
            with self.assertRaises(ph.plugin_hostd_down):
                ph.daemon.ensure()

    def test_ready_is_the_declared_line_not_a_port_that_answers(self):
        ph.daemon.binary = FAKE
        os.environ["FAKE_HOSTD_READY_DELAY"] = "0.6"
        log = os.path.join(tempfile.gettempdir(), "plugin-hostd.log")
        before = os.path.getsize(log) if os.path.exists(log) else 0
        started = time.monotonic()
        ph.daemon.ensure()
        self.assertGreaterEqual(time.monotonic() - started, 0.5, "the port answered at once; the line came later")
        with open(log, "rb") as f:
            f.seek(before)
            self.assertIn("plugin-hostd ready!", f.read().decode().splitlines())

    def test_a_daemon_that_never_says_ready_is_down(self):
        ph.daemon.binary = FAKE
        os.environ["FAKE_HOSTD_READY_DELAY"] = "3"
        with mock.patch.object(ph, "START_TIMEOUT", 0.5):
            with self.assertRaises(ph.plugin_hostd_down) as down:
                ph.daemon.ensure()
        self.assertIn("plugin-hostd ready!", str(down.exception))


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

    def test_a_mixed_lv2_and_clap_chain_keeps_a_client_per_member(self):
        fake = self.serve()
        chain = self.chain()
        a, b = self.add(chain, "PH/Amp"), self.add(chain, self.CLAP)
        self.assertEqual(fake.chains[1], (1000, 1001))
        self.assertIn(":lv2-clap:up:chain:1:1000,1001", fake.verb_worker_list())
        self.assertEqual((a.jackname, b.jackname), ("effect_1000", "effect_1001"))
        self.assertEqual(chain.audio_routes["effect_1001"], ["effect_1000"])
        for out in chain.get_audio_out():
            self.assertEqual(chain.audio_routes[out], ["effect_1001"])

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
            ["Main 1 1", ["p0", "p1", "p2", "p3"]], ["Main 1 2", ["p4", "p5", "p6", "p7"]], ["Main 2", ["p8", "p9"]]])
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

    def test_without_omx_panel_the_pages_follow_the_parameter_order(self):
        self.big()
        with self.assertLogs(level=logging.WARNING) as logs:
            proc = self.add(self.chain(), "PH/Big")
            self.add(self.chain(2), "PH/Big")
            logging.warning("end")
        self.assertEqual([s[0] for s in proc.engine._ctrl_screens], ["Page 1 1", "Page 1 2", "Page 2"])
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
