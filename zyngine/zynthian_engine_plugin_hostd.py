# -*- coding: utf-8 -*-
# ******************************************************************************
# ZYNTHIAN PROJECT: Zynthian Engine (zynthian_engine_plugin_hostd)
#
# zynthian_engine implementation for plugin-hostd: LV2 and CLAP plugins, each processor
# one instance of a shared daemon that speaks the mod-host protocol on its command port.
#
# Copyright (C) 2026 Pau Aliagas <linuxnow@gmail.com>
#
# This program is free software; you can redistribute it and/or modify it under the terms
# of the GNU General Public License as published by the Free Software Foundation; either
# version 2 of the License, or any later version. See the LICENSE.txt file.
# ******************************************************************************

import io
import os
import re
import json
import time
import base64
import shlex
import shutil
import signal
import socket
import logging
import tarfile
import tempfile
import threading
import subprocess

import zynautoconnect
from zyngine.zynthian_signal_manager import zynsigman
from zyngine import zynthian_plugin_hostd_protocol
from zyngine import zynthian_plugin_hostd_grouping
from zyngine import zynthian_plugin_hostd_estimate
from zyngine.zynthian_engine import zynthian_engine
from zyngine.zynthian_controller import zynthian_controller
from zyngine.zynthian_plugin_hostd_catalogue import clap_plugins

__all__ = ["zynthian_engine_plugin_hostd"]

# The command port. mod-host owns 5555 on a Zynthian, so the daemon takes another one.
DEFAULT_PORT = int(os.environ.get("ZYNTHIAN_PLUGIN_HOSTD_PORT", "5570"))
# Four-digit instance numbers, so no client name effect_N is a prefix of another one.
FIRST_INSTANCE = 1000
LAST_INSTANCE = 9989
TIMEOUT = 5.0
RUN_NUMBERS = 1000  # a chain's serial runs numbered to the daemon, see run_number
RETRY_S = 60.0  # a run the daemon refused, or planned alone for a reason that can change, is asked again after this
RETRY_REASONS = ("no-verdict", "plan-refused:")  # plan_get reasons that the qualification sync or a pin can lift
WATCH_S = 2.0  # how often the grouped workers are read back, to follow what the daemon moves by itself
SLOW_TIMEOUT = 30.0  # add, chain, state_save and state_load spawn or move workers
START_TIMEOUT = 10.0  # the wait for the daemon's declared line, in plugin-hostd-start.sh
START_SCRIPT = os.path.join(os.environ.get("ZYNTHIAN_SYS_DIR", "/zynthian/zynthian-sys"), "sbin",
                            "plugin-hostd-start.sh")
DESCRIPTOR_WAIT = 5.0
PROBE_TIMEOUT = 30.0
# The worker rows whose chain is one JACK client, named like its head (measured on a Pi 5 with plugin-hostd
# 0.12.0 and omx-clap-host 0.22.1, jack_lsp on a private server: a chain on the clap row, and one on the
# lv2-clap row, LV2 members included, is the one client effect_<head> with the chain's ports; a chain on the
# lv2 row, run by mod-host, keeps a client per member). plugin-hostd declares the row, not the client, so
# this is the one fact here that comes from a measurement; a row not listed keeps a client per member.
ONE_CLIENT_ROWS = ("clap", "lv2-clap")
STATE_FILE_RE = re.compile(r"^effect_(\d+)(?=\D|$)")

# The reply codes are plugin-hostd's own declaration, never retyped here. They are read once, here; without the
# files, or without a name below, the engine refuses to start and says which. Ready, the line the daemon declares,
# is waited for by zynthian-sys's plugin-hostd-start.sh, the one place a plugin-hostd is started on a Zynthian.
CHAIN_FALLBACK_NAMES = ("PLACEMENT_INVALID", "CHAIN_DUPLICATE", "CHAIN_INSTANCE_BUSY", "CHAIN_MIXED_FORMAT")
DESCRIPTOR_NONE_NAMES = ("DESCRIPTOR_ABSENT", "DESCRIPTOR_SCAN_FAILED")
NEEDS = CHAIN_FALLBACK_NAMES + DESCRIPTOR_NONE_NAMES + (
    "DESCRIPTOR_PENDING", "ERR_INSTANCE_ALREADY_EXISTS", "ERR_INVALID_OPERATION", "ERR_MEMORY_ALLOCATION")
protocol = zynthian_plugin_hostd_protocol.protocol(needs=NEEDS)
ERROR_NAMES = protocol.names
CHAIN_FALLBACK_CODES = tuple(protocol.codes[n] for n in CHAIN_FALLBACK_NAMES) if protocol.loaded else ()
DESCRIPTOR_PENDING = protocol.codes.get("DESCRIPTOR_PENDING")
DESCRIPTOR_NONE = tuple(protocol.codes[n] for n in DESCRIPTOR_NONE_NAMES) if protocol.loaded else ()

_logged = set()


def log_once(key, msg, *args):
    if key not in _logged:
        _logged.add(key)
        logging.warning(msg, *args)


class plugin_hostd_error(Exception):
    """A negative reply, carrying its code and its name."""

    def __init__(self, code, line):
        self.code = code
        self.name = protocol.name(code)
        super().__init__(f"'{line}' => {self.name} ({code})")


class plugin_hostd_down(Exception):
    """No daemon answers and none could be started."""


class plugin_hostd_timeout(Exception):
    """The daemon took longer than the timeout to answer."""


FAILURES = (plugin_hostd_error, plugin_hostd_down, plugin_hostd_timeout)


# ---- Protocol client: one NUL-terminated line out, one NUL-terminated reply back ----

class plugin_hostd_client:

    def __init__(self, port, host="127.0.0.1"):
        self.host = host
        self.port = port
        self.sock = None
        self.buf = b""

    def close(self):
        if self.sock:
            self.sock.close()
        self.sock = None
        self.buf = b""

    def call(self, line, timeout=TIMEOUT):
        """Send a line, return (code, rest) or raise plugin_hostd_error for a negative code.
        A connection error is raised as OSError; a timeout as plugin_hostd_timeout."""
        try:
            if self.sock is None:
                self.sock = socket.create_connection((self.host, self.port), timeout=TIMEOUT)
            self.sock.settimeout(timeout)
            self.sock.sendall(line.encode() + b"\0")
            while b"\0" not in self.buf:
                chunk = self.sock.recv(4096)
                if not chunk:
                    raise ConnectionResetError("plugin-hostd closed the connection")
                self.buf += chunk
        except socket.timeout:
            # The reply may still come: drop the connection so it is never read as the next one
            self.close()
            raise plugin_hostd_timeout(f"'{line}' got no reply in {timeout}s")
        except OSError:
            self.close()
            raise
        reply, _, self.buf = self.buf.partition(b"\0")
        words = reply.decode(errors="replace").split(" ", 2)
        if len(words) < 2 or words[0] != "resp":
            raise plugin_hostd_error(protocol.code("ERR_INVALID_OPERATION"), f"{line} (reply '{reply[:64]}')")
        code = int(words[1])
        if code < 0:
            raise plugin_hostd_error(code, line)
        return code, (words[2] if len(words) > 2 else "")


# ---- The daemon: started when none answers, reused when one does (a service, or another UI started it) ----

