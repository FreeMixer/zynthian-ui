#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# A stand-in for plugin-hostd on its command port (0.12 verbs; plan, plan_get and plan_clear of 0.13 on request): NUL-terminated lines, one reply each,
# the replies and refusal codes its README documents. It runs in a thread of a test, or as a
# program started the way the engine starts the daemon: fake_plugin_hostd.py -n -p <port> [-c <conf>].
# Every line it takes is appended to $FAKE_HOSTD_JOURNAL when that is set.
#
# Copyright (C) 2026 Pau Aliagas <linuxnow@gmail.com>
# SPDX-License-Identifier: GPL-2.0-or-later

import os
import sys
import json
import socket
import threading
import time

# The plugins it knows: each parameter (unit, scale, min, max, default), whether param_info
# answers for it, the presets it lists and what loading one sets.
PLUGINS = {
    "urn:test:amp": {"params": {"gain": ("dB", "linear", -20.0, 20.0, 0.0)}, "info": True, "presets": None},
    "urn:test:nopin": {"params": {"level": ("dB", "linear", -60.0, 6.0, 0.0)}, "info": True, "presets": None},
    "clap:/usr/lib/clap/test.clap#org.test.synth": {
        "params": {"1": ("-", "linear", 0.0, 1.0, 0.5), "2": ("-", "stepped", 0.0, 3.0, 0.0)},
        "info": False,  # omx-clap-host 0.8 answers -511 for every parameter
        "presets": [("factory:warm", "Warm Pad", {"1": 0.25}), ("factory:bright", "Bright Lead", {"1": 0.75})]},
    "clap:/usr/lib/clap/lsp-plugins.clap#in.lsp-plug.limiter_mono": {
        "params": {"1574856808": ("-", "linear", -48.0, 0.0, 0.0), "2849352001": ("-", "stepped", 0.0, 1.0, 1.0)}, "info": False, "presets": None},
    "urn:test:big": {"params": {f"p{i}": ("Hz", "linear", 0.0, 100.0, 10.0) for i in range(10)},
                     "info": False, "presets": None},
}


