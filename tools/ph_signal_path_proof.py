#!/usr/bin/env python3
# What a chain does to its signal, measured at JACK ports. The engine, the chain, its routes, plugin-hostd and its
# worker are the real ones; a tone goes in, the routes are connected in order, and the level comes back.
#
# Every plugin is the plugin given by --uri, flat, so unity gain: two in series read +0.00 dB and two in parallel
# +6.02 dB. A client of this script stands for the mixer strip at -20 dB and for a jalv plugin at -6 dB. The bypass rows
# need a plugin with a bell to set (omx-eq8: band 4 at 1 kHz, -6 dB, found by its stable symbols), so a member in the
# middle of a run that is bypassed shows in the level: -6.00 dB inline, +0.00 bypassed.
#
#   tools/ph_signal_path_proof.py --uri 'clap:/usr/lib/clap/omx-eq8.clap#org.openmixer.eq8'
#
# Needs a JACK server already running (a private one: JACK_DEFAULT_SERVER), plugin-hostd, python3-jack-client and
# numpy, and for notes_behind_effect omx-plugins-test (omx-test-sine-voice). The two zynthian_ scenarios run under a
# Zynthian's settings (require_pins 1, first-use pins): with no verdict nothing is grouped, and with the verdict the
# qualification sync writes (put on the pin here) the two run in one worker. It starts its own daemon on a free port, one per scenario in a process of its own, and stops them. Exit 1 when a
# level is not the right one.
#
# Copyright (C) 2026 Pau Aliagas <linuxnow@gmail.com>
# SPDX-License-Identifier: GPL-2.0-or-later

import argparse
import math
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FADER = 0.1  # -20 dB
OTHER = 0.5  # -6 dB
TONE = 0.1
SCENARIOS = ("series", "parallel", "serial_then_parallel", "strip", "other", "mixbus_series", "mixbus_parallel",
             "grouping_off", "edit_elsewhere", "notes_behind_effect", "zynthian_unqualified", "zynthian_qualified",
             "zynthian_packaged", "bypass_middle", "bypass_chain_added", "nudge_parallel", "nudge_strip", "mixbus_main")
NOTE_URI = "clap:/usr/libexec/omx-plugins/test/omx-test-sine-voice.clap#org.openmixer.test-sine-voice"
# The daemon's settings per scenario, besides state_root: a Zynthian's own (require_pins, first-use pins) for the two
# zynthian_ ones, grouping off for grouping_off, and grouping on for every other, since they measure grouped paths
CONF = {"grouping_off": ["require_pins 0", "grouping off"],
        "zynthian_unqualified": ["require_pins 1", "pin_mode first-use", "grouping on"],
        "zynthian_qualified": ["require_pins 1", "pin_mode first-use", "grouping on"],
        "zynthian_packaged": ["require_pins 1", "pin_mode first-use", "grouping on"]}
# The two zynthian_ rows that set the verdict themselves read no package pins (pins_dir an empty directory): a host
# whose packages pin the plugin, a Zynthian with omx-plugins, would otherwise answer them before the first add.
# zynthian_packaged reads the installed ones, as a Zynthian does.
NO_PACKAGE_PINS = ("zynthian_unqualified", "zynthian_qualified")


def db(x):
    return 20 * math.log10(x) if x > 1e-9 else -999.0