class plugin_hostd_daemon:

    def __init__(self, port=DEFAULT_PORT):
        self.client = plugin_hostd_client(port)
        self.lock = threading.RLock()  # the estimate is asked from a thread of its own, on the one connection
        self.start_script = START_SCRIPT
        self.conf = os.environ.get("ZYNTHIAN_PLUGIN_HOSTD_CONF",
                                   os.path.join(zynthian_engine.config_dir, "plugin-hostd.conf"))
        self.pid = None  # set only when this process started the daemon
        self.engines = {}  # instance number => engine
        self.runs = {}  # zynthian chain id => {run number: member instances}, see run_number
        self.chains = {}  # run number => member instances declared to the daemon
        self.chain_refused = {}  # run number => members the daemon refused to run together
        self.clients = {}  # run number => {member instance: jack client of the run's worker}
        self.plans = {}  # run number => member instances the daemon planned
        self.reasons = {}  # member instance => the reason word plan_get gave for it
        self.plan_verbs = None  # whether the daemon answers plan: None until it was asked
        self.grouping = None  # False once a plan came back with grouping-off; None while that is not known
        self.estimate_verb = None  # whether the daemon answers estimate: False once it refused it, for the session
        self.chain_figures = {}  # zynthian chain id => (steps, rate, quantum, footer text) of the chain as it is
        self.asked_at = {}  # run number => when it was last refused, or planned with a reason that can change
        self.pending = {}  # zynthian chain id => chain whose runs wait for the end of a busy section, see sync_chain
        self.synced = {}  # zynthian chain id => chain whose runs were declared, read back by the watch
        self.watch = None  # the thread that reads the grouped workers back, see watch_workers

    def identify(self):
        """True when a plugin-hostd answers on the port: mod-host refuses worker_list."""
        try:
            self.client.call("worker_list")
            return True
        except plugin_hostd_error:
            self.client.close()
            raise plugin_hostd_down(f"something that is not plugin-hostd answers on port {self.client.port}")
        except (OSError, plugin_hostd_timeout):
            return False

    def ensure(self):
        if not protocol.loaded:
            raise plugin_hostd_down(protocol.failure)
        if self.identify():
            return
        port = self.client.port
        if not os.access(self.start_script, os.X_OK):
            raise plugin_hostd_down(f"no daemon on port {port} and {self.start_script} is not installed")
        # The script returns once the daemon prints the line it declares for ready, and prints its PID; it
        # prints none for a daemon that was already running, which is then not this process's to stop
        cmd = [self.start_script, str(port)]
        env = dict(os.environ, PHD_CONF=self.conf, READY_TIMEOUT=str(int(START_TIMEOUT)))
        logging.info(f"Starting plugin-hostd: {' '.join(cmd)}")
        try:
            done = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, env=env,
                                  timeout=START_TIMEOUT + 5, start_new_session=True)
        except (OSError, subprocess.SubprocessError) as e:
            raise plugin_hostd_down(f"{' '.join(cmd)} failed: {e}")
        if done.returncode != 0:
            why = (done.stderr.strip().splitlines() or [f"exit code {done.returncode}"])[-1]
            raise plugin_hostd_down(f"{' '.join(cmd)} => {why}")
        pid = done.stdout.strip()
        self.pid = int(pid) if pid.isdigit() else None

    def call(self, line, timeout=TIMEOUT):
        """One command; if the connection is gone, bring the daemon back, replay and send it again."""
        with self.lock:
            if self.client.sock is None:
                self.restore()
            try:
                return self.client.call(line, timeout)
            except OSError as e:
                logging.warning(f"plugin-hostd connection lost ({e}), restoring")
            self.restore()
            return self.client.call(line, timeout)

    def restore(self):
        self.client.close()
        self.ensure()
        self.runs = {}
        self.chains = {}
        self.chain_refused = {}
        self.clients = {}
        self.plans = {}
        self.reasons = {}
        self.asked_at = {}
        self.plan_verbs = None  # the daemon that came back may be another version, with another setting
        self.grouping = None
        for instance in sorted(self.engines):
            for line in self.engines[instance].replay_lines():
                try:
                    self.client.call(line, SLOW_TIMEOUT)
                except plugin_hostd_error as e:
                    # An instance that outlived the connection has nothing to put back
                    outlived = e.code == protocol.code("ERR_INSTANCE_ALREADY_EXISTS")
                    if not outlived:
                        logging.error(f"plugin-hostd replay => {e}")
                    if line.startswith("add ") or outlived:
                        break
        for chain_manager in {id(e.chain_manager): e.chain_manager for e in self.engines.values()}.values():
            for chain in list(chain_manager.chains.values()):
                sync_chain(chain)

    def add(self, engine, uri):
        """Add the plugin under the first free instance number and register the engine."""
        for instance in range(FIRST_INSTANCE, LAST_INSTANCE + 1):
            if instance not in self.engines:
                try:
                    self.call(f"add {uri} {instance}", SLOW_TIMEOUT)
                    self.engines[instance] = engine
                    return instance
                except plugin_hostd_error as e:
                    if e.code != protocol.code("ERR_INSTANCE_ALREADY_EXISTS"):  # taken by another controller of this daemon
                        raise
        raise plugin_hostd_error(protocol.code("ERR_MEMORY_ALLOCATION"), f"add {uri}")

    def remove(self, instance):
        with self.lock:
            self.engines.pop(instance, None)
            self.reasons.pop(instance, None)
            try:
                self.client.call(f"remove {instance}")
            except (plugin_hostd_error, plugin_hostd_timeout, OSError) as e:
                logging.warning(f"plugin-hostd remove {instance} => {e}")
            if not self.engines:
                # The daemon serves one controller at a time: let another one in
                self.stop() if self.pid else self.client.close()

    def stop(self):
        """Stop the daemon, only if this process started it."""
        if not self.pid:
            return
        try:
            self.client.call("quit", 2.0)
        except (OSError, *FAILURES):
            pass
        self.client.close()
        for sig in (signal.SIGTERM, signal.SIGKILL):
            if self.wait_gone(5.0):
                break
            try:
                os.kill(self.pid, sig)
            except OSError:
                break
        self.pid = None

    def wait_gone(self, timeout):
        """True once the daemon's process is gone: it is not this process's child, so it is polled."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                os.kill(self.pid, 0)
            except ProcessLookupError:
                return True
            except OSError:
                return False
            time.sleep(0.05)
        return False


daemon = plugin_hostd_daemon()


def worker_record(record):
    """One record of worker_list, <worker>:<pid>:<row>:<state>:<core>:<pool>:<place>:<instance>,... with a place of
    own or chain:<number>: (worker, row, state, place, number, members). The fields before the place have grown with
    the daemon's versions, so the place is read from the end."""
    fields = record.split(":")
    if len(fields) < 5:
        return None
    chained = len(fields) >= 7 and fields[-3] == "chain"
    return (fields[0], fields[2], fields[3], "chain" if chained else fields[-2], fields[-2] if chained else None,
            [int(i) for i in fields[-1].split(",") if i.isdigit()])


def head_client(members):
    """{member: jack client} when the graph agrees that a worker runs these members as the one client of the first:
    no other member has a port left. A worker that refused to run them as one keeps a client per member, which the
    daemon may not say, and then {}: each member keeps its own, and the chain's routes connect them in order."""
    try:
        if any(zynautoconnect.get_ports(f"^{daemon.engines[i].jackname}:") for i in members[1:]):
            log_once(("clients", tuple(members)), "plugin-hostd: members %s of one worker keep their own clients", members)
            return {}
    except Exception as e:
        logging.debug(f"plugin-hostd client of {members} not read from the graph => {e}")
    return {i: daemon.engines[members[0]].jackname for i in members}


