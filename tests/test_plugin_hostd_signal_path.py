# -*- coding: utf-8 -*-
#
# What a chain of plugin-hostd plugins does to the chain's signal path. A slot of several processors is parallel, and
# a worker runs its chain in series inside one JACK client: only a serial run of plugins with nothing between them may
# be grouped. The chain, its processors, its routes and the chain manager are the real ones; the daemon is the fake.
#
# Copyright (C) 2026 Pau Aliagas <linuxnow@gmail.com>
# SPDX-License-Identifier: GPL-2.0-or-later

import os
import sys
import json
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import test_engine_plugin_hostd as phtest  # noqa: E402
from test_engine_plugin_hostd import ph, zynthian_processor  # noqa: E402
from zyngine.zynthian_controller import zynthian_controller  # noqa: E402

CLAP = "PH/clap:/usr/lib/clap/lsp-plugins.clap#in.lsp-plug.limiter_mono"
SEND = "zynmixer_chan:send_03"


class signal_path_case(phtest.plugin_hostd_case):

    def strip(self, chain, code="MI", mixer_chan=3):
        """The mixer strip of a chain: an audio slot of its own, a client that is not the plugin's."""
        proc = zynthian_processor(code, {"TYPE": "Audio Effect", "NAME": code, "TITLE": code, "CAT": "Mixer"}, self.next_id)
        self.next_id += 1
        proc.jackname = "zynmixer_bus" if code == "MR" else "zynmixer_chan"
        proc.mixer_chan = mixer_chan
        chain.insert_processor(proc)
        chain.zynmixer_proc = proc
        chain.rebuild_graph()
        return proc

    def foreign(self, chain, jackname="jalv_x"):
        """A plugin of another engine, as jalv's is to the chain: a client of its own."""
        proc = zynthian_processor("JV/Other", {"TYPE": "Audio Effect", "NAME": "Other", "TITLE": "Other", "CAT": "Other"},
                                  self.next_id)
        self.next_id += 1
        proc.jackname = jackname
        proc.engine = mock.MagicMock(jackname=jackname)
        chain.insert_processor(proc)
        chain.rebuild_graph()
        return proc

    def parallel(self, chain, proc):
        """Nudge a processor up into the slot before it, as the chain view's move does: the slot is then parallel."""
        chain.nudge_processor(proc, True)
        self.assertGreater(len(chain.audio_slots[chain.get_slot(proc)]), 1, "the nudge made a parallel slot")

    def held(self, fake):
        """The groups the daemon runs: chains declared and segments of two or more of plans that apply."""
        groups = {m for m in fake.chains.values()}
        for segments, _ in fake.applied_plans().values():
            groups |= {tuple(s) for s in segments if len(s) > 1}
        return groups


class test_parallel_slot(signal_path_case):

    def test_two_plugins_of_a_parallel_slot_stay_parallel(self):
        fake = self.serve()
        chain = self.chain()
        a, b = self.add(chain, CLAP), self.add(chain, CLAP)
        self.parallel(chain, b)
        self.assertEqual(self.held(fake), set(), "two branches are not one worker's series")
        self.assertEqual((a.get_jackname(), b.get_jackname()), ("effect_1000", "effect_1001"))
        self.assertEqual(chain.audio_routes["effect_1000"], [])
        self.assertEqual(chain.audio_routes["effect_1001"], [], "the sibling has its own input")
        for out in chain.get_audio_out():
            self.assertEqual(chain.audio_routes[out], ["effect_1000", "effect_1001"], "both reach the output")

    def test_a_parallel_slot_between_serial_runs_splits_them(self):
        fake = self.serve()
        chain = self.chain()
        a, b, c, d, e, f = (self.add(chain, CLAP) for _ in range(6))
        self.parallel(chain, d)  # a, b, [c, d], e, f
        self.assertEqual(self.held(fake), {(1000, 1001), (1004, 1005)})
        self.assertEqual({p.get_jackname() for p in (a, b)}, {"effect_1000"})
        self.assertEqual({p.get_jackname() for p in (e, f)}, {"effect_1004"})
        self.assertEqual(chain.audio_routes["effect_1002"], ["effect_1000"])
        self.assertEqual(chain.audio_routes["effect_1003"], ["effect_1000"])
        self.assertEqual(chain.audio_routes["effect_1004"], ["effect_1002", "effect_1003"])
        for out in chain.get_audio_out():
            self.assertEqual(chain.audio_routes[out], ["effect_1004"])

    def test_the_planner_is_asked_of_each_run_and_never_of_a_parallel_slot(self):
        fake = self.serve(planner=True, grouping=True)
        chain = self.chain()
        a, b, c, d, e, f = (self.add(chain, CLAP) for _ in range(6))
        self.parallel(chain, d)
        self.assertEqual(self.held(fake), {(1000, 1001), (1004, 1005)})
        self.assertEqual({p.get_jackname() for p in (e, f)}, {"effect_1004"})
        self.assertEqual((c.get_jackname(), d.get_jackname()), ("effect_1002", "effect_1003"))
        self.assertEqual(ph.daemon.reasons, {}, "no plan was refused")
        self.assertFalse(any(1002 in m or 1003 in m for m in ph.daemon.plans.values()), "the parallel slot is not planned")

    def test_a_chain_moved_back_out_of_parallel_is_grouped_again(self):
        fake = self.serve()
        chain = self.chain()
        a, b, c = (self.add(chain, CLAP) for _ in range(3))
        self.parallel(chain, c)
        self.assertEqual(self.held(fake), set())
        chain.nudge_processor(c, True)  # out of the slot, before b
        self.assertEqual(self.held(fake), {(1000, 1002, 1001)})
        self.assertEqual(c.get_jackname(), "effect_1000")

    def test_a_member_moved_between_runs_is_never_refused_as_busy(self):
        fake = self.serve()
        chain = self.chain()
        procs = [self.add(chain, CLAP) for _ in range(7)]
        self.parallel(chain, procs[3])  # 0 1 [2 3] 4 5 6: two runs
        self.assertEqual(self.held(fake), {(1000, 1001), (1004, 1005, 1006)})
        chain.nudge_processor(procs[3], False)  # 0 1 2 3 4 5 6, out of the slot: one run
        chain.nudge_processor(procs[1], False)  # 0 [2 1] 3 4 5 6: 1 joins 2's slot, the run after it holds 3 to 6
        self.assertEqual(self.held(fake), {(1003, 1004, 1005, 1006)})
        self.assertEqual(ph.daemon.chain_refused, {}, "the daemon refused nothing")

    def test_a_restored_daemon_gets_the_runs_back(self):
        fake = self.serve()
        chain = self.chain()
        for _ in range(6):
            self.add(chain, CLAP)
        self.parallel(chain, chain.audio_slots[3][0])
        groups = self.held(fake)
        fake.chains.clear()
        ph.daemon.restore()
        self.assertEqual(self.held(fake), groups)

    def test_removing_the_members_clears_every_run(self):
        fake = self.serve()
        chain = self.chain()
        procs = [self.add(chain, CLAP) for _ in range(6)]
        self.parallel(chain, procs[3])
        self.assertEqual(len(fake.chains), 2)
        for p in procs:
            self.remove(chain, p)
        self.assertEqual(fake.chains, {})
        self.assertEqual(ph.daemon.clients, {})