def scenario(name, uri, conf, daemon_bin):
    sys.path[:0] = [os.path.join(ROOT, "tests"), ROOT]
    import jack
    import numpy as np
    import test_plugin_hostd_signal_path as t
    ph = t.ph

    class probe:
        def __init__(self):
            self.c = jack.Client("probe", no_start_server=True)
            self.o = self.c.outports.register("src")
            self.i = self.c.inports.register("in_1")
            self.m = self.c.midi_outports.register("midi")
            self.note = False
            self.pos, self.peak = 0, 0.0
            self.c.set_process_callback(self.process)
            self.c.activate()

        def process(self, n):
            self.o.get_array()[:] = TONE * np.sin(2 * np.pi * 1000.0 * np.arange(self.pos, self.pos + n) / self.c.samplerate)
            self.pos += n
            self.peak = max(self.peak, float(np.abs(self.i.get_array()).max()))
            self.m.clear_buffer()
            if self.note:
                self.m.write_midi_event(0, (0x90, 60, 100))  # held: sent every cycle, the voice keeps sounding

    class gain_client:
        def __init__(self, name, gain, port_in="input_03a", port_out="output_03a"):
            self.c = jack.Client(name, no_start_server=True)
            i, o = self.c.inports.register(port_in), self.c.outports.register(port_out)
            self.c.set_process_callback(lambda n: o.get_array().__setitem__(slice(None), gain * i.get_array()))
            self.c.activate()

    class case(t.signal_path_case):
        def start(self):
            env = dict(self.saved_env)
            tmp = tempfile.mkdtemp()
            path = os.path.join(tmp, "plugin-hostd.conf")
            with open(path, "w") as f:
                settings = CONF.get(name, ["require_pins 0", "grouping on"]) + [f"pin_store {tmp}/pins"]
                if name in NO_PACKAGE_PINS:
                    os.makedirs(os.path.join(tmp, "no-package-pins"))
                    settings.append(f"pins_dir {tmp}/no-package-pins")
                f.write(f"state_root {tmp}\n" + "".join(f"{line}\n" for line in settings + list(conf)))
            self.daemon = subprocess.Popen([daemon_bin, "-n", "-p", str(self.port), "-c", path], env=env,
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(1.0)
            self.info["PH/E"] = dict(self.info["PH/Amp"], URL=uri, NAME="E")
            self.info["PH/N"] = dict(self.info["PH/Amp"], URL=NOTE_URI, NAME="N")  # an effect slot that takes notes
            self.pins = os.path.join(tmp, "pins")
            self.pr = probe()
            sys.modules["zynautoconnect"].get_ports = lambda name, **kw: self.pr.c.get_ports(name)

        def setUp(self):
            self.saved_env = dict(os.environ)
            super().setUp()

        def wire(self, chain):
            """What autoconnect does with the chain's routes, on one channel: the probe is the chain's input and output."""
            c = self.pr.c
            for dst, sources in chain.audio_routes.items():
                to = [self.pr.i] if isinstance(dst, int) else c.get_ports(dst, is_input=True, is_audio=True)
                for src in [s for s in sources if s != t.SEND] or ["probe:src"]:
                    frm = c.get_ports(src, is_output=True, is_audio=True)
                    if frm and to and to[0].name not in [p.name for p in c.get_all_connections(frm[0])]:
                        c.connect(frm[0], to[0])

        def wire_midi(self, chain):
            c = self.pr.c
            for dst in chain.midi_routes:
                to = c.get_ports(f"^{dst}:", is_input=True, is_midi=True)
                if to and to[0].name not in [p.name for p in c.get_all_connections(self.pr.m)]:
                    c.connect(self.pr.m, to[0])

        def pids(self):
            return {r.split(":")[-1]: r.split(":")[1] for r in ph.daemon.call("worker_list")[1].split()}

        def measure(self, chain, want, clients=None, also=True, why=""):
            if chain is not None:  # None: the row connected the probe itself
                self.wire(chain)
            time.sleep(1.5)
            self.pr.peak = 0.0
            time.sleep(1.0)
            got = db(self.pr.peak) - db(TONE)
            want_clients = clients
            clients = sorted({p.name.split(":")[0] for p in self.pr.c.get_ports() if p.name.startswith("effect_")})
            workers = ph.daemon.call("worker_list")[1]
            ok = abs(got - want) < 0.5 and also and (want_clients is None or len(clients) == want_clients)
            print(f"{'ok  ' if ok else 'FAIL'} {name}: {got:+.2f} dB, right answer {want:+.2f}; clients {clients}"
                  f"{'' if want_clients is None else f' (want {want_clients})'}; workers {workers}{why}", flush=True)
            # the daemon first: it would respawn a worker that goes before it
            with open(f"/proc/{self.daemon.pid}/task/{self.daemon.pid}/children") as f:
                workers = f.read().split()
            self.daemon.kill()
            self.daemon.wait()
            for pid in workers:
                os.kill(int(pid), signal.SIGKILL)
            time.sleep(0.5)
            os._exit(0 if ok else 1)

        def unwire(self):
            """Every connection of the probe, the plugins and the strips undone, as autoconnect drops the routes a
            move leaves behind"""
            c = self.pr.c
            for port in c.get_ports(is_output=True, is_audio=True):
                if port.name.split(":")[0] in ("probe", "zynmixer_chan", "zynmixer_bus") or port.name.startswith("effect_"):
                    for to in c.get_all_connections(port):
                        c.disconnect(port, to)

        def level(self, chain=None, wait=1.5):
            """The level the probe reads now, in dB over the tone, the chain's routes connected again first"""
            if chain is not None:
                self.unwire()
                self.wire(chain)
            time.sleep(wait)
            self.pr.peak = 0.0
            time.sleep(1.0)
            return db(self.pr.peak) - db(TONE)

        def stable(self, instance):
            """{stable symbol: parameter} of an instance, from its descriptor and param_info"""
            import json
            path = ph.daemon.call(f"descriptor_get {uri}")[1].strip().strip('"')
            out = {}
            for c in json.load(open(path)).get("controls", []):
                sym = str(c.get("symbol") or c.get("id"))
                try:
                    info = ph.daemon.call(f"param_info {instance} {sym}")[1].split()
                except ph.plugin_hostd_error:
                    continue  # a control that is no parameter (a meter, a port)
                if len(info) >= 7:
                    out[info[6]] = sym
            return out

        def bell(self, instance, gain=-6.0):
            """band 4 of omx-eq8 a bell at 1 kHz: the tone moves by `gain` through this member alone"""
            sym = self.stable(instance)
            for name, value in (("b4_type", 0), ("b4_freq", 1000), ("b4_q", 1), ("b4_gain", gain), ("b4_on", 1)):
                ph.daemon.call(f"param_set {instance} {sym[name]} {value}")

        def clients(self):
            return sorted({p.name.split(":")[0] for p in self.pr.c.get_ports() if p.name.startswith("effect_")})

        def plugins(self, chain, n):
            return [self.add(chain, "PH/E") for _ in range(n)]

        def mixbus(self):
            self.bus = gain_client("zynmixer_bus", 1.0)
            chain = self.chain()
            mr = self.strip(chain, "MR")
            chain.audio_slots.remove([mr])
            procs = self.plugins(chain, 2)
            chain.audio_slots.append([mr])
            return chain, procs

        def test_run(self):
            self.start()
            chain = self.chain()
            if name == "series":
                self.plugins(chain, 2)
                want = 0.0
            elif name == "parallel":
                self.parallel(chain, self.plugins(chain, 2)[1])
                want = 6.02
            elif name == "serial_then_parallel":
                self.parallel(chain, self.plugins(chain, 3)[2])
                want = 6.02
            elif name == "strip":
                self.fader = gain_client("zynmixer_chan", FADER)
                self.add(chain, "PH/E")
                self.strip(chain)
                self.add(chain, "PH/E")
                want = -20.0
            elif name == "other":
                self.other = gain_client("jalv_x", OTHER, "in", "out")
                self.add(chain, "PH/E")
                self.foreign(chain)
                self.add(chain, "PH/E")
                want = -6.02
            elif name == "grouping_off":
                self.plugins(chain, 2)
                time.sleep(1.0)
                self.measure(chain, 0.0, clients=2, why="; grouping off: each plugin in a worker of its own")
            elif name == "edit_elsewhere":
                self.fader = gain_client("zynmixer_chan", FADER)
                first = self.plugins(chain, 2)
                self.strip(chain)
                self.plugins(chain, 2)
                before = self.pids().get("1002,1003")
                for p in first:
                    self.remove(chain, p)
                time.sleep(1.0)
                after = self.pids().get("1002,1003")
                self.measure(chain, -20.0, also=before is not None and before == after,
                             why=f"; the run after the strip, worker {before} -> {after}")
            elif name == "notes_behind_effect":
                self.add(chain, "PH/E")
                n = self.add(chain, "PH/N")
                time.sleep(1.0)
                self.wire(chain)
                self.wire_midi(chain)
                time.sleep(1.5)
                self.pr.peak = 0.0
                time.sleep(1.0)
                silent = db(self.pr.peak)
                self.pr.note = True
                time.sleep(1.0)
                self.pr.peak = 0.0
                time.sleep(1.0)
                sounding = db(self.pr.peak)
                heard = sounding > silent + 6.0
                self.measure(chain, db(self.pr.peak) - db(TONE), also=heard,
                             why=f"; a note to the effect that takes notes: {silent:+.1f} -> {sounding:+.1f} dBFS, "
                                 f"its client {n.get_jackname()}")
            elif name == "zynthian_packaged":
                # what the installed package pins say decides: grouped when they pin the plugin in-process
                self.add(chain, "PH/E")
                self.add(chain, "PH/E")
                time.sleep(1.0)
                pinned = os.path.isdir("/usr/share/plugin-hostd/pins.d") and any(
                    uri in open(os.path.join("/usr/share/plugin-hostd/pins.d", f)).read() and "verdict=in-process" in
                    open(os.path.join("/usr/share/plugin-hostd/pins.d", f)).read()
                    for f in os.listdir("/usr/share/plugin-hostd/pins.d") if f.endswith(".pins"))
                self.measure(chain, 0.0, clients=1 if pinned else 2,
                             why=f"; package pins {'pin it in-process' if pinned else 'do not pin it'}; reasons {dict(ph.daemon.reasons)}")
            elif name in ("zynthian_unqualified", "zynthian_qualified"):
                a = self.add(chain, "PH/E")
                if name == "zynthian_qualified":
                    # the verdict the qualification sync writes, put on the pin the first add recorded
                    time.sleep(0.5)
                    with open(self.pins) as f:
                        pin = [line for line in f.read().splitlines() if uri in line][-1].split(" verdict=")[0]
                    line = pin if pin.startswith("pin_set ") else f"pin_set {pin}"
                    ph.daemon.call(f"{line} verdict=in-process")
                self.add(chain, "PH/E")
                time.sleep(1.0)
                self.measure(chain, 0.0, clients=1 if name == "zynthian_qualified" else 2,
                             why=f"; reasons {dict(ph.daemon.reasons)}")
            elif name == "bypass_middle":
                # a member in the middle of a run bypassed by its processor's bypass control (the one every audio
                # processor shows), the run one client, its level inline, bypassed and inline again
                procs = self.plugins(chain, 3)
                time.sleep(1.0)
                self.bell(procs[1].engine.instance)
                bypass = procs[1].get_bypass_zctrl()
                steps = [("inline", self.level(chain), -6.0)]
                bypass.set_value(1)
                steps.append(("bypassed", self.level(), 0.0))
                bypass.set_value(0)
                steps.append(("inline again", self.level(), -6.0))
                good = all(abs(got - want) < 0.5 for _, got, want in steps)
                self.measure(chain, -6.0, clients=1, also=good,
                             why="; " + ", ".join(f"{what} {got:+.2f} (want {want:+.2f})" for what, got, want in steps))
            elif name == "bypass_chain_added":
                # the same, on members added straight into a chain (plugin-hostd#348, chain_expect): the daemon is
                # driven as the snapshot load will drive it, no Zynthian chain, the probe on the head's client
                ids = (901, 902, 903)
                answers = [ph.daemon.call(f"add {uri} {i} chain:77")[0] for i in ids]
                time.sleep(1.0)
                try:
                    ph.daemon.call("chain_get 77")
                except ph.plugin_hostd_error as e:
                    self.measure(None, -6.0, also=False, why=f"; no chain 77 after the adds ({e}): this daemon reads "
                                 f"chain:77 as a client name, it predates plugin-hostd#348")
                c = self.pr.c
                c.connect(self.pr.o, "effect_901:in_1")
                c.connect("effect_901:out_1", self.pr.i)
                self.bell(902)
                steps = [("inline", self.level(), -6.0)]
                ph.daemon.call("bypass 902 1")
                steps.append(("bypassed", self.level(), 0.0))
                ph.daemon.call("bypass 902 0")
                steps.append(("inline again", self.level(), -6.0))
                good = all(abs(got - want) < 0.5 for _, got, want in steps) and self.clients() == ["effect_901"]
                self.measure(None, -6.0, clients=1, also=good, why=f"; adds {answers}; " + ", ".join(
                    f"{what} {got:+.2f} (want {want:+.2f})" for what, got, want in steps))
            elif name == "nudge_parallel":
                # a member of a run nudged into the slot before it (parallel), then out again
                procs = self.plugins(chain, 3)
                time.sleep(1.0)
                steps = [("run of three", self.level(chain), 0.0, self.clients())]
                chain.nudge_processor(procs[1], True)
                time.sleep(1.0)
                steps.append(("second nudged into a parallel slot", self.level(chain), 6.02, self.clients()))
                chain.nudge_processor(procs[1], False)
                time.sleep(1.0)
                steps.append(("nudged out again", self.level(chain), 0.0, self.clients()))
                good = all(abs(got - want) < 0.5 for _, got, want, _ in steps)
                self.unwire()
                self.measure(chain, 0.0, clients=1, also=good, why="; " + ", ".join(
                    f"{what} {got:+.2f} (want {want:+.2f}, clients {cl})" for what, got, want, cl in steps))
            elif name == "nudge_strip":
                # the last member of a run nudged across the mixer strip (post fader) and back (pre fader)
                self.fader = gain_client("zynmixer_chan", FADER)
                procs = self.plugins(chain, 3)
                self.strip(chain)
                time.sleep(1.0)
                steps = [("pre fader", self.level(chain), -20.0, self.clients(), 1)]
                chain.nudge_processor(procs[2], False)
                time.sleep(1.5)
                steps.append(("third nudged after the strip (post fader)", self.level(chain), -20.0, self.clients(), 2))
                chain.nudge_processor(procs[2], True)
                time.sleep(1.5)
                steps.append(("nudged back before it (pre fader)", self.level(chain), -20.0, self.clients(), 1))
                good = all(abs(got - want) < 0.5 and len(cl) == n for _, got, want, cl, n in steps)
                self.unwire()
                self.measure(chain, -20.0, clients=1, also=good, why="; " + ", ".join(
                    f"{what} {got:+.2f} (want {want:+.2f}, clients {cl}, want {n})" for what, got, want, cl, n in steps))
            elif name == "mixbus_main":
                # a run on chain 0, the main mixbus: chain numbers are anything but 0 elsewhere
                self.bus = gain_client("zynmixer_bus", 1.0)
                chain = self.chain(0)
                chain.audio_out = [0]
                mr = self.strip(chain, "MR")  # the stand-in bus client has the ports of mixer channel 3
                chain.audio_slots.remove([mr])
                self.plugins(chain, 2)
                chain.audio_slots.append([mr])
                chain.rebuild_graph()
                time.sleep(1.0)
                self.measure(chain, 0.0, clients=1, why=f"; runs {ph.daemon.runs.get(0)}")
            elif name == "mixbus_series":
                chain, _ = self.mixbus()
                chain.rebuild_graph()
                want = 0.0
            else:
                chain, procs = self.mixbus()
                self.parallel(chain, procs[1])
                want = 6.02
            time.sleep(1.0)
            self.measure(chain, want)

    unittest.TextTestRunner(verbosity=0).run(case("test_run"))
    os._exit(2)


def main():
    ap = argparse.ArgumentParser(description="a chain's signal at JACK ports")
    ap.add_argument("--uri", required=True, help="a plugin that is flat by default, as add names it")
    ap.add_argument("--conf", action="append", default=[], help="a line of the daemon's settings: --conf 'grouping on'")
    ap.add_argument("--daemon", default="/usr/bin/plugin-hostd")
    ap.add_argument("--one", help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.one:
        scenario(args.one, args.uri, args.conf, args.daemon)
    failed = 0
    for name in SCENARIOS:
        cmd = [sys.executable, __file__, "--uri", args.uri, "--daemon", args.daemon, "--one", name]
        for line in args.conf:
            cmd += ["--conf", line]
        failed |= subprocess.run(cmd, stderr=subprocess.DEVNULL, timeout=60).returncode != 0
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