def chain_clients(chain_id):
    """The jack client of each member the daemon's worker for this chain runs as one client, as the daemon
    says it: chain_get names the worker and the members it runs, worker_list the row of that worker.
    {} when the worker is not up, runs another row, or the daemon cannot say; a member it does not run
    (quarantined) keeps a client of its own."""
    try:
        worker, running = daemon.call(f"chain_get {chain_id}")[1].split()[0:3:2]
        records = daemon.call("worker_list")[1].split()
    except (OSError, ValueError, *FAILURES) as e:
        logging.debug(f"plugin-hostd chain {chain_id} read back => {e}")
        return {}
    for record in filter(None, map(worker_record, records)):
        if record[0] == worker and record[3] == "chain" and record[4] == str(chain_id):
            if record[1] not in ONE_CLIENT_ROWS or record[2] != "up":
                return {}
            members = [int(i) for i in running.split(",") if i.isdigit()]
            if members and all(i in daemon.engines for i in members):
                return head_client(members)
    return {}


class note_head:
    """A member that must head its run: a plugin that takes notes. A worker's one client reads MIDI for the run's head
    only (omx-clap-core docs/chain.md, notes to a member go through its ring, not through JACK), and Zynthian routes a
    chain's MIDI to each processor's client, so a member that takes notes behind another one would hear nothing."""
    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value


def unwrap(member):
    return member.value if isinstance(member, note_head) else member


def signal_steps(slots):
    """The chain's plugin-hostd members in signal order, as the steps a signal takes. A slot is a list of members, None
    for a processor of something else. A step is a tuple of parallel branches, a branch a tuple of members in series.
    A slot of one member extends the run in hand, since a worker runs a chain in series in one JACK client; a slot of
    several is parallel, so each of its members is a branch alone; any slot that holds something else ends the run,
    since the signal has to go through that processor (the mixer strip, a jalv plugin) on its way to the next member."""
    steps, run = [], []
    for slot in slots:
        if len(slot) == 1 and slot[0] is not None:
            if isinstance(slot[0], note_head) and run:  # it takes notes: it heads a run of its own
                steps.append((tuple(run),))
                run = []
            run.append(unwrap(slot[0]))
            continue
        if run:
            steps.append((tuple(run),))
            run = []
        branches = tuple((unwrap(member),) for member in slot if member is not None)
        if branches:
            steps.append(branches)
    if run:
        steps.append((tuple(run),))
    return steps


def audio_path_slots(chain):
    """The slots a chain's signal goes through: the chain's graph takes its source from the last synth slot."""
    return chain.synth_slots[-1:] + chain.audio_slots


def member_of(p, value):
    """What signal_steps is given for a processor: None for one of another engine, the value, wrapped when the
    plugin takes notes."""
    if not isinstance(p.engine, zynthian_engine_plugin_hostd) or value is None:
        return None
    return note_head(value) if p.engine.takes_notes() else value


def chain_steps(chain):
    """signal_steps of a zynthian chain, of the plugin refs of its plugin-hostd members."""
    return signal_steps([[member_of(p, getattr(p.engine, "plugin_uri", None)) for p in slot]
                         for slot in audio_path_slots(chain)])


def menu_refs(chain, eng_code, info, replacing=None):
    """The steps to ask the estimate for while a row of the plugin menu is highlighted: the chain's with the row's
    plugin in the place the chain options give it, in the slot of the processor being replaced, else before the mixer
    strip for an effect, and first for anything else; [] for a row the daemon does not run, or when it has no ref."""
    if eng_code.split("/")[0] != "PH" or not info.get("URL"):
        return []
    slots = [[info["URL"] if p is replacing else p.engine.plugin_uri if isinstance(p.engine, zynthian_engine_plugin_hostd)
              else None for p in slot] for slot in (audio_path_slots(chain) if chain else ())]
    if replacing is None or not any(info["URL"] in slot for slot in slots):
        at = 0
        if info.get("TYPE") == "Audio Effect":
            strips = [i for i, slot in enumerate(audio_path_slots(chain) if chain else ())
                      if slot and slot[0].eng_code in ("MI", "MR")]
            at = strips[0] if strips else len(slots)
        slots.insert(at, [info["URL"]])
    return signal_steps(slots)


def estimate(rate, quantum, refs):
    """The daemon's figures for a chain of these plugin refs at this rate and quantum, from its qualification
    records: plugin_hostd_estimate.parse's dict. estimate_unavailable once the daemon has refused the verb as
    mod-host does, and for the rest of the session, so it is not asked again; a refusal of the question itself
    (a plugin it does not know) is a plugin_hostd_error, and no figure."""
    if daemon.estimate_verb is False:
        raise zynthian_plugin_hostd_estimate.estimate_unavailable()
    try:
        reply = daemon.call(f"estimate {rate} {quantum} {' '.join(refs)}")[1]
    except plugin_hostd_error as e:
        if e.code == protocol.code("ERR_INVALID_OPERATION"):
            daemon.estimate_verb = False
            logging.warning("plugin-hostd does not answer estimate: no chain figures this session")
            raise zynthian_plugin_hostd_estimate.estimate_unavailable()
        raise
    daemon.estimate_verb = True
    return zynthian_plugin_hostd_estimate.parse(reply)


def estimate_steps(rate, quantum, steps):
    """The figures of a chain as signal_steps gives it, one estimate for each branch: a branch is what one worker
    runs. Latency adds along the steps, and a step takes its longest branch; everything else adds up. A member the
    records lack is named by its place among all of them. plugin_hostd_estimate.parse's dict."""
    total = {"latency": 0, "cpu": 0.0, "ram": 0, "workers": 0, "unknown": 0, "members": {}}
    place = 0
    for step in steps:
        longest = 0
        for branch in step:
            est = estimate(rate, quantum, branch)
            longest = max(longest, est["latency"])
            total["cpu"] += est["cpu"]
            total["ram"] = None if total["ram"] is None or est["ram"] is None else total["ram"] + est["ram"]
            total["workers"] += est["workers"]
            total["unknown"] += est["unknown"]
            total["members"].update({place + i: code for i, code in est["members"].items()})
            place += len(branch)
        total["latency"] += longest
    return total


def audio_format(state_manager):
    """(rate, quantum) of the audio device, None when it does not say integers."""
    try:
        rate, quantum = state_manager.get_jackd_samplerate(), state_manager.get_jackd_blocksize()
    except Exception:
        return None
    return (rate, quantum) if isinstance(rate, int) and isinstance(quantum, int) else None


def figure_chain(chain, state_manager):
    """Ask, off the caller's thread, for the figures of the chain as it is: they are in daemon.chain_figures
    when the answer is there. The chain view shows them."""
    steps, fmt = chain_steps(chain), audio_format(state_manager)
    if not steps or fmt is None or daemon.estimate_verb is False:
        daemon.chain_figures.pop(chain.chain_id, None)
        return
    if daemon.chain_figures.get(chain.chain_id, (None,))[0:3] == (steps, *fmt):
        return

    def ask():
        try:
            text = zynthian_plugin_hostd_estimate.footer(estimate_steps(fmt[0], fmt[1], steps))
        except Exception as e:
            logging.debug(f"plugin-hostd estimate of chain {chain.chain_id} => {e}")
            return
        if chain_steps(chain) == steps:  # the chain has not moved on since the question
            daemon.chain_figures[chain.chain_id] = (steps, fmt[0], fmt[1], text)
    threading.Thread(target=ask, daemon=True).start()


def busy(chain):
    """Whether a busy section of the state manager is running (a snapshot load, an add, a removal): the runs are then
    declared once, when it ends (flush_pending)."""
    for slot in chain.synth_slots + chain.audio_slots:
        for p in slot:
            if isinstance(p.engine, zynthian_engine_plugin_hostd):
                return getattr(p.engine.state_manager, "is_busy", lambda: False)() is True
    return False


