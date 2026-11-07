#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# A stand-in for plugin-hostd on its command port (0.12 verbs; plan, plan_get and plan_clear of 0.13 on request): NUL-terminated lines, one reply each,
# the replies and refusal codes its README documents. Protocol 0.3 of plugin-hostd-protocol (hello with tags and
# error words, param_get_many, param_set_many) on request: hello=True; without it the fake is an old daemon. It runs in a thread of a test, or as a
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
import importlib.util

# the words of a line and the key of a pin are the engine's own module's (the grammar of plugin-hostd-protocol), loaded
# by path: the fake also runs as a program, without the zyngine package
_spec = importlib.util.spec_from_file_location(
    "phd_words", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "zyngine", "zynthian_plugin_hostd_protocol.py"))
grammar = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(grammar)

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
    "clap:/usr/lib/clap/test.clap#org.test.remote": {
        "params": {str(i): ("-", "linear", 0.0, 1.0, 0.5) for i in range(1, 13)}, "info": False, "presets": None,
        "remote": [("Global", "Macros", ["3", "1", "-", "2"]), ("Scene A", "Mixer", ["12", "1"])]},
    "clap:/usr/lib/clap/Surge XT.clap#org.test.surge": {
        "params": {"1": ("-", "linear", 0.0, 1.0, 0.5)}, "info": False, "presets": None},
    "clap:/usr/lib/clap/Two  Spaces.clap#org.test.two": {
        "params": {"1": ("-", "linear", 0.0, 1.0, 0.5)}, "info": False, "presets": None},
    # plugins with a patch property that names a file: the property's URI is the plugin's own, read from its TTL
    "urn:test:sampler": {"params": {"gain": ("dB", "linear", -20.0, 20.0, 0.0)}, "info": True, "presets": None,
                         "patch": ("http://sampler.test/lv2#P101_SAMPLE_FILE",)},
    "urn:test:kit": {"params": {"gain": ("dB", "linear", -20.0, 20.0, 0.0)}, "info": True, "presets": None,
                     "patch": ("http://kit.test/lv2#KIT_FILE", "http://kit.test/lv2#TUNING_FILE")},
    "urn:test:big": {"params": {f"p{i}": ("Hz", "linear", 0.0, 100.0, 10.0) for i in range(10)},
                     "info": False, "presets": None},
}