class test_a_chain_with_other_members(signal_path_case):

    def test_a_strip_between_two_plugins_stays_in_the_path(self):
        """pre-fader, strip, post-fader: one worker would take the signal past the fader."""
        fake = self.serve()
        chain = self.chain()
        pre = self.add(chain, CLAP)
        strip = self.strip(chain)
        post = self.add(chain, CLAP)
        self.assertEqual(self.held(fake), set(), "the fader runs between them")
        self.assertEqual(post.get_jackname(), "effect_1001")
        self.assertEqual(chain.audio_routes["zynmixer_chan:input_03"], ["effect_1000"])
        self.assertEqual(chain.audio_routes["effect_1001"], ["zynmixer_chan:output_03"])
        for out in chain.get_audio_out():
            self.assertEqual(chain.audio_routes[out], ["effect_1001"], "the output is after the fader")

    def test_runs_on_each_side_of_the_strip_are_grouped_apart(self):
        fake = self.serve()
        chain = self.chain()
        a, b = self.add(chain, CLAP), self.add(chain, CLAP)
        self.strip(chain)
        c, d = self.add(chain, CLAP), self.add(chain, CLAP)
        self.assertEqual(self.held(fake), {(1000, 1001), (1002, 1003)})
        self.assertEqual(chain.audio_routes["zynmixer_chan:input_03"], ["effect_1000"])
        self.assertEqual(chain.audio_routes["effect_1002"], ["zynmixer_chan:output_03"])
        for out in chain.get_audio_out():
            self.assertEqual(chain.audio_routes[out], ["effect_1002"])

    def test_a_jalv_plugin_between_two_plugins_stays_in_the_path(self):
        fake = self.serve()
        chain = self.chain()
        self.add(chain, CLAP)
        self.foreign(chain)
        self.add(chain, CLAP)
        self.assertEqual(self.held(fake), set())
        self.assertEqual(chain.audio_routes["jalv_x"], ["effect_1000"])
        self.assertEqual(chain.audio_routes["effect_1001"], ["jalv_x"])
        for out in chain.get_audio_out():
            self.assertEqual(chain.audio_routes[out], ["effect_1001"], "the jalv plugin's output is used")

    def test_a_parallel_slot_of_a_plugin_and_a_jalv_plugin_keeps_both(self):
        fake = self.serve()
        chain = self.chain()
        self.add(chain, CLAP)
        self.add(chain, CLAP)
        jalv = self.foreign(chain)
        chain.nudge_processor(jalv, True)  # a, [b, jalv]
        self.add(chain, CLAP)  # a, [b, jalv], c
        self.assertEqual(self.held(fake), set())
        self.assertEqual(chain.audio_routes["effect_1001"], ["effect_1000"])
        self.assertEqual(chain.audio_routes["jalv_x"], ["effect_1000"])
        self.assertEqual(chain.audio_routes["effect_1002"], ["effect_1001", "jalv_x"], "both feed the next slot")

    def test_a_synth_heads_the_run_of_the_effects_after_it_and_not_of_a_parallel_slot(self):
        fake = self.serve()
        chain = self.chain()
        synth = self.add(chain, "PH/clap:/usr/lib/clap/test.clap#org.test.synth")
        self.assertIn(synth, chain.synth_slots[0])
        a, b = self.add(chain, CLAP), self.add(chain, CLAP)
        self.assertEqual(self.held(fake), {(1000, 1001, 1002)})
        self.parallel(chain, b)
        self.assertEqual(self.held(fake), set(), "the synth feeds two branches")
        self.assertEqual(chain.audio_routes["effect_1001"], ["effect_1000"])
        self.assertEqual(chain.audio_routes["effect_1002"], ["effect_1000"])


