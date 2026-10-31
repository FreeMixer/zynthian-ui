#!/usr/bin/env python3
# What a chain does to its signal, measured at JACK ports. The engine, the chain, its routes, plugin-hostd and its
# worker are the real ones; a tone goes in, the routes are connected in order, and the level comes back.
#
# Every plugin is the plugin given by --uri, flat, so unity gain: two in series read +0.00 dB and two in parallel
# +6.02 dB. A client of this script stands for the mixer strip at -20 dB and for a jalv plugin at -6 dB.
#
#   tools/ph_signal_path_proof.py --uri 'clap:/usr/lib/clap/omx-eq8.clap#org.openmixer.eq8'
#
# Needs a JACK server already running (a private one: JACK_DEFAULT_SERVER), plugin-hostd, python3-jack-client and
# numpy. It starts its own daemon on a free port, one per scenario in a process of its own, and stops them. Exit 1 when a
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
SCENARIOS = ("series", "parallel", "serial_then_parallel", "strip", "other", "mixbus_series", "mixbus_parallel")


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
            self.pos, self.peak = 0, 0.0
            self.c.set_process_callback(self.process)
            self.c.activate()

        def process(self, n):
            self.o.get_array()[:] = TONE * np.sin(2 * np.pi * 1000.0 * np.arange(self.pos, self.pos + n) / self.c.samplerate)
            self.pos += n
            self.peak = max(self.peak, float(np.abs(self.i.get_array()).max()))

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
                f.write(f"state_root {tmp}\nrequire_pins 0\n" + "".join(f"{line}\n" for line in conf))
            self.daemon = subprocess.Popen([daemon_bin, "-n", "-p", str(self.port), "-c", path], env=env,
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(1.0)
            self.info["PH/E"] = dict(self.info["PH/Amp"], URL=uri, NAME="E")
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

        def measure(self, chain, want):
            self.wire(chain)
            time.sleep(1.5)
            self.pr.peak = 0.0
            time.sleep(1.0)
            got = db(self.pr.peak) - db(TONE)
            clients = sorted({p.name.split(":")[0] for p in self.pr.c.get_ports() if p.name.startswith("effect_")})
            workers = ph.daemon.call("worker_list")[1]
            ok = abs(got - want) < 0.5
            print(f"{'ok  ' if ok else 'FAIL'} {name}: {got:+.2f} dB, right answer {want:+.2f}; clients {clients}; workers {workers}",
                  flush=True)
            # the daemon first: it would respawn a worker that goes before it
            with open(f"/proc/{self.daemon.pid}/task/{self.daemon.pid}/children") as f:
                workers = f.read().split()
            self.daemon.kill()
            self.daemon.wait()
            for pid in workers:
                os.kill(int(pid), signal.SIGKILL)
            time.sleep(0.5)
            os._exit(0 if ok else 1)

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