class fake_plugin_hostd:

    def __init__(self, port, journal=None, descriptors=None, mod_host=False, planner=False, grouping=False,
                 group_max=8, verdicts=None, estimator=False, hello=False, bulk=False, stale_tags=False,
                 hello_reply="resp -902"):
        self.port = port
        self.journal = journal
        self.descriptors = descriptors or {}
        self.mod_host = mod_host  # answer like mod-host: no plugin-hostd verbs
        self.instances = {}  # instance => uri
        self.params = {}  # instance => {symbol: value}
        self.patches = {}  # instance => {property uri: the file the plugin was given}
        self.bypass = {}
        self.windows = set()  # instances whose own window is shown
        self.chains = {}  # chain => tuple of instances
        self.planner = planner  # answer plan, plan_get and plan_clear; else -902 like plugin-hostd 0.12
        self.grouping = grouping  # the setting: whether a plan moves anything
        self.group_max = group_max
        self.estimator = estimator  # answer estimate (plugin-hostd 0.14); else -902 like the daemons before it
        self.verdicts = verdicts or {}  # uri => pin verdict word; none is "in-process"
        self.plans = {}  # chain => (segments, {instance: reason}) as the last plan answered it
        self.hello = hello  # answer hello, protocol 0.3; else an old daemon, which answers hello_reply
        self.hello_reply = hello_reply  # what an old daemon says to hello: not found, as mod-host does, or the refusal
        self.bulk = bulk  # answer param_get_many and param_set_many; else -902 like a daemon that does not serve them
        self.stale_tags = stale_tags  # send a reply of a tag nobody waits for before each tagged reply
        self.granted = set()  # the extensions this connection was granted
        self.connections = 0
        self.wire = []  # every line as it came, tag included
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
            self.connections += 1
            self.granted = set()
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
                    try:
                        for reply in self.answer(line.decode()):
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

    # The reply codes that carry a word in a session granted errors, the ones of plugin-hostd-protocol's reply_codes
    WORDS = {-1: "reply.host.instance-invalid", -3: "reply.host.instance-non-exists",
             -103: "reply.host.param-symbol-invalid", -511: "reply.host.no-param-contract",
             -522: "reply.plugin-hostd.no-such-chain", -529: "reply.plugin-hostd.descriptor-absent",
             -901: "reply.host.memory-allocation", -902: "reply.host.invalid-operation"}

    def answer(self, wire):
        """The lines sent back for one line of the wire: with tags granted the tag is taken off the request and put
        on the reply; with errors granted a refusal that is a number alone is followed by its word."""
        self.wire.append(wire)
        tag = None
        if "tags" in self.granted and wire.startswith("@"):
            tag, _, wire = wire.partition(" ")
        reply = self.handle(wire)
        words = reply.split()
        if "errors" in self.granted and len(words) == 2 and words[0] == "resp" and int(words[1]) in self.WORDS:
            reply += " " + self.WORDS[int(words[1])]
        if tag is None:
            return [reply]
        return ([f"@stale {reply}"] if self.stale_tags else []) + [f"{tag} {reply}"]

    def verb_hello(self, version, *extensions):
        if not self.hello:
            return self.hello_reply
        if not version.isdigit() or int(version) < 1:
            return "resp -902"
        self.granted = {e for e in extensions if e in ("tags", "errors", "seq")}
        return "resp 0 1" + "".join(f" {e}" for e in ("tags", "errors", "seq") if e in self.granted)

    def verb_param_get_many(self, instance, *symbols):
        n = int(instance)
        if not self.bulk or not symbols:
            return "resp -902"
        if n not in self.instances:
            return "resp -3"
        if any(s not in self.params[n] for s in symbols):
            return "resp -103"
        return "resp 0" + "".join(f" {self.params[n][s]:.4f}" for s in symbols)

    def verb_param_set_many(self, instance, *pairs):
        """All or nothing: every pair is checked before one is set, the first refused answers its position."""
        n = int(instance)
        if not self.bulk:
            return "resp -902"
        if n not in self.instances:
            return "resp -3"
        if not pairs or len(pairs) % 2:
            return f"resp -902 {len(pairs) // 2 + 1}"
        for position, (symbol, value) in enumerate(zip(pairs[::2], pairs[1::2]), 1):
            if symbol not in self.params[n]:
                return f"resp -103 {position}"
            try:
                float(value)
            except ValueError:
                return f"resp -902 {position}"
        for symbol, value in zip(pairs[::2], pairs[1::2]):
            self.params[n][symbol] = float(value)
        return "resp 0"

    def handle(self, line):
        self.lines.append(line)
        if self.journal:
            with open(self.journal, "a") as f:
                f.write(f"{os.getpid()} {line}\n")
        w = grammar.words(line)
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
        records = [f"w{i}:{os.getpid()}:{self.row([i])}:up:1:qualified:own:{i}" for i in sorted(self.instances) if i not in held]
        records += [f"{self.chain_worker(c)}:{os.getpid()}:{self.row(m)}:up:1:qualified:chain:{c}:{','.join(map(str, m))}"
                    for c, m in sorted(self.chains.items()) if m]
        records += [f"w{300 + 10 * c + k}:{os.getpid()}:{self.row(s)}:up:1:qualified:chain:{c}.{k}:{','.join(map(str, s))}"
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
        refs = tuple(grammar.pin_key(r) for r in refs)
        known = [self.ESTIMATES.get(r) for r in refs]
        have = [k for k in known if k]
        lost = ",".join(f"{i}:-533" for i, k in enumerate(known) if not k) or "-"
        ram = str(sum(k[2] for k in have)) if len(have) == len(refs) else "-"
        return "resp 0 %d %.2f %s 1 %d %s" % (sum(k[0] for k in have), sum(k[1] for k in have), ram,
                                              len(refs) - len(have), lost)

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
                any(m in s for c, (segments, _) in self.plans.items() if c != chain for s in segments for m in members):
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
        return "resp 0 " + ";".join(",".join(map(str, s)) for s in segments) + " " + \
            ";".join(",".join("-" for _ in s) for s in segments)

    def verb_plan_get(self, chain):
        plan = self.plans.get(int(chain))
        if plan is None:
            return "resp -532"
        segments, reasons = plan
        listed = ",".join(f"{m}:{r}" for m, r in sorted(reasons.items())) or "-"
        return "resp 0 " + ";".join(",".join(map(str, s)) for s in segments) + " " + listed + " " + \
            ";".join(",".join("-" for _ in s) for s in segments)

    def verb_plan_clear(self, chain):
        return "resp 0" if self.plans.pop(int(chain), None) is not None else "resp -532"

    # plugins whose pin carries verdict=refused: their add answers -516 until pin_accept takes them
    REFUSED = set()

    def verb_pin_accept(self, uri):
        uri = grammar.pin_key(uri)
        self.accepted = getattr(self, "accepted", set()) | {uri}
        return "resp 0"

    def verb_pin_clear(self, uri):
        """The user's choice taken back (or every one, with all), and the pin with it: as the daemon does, a refused pin
        of the pin_file is forgotten too, until pin_reload reads the file again."""
        keys = set(PLUGINS) if uri == "all" else {grammar.pin_key(uri)}
        self.accepted = getattr(self, "accepted", set()) - keys
        self.cleared = getattr(self, "cleared", set()) | keys
        return "resp 0"

    def verb_pin_reload(self):
        self.cleared = set()
        return "resp 0"

    def verb_add(self, uri, instance, *rest):
        uri = grammar.pin_key(uri)
        n = int(instance)
        if n in self.instances:
            return "resp -2"
        if uri not in PLUGINS:
            return "resp -101"
        if uri in self.REFUSED and uri not in getattr(self, "accepted", set()) and uri not in getattr(self, "cleared", set()):
            return "resp -516"
        self.instances[n] = uri
        self.params[n] = {s: p[4] for s, p in PLUGINS[uri]["params"].items()}
        self.patches[n] = {}
        return f"resp {n}"

    def verb_remove(self, instance):
        n = int(instance)
        if n not in self.instances:
            return "resp -3"
        del self.instances[n], self.params[n], self.patches[n]
        for chain, members in list(self.chains.items()):
            if n in members:
                self.chains[chain] = tuple(m for m in members if m != n)
        for chain, (segments, reasons) in list(self.plans.items()):
            self.plans[chain] = ([[m for m in s if m != n] for s in segments if s != [n]], reasons)
        return "resp 0"

    def verb_show_external_ui(self, instance):
        if int(instance) not in self.instances:
            return "resp -3"
        self.windows.add(int(instance))
        return "resp 0"

    def verb_hide_external_ui(self, instance):
        if int(instance) not in self.instances:
            return "resp -3"
        self.windows.discard(int(instance))
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

    def verb_remote_pages(self, instance):
        return f"resp {len(self.plugin(instance).get('remote', []))}"

    def verb_remote_page_get(self, instance, page):
        section, name, slots = self.plugin(instance)["remote"][int(page)]
        slots = list(slots) + ["-"] * (8 - len(slots))
        return f'resp 0 {2054 + int(page)} "{section}" "{name}" {" ".join(slots)}'

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

    def verb_preset_save(self, instance, path, *name):
        # what omx-clap-host writes for preset_save <N> <file> <name>: the file, its name the rest of the line (one
        # quoted word as sent), its parameters the instance's values; a preset file there keeps its other members
        n = int(instance)
        if not path.startswith("/") or not name:
            return "resp -104 PRESET_INVALID"
        doc = {}
        if os.path.exists(path):
            with open(path) as f:
                doc = json.load(f)
        doc.update({"format": "org.openmixer.preset/2", "plugin": {"id": self.instances[n], "version": "1",
                    "fingerprint": "omx-layout/1:" + "0" * 64}, "name": " ".join(name), "carries": "params",
                    "params": [{"symbol": s, "unit": "", "value": v} for s, v in self.params[n].items()]})
        with open(path, "w") as f:
            json.dump(doc, f)
        return "resp 0"

    def verb_preset_load(self, instance, key):
        if key.startswith("/"):
            with open(key) as f:
                doc = json.load(f)
            if doc["plugin"]["id"] != self.instances[int(instance)]:
                return "resp -104 PRESET_PLUGIN_CHANGED"
            self.params[int(instance)].update({p["symbol"]: p["value"] for p in doc["params"]})
            return "resp 0"
        for k, _, values in self.plugin(instance)["presets"] or []:
            if k == key:
                self.params[int(instance)].update(values)
                return "resp 0"
        return "resp -523"

    NOTES = ("clap:/usr/lib/clap/test.clap#org.test.synth",)  # the plugins that take notes

    def verb_note_input(self, instance):
        """plugin-hostd 0.15 with omx-clap-host: midi_in for a plugin that takes notes, - for one that does not."""
        n = int(instance)
        if n not in self.instances:
            return "resp -3"
        return "resp 0 midi_in" if self.instances[n] in self.NOTES else "resp 0 -"

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

    def verb_patch_set(self, instance, prop, value):
        """patch_set <instance> <property uri> <value>: a path with a space is one word only in quotation marks, so an
        unquoted one is too many arguments, which the fake answers -902 as the daemon's grammar refuses it."""
        n = int(instance)
        if n not in self.instances:
            return "resp -3"
        if prop not in PLUGINS[self.instances[n]].get("patch", ()):
            return "resp -902"
        self.patches[n][prop] = value
        return "resp 0"

    def verb_patch_get(self, instance, prop):
        n = int(instance)
        if n not in self.instances:
            return "resp -3"
        if prop not in PLUGINS[self.instances[n]].get("patch", ()):
            return "resp -902"
        return "resp 0 " + self.patches[n][prop] if prop in self.patches[n] else "resp 0"

    def verb_descriptor_get(self, uri):
        uri = grammar.pin_key(uri)
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
    new = os.environ.get("FAKE_HOSTD_PROTOCOL") == "0.3"  # a daemon of protocol 0.3, else an old one
    daemon = fake_plugin_hostd(port, os.environ.get("FAKE_HOSTD_JOURNAL"), descriptors, hello=new,
                               bulk=new and os.environ.get("FAKE_HOSTD_BULK", "1") == "1")
    time.sleep(float(os.environ.get("FAKE_HOSTD_READY_DELAY", "0")))  # listening, not yet ready
    print("plugin-hostd ready!", flush=True)
    daemon.serve()


if __name__ == "__main__":
    main(sys.argv)