def sync_chain(chain):
    """Run each serial run of the plugin-hostd members of a zynthian chain in one worker, in signal order, then give
    each processor the jack client it has now: its run's for the members a worker runs as one client, the client of
    its instance for every other.
    Called from the chain's graph rebuild, so after insert, remove, swap and nudge. Inside a busy section the daemon is
    not asked: the chain waits for its end (flush_pending), so a snapshot load or an edit of several steps declares
    each run once, and a removed member is gone from the daemon before its run is declared without it (else its
    worker would be spawned for it alone, just to be removed)."""
    if busy(chain):
        daemon.pending[chain.chain_id] = chain
    else:
        daemon.pending.pop(chain.chain_id, None)
        declare_chain(chain)
        daemon.synced[chain.chain_id] = chain
        ensure_watch()
    apply_clients(chain)


def apply_clients(chain):
    """Give each plugin-hostd processor of the chain its jack client; True when one of them changed."""
    clients = {}
    for number in daemon.runs.get(chain.chain_id, ()):
        clients.update(daemon.clients.get(number, {}))
    engines, changed = [], False
    for slot in chain.synth_slots + chain.audio_slots:
        for p in slot:
            if isinstance(p.engine, zynthian_engine_plugin_hostd) and p.engine.instance is not None:
                jackname = clients.get(p.engine.instance, p.engine.jackname)
                changed |= p.jackname != jackname
                p.jackname = jackname
                engines.append(p.engine)
    if engines:
        figure_chain(chain, engines[0].state_manager)
    else:
        daemon.chain_figures.pop(chain.chain_id, None)
    return changed


def flush_pending(state=None, **kw):
    """The end of a busy section (zynsigman SS_BUSY, state False): declare the chains that waited, and rebuild the
    graph of every one whose clients changed so that its routes and autoconnect follow."""
    if state is not False or not daemon.pending:
        return
    chains, daemon.pending = list(daemon.pending.values()), {}
    rebuilt = False
    for chain in chains:
        declare_chain(chain)
        daemon.synced[chain.chain_id] = chain
        if apply_clients(chain):
            chain.rebuild_audio_graph()
            rebuilt = True
    ensure_watch()
    if rebuilt:
        zynautoconnect.request_audio_connect(True)
        zynautoconnect.request_midi_connect(True)


zynsigman.register(zynsigman.S_STATE_MAN, zynsigman.SS_BUSY, flush_pending)


def ensure_watch():
    if daemon.runs and (daemon.watch is None or not daemon.watch.is_alive()):
        daemon.watch = threading.Thread(target=watch_workers, name="plugin-hostd watch", daemon=True)
        daemon.watch.start()


def watch_workers():
    """Read the grouped workers back while there are any (follow_daemon), every WATCH_S."""
    watched = daemon
    while watched is daemon and watched.runs:
        time.sleep(WATCH_S)
        if watched is daemon:
            follow_daemon()


def follow_daemon():
    """A member the daemon quarantined after a crash, or a run it planned again by itself, leaves the clients Zynthian
    routes by out of date: the chain's graph is rebuilt, so that a member that runs in a worker of its own is routed
    through its own client again."""
    for chain_id, chain in list(daemon.synced.items()):
        if chain_id in daemon.pending or chain_id not in daemon.runs:
            continue
        try:
            now = {number: read_clients(number) for number in daemon.runs[chain_id]}
        except Exception as e:
            logging.debug(f"plugin-hostd watch of chain {chain_id} => {e}")
            continue
        if all(now[number] == daemon.clients.get(number, {}) for number in now):
            continue
        logging.info("plugin-hostd moved members of chain %s: its routes follow", chain_id)
        daemon.clients.update(now)
        if apply_clients(chain):
            chain.rebuild_audio_graph()
            zynautoconnect.request_audio_connect(True)


def read_clients(number):
    """The clients of a run as the daemon and the graph say they are now: a plan's segments, or a declared chain."""
    if number in daemon.plans:
        try:
            segments = parse_segments(daemon.call(f"plan_get {number}")[1])
        except (OSError, ValueError, *FAILURES):
            return daemon.clients.get(number, {})
        return segment_clients(number, segments)
    if number in daemon.chains:
        return chain_clients(number)
    return {}


def serial_runs(chain):
    """The runs of two or more plugin-hostd instances of a zynthian chain that may share a worker, each in signal
    order: signal_steps says which. A member of a parallel slot, or one with nothing in series beside it, is in none
    and has a worker of its own."""
    steps = signal_steps([[member_of(p, getattr(p.engine, "instance", None)) for p in slot]
                          for slot in audio_path_slots(chain)])
    return [branch for step in steps for branch in step if len(branch) > 1]


def run_number(chain_id, k):
    """The number the daemon knows run k of a zynthian chain by: the chain's own for the first, and above every chain
    id for the others (a chain id fits a byte, chain_manager's MIDI learn table packs it in one)."""
    return chain_id if k == 0 else (chain_id + 1) * RUN_NUMBERS + k


def declare_chain(chain):
    """Hand each serial run of the chain to the daemon. A number whose members now belong to another run is cleared
    first: the daemon refuses a member that another chain holds."""
    chain_id = chain.chain_id
    wanted = stable_numbers(chain_id, serial_runs(chain), daemon.runs.get(chain_id, {}))
    for number, members in daemon.runs.get(chain_id, {}).items():
        if number not in wanted or any(set(members) & set(other) for n, other in wanted.items() if n != number):
            declare_run(number, ())
    for number, members in wanted.items():
        declare_run(number, members)
    if wanted:
        daemon.runs[chain_id] = wanted
    else:
        daemon.runs.pop(chain_id, None)


def stable_numbers(chain_id, runs, before):
    """{number: members} for the chain's runs: a run keeps the number of the run it shares most members with, so an
    edit elsewhere in the chain leaves its worker alone; a run that shares none takes the lowest free number."""
    wanted, left = {}, []
    for members in runs:
        best = max((n for n in before if n not in wanted and set(before[n]) & set(members)),
                   key=lambda n: len(set(before[n]) & set(members)), default=None)
        if best is None:
            left.append(members)
        else:
            wanted[best] = members
    k = 0
    for members in left:
        while run_number(chain_id, k) in wanted or run_number(chain_id, k) in before:
            k += 1
        wanted[run_number(chain_id, k)] = members
    return wanted


def declare_run(number, members):
    """Hand a run's members to the daemon: to its planner, which decides what runs together, or, for a
    daemon that has no plan verb or has grouping off, as one chain, the way plugin-hostd 0.12 took them."""
    if daemon.plan_verbs is not False and daemon.grouping is not False:
        if plan_chain(number, members):
            return
    if daemon.plan_verbs is False:
        # a daemon with no planner (plugin-hostd 0.12) has no grouping setting either: chain is its only way
        declare_whole_chain(number, members)
        return
    # the daemon has grouping off: nothing runs together, and a run declared before (a daemon restarted with the
    # setting changed comes back through restore, which forgets them) is let go
    declare_whole_chain(number, ())


def declare_whole_chain(chain_id, members):
    if not members:
        daemon.chain_refused.pop(chain_id, None)
        daemon.asked_at.pop(chain_id, None)
    if daemon.chains.get(chain_id, ()) == members:
        return
    if daemon.chain_refused.get(chain_id) == members and not due(chain_id):
        return
    try:
        if members:
            daemon.call(f"chain {chain_id} {' '.join(map(str, members))}", SLOW_TIMEOUT)
            daemon.chains[chain_id] = members
            daemon.chain_refused.pop(chain_id, None)
            daemon.clients[chain_id] = chain_clients(chain_id)
            return
    except plugin_hostd_error as e:
        if e.code not in CHAIN_FALLBACK_CODES:
            logging.error(f"plugin-hostd chain {chain_id} => {e}")
            return
        log_once(("chain", chain_id), "plugin-hostd runs chain %s in separate workers: %s", chain_id, e.name)
        daemon.chain_refused[chain_id] = members
        daemon.asked_at[chain_id] = time.monotonic()
    except (plugin_hostd_down, plugin_hostd_timeout) as e:
        logging.error(f"plugin-hostd chain {chain_id} => {e}")
        return
    daemon.clients.pop(chain_id, None)
    if daemon.chains.pop(chain_id, None):
        try:
            daemon.call(f"chain_clear {chain_id}")
        except (OSError, *FAILURES) as e:
            logging.debug(f"plugin-hostd chain_clear {chain_id} => {e}")