class test_daemon_as_it_answers_today(signal_path_case):
    """The records and replies as plugin-hostd 0.15.4 writes them, the fake's now."""

    def test_a_record_with_a_core_and_a_pool_is_read_from_its_place(self):
        self.assertEqual(ph.worker_record("w3:4513:clap:up:1:qualified:chain:1:1000,1001"),
                         ("w3", "clap", "up", "chain", "1", [1000, 1001]))
        self.assertEqual(ph.worker_record("w1:4309:clap:up:1:qualified:own:1000"),
                         ("w1", "clap", "up", "own", None, [1000]))
        self.assertEqual(ph.worker_record("w3:4513:clap:up:1:qualified:chain:2001.0:1000,1001")[4], "2001.0")

    def test_the_cores_after_a_plan_are_not_read_as_segments(self):
        self.assertEqual(ph.parse_segments("1000,1001;1002 -,-;-"), [[1000, 1001], [1002]])

    def test_a_worker_that_keeps_a_client_per_member_is_routed_member_by_member(self):
        # plugin-hostd 0.15.4 answers resp 0 when the worker refuses the chain line: the graph says what runs
        self.serve()
        chain = self.chain()
        with mock.patch.object(sys.modules["zynautoconnect"], "get_ports", side_effect=lambda name: ["port"]):
            a, b = self.add(chain, CLAP), self.add(chain, CLAP)
        self.assertEqual((a.get_jackname(), b.get_jackname()), ("effect_1000", "effect_1001"))
        self.assertEqual(chain.audio_routes["effect_1001"], ["effect_1000"])
        for out in chain.get_audio_out():
            self.assertEqual(chain.audio_routes[out], ["effect_1001"])

    def test_a_graph_that_cannot_be_read_leaves_the_daemons_word(self):
        self.serve()
        chain = self.chain()
        with mock.patch.object(sys.modules["zynautoconnect"], "get_ports", side_effect=AttributeError("no client")):
            a, b = self.add(chain, CLAP), self.add(chain, CLAP)
        self.assertEqual((a.get_jackname(), b.get_jackname()), ("effect_1000", "effect_1000"))


class test_mixbus(signal_path_case):
    """A mixbus chain is what an effects send returns through: no synth, its input the mixer's send."""

    def test_its_effects_are_fed_by_the_send_and_grouped_up_to_the_strip(self):
        fake = self.serve()
        chain = self.chain()
        mr = self.strip(chain, "MR")
        chain.audio_slots.remove([mr])
        a, b = self.add(chain, CLAP), self.add(chain, CLAP)
        chain.audio_slots.append([mr])
        chain.rebuild_graph()
        self.assertTrue(chain.is_mixbus())
        self.assertEqual(self.held(fake), {(1000, 1001)})
        self.assertEqual(chain.audio_routes["effect_1000"], [SEND])
        self.assertEqual(chain.audio_routes["zynmixer_bus:input_03"], ["effect_1000"])

    def test_parallel_effects_of_a_mixbus_each_take_the_send(self):
        fake = self.serve()
        chain = self.chain()
        mr = self.strip(chain, "MR")
        chain.audio_slots.remove([mr])
        a, b = self.add(chain, CLAP), self.add(chain, CLAP)
        chain.audio_slots.append([mr])
        self.parallel(chain, b)
        self.assertEqual(self.held(fake), set())
        self.assertEqual(chain.audio_routes["effect_1000"], [SEND])
        self.assertEqual(chain.audio_routes["effect_1001"], [SEND])
        self.assertEqual(chain.audio_routes["zynmixer_bus:input_03"], ["effect_1000", "effect_1001"])

    def test_a_mixbus_named_from_its_plugin_gives_the_send_page_a_name(self):
        # refresh_mixbus_sends names a send's page from the chain's first audio effect when it has no title
        self.serve()
        chain = self.chain()
        proc = self.add(chain, "PH/Amp")
        self.assertEqual(chain.get_processors("Audio Effect")[0], proc)
        self.assertEqual(proc.get_name(), "Amp")