class fake_plugin_hostd:

    def __init__(self, port, journal=None, descriptors=None, mod_host=False, planner=False, grouping=False,
                 group_max=8, verdicts=None, estimator=False):
        self.port = port
        self.journal = journal
        self.descriptors = descriptors or {}
        self.mod_host = mod_host  # answer like mod-host: no plugin-hostd verbs
        self.instances = {}  # instance => uri
        self.params = {}  # instance => {symbol: value}
        self.bypass = {}
        self.chains = {}  # chain => tuple of instances
        self.planner = planner  # answer plan, plan_get and plan_clear; else -902 like plugin-hostd 0.12
        self.grouping = grouping  # the setting: whether a plan moves anything
        self.group_max = group_max
        self.estimator = estimator  # answer estimate (plugin-hostd 0.14); else -902 like the daemons before it
        self.verdicts = verdicts or {}  # uri => pin verdict word; none is "in-process"
        self.plans = {}  # chain => (segments, {instance: reason}) as the last plan answered it
        self.lines = []
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", port))
        self.listener.listen(4)
        self.conn = None
        self.running = True

    # ---------------------------------------------------------------------------

    def serve(self):
        while self.running:
            try:
                self.conn, _ = self.listener.accept()
            except OSError:
                return
            buf = b""
            while self.running:
                try:
                    chunk = self.conn.recv(4096)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                while b"\0" in buf:
                    line, _, buf = buf.partition(b"\0")
                    reply = self.handle(line.decode())
                    try:
                        self.conn.sendall(reply.encode() + b"\0")
                    except OSError:
                        break
            self.conn.close()
            self.conn = None

    def start(self):
        threading.Thread(target=self.serve, daemon=True).start()
        return self

    def stop(self):
        """Die: close the port and the connection, as a killed daemon would."""
        self.running = False
        for s in (self.listener, self.conn):
            if s:
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                s.close()

    # ---------------------------------------------------------------------------

    def handle(self, line):
        self.lines.append(line)
        if self.journal:
            with open(self.journal, "a") as f:
                f.write(f"{os.getpid()} {line}\n")
        w = line.split()
        verb, args = w[0], w[1:]
        try:
            method = getattr(self, "verb_" + verb, None)
            if method is None or (self.mod_host and verb in ("worker_list", "chain", "chain_get", "chain_clear",
                                                              "descriptor_get", "preset_list", "preset_get")) \
                    or (not self.planner and verb.startswith("plan")) \
                    or (not self.estimator and verb == "estimate"):
                return "resp -902"
            return method(*args)
        except (TypeError, ValueError, KeyError, IndexError):
            return "resp -902"

    def plugin(self, instance):
        return PLUGINS[self.instances[int(instance)]]

    def row(self, members):
        """The worker row that runs these members: omx-clap-host's clap row for CLAP ones, its lv2-clap row
        when LV2 ones are among them, mod-host's lv2 row for LV2 ones alone."""
        clap = [self.instances[m].startswith("clap:") for m in members]
        return "clap" if all(clap) else "lv2-clap" if any(clap) else "lv2"

    def chain_worker(self, chain):
        return f"w{100 + chain}"

    def verb_worker_list(self):
        held = {m for members in self.chains.values() for m in members}
        held |= {m for segments, _ in self.applied_plans().values() for s in segments if len(s) > 1 for m in s}
        records = [f"w{i}:{os.getpid()}:{self.row([i])}:up:own:{i}" for i in sorted(self.instances) if i not in held]
        records += [f"{self.chain_worker(c)}:{os.getpid()}:{self.row(m)}:up:chain:{c}:{','.join(map(str, m))}"
                    for c, m in sorted(self.chains.items()) if m]
        records += [f"w{300 + 10 * c + k}:{os.getpid()}:{self.row(s)}:up:chain:{c}.{k}:{','.join(map(str, s))}"
                    for c, (segments, _) in sorted(self.applied_plans().items())
                    for k, s in enumerate(segments) if len(s) > 1]
        return f"resp {len(records)}" + "".join(f" {r}" for r in records)

    def applied_plans(self):
        """The plans whose segments run: none while the setting grouping is off."""
        return self.plans if self.grouping else {}

    # What the qualification records hold per plugin at 48 kHz in blocks of 256: latency frames, CPU %, RAM bytes
    ESTIMATES = {"urn:test:amp": (0, 1.5, 4000000), "urn:test:nopin": (64, 2.25, 8500000)}

    def verb_estimate(self, rate, quantum, *refs):
        """The chain's figures from the records: latencies add, CPU adds, RAM adds, one worker per chain; a
        plugin without a record counts as unknown and adds nothing."""
        if not refs:
            return "resp -902"
        known = [self.ESTIMATES[r] for r in refs if r in self.ESTIMATES]
        return "resp 0 %d %.2f %d 1 %d\n%s" % (sum(k[0] for k in known), sum(k[1] for k in known),
                                                 sum(k[2] for k in known), len(refs) - len(known),
                                                 "\n".join(f"{r} rate={rate} quantum={quantum}" for r in refs))

    def verb_plan(self, chain, *members):
        """The planner of plugin-hostd 0.13, reduced to what the pin verdicts and group_max decide: a member
        whose pin is isolated or has no verdict stays alone with that reason, the others join the open segment
        until it holds group_max; a segment of one that only its neighbours made alone says no-neighbour."""
        chain = int(chain)
        members = [int(m) for m in members]
        if not members:
            return "resp -902"
        if any(m not in self.instances for m in members):
            return "resp -518"
        if len(set(members)) != len(members):
            return "resp -519"
        if any(m in held for c, held in self.chains.items() for m in members) or \
                any(m in s for c, (segments, _) in self.plans.items() if c != chain for s in segments for m in s):
            return "resp -520"
        segments, reasons, open_segment = [], {}, []
        for m in members:
            verdict = self.verdicts.get(self.instances[m], "in-process")
            if verdict != "in-process":
                reasons[m] = "verdict-isolated" if verdict == "isolated" else "no-verdict"
                if open_segment:
                    segments.append(open_segment)
                    open_segment = []
                segments.append([m])
                continue
            if len(open_segment) == self.group_max:
                segments.append(open_segment)
                open_segment = []
                reasons[m] = "group-full"
            open_segment.append(m)
        if open_segment:
            segments.append(open_segment)
        for s in segments:
            if len(s) == 1 and s[0] not in reasons:
                reasons[s[0]] = "no-neighbour"
        if not self.grouping:
            for s in segments:
                if len(s) > 1:
                    reasons.update({m: "grouping-off" for m in s})
        self.plans[chain] = (segments, reasons)
        return "resp 0 " + ";".join(",".join(map(str, s)) for s in segments)

    def verb_plan_get(self, chain):
        plan = self.plans.get(int(chain))
        if plan is None:
            return "resp -532"
        segments, reasons = plan
        listed = ",".join(f"{m}:{r}" for m, r in sorted(reasons.items())) or "-"
        return "resp 0 " + ";".join(",".join(map(str, s)) for s in segments) + " " + listed

    def verb_plan_clear(self, chain):
        return "resp 0" if self.plans.pop(int(chain), None) is not None else "resp -532"

    def verb_add(self, uri, instance, *rest):
        n = int(instance)
        if n in self.instances:
            return "resp -2"
        if uri not in PLUGINS:
            return "resp -101"
        self.instances[n] = uri
        self.params[n] = {s: p[4] for s, p in PLUGINS[uri]["params"].items()}
        return f"resp {n}"

    def verb_remove(self, instance):
        n = int(instance)
        if n not in self.instances:
            return "resp -3"
        del self.instances[n], self.params[n]
        for chain, members in list(self.chains.items()):
            if n in members:
                self.chains[chain] = tuple(m for m in members if m != n)
        for chain, (segments, reasons) in list(self.plans.items()):
            self.plans[chain] = ([[m for m in s if m != n] for s in segments if s != [n]], reasons)
        return "resp 0"

    def verb_bypass(self, instance, value):
        self.bypass[int(instance)] = int(value)
        return "resp 0"

    def verb_param_set(self, instance, symbol, value):
        n = int(instance)
        if symbol not in self.params[n]:
            return "resp -103"
        self.params[n][symbol] = float(value)
        return "resp 0"

    def verb_param_get(self, instance, symbol):
        n = int(instance)
        if symbol not in self.params[n]:
            return "resp -103"
        return f"resp 0 {self.params[n][symbol]:.4f}"

    def verb_param_info(self, instance, symbol):
        plugin = self.plugin(instance)
        if symbol not in plugin["params"]:
            return "resp -103"
        if not plugin["info"]:
            return "resp -511"
        unit, scale, lo, hi, default = plugin["params"][symbol]
        return f"resp 0 {unit} {scale} {lo} {hi} {default} 0 {symbol}"

    def verb_preset_list(self, instance):
        presets = self.plugin(instance)["presets"]
        return "resp -515" if presets is None else f"resp {len(presets)}"

    def verb_preset_get(self, instance, index):
        presets = self.plugin(instance)["presets"]
        if presets is None:
            return "resp -515"
        if not 0 <= int(index) < len(presets):
            return "resp -902"
        key, name, _ = presets[int(index)]
        return f"resp 0 {key} {name}"

    def verb_preset_load(self, instance, key):
        for k, _, values in self.plugin(instance)["presets"] or []:
            if k == key:
                self.params[int(instance)].update(values)
                return "resp 0"
        return "resp -523"

    def verb_chain(self, chain, *members):
        chain = int(chain)
        members = tuple(int(m) for m in members)
        if not members:
            return "resp -902"
        if any(m not in self.instances for m in members):
            return "resp -518"
        if len(set(members)) != len(members):
            return "resp -519"
        for other, held in self.chains.items():
            if other != chain and set(held) & set(members):
                return "resp -520"
        if any(self.instances[m] == "urn:test:nopin" for m in members):
            return "resp -501"
        self.chains[chain] = members
        return "resp 0"

    def verb_chain_get(self, chain):
        members = self.chains.get(int(chain))
        if members is None:
            return "resp -522"
        listed = ",".join(map(str, members)) or "-"
        return f"resp 0 {self.chain_worker(int(chain))} {listed} {listed}"

    def verb_chain_clear(self, chain):
        if self.chains.pop(int(chain), None) is None:
            return "resp -522"
        return "resp 0"

    def verb_descriptor_get(self, uri):
        if uri not in self.descriptors:
            return "resp -529"
        if self.descriptors[uri] is None:  # a scan that wrote nothing
            return "resp -531"
        return f'resp 0 "{self.descriptors[uri]}"'

    def verb_state_save(self, directory):
        for n, params in self.params.items():
            with open(os.path.join(directory, f"effect_{n}.fakestate"), "w") as f:
                json.dump(params, f)
        return "resp 0"

    def verb_state_load(self, directory):
        for name in os.listdir(directory):
            n = int(name.split("_", 1)[1].split(".", 1)[0])
            if n in self.params:
                with open(os.path.join(directory, name)) as f:
                    self.params[n].update(json.load(f))
        return "resp 0"

    def verb_quit(self):
        threading.Timer(0.05, self.stop).start()
        return "resp 0"


def main(argv):
    port = int(argv[argv.index("-p") + 1])
    descriptors = {}
    if os.environ.get("FAKE_HOSTD_DESCRIPTORS"):
        with open(os.environ["FAKE_HOSTD_DESCRIPTORS"]) as f:
            descriptors = json.load(f)
    daemon = fake_plugin_hostd(port, os.environ.get("FAKE_HOSTD_JOURNAL"), descriptors)
    time.sleep(float(os.environ.get("FAKE_HOSTD_READY_DELAY", "0")))  # listening, not yet ready
    print("plugin-hostd ready!", flush=True)
    daemon.serve()


if __name__ == "__main__":
    main(sys.argv)