def due(number):
    """Whether a run that was refused, or planned alone for a reason that can change, is to be asked again."""
    asked = daemon.asked_at.get(number)
    return asked is not None and time.monotonic() - asked >= RETRY_S


# ---- The plan: the daemon says which of the members run together ----

def parse_segments(text):
    """'1,2;3;4,5' => [[1, 2], [3], [4, 5]]; the cores that follow the segments in a plan's reply are not read."""
    return [[int(i) for i in segment.split(",")] for segment in text.split()[0].split(";")]


def parse_reasons(text):
    """'3:verdict-isolated,5:plan-refused:<code>' => {3: 'verdict-isolated', 5: 'plan-refused:<code>'}; '-' is none.
    The instance ends at the first colon: a reason may hold more of them."""
    reasons = {}
    if text and text != "-":
        for item in text.split(","):
            instance, _, reason = item.partition(":")
            if instance.isdigit() and reason:
                reasons.setdefault(int(instance), reason)
    return reasons


def plan_answered():
    """False when the daemon does not know the plan verbs, asked with a read that a daemon which has
    them answers: plan_get of a chain nobody planned is the daemon's no-such-plan refusal, a daemon without them
    refuses the verb as mod-host does."""
    try:
        daemon.call("plan_get 0")
    except plugin_hostd_error as e:
        return e.code != protocol.code("ERR_INVALID_OPERATION")
    except (OSError, plugin_hostd_down, plugin_hostd_timeout):
        return True
    return True


def forget_plan(chain_id):
    """Every member of the chain back in a worker of its own, and nothing left of what was said of it."""
    for instance in daemon.plans.pop(chain_id, ()):
        daemon.reasons.pop(instance, None)
    daemon.clients.pop(chain_id, None)


def clear_plan(chain_id):
    try:
        daemon.call(f"plan_clear {chain_id}")
    except (OSError, *FAILURES) as e:
        logging.debug(f"plugin-hostd plan_clear {chain_id} => {e}")


def segment_clients(chain_id, segments):
    """{member: jack client} for the members of the segments whose worker is one client: worker_list names the
    worker of segment k as place chain:<chain>.<k> and its row; the head of the members it runs gives the client.
    A member that has a worker of its own (quarantined out of its segment) keeps its own."""
    try:
        records = daemon.call("worker_list")[1].split()
    except (OSError, *FAILURES) as e:
        logging.debug(f"plugin-hostd chain {chain_id} read back => {e}")
        return {}
    workers = list(filter(None, map(worker_record, records)))
    alone = {i for w in workers if w[3] == "own" for i in w[5]}
    clients = {}
    for _, row, state, place, number, members in workers:
        if place != "chain" or row not in ONE_CLIENT_ROWS or state != "up":
            continue
        owner, _, k = number.partition(".")
        if owner != str(chain_id) or not k.isdigit() or int(k) >= len(segments):
            continue
        running = [i for i in members if i not in alone]
        if running and all(i in daemon.engines for i in running):
            clients.update(head_client(running))
    return clients


def plan_chain(chain_id, members):
    """Plan the chain. False when the daemon cannot plan (no plan verb, or grouping off), and the chain is
    then declared whole; True when the plan is the daemon's answer and has been taken."""
    if members == daemon.plans.get(chain_id, ()) and not due(chain_id):
        return True
    if not members:
        daemon.asked_at.pop(chain_id, None)
        if chain_id in daemon.plans:
            clear_plan(chain_id)
        forget_plan(chain_id)
        return True
    if daemon.chains.get(chain_id):  # a chain of the same number the controller declared before
        declare_whole_chain(chain_id, ())
    try:
        segments = parse_segments(daemon.call(f"plan {chain_id} {' '.join(map(str, members))}", SLOW_TIMEOUT)[1])
    except plugin_hostd_error as e:
        if e.code == protocol.code("ERR_INVALID_OPERATION") and not plan_answered():
            daemon.plan_verbs = False
            logging.warning("plugin-hostd does not answer plan: chain %s runs as one chain, as plugin-hostd 0.12 does",
                            chain_id)
            return False
        return plan_refused(chain_id, members, e)
    except (plugin_hostd_down, plugin_hostd_timeout, ValueError) as e:
        logging.error(f"plugin-hostd plan {chain_id} => {e}")
        return True
    daemon.plan_verbs = True
    try:
        # <segments> <reasons> <cores>
        reasons = parse_reasons((daemon.call(f"plan_get {chain_id}")[1].split() + [""])[1])
    except (OSError, ValueError, *FAILURES) as e:
        logging.error(f"plugin-hostd plan_get {chain_id} => {e}")
        reasons = {}
    if "grouping-off" in reasons.values():
        # The daemon answered the plan and moved nothing: the setting grouping is off, so the chain is
        # declared whole, as it was before there was a plan
        daemon.grouping = False
        logging.info("plugin-hostd has grouping off: chain %s is declared as one chain", chain_id)
        clear_plan(chain_id)
        forget_plan(chain_id)
        return False
    if any(len(segment) > 1 for segment in segments):
        daemon.grouping = True
    forget_plan(chain_id)
    daemon.plans[chain_id] = members
    daemon.reasons.update(reasons)
    daemon.clients[chain_id] = segment_clients(chain_id, segments)
    if any(r.startswith(RETRY_REASONS) for r in reasons.values()):
        daemon.asked_at[chain_id] = time.monotonic()
    else:
        daemon.asked_at.pop(chain_id, None)
    return True


def plan_refused(chain_id, members, e):
    """The daemon refused the plan and moved nothing: a plan it had stays, so it is cleared, and every
    member runs alone with the refusal as its reason."""
    if chain_id in daemon.plans:
        clear_plan(chain_id)
    forget_plan(chain_id)
    logging.warning("plugin-hostd plan %s => %s: every member runs alone", chain_id, e.name)
    daemon.plans[chain_id] = members
    daemon.reasons.update({i: f"plan-refused:{e.code}" for i in members})
    daemon.asked_at[chain_id] = time.monotonic()
    return True


# ---- Controller pages from omx-panel's layout of the plugin's descriptor ----

layout_cache = {}  # (descriptor path, mtime) => (pages, layout controls by param)


per_page_support = {}  # omx-panel binary => whether its `layout` takes --per-page (omx-panel#93)


def panel_per_page(panel):
    """Detect once whether the installed omx-panel can lay out pages of the Zynthian's four encoders."""
    if panel not in per_page_support:
        try:
            out = subprocess.run([panel, "layout", "--help"], capture_output=True, text=True, timeout=30)
            per_page_support[panel] = "--per-page" in (out.stdout + out.stderr)
        except (OSError, subprocess.SubprocessError):
            per_page_support[panel] = False
    return per_page_support[panel]


def panel_layout(path):
    try:
        key = (path, os.stat(path).st_mtime)
    except OSError:
        return None
    if key in layout_cache:
        return layout_cache[key]
    panel = shutil.which("omx-panel")
    if not panel:
        log_once("omx-panel", "omx-panel is not installed: plugin-hostd pages follow the parameter order")
        return None

    def run(*extra):
        cmd = [panel, "layout", "--descriptor", path, *extra]
        return json.loads(subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30).stdout)

    try:
        layout = run()
        pages = run("--pages", "--per-page", "4") if panel_per_page(panel) else run("--pages")
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        log_once(("omx-panel", path), "omx-panel layout of %s failed (%s): pages follow the parameter order", path, e)
        return None
    layout_cache[key] = (pages, {str(c["param"]): c for c in layout.get("controls", [])})
    return layout_cache[key]