class test_figures_follow_the_signal(signal_path_case):
    """The chain view's figures: a parallel slot's latency is its longest branch's, and each group is its own worker."""

    def figures(self, chain):
        return ph.estimate_steps(48000, 256, ph.chain_steps(chain))

    def test_a_series_chain_is_asked_once(self):
        fake = self.serve(estimator=True)
        chain = self.chain()
        self.add(chain, "PH/Amp"), self.add(chain, "PH/NoPin")
        self.assertEqual(ph.chain_steps(chain), [(("urn:test:amp", "urn:test:nopin"),)])
        self.assertEqual(self.figures(chain)["latency"], 64)
        self.assertEqual(len([line for line in fake.lines if line.startswith("estimate ")]), 1)

    def test_a_parallel_slot_takes_the_longest_latency_and_adds_the_rest(self):
        fake = self.serve(estimator=True)
        fake.ESTIMATES = {"urn:test:amp": (10, 1.0, 100), "urn:test:nopin": (40, 2.0, 200)}
        chain = self.chain()
        a, b, c = self.add(chain, "PH/Amp"), self.add(chain, "PH/Amp"), self.add(chain, "PH/NoPin")
        self.parallel(chain, c)
        self.assertEqual(ph.chain_steps(chain), [(("urn:test:amp",),), (("urn:test:amp",), ("urn:test:nopin",))])
        est = self.figures(chain)
        self.assertEqual(est["latency"], 10 + 40, "not the sum of the two branches")
        self.assertAlmostEqual(est["cpu"], 1.0 + 1.0 + 2.0)
        self.assertEqual(est["ram"], 400)
        self.assertEqual(est["workers"], 3)

    def test_the_strip_splits_the_figures_as_it_splits_the_workers(self):
        self.serve(estimator=True)
        chain = self.chain()
        self.add(chain, "PH/Amp")
        self.strip(chain)
        self.add(chain, "PH/NoPin")
        self.assertEqual(ph.chain_steps(chain), [(("urn:test:amp",),), (("urn:test:nopin",),)])
        self.assertEqual(self.figures(chain)["workers"], 2)

    def test_an_unknown_member_is_named_by_its_place_in_the_chain(self):
        self.serve(estimator=True)
        chain = self.chain()
        self.add(chain, "PH/Amp")
        self.foreign(chain)
        self.add(chain, "PH/Big")
        est = self.figures(chain)
        self.assertEqual(est["unknown"], 1)
        self.assertEqual(est["members"].keys(), {1}, "the second plugin of the chain's, not the first of its group")

    def test_the_menu_counts_the_candidate_before_the_strip_where_it_will_go(self):
        self.serve(estimator=True)
        chain = self.chain()
        self.add(chain, "PH/Amp")
        self.strip(chain)
        info = self.info["JV/NoPin"] | {"URL": "urn:test:nopin"}
        self.assertEqual(ph.menu_refs(chain, "PH/NoPin", info), [(("urn:test:amp", "urn:test:nopin"),)])

    def test_the_menu_puts_a_replacement_in_the_place_of_what_it_replaces(self):
        self.serve(estimator=True)
        chain = self.chain()
        a, b = self.add(chain, "PH/Amp"), self.add(chain, "PH/Amp")
        self.parallel(chain, b)
        info = self.info["JV/NoPin"] | {"URL": "urn:test:nopin"}
        self.assertEqual(ph.menu_refs(chain, "PH/NoPin", info, replacing=b),
                         [(("urn:test:amp",), ("urn:test:nopin",))])


def load_chain_manager():
    """The real chain manager: only the other engines, and what needs a Zynthian's native libraries, are stand-ins."""
    pkg = sys.modules["zyngine"]
    native = sys.modules["zyncoder.zyncore"].lib_zyncore
    for name, value in (("zmop_get_num_chains", 17), ("zmip_get_num_devs", 4), ("zmop_get_num_devs", 4)):
        getattr(native, name).return_value = value
    engines = ("zynaddsubfx", "fluidsynth", "sfizz", "linuxsampler", "setbfree", "aeolus", "audioplayer", "sooperlooper",
               "sysex", "midi_control", "puredata", "modui", "inet_radio", "audio_mixer", "alsa_mixer", "tempo", "clippy",
               "jalv", "pianoteq")
    names = {f"zynthian_engine_{e}": mock.MagicMock() for e in engines}
    names["zynthian_engine_plugin_hostd"] = ph.zynthian_engine_plugin_hostd
    names["get_pianoteq_binary_info"] = lambda: None
    names["sleep"] = lambda seconds: None
    names["zynthian_controller"] = zynthian_controller
    stand_ins = {f"zyngine.zynthian_engine_{e}": mock.MagicMock(__all__=[]) for e in ("jalv", "pianoteq")}
    stand_ins.update({n: mock.MagicMock() for n in ("zyngine.zynthian_state_manager", "zyngui", "zyngui.zynthian_gui_config")})
    with mock.patch.dict(sys.modules, stand_ins), mock.patch.multiple(pkg, create=True, __all__=list(names),
                                                                      zynthian_state_manager=mock.MagicMock(), **names):
        with mock.patch.object(phtest._lv2, "get_engines", lambda: {"PT": {}}, create=True):
            from zyngine import zynthian_chain_manager
    sys.modules.pop("zyngine.zynthian_chain_manager", None)
    return zynthian_chain_manager


class strip_engine:
    """The mixer's engine, as far as the chain manager and the chain ask of it."""

    def __init__(self, state_manager=None):
        self.processors = []
        self.nickname = "MI"

    def add_processor(self, processor):
        self.processors.append(processor)
        processor.jackname = "zynmixer_bus" if processor.eng_code == "MR" else "zynmixer_chan"
        processor.mixer_chan = processor.id
        processor.zynmixer = mock.MagicMock()
        processor.zynmixer.get_send_level.return_value = 0
        processor.zynmixer.get_send_mode.return_value = 0

    def remove_processor(self, processor):
        self.processors.remove(processor)

    def get_jackname(self):
        return "zynmixer_chan"

    def get_name(self, processor=None):
        return "strip"

    def stop(self):
        pass

    def __getattr__(self, name):
        return mock.MagicMock()