probe_cache = {}  # plugin uri => the descriptor file plugin-hostd-probe describe wrote, None when it refused


def probe_descriptor(uri, why):
    """The daemon has no descriptor (absent, or its scan failed): describe the plugin here, once."""
    if uri not in probe_cache:
        probe_cache[uri] = None
        probe = shutil.which("plugin-hostd-probe")
        if not probe:
            log_once(("descriptor", uri), "plugin-hostd has no descriptor for %s (%s) and plugin-hostd-probe "
                     "is not installed", uri, why)
            return None
        target = uri if uri.startswith("clap:") else f"lv2:{uri}"
        out_dir = os.path.join(tempfile.gettempdir(), "plugin-hostd-probe")
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, re.sub(r"[^A-Za-z0-9._-]+", "_", uri) + ".json")
        try:
            done = subprocess.run([probe, "describe", target], capture_output=True, timeout=PROBE_TIMEOUT)
            if done.returncode == 0 and done.stdout.strip():
                with open(path, "wb") as f:
                    f.write(done.stdout)
                probe_cache[uri] = path
        except (OSError, subprocess.SubprocessError) as e:
            logging.warning("plugin-hostd-probe describe %s failed (%s)", target, e)
        if not probe_cache[uri]:
            log_once(("descriptor", uri), "plugin-hostd has no descriptor for %s (%s) and the probe made none",
                     uri, why)
    return probe_cache[uri]


# ---- The engine: one per processor, its plugin one instance of the shared daemon ----