class test_chain_manager(signal_path_case):
    """The real manager's graph work over the engine: a snapshot load, a mixbus, its sends and pinning."""

    @classmethod
    def setUpClass(cls):
        cls.manager = load_chain_manager()

    def setUp(self):
        super().setUp()
        info = {code: dict(entry, ENGINE=ph.zynthian_engine_plugin_hostd) for code, entry in self.info.items()
                if code.startswith("PH/")}
        strip = {"NAME": "Strip", "TITLE": "Strip", "TYPE": "Audio Effect", "CAT": "Other", "ENGINE": strip_engine, "URL": ""}
        info.update({"MI": strip, "MR": strip})
        patcher = mock.patch.object(self.manager.zynthian_chain_manager, "engine_info", info, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cm = self.manager.zynthian_chain_manager(self.sm)
        self.sm.chain_manager = self.cm
        self.sm.is_busy.return_value = False
        self.cm.add_chain(0, audio_thru=True, midi_chan=None)
        self.cm.add_processor(0, "MR", 0, 1000000)

    def busy_like_a_state_manager(self):
        """start_busy and end_busy as the state manager has them: the last end sends SS_BUSY with state False."""
        clients = set()
        self.sm.is_busy.side_effect = lambda client=None: bool(clients)

        def start(clid, *a, **kw):
            clients.add(clid)

        def end(clid, *a, **kw):
            clients.discard(clid)
            if not clients:
                ph.zynsigman.send(ph.zynsigman.S_STATE_MAN, ph.zynsigman.SS_BUSY, state=False)
        self.sm.start_busy.side_effect = start
        self.sm.end_busy.side_effect = end

    def test_a_removed_member_leaves_the_daemon_before_its_run_is_declared_again(self):
        fake = self.serve()
        self.busy_like_a_state_manager()
        chain_id = self.cm.add_chain(None, audio_thru=True)
        procs = [self.cm.add_processor(chain_id, CLAP) for _ in range(3)]
        self.assertEqual(self.held(fake), {(1000, 1001, 1002)})
        sent = len(fake.lines)
        middle = procs[1].engine.instance
        self.cm.remove_processor(chain_id, procs[1])
        after = fake.lines[sent:]
        removed = after.index(f"remove {middle}")
        declared = [i for i, line in enumerate(after) if line.startswith(("chain ", "plan "))]
        self.assertTrue(declared and all(i > removed for i in declared), f"the run is declared after the remove: {after}")
        self.assertEqual(self.held(fake), {(1000, 1002)})

    def test_a_snapshot_declares_each_run_once(self):
        fake = self.serve()
        self.busy_like_a_state_manager()
        self.sm.start_busy("load_snapshot")  # the load is one busy section, as zynthian_state_manager's is
        self.cm.set_state(self.snapshot(*({str(10 + i): CLAP} for i in range(6))), {})
        self.sm.end_busy("load_snapshot")
        chains = [line for line in fake.lines if line.startswith("chain ")]
        self.assertEqual(len(chains), 1, f"one chain line for one run: {chains}")
        self.assertEqual(self.held(fake), {tuple(range(1000, 1006))})
        routes = self.cm.chains[1].audio_routes
        self.assertIn("effect_1000", {s for sources in routes.values() for s in sources}, "the routes follow the group")

    def snapshot(self, *slots, chain_id="1", title="c"):
        return {chain_id: {"title": title, "midi_chan": None, "audio_thru": True, "slots": list(slots)}}

    def test_a_snapshot_with_a_strip_comes_back_with_each_side_grouped_apart(self):
        fake = self.serve()
        state = self.snapshot({"10": CLAP}, {"11": CLAP}, {"12": "MI"}, {"13": CLAP}, {"14": CLAP})
        self.cm.set_state(state, {})
        chain = self.cm.chains[1]
        self.assertEqual([[p.id for p in slot] for slot in chain.audio_slots], [[10], [11], [12], [13], [14]])
        self.assertEqual(self.held(fake), {(1000, 1001), (1002, 1003)})
        self.assertEqual(chain.audio_routes["zynmixer_chan:input_12"], ["effect_1000"])
        self.assertEqual(chain.audio_routes["effect_1002"], ["zynmixer_chan:output_12"])

    def test_a_second_load_stops_the_first_ones_plugins_and_groups_the_new_ones(self):
        # upstream's f43b7da5, stop_unused_single_proc_engines: every unused PH engine is stopped before the load
        fake = self.serve()
        self.cm.set_state(self.snapshot({"10": CLAP}, {"11": CLAP}), {})
        first = dict(fake.instances)
        self.assertEqual(len(first), 2)
        self.cm.set_state(self.snapshot({"20": CLAP}, {"21": CLAP}, {"22": CLAP}), {})
        self.assertEqual(sorted(self.cm.zyngines), ["MR", "PH#20", "PH#21", "PH#22"])
        self.assertEqual(len(fake.instances), 3, "the first load's plugins are gone")
        self.assertEqual(self.held(fake), {tuple(sorted(fake.instances))})
        self.assertEqual(ph.daemon.chain_refused, {})

    def test_a_merge_keeps_the_plugins_in_use_and_stops_the_unused(self):
        fake = self.serve()
        self.cm.set_state(self.snapshot({"10": CLAP}, {"11": CLAP}), {})
        kept = set(fake.instances)
        spare = self.cm.add_processor(1, CLAP)
        self.cm.chains[1].remove_processor(spare)
        self.assertEqual(len(fake.instances), 3)
        self.cm.set_state(self.snapshot({"30": CLAP}, {"31": CLAP}), {}, merge=True)
        self.assertLessEqual(kept, set(fake.instances))
        self.assertEqual(len(fake.instances), 4, "the spare's plugin is stopped, the merged ones are added")
        self.assertEqual(len(self.held(fake)), 2, "each chain has its run")

    def test_a_parallel_slot_in_a_snapshot_comes_back_as_the_chain_builds_it_and_the_workers_follow(self):
        # set_state adds each saved processor as a new slot (its own comment says so), for every engine: the chain
        # that comes back is a series, and the group is what that chain holds
        fake = self.serve()
        self.cm.set_state(self.snapshot({"10": CLAP, "11": CLAP}), {})
        chain = self.cm.chains[1]
        self.assertEqual([len(slot) for slot in chain.audio_slots], [1, 1])
        self.assertEqual(len(self.held(fake)), 1)

    def mixbus(self, *codes, title=""):
        chain_id = self.cm.add_chain(None, audio_thru=False, title=title)
        mr = self.cm.add_processor(chain_id, "MR", 0)
        procs = [self.cm.add_processor(chain_id, code, 0) for code in codes]
        return self.cm.chains[chain_id], mr, procs

    def test_a_mixbus_of_plugins_takes_the_send_as_its_input(self):
        fake = self.serve()
        chain, mr, procs = self.mixbus(CLAP, CLAP)
        self.assertTrue(chain.is_mixbus())
        self.assertIn(chain.chain_id, self.cm.get_chain_ids_filtered(["mixbus"]))
        self.assertEqual(self.held(fake), {(1001, 1000)}, "each new effect went in first")
        head = chain.audio_slots[0][0].get_jackname()
        self.assertEqual(chain.audio_routes[head], [f"zynmixer_chan:send_{mr.mixer_chan:02d}"])
        self.assertEqual(chain.audio_routes[f"zynmixer_bus:input_{mr.mixer_chan:02d}"], [head])

    def test_the_send_page_is_named_from_the_plugin_and_the_send_asks_the_daemon_nothing(self):
        fake = self.serve()
        chain, mr, procs = self.mixbus(CLAP)
        source = self.cm.add_chain(None, audio_thru=True)
        strip = self.cm.add_processor(source, "MI", 0)
        sent = len(fake.lines)
        self.cm.refresh_mixbus_sends()
        pages = [page for page in strip.ctrl_screens_dict if page.startswith("send ")]
        self.assertEqual(len(pages), 1)
        self.assertIn(procs[0].get_name(), pages[0])
        for name in (f"send_{chain.chain_id}_level", f"send_{chain.chain_id}_mode"):
            self.assertIn(name, strip.controllers_dict)
        self.assertEqual(fake.lines[sent:], [], "level and mode are the mixer's; the engine is not told")

    def test_pinning_and_moving_a_mixbus_ask_the_daemon_nothing(self):
        fake = self.serve()
        chain, _, _ = self.mixbus(CLAP, CLAP)
        before = list(self.cm.chains)
        sent = len(fake.lines)
        self.cm.set_pinned(2)
        self.assertEqual(self.cm.get_pinned_pos(), len(before) - 2)
        self.cm.move_chain(chain.chain_id, 0)
        self.assertEqual(fake.lines[sent:], [])


SYNTH = "PH/clap:/usr/lib/clap/test.clap#org.test.synth"
NOPIN = "PH/NoPin"


class test_grouping_follows_the_setting(signal_path_case):
    """grouping off in plugin-hostd.conf (its default) groups nothing, and going back to it ungroups."""

    def test_grouping_off_groups_nothing(self):
        fake = self.serve(planner=True, grouping=False)
        chain = self.chain()
        a, b = self.add(chain, CLAP), self.add(chain, CLAP)
        self.assertEqual(self.held(fake), set())
        self.assertFalse([line for line in fake.lines if line.startswith("chain ")], "no chain line goes around the setting")
        self.assertEqual((a.get_jackname(), b.get_jackname()), ("effect_1000", "effect_1001"))

    def test_going_back_to_grouping_off_ungroups(self):
        fake = self.serve(planner=True, grouping=True)
        chain = self.chain()
        a, b = self.add(chain, CLAP), self.add(chain, CLAP)
        self.assertEqual(self.held(fake), {(1000, 1001)})
        fake.grouping = False  # the setting changed and the daemon restarted with it
        ph.daemon.restore()
        chain.rebuild_graph()
        self.assertEqual(self.held(fake), set())
        self.assertFalse([line for line in fake.lines if line.startswith("chain ")])
        self.assertEqual(b.get_jackname(), "effect_1001")

    def test_a_daemon_without_a_planner_is_still_given_its_chain(self):
        fake = self.serve(planner=False)
        chain = self.chain()
        self.add(chain, CLAP), self.add(chain, CLAP)
        self.assertEqual(self.held(fake), {(1000, 1001)})


class test_an_edit_leaves_other_runs_alone(signal_path_case):

    def runs_of(self, fake, planner):
        if planner:
            return {tuple(s): c for c, (segments, _) in fake.applied_plans().items() for s in segments if len(s) > 1}
        return {m: c for c, m in fake.chains.items()}

    def check(self, planner):
        fake = self.serve(planner=planner, grouping=True)
        chain = self.chain()
        a, b = self.add(chain, CLAP), self.add(chain, CLAP)
        self.strip(chain)
        self.add(chain, CLAP), self.add(chain, CLAP)
        number = self.runs_of(fake, planner)[(1002, 1003)]
        sent = len(fake.lines)
        self.remove(chain, a)
        self.remove(chain, b)
        self.assertEqual(self.runs_of(fake, planner), {(1002, 1003): number}, "the run after the strip kept its number")
        touched = [line for line in fake.lines[sent:] if line.split()[0] in ("chain", "chain_clear", "plan", "plan_clear")
                   and line.split()[1] == str(number)]
        self.assertEqual(touched, [], "nothing was said of the untouched run")

    def test_with_chain(self):
        self.check(planner=False)

    def test_with_the_planner(self):
        self.check(planner=True)


class test_a_plugin_that_takes_notes(signal_path_case):
    """An audio effect that takes notes (a vocoder, a harmoniser): the worker's one client reads MIDI for its head
    only, so the effect heads a run of its own and keeps the client its MIDI is routed to."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(phtest.fake_plugin_hostd, "NOTES", ("urn:test:amp",))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_behind_another_effect_it_heads_its_own_run(self):
        fake = self.serve()
        chain = self.chain()
        self.add(chain, CLAP)
        notes = self.add(chain, "PH/Amp")
        self.add(chain, CLAP)
        self.assertEqual(self.held(fake), {(1001, 1002)}, "it heads the run after it, never joins the one before")
        self.assertEqual(notes.get_jackname(), notes.engine.jackname)
        self.assertIn(notes.engine.jackname, chain.midi_routes, "its MIDI goes to the client it has")

    def test_first_it_heads_the_run(self):
        fake = self.serve()
        chain = self.chain()
        self.add(chain, "PH/Amp")
        self.add(chain, CLAP)
        self.assertEqual(self.held(fake), {(1000, 1001)})


class test_a_run_ends_where_the_width_changes(signal_path_case):
    """A worker runs a chain only of members as wide as the head's output (omx-clap-core refuses any other): a mono
    effect after a stereo one is a run of its own, so the daemon is never offered a chain its worker refuses."""

    def descriptor(self, name, width):
        path = os.path.join(self.tmp, name + ".json")
        with open(path, "w") as f:
            json.dump({"ports": [{"role": "main", "direction": "in", "channels": width},
                                 {"role": "main", "direction": "out", "channels": width}]}, f)
        return path

    def serve_widths(self):
        return self.serve(descriptors={"clap:/usr/lib/clap/lsp-plugins.clap#in.lsp-plug.limiter_mono": self.descriptor("stereo", 2),
                                       "urn:test:amp": self.descriptor("mono", 1)})

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(ph.mono_in_stereo_cache, {"omx-clap-host": False})  # a worker that does not declare mono_in_stereo
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_mono_effect_between_stereo_ones_groups_nothing(self):
        fake = self.serve_widths()
        chain = self.chain()
        self.add(chain, CLAP), self.add(chain, "PH/Amp"), self.add(chain, CLAP)
        self.assertEqual(self.held(fake), set())
        self.assertEqual(ph.daemon.chain_refused, {}, "nothing was offered that the worker refuses")

    def test_members_of_one_width_still_share(self):
        fake = self.serve_widths()
        chain = self.chain()
        self.add(chain, CLAP), self.add(chain, CLAP), self.add(chain, "PH/Amp"), self.add(chain, "PH/Amp")
        self.assertEqual(self.held(fake), {(1000, 1001), (1002, 1003)})



class test_a_mono_effect_runs_inside_a_stereo_run(test_a_run_ends_where_the_width_changes):
    """A worker that declares mono_in_stereo (omx-clap-host 0.26.1 or later) runs a mono member inside a stereo chain (omx-clap-core#447): its input
    the sum of both channels, its output given to both, as zynautoconnect wires a mono plugin between stereo ones, so
    the mono effect joins the stereo run instead of splitting it. A stereo member after a mono head still heads its own."""

    def setUp(self):
        super().setUp()
        ph.mono_in_stereo_cache["omx-clap-host"] = True

    def test_a_mono_effect_between_stereo_ones_groups_nothing(self):
        fake = self.serve_widths()
        chain = self.chain()
        self.add(chain, CLAP), self.add(chain, "PH/Amp"), self.add(chain, CLAP)
        self.assertEqual(self.held(fake), {(1000, 1001, 1002)}, "the mono effect joined the stereo run")
        self.assertEqual(ph.daemon.chain_refused, {})

    def test_members_of_one_width_still_share(self):
        fake = self.serve_widths()
        chain = self.chain()
        self.add(chain, CLAP), self.add(chain, CLAP), self.add(chain, "PH/Amp"), self.add(chain, "PH/Amp")
        self.assertEqual(self.held(fake), {(1000, 1001, 1002, 1003)}, "mono members after a stereo head join it")

    def test_a_stereo_effect_after_a_mono_head_heads_its_own_run(self):
        fake = self.serve_widths()
        chain = self.chain()
        self.add(chain, "PH/Amp"), self.add(chain, "PH/Amp"), self.add(chain, CLAP), self.add(chain, CLAP)
        self.assertEqual(self.held(fake), {(1000, 1001), (1002, 1003)})


class test_the_worker_declares_mono_in_stereo(unittest.TestCase):
    """The worker's --capabilities decides; its version only when its --help lists no such option (older than 0.26.1),
    and an option the help does not list is never asked: omx-clap-host 0.26.0 starts a host on one it does not know."""

    def check(self, answers, expected):
        answers = dict(answers)
        answers.setdefault("--version", "")  # every omx-clap-host takes it; one that names no version is a no
        answers["--help"] = "  -V, --version\n" + ("      --capabilities\n" if "--capabilities" in answers else "")

        def run(argv, **kw):
            said = answers.get(argv[1])
            # an omx-clap-host older than --capabilities warns of an option it does not know, says 0 and starts
            self.assertIsNotNone(said, "asked an option the worker's --help does not list: %s" % argv[1])
            return mock.Mock(stdout=said, stderr="", returncode=0)
        with mock.patch.dict(ph.mono_in_stereo_cache, clear=True), \
             mock.patch.object(ph.subprocess, "run", side_effect=run) as called:
            self.assertEqual(ph.mono_in_stereo(), expected, answers)
            n = called.call_count
            ph.mono_in_stereo()
        self.assertEqual(called.call_count, n, "asked once")

    def test_the_declared_capability_decides(self):
        self.check({"--capabilities": "chain_expect\nmono_in_stereo\n", "--version": "omx-clap-host version: 0.1.0\n"}, True)
        self.check({"--capabilities": "chain_expect\n", "--version": "omx-clap-host version: 9.0.0\n"}, False)

    def test_the_version_is_the_fallback(self):
        self.check({"--version": "omx-clap-host version: 0.26.0\n"}, False)
        self.check({"--version": "omx-clap-host version: 0.26.1\n"}, True)
        self.check({"--version": "omx-clap-host version: 0.27.0~meas\n"}, True)
        self.check({}, False)

    def test_no_binary_is_no(self):
        with mock.patch.dict(ph.mono_in_stereo_cache, clear=True), \
             mock.patch.object(ph.subprocess, "run", side_effect=OSError("no such file")):
            self.assertFalse(ph.mono_in_stereo())


class test_a_refusal_is_asked_again(signal_path_case):

    def test_a_refused_chain_is_declared_once_it_can_be(self):
        fake = self.serve()
        chain = self.chain()
        self.add(chain, NOPIN), self.add(chain, CLAP)
        self.assertEqual(self.held(fake), set())
        self.assertEqual(ph.daemon.chain_refused, {1: (1000, 1001)})
        fake.instances[1000] = "urn:test:amp"  # the qualification sync gave it a pin
        chain.rebuild_graph()
        self.assertEqual(self.held(fake), set(), "not before the retry is due")
        with mock.patch.object(ph, "RETRY_S", 0.0):
            chain.rebuild_graph()
        self.assertEqual(self.held(fake), {(1000, 1001)})

    def test_a_plan_with_no_verdict_is_planned_again(self):
        fake = self.serve(planner=True, grouping=True, verdicts={"urn:test:amp": "none"})
        chain = self.chain()
        self.add(chain, "PH/Amp"), self.add(chain, CLAP)
        self.assertEqual(self.held(fake), set())
        fake.verdicts.clear()  # qualified meanwhile
        with mock.patch.object(ph, "RETRY_S", 0.0):
            chain.rebuild_graph()
        self.assertEqual(self.held(fake), {(1000, 1001)})

    def test_a_cleared_number_forgets_its_refusal(self):
        fake = self.serve()
        chain = self.chain()
        a, b = self.add(chain, NOPIN), self.add(chain, CLAP)
        self.remove(chain, a)
        self.remove(chain, b)
        self.assertEqual(ph.daemon.chain_refused, {})


class test_the_routes_follow_what_the_daemon_moves(signal_path_case):

    def test_a_quarantined_member_is_routed_through_its_own_client(self):
        fake = self.serve()
        chain = self.chain()
        a, b, c = (self.add(chain, CLAP) for _ in range(3))
        self.assertEqual(c.get_jackname(), "effect_1000")
        fake.chains[1] = (1000, 1001)  # the daemon quarantined 1002 to a worker of its own
        ph.follow_daemon()
        self.assertEqual(c.get_jackname(), "effect_1002")
        self.assertEqual(b.get_jackname(), "effect_1000")


if __name__ == "__main__":
    unittest.main()