class zynthian_engine_plugin_hostd(zynthian_engine):

    def __init__(self, eng_code, state_manager=None):
        super().__init__(state_manager)
        self.eng_info = self.chain_manager.engine_info[eng_code]
        self.type = self.eng_info["TYPE"]
        self.name = self.plugin_name = self.eng_info["NAME"]
        self.nickname = eng_code
        self.plugin_uri = self.eng_info["URL"]
        self.instance = None
        self.notes = None  # whether the plugin takes notes, see takes_notes
        self.preset_key = None
        self.pending_state = None
        self.bypass_zctrl = None
        self.zctrls = {}
        self.labels = {}
        self._ctrls = []
        self._ctrl_screens = []
        self.options['ctrl_fb'] = False

    def add_processor(self, processor):
        if self.instance is None:
            self.instance = daemon.add(self, self.plugin_uri)
            self.jackname = f"effect_{self.instance}"
            self.zctrls = self.build_controllers()
            self.generate_ctrl_screens(self.zctrls)
            if self.pending_state:
                self.load_state(self.pending_state)
                self.pending_state = None
        if processor not in self.processors:
            self.processors.append(processor)
        # a save always goes to User: the save screen asks for no bank
        processor.auto_save_bank = True
        processor.jackname = self.jackname
        self.set_midi_chan(processor)
        processor.refresh_controllers()

    def takes_notes(self):
        """Whether the plugin takes notes, as the daemon's note_input answers it (midi_in, or - for none), asked once.
        A daemon that does not answer it leaves every plugin free to join a run: the synth heads its run anyway."""
        if self.notes is None and self.instance is not None:
            try:
                self.notes = daemon.call(f"note_input {self.instance}")[1].strip() not in ("", "-")
            except (OSError, *FAILURES):
                self.notes = False
        return bool(self.notes)

    def grouping_reason(self):
        """The reason word the daemon's plan gave for this plugin, None when it has none."""
        return daemon.reasons.get(self.instance)

    def grouping_text(self):
        """What the plugin's entry says of why it is not grouped with its neighbours, None when nothing."""
        reason = self.grouping_reason()
        return zynthian_plugin_hostd_grouping.text(reason, protocol.name) if reason else None

    def remove_processor(self, processor):
        # The instance stays until stop: a processor moved to another chain keeps its plugin
        if processor in self.processors:
            self.processors.remove(processor)
        processor.jackname = None

    def stop(self):
        if self.instance is not None:
            daemon.remove(self.instance)
            self.instance = None

    def replay_lines(self):
        """What puts the instance back in a daemon that lost it: the add, the preset, every value."""
        lines = [f"add {self.plugin_uri} {self.instance}"]
        if self.preset_key:
            lines.append(f"preset_load {self.instance} {self.preset_key}")
        return lines + [self.value_line(zctrl) for zctrl in self.zctrls.values()]

    def send(self, line, timeout=TIMEOUT):
        try:
            return daemon.call(line, timeout)
        except FAILURES as e:
            logging.error(f"plugin-hostd {e}")

    def send_quiet(self, line):
        """The reply of a query, None when the daemon has no answer for it (no parameter contract, no such symbol)."""
        try:
            return daemon.call(line)[1]
        except FAILURES:
            return None

    # ---- The plugin's own window (CLAP gui or an LV2 UI, the user never sees which) ----

    def has_window(self):
        """True unless the descriptor says the plugin has no window of its own."""
        path = self.descriptor_path()
        if not path:
            return True
        try:
            with open(path) as f:
                return json.load(f).get("plugin", {}).get("gui", True) is not False
        except (OSError, ValueError, AttributeError):
            return True

    def window_shown(self):
        return getattr(self, "window_open", False)

    def toggle_window(self):
        """Show the plugin's window on the display, or hide it. Returns the reply text when the daemon refuses."""
        verb = "hide_external_ui" if self.window_shown() else "show_external_ui"
        try:
            reply = daemon.call(f"{verb} {self.instance}")
        except FAILURES as e:
            logging.error(f"plugin-hostd {e}")
            return str(e)
        self.window_open = verb == "show_external_ui"
        return None

    def get_name(self, processor=None):
        return self.plugin_name

    def get_path(self, processor=None):
        return self.plugin_name

    def set_midi_chan(self, processor):
        processor.midi_chan_engine = processor.midi_chan

    # ---- Controllers ----

    def descriptor_path(self):
        """The descriptor file of the plugin: the daemon's scan of it, else a describe of our own."""
        deadline = time.monotonic() + DESCRIPTOR_WAIT
        while True:
            try:
                return daemon.call(f"descriptor_get {self.plugin_uri}")[1].strip().strip('"')
            except plugin_hostd_error as e:
                if e.code == DESCRIPTOR_PENDING and time.monotonic() <= deadline:  # the add's scan still runs
                    time.sleep(0.25)
                    continue
                if e.code in DESCRIPTOR_NONE:
                    return probe_descriptor(self.plugin_uri, e.name)
                log_once(("descriptor", self.plugin_uri), "plugin-hostd has no descriptor for %s (%s)",
                         self.plugin_uri, e.name)
                return None
            except (plugin_hostd_down, plugin_hostd_timeout):
                return None

    def param_list(self, dlist):
        """(symbol, name, meta) in the plugin's own order: its descriptor, else its CLAP scan or its LV2 ports.
        The ports and the scan carry no scale: a stepped parameter is the descriptor's or param_info's to say."""
        if dlist:
            return [(str(c["id"]), c.get("label") or c.get("name"), c) for c in dlist]
        if self.nickname in clap_plugins:
            return [(p["id"], p["name"], {"travel": {"min": p["min"], "max": p["max"], "default": p["default"]}})
                    for p in clap_plugins[self.nickname]["params"]]
        try:
            from zyngine import zynthian_lv2
            ports = zynthian_lv2.get_plugin_ports(self.plugin_uri).values()
        except Exception as e:
            logging.error(f"Can't read the ports of {self.plugin_uri} => {e}")
            return []
        return [(p["symbol"], p["name"], {"travel": {"min": p["range"]["min"], "max": p["range"]["max"],
                 "default": p["value"]}, "kind": "toggle" if p["is_toggled"] else "travel"})
                for p in ports if not p.get("is_path")]

    def remote_pages(self, dcontrols):
        """The plugin's own remote-control pages (clap.remote-controls), whatever its format calls them:
        [(page name, [(symbol, label, unit, descriptor control)])], empty when it has none or the daemon cannot say."""
        if self.instance is None:
            return []
        try:
            count = daemon.call(f"remote_pages {self.instance}")[0]
        except FAILURES:
            return []
        result = []
        for n in range(count):
            try:
                words = shlex.split(daemon.call(f"remote_page_get {self.instance} {n}")[1])
            except (ValueError,) + FAILURES:
                continue
            # <page_id> <section> <page_name> then eight slots, "-" for an empty one
            if len(words) < 3:
                continue
            rows = []
            for symbol in words[3:]:
                dc = dcontrols.get(symbol, {})
                if symbol != "-" and not dc.get("readonly"):
                    rows.append((symbol, dc.get("label") or dc.get("name") or symbol, None, dc))
            if rows:
                result.append((words[2] or words[1], rows))
        return result

    def pages(self):
        """[(page name, [(symbol, label, unit, descriptor control)])]: the plugin's remote pages when it has them,
        then omx-panel's pages of four (of eight when it can't), else the plain parameter order cut in eights.
        A control is on the first page that shows it: the layout pages keep what the remote pages left out."""
        path = self.descriptor_path()
        path = self.descriptor_path()
        dcontrols = {}
        if path:
            try:
                with open(path) as f:
                    for c in json.load(f).get("controls", []):
                        dcontrols[str(c["id"])] = c
            except (OSError, ValueError, KeyError):
                pass
        result, seen = [], set()
        for name, rows in self.remote_pages(dcontrols) + self.layout_pages(path):
            rows = [r for r in rows if r[0] not in seen]
            seen.update(r[0] for r in rows)
            if rows:
                result.append((name, rows))
        return result

    def layout_pages(self, path):
        """The pages of the plugin's descriptor: omx-panel's pages of eight, else the parameter order cut in eights."""
        dlist, dcontrols = [], {}
        if path:
            try:
                with open(path) as f:
                    dlist = json.load(f).get("controls", [])
                for c in dlist:
                    dcontrols[str(c["id"])] = c
            except (OSError, ValueError, KeyError) as e:
                logging.error(f"Can't read descriptor {path} => {e}")
        layout = panel_layout(path) if path else None
        if layout:
            result = []
            for page in layout[0]:
                rows = []
                for pid in filter(None, page["ids"]):
                    lc, dc = layout[1].get(str(pid), {}), dcontrols.get(str(pid), {})
                    if lc.get("widget") in ("meter", "display") or dc.get("readonly"):
                        continue
                    # The daemon addresses a parameter by the descriptor's id (a CLAP parameter's number, an LV2
                    # port's symbol); the layout's symbol is omx-panel's own name for it (p<number> for CLAP)
                    rows.append((str(pid), lc.get("label") or dc.get("name"), lc.get("unit"), dc))
                if rows:
                    result.append((page.get("pageName") or page.get("sectionName"), rows))
            return result
        rows = [(s, n, None, c) for s, n, c in self.param_list(dlist) if not c.get("hidden") and not c.get("readonly")]
        if len(rows) <= 8:
            return [("Params", rows)] if rows else []
        return [(f"Page {i // 8 + 1}", rows[i:i + 8]) for i in range(0, len(rows), 8)]

    def generate_ctrl_screens(self, zctrl_dict):
        """A page holds more controls than the four encoders: each of its screens is named by what is on it
        ("Band 1: Freq Gain Q Type", "Band 1: On"), not by a numbered suffix."""
        first = len(self._ctrl_screens)
        super().generate_ctrl_screens(zctrl_dict)
        added = self._ctrl_screens[first:]
        by_group = {}
        for screen in added:
            by_group.setdefault(zctrl_dict[screen[1][0]].group_symbol, []).append(screen)
        for screens in by_group.values():
            if len(screens) > 1:
                group = zctrl_dict[screens[0][1][0]].group_name
                for screen in screens:
                    labels = [self.labels.get(symbol) or zctrl_dict[symbol].name for symbol in screen[1]]
                    screen[0] = f"{group}: {' '.join(labels)}"

    def build_controllers(self):
        """A zctrl per control, grouped by page: generate_ctrl_screens cuts each page in screens of four.
        Unit, scale and range come from param_info when it answers, else from the descriptor."""
        zctrls = {}
        pages = self.pages()
        priority = sum(len(rows) for _, rows in pages) + 1
        for k, (page_name, rows) in enumerate(pages):
            for symbol, label, unit, meta in rows:
                travel = meta.get("travel", {})
                o = {"min": travel.get("min", 0.0), "max": travel.get("max", 1.0), "unit": unit or travel.get("unit"),
                     "default": travel.get("default", meta.get("default", 0.0)), "scale": travel.get("scale", "linear")}
                info = self.send_quiet(f"param_info {self.instance} {symbol}")
                if info:
                    w = info.split()
                    o.update(unit=w[0], scale=w[1], min=float(w[2]), max=float(w[3]), default=float(w[4]))
                value = self.send_quiet(f"param_get {self.instance} {symbol}")
                unit = o["unit"] if o["unit"] not in (None, "", "-", "unknown") else ""
                zopts = {"name": f"{label} {unit}" if unit else label, "group_symbol": f"page{k}",
                         "group_name": page_name, "display_priority": priority, "value_min": o["min"],
                         "value_max": o["max"], "value_default": o["default"], "is_logarithmic": o["scale"] == "log",
                         "is_integer": o["scale"] == "stepped"}
                if meta.get("kind") == "toggle":
                    zopts.update(is_toggle=True, is_integer=True, value_min=0, value_max=1, labels=["off", "on"],
                                 ticks=[0, 1], value_default=int(bool(meta.get("default"))))
                elif meta.get("kind") == "choice" and meta.get("values"):
                    zopts.update(labels=meta.get("labels") or meta["ids"], ticks=meta["values"], is_integer=True,
                                 value_min=meta["values"][0], value_max=meta["values"][-1])
                    if meta.get("default") in meta.get("ids", []):
                        zopts["value_default"] = meta["values"][meta["ids"].index(meta["default"])]
                zopts["value"] = float(value) if value else zopts["value_default"]
                zctrls[symbol] = zynthian_controller(self, symbol, zopts)
                self.labels[symbol] = label
                priority -= 1
        if self.type == "Audio Effect":
            self.bypass_zctrl = zctrls["bypass"] = zynthian_controller(self, "bypass", {
                "name": "bypass", "is_toggle": True, "is_bypass": True, "bypass_value": 1, "value_max": 1,
                "value_default": 0, "value": 0, "labels": ["inline", "bypass"], "ticks": [0, 1],
                "display_priority": 0, "not_on_gui": True})
        return zctrls

    def get_controllers_dict(self, processor):
        zctrls = super().get_controllers_dict(processor)
        for zctrl in self.zctrls.values():
            zctrl.set_options({"processor": processor})
        zctrls.update(self.zctrls)
        return zctrls

    def value_line(self, zctrl):
        if zctrl is self.bypass_zctrl:
            return f"bypass {self.instance} {int(zctrl.value)}"
        return f"param_set {self.instance} {zctrl.symbol} {zctrl.value}"

    def send_controller_value(self, zctrl):
        if self.instance is not None:
            self.send(self.value_line(zctrl))

    def refresh_values(self):
        """Read every value back, after a preset or a state changed them in the plugin."""
        for symbol, zctrl in self.zctrls.items():
            if zctrl is not self.bypass_zctrl:
                value = self.send_quiet(f"param_get {self.instance} {symbol}")
                if value:
                    zctrl.set_value(float(value), False)

    # ---- Presets ----
    # Every plugin shows the same two banks, whatever its format (plugin-hostd#269): Factory, the presets the
    # plugin ships, and User, the user's; plain names, sorted by name, the same save, rename and delete. The format is
    # only in a preset's key: an LV2 preset's URI (its bundle's, or a user preset Zynthian saved under jalv, from
    # zynthian_lv2's cache), a key of the daemon's preset_get for a CLAP preset, or the path of a preset file the worker
    # wrote through preset_save. A bank with nothing in it shows one row that says so, which loads nothing.

    NO_PRESETS = "No presets"
    FACTORY = ("factory", None, "Factory", None)
    USER = ("user", None, "User", None)

    def is_lv2(self):
        return not self.plugin_uri.startswith("clap:")

    def lv2_preset_info(self):
        from zyngine import zynthian_lv2
        try:
            return zynthian_lv2.get_plugin_presets_cache(self.plugin_name) or {}
        except Exception as e:
            log_once(("presets", self.nickname), "no LV2 presets cache for %s: %s", self.plugin_name, e)
            return {}

    def user_dir(self):
        return os.path.join(self.my_data_dir, "presets", "plugin-hostd", re.sub(r"[^A-Za-z0-9._-]+", "_", self.plugin_name))

    def is_lv2_user_url(self, url):
        return isinstance(url, str) and url.startswith(f"file://{self.my_data_dir}/presets/lv2/")

    def preset_files(self):
        """[(path, name)] of the preset files the worker wrote for this plugin."""
        found = []
        try:
            names = os.listdir(self.user_dir())
        except OSError:
            return found
        for f in names:
            if f.endswith(".json"):
                path = os.path.join(self.user_dir(), f)
                try:
                    with open(path) as fh:
                        found.append((path, json.load(fh).get("name") or f[:-5]))
                except (OSError, ValueError) as e:
                    log_once(("user-preset", path), "a user preset of %s cannot be read (%s): %s", self.plugin_name, path, e)
        return found

    def factory_presets(self):
        """[(key, name)] of the presets the plugin ships."""
        if self.is_lv2():
            return [(p["url"], p["label"].replace("_", " ").strip()) for info in self.lv2_preset_info().values()
                    for p in info.get("presets", []) if not self.is_lv2_user_url(p.get("url"))]
        presets = []
        try:
            for i in range(daemon.call(f"preset_list {self.instance}")[0]):
                key, _, name = daemon.call(f"preset_get {self.instance} {i}")[1].partition(" ")
                presets.append((key, name or key))
        except FAILURES as e:
            log_once(("presets", self.nickname), "plugin-hostd lists no presets for %s: %s", self.plugin_name, e)
        return presets

    def user_presets(self):
        """[(key, name)] of the user's presets: the files saved through plugin-hostd, and an LV2 plugin's under jalv."""
        lv2 = [(p["url"], p["label"].replace("_", " ").strip()) for info in self.lv2_preset_info().values()
               for p in info.get("presets", []) if self.is_lv2_user_url(p.get("url"))] if self.is_lv2() else []
        return self.preset_files() + lv2

    def get_bank_list(self, processor=None):
        return [self.FACTORY, self.USER]

    def set_bank(self, processor, bank):
        return True

    def get_preset_list(self, bank, processor=None):
        user = bool(bank) and bank[0] == self.USER[0]
        listed = self.user_presets() if user else self.factory_presets()
        presets = [[key, None, name, (self.USER if user else self.FACTORY)[0]]
                   for key, name in sorted(listed, key=lambda p: p[1].lower())]
        return presets or [["", None, self.NO_PRESETS, ""]]

    def is_preset_user(self, preset):
        if not preset or not isinstance(preset[0], str):
            return False
        return preset[0] == self.USER[0] or preset[0].startswith(self.user_dir() + os.sep) or self.is_lv2_user_url(preset[0])

    def edit_preset_file(self, path, **members):
        with open(path) as fh:
            doc = json.load(fh)
        doc.update(members)
        with open(path + ".tmp", "w") as fh:
            json.dump(doc, fh, indent=2)
        os.replace(path + ".tmp", path)

    def preset_exists(self, bank, preset_name):
        return any(name == preset_name.strip() for _, name in self.preset_files())

    def save_preset(self, bank, preset_name):
        """The instance into a user preset file through the worker (preset_save), whatever the bank asked; its path,
        None when the save failed."""
        name = preset_name.strip()
        path = os.path.join(self.user_dir(), re.sub(r"[^A-Za-z0-9._-]+", "_", name) + ".json")
        os.makedirs(self.user_dir(), exist_ok=True)
        quoted = name.replace("\\", "\\\\").replace('"', '\\"')
        if self.send(f'preset_save {self.instance} {path} "{quoted}"') is None:
            return None
        self.preset_key = path
        for processor in self.processors:
            if not processor.bank_list:
                processor.get_bank_list()
            processor.set_bank_by_id(self.USER[0])
        return path

    def delete_preset(self, bank, preset):
        if preset and isinstance(preset[0], str) and preset[0].startswith(self.user_dir() + os.sep):
            os.unlink(preset[0])
        return len(self.user_presets())

    def rename_preset(self, bank, preset, new_preset_name):
        if preset and isinstance(preset[0], str) and preset[0].startswith(self.user_dir() + os.sep):
            self.edit_preset_file(preset[0], name=new_preset_name.strip())

    def set_preset(self, processor, preset, preload=False):
        if not preset or not preset[0] or self.send(f"preset_load {self.instance} {preset[0]}") is None:
            return False
        self.preset_key = preset[0]
        self.refresh_values()
        return True

    def cmp_presets(self, preset1, preset2):
        try:
            return preset1[0] == preset2[0]
        except (TypeError, IndexError):
            return False

    # ---- Snapshots: the plugin's own state through state_save and state_load ----

    def get_extended_config(self):
        """The files state_save writes for this instance, as a base64 tar."""
        if self.instance is None:
            return None
        with tempfile.TemporaryDirectory(prefix="ph-state-") as tmp:
            try:
                daemon.call(f"state_save {tmp}", SLOW_TIMEOUT)
            except FAILURES as e:
                log_once("state_save", "plugin-hostd state_save failed (%s): snapshots keep parameters and presets only", e)
                return None
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as tar:
                for name in sorted(os.listdir(tmp)):
                    m = STATE_FILE_RE.match(name)
                    if m and int(m[1]) == self.instance:
                        tar.add(os.path.join(tmp, name), arcname="effect_X" + name[m.end(1):])
        return {"plugin": self.plugin_uri, "state": base64.b64encode(buf.getvalue()).decode()}

    def set_extended_config(self, xconfig):
        """Called before the processor is added: the state waits for the instance."""
        if not xconfig or xconfig.get("plugin") != self.plugin_uri:
            return
        if self.instance is None:
            self.pending_state = xconfig
        else:
            self.load_state(xconfig)

    def load_state(self, xconfig):
        with tempfile.TemporaryDirectory(prefix="ph-state-") as tmp:
            with tarfile.open(fileobj=io.BytesIO(base64.b64decode(xconfig["state"])), mode="r:gz") as tar:
                for member in tar.getmembers():
                    if member.name.startswith("effect_X") and ".." not in member.name:
                        member.name = f"effect_{self.instance}" + member.name[len("effect_X"):]
                        tar.extract(member, tmp, filter="data")
            if self.send(f"state_load {tmp}", SLOW_TIMEOUT) is not None:
                self.refresh_values()
