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
import shutil
import socket
import logging
import tarfile
import tempfile
import subprocess

from zyngine import zynthian_plugin_hostd_protocol
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
SLOW_TIMEOUT = 30.0  # add, chain, state_save and state_load spawn or move workers
START_TIMEOUT = 10.0
DESCRIPTOR_WAIT = 5.0
PROBE_TIMEOUT = 30.0
# The worker rows whose chain is one JACK client, named like its head (measured on a Pi 5 with plugin-hostd
# 0.12.0 and omx-clap-host 0.22.1, jack_lsp on a private server: a chain on the clap row, and one on the
# lv2-clap row, LV2 members included, is the one client effect_<head> with the chain's ports; a chain on the
# lv2 row, run by mod-host, keeps a client per member). plugin-hostd declares the row, not the client, so
# this is the one fact here that comes from a measurement; a row not listed keeps a client per member.
ONE_CLIENT_ROWS = ("clap", "lv2-clap")
STATE_FILE_RE = re.compile(r"^effect_(\d+)(?=\D|$)")

# The reply codes and the readiness line are plugin-hostd's own declaration, never retyped here
protocol = zynthian_plugin_hostd_protocol.protocol()
ERROR_NAMES = protocol.names
CHAIN_FALLBACK_CODES = tuple(protocol.codes[n] for n in (
    "PLACEMENT_INVALID", "CHAIN_DUPLICATE", "CHAIN_INSTANCE_BUSY", "CHAIN_MIXED_FORMAT")) if protocol.loaded else ()
DESCRIPTOR_PENDING = protocol.codes.get("DESCRIPTOR_PENDING")
DESCRIPTOR_NONE = tuple(protocol.codes[n] for n in ("DESCRIPTOR_ABSENT", "DESCRIPTOR_SCAN_FAILED")) \
    if protocol.loaded else ()

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


# ---- The daemon: one per Zynthian, started when none answers, reused when one does ----

class plugin_hostd_daemon:

    def __init__(self, port=DEFAULT_PORT):
        self.client = plugin_hostd_client(port)
        self.binary = "plugin-hostd"
        self.conf = os.environ.get("ZYNTHIAN_PLUGIN_HOSTD_CONF",
                                   os.path.join(zynthian_engine.config_dir, "plugin-hostd.conf"))
        self.proc = None  # set only when this process started the daemon
        self.engines = {}  # instance number => engine
        self.chains = {}  # zynthian chain id => member instances declared to the daemon
        self.chain_refused = {}  # zynthian chain id => members the daemon refused to run together
        self.clients = {}  # zynthian chain id => {member instance: jack client of the chain's worker}

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
        if not protocol.ready_line:
            raise plugin_hostd_down("plugin-hostd's protocol.json is not installed: no declared readiness line")
        if self.identify():
            return
        binary = shutil.which(self.binary)
        if not binary:
            raise plugin_hostd_down(f"no daemon on port {self.client.port} and {self.binary} is not installed")
        cmd = [binary, "-n", "-p", str(self.client.port)]
        if os.path.isfile(self.conf):
            cmd += ["-c", self.conf]
        logging.info(f"Starting {' '.join(cmd)}")
        log_path = os.path.join(tempfile.gettempdir(), "plugin-hostd.log")
        with open(log_path, "ab") as log:
            seen = log.tell()
            self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        # Ready is the line the daemon declares, printed once both its ports accept
        deadline = time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise plugin_hostd_down(f"{binary} exited with {self.proc.returncode}, see plugin-hostd.log")
            with open(log_path, "rb") as log:
                log.seek(seen)
                if protocol.ready_line in log.read().decode(errors="replace").splitlines():
                    return
            time.sleep(0.05)
        raise plugin_hostd_down(f"{binary} did not print '{protocol.ready_line}' in {START_TIMEOUT}s")

    def call(self, line, timeout=TIMEOUT):
        """One command; if the connection is gone, bring the daemon back, replay and send it again."""
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
        self.chains = {}
        self.clients = {}
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
        self.engines.pop(instance, None)
        try:
            self.client.call(f"remove {instance}")
        except (plugin_hostd_error, plugin_hostd_timeout, OSError) as e:
            logging.warning(f"plugin-hostd remove {instance} => {e}")
        if not self.engines:
            # The daemon serves one controller at a time: let another one in
            self.stop() if self.proc else self.client.close()

    def stop(self):
        """Stop the daemon, only if this process started it."""
        if not self.proc:
            return
        try:
            self.client.call("quit", 2.0)
        except (OSError, *FAILURES):
            pass
        self.client.close()
        try:
            self.proc.terminate()
            self.proc.wait(5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.proc = None


daemon = plugin_hostd_daemon()


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
    for record in records:
        fields = record.split(":")
        # <worker>:<pid>:<row>:<state>:chain:<chain>:<instance>,...
        if len(fields) >= 7 and fields[0] == worker and fields[4] == "chain" and fields[5] == str(chain_id):
            if fields[2] not in ONE_CLIENT_ROWS or fields[3] != "up":
                return {}
            members = [int(i) for i in running.split(",") if i.isdigit()]
            if members and members[0] in daemon.engines:
                return {i: daemon.engines[members[0]].jackname for i in members}
    return {}


def sync_chain(chain):
    """Run the plugin-hostd members of a zynthian chain in one worker, in signal order, then give each
    processor the jack client it has now: the chain's own for the members its worker runs as one client,
    the client of its instance for every other.
    Called from the chain's graph rebuild, so after insert, remove, swap and nudge."""
    declare_chain(chain)
    clients = daemon.clients.get(chain.chain_id, {})
    for slot in chain.synth_slots + chain.audio_slots:
        for p in slot:
            if isinstance(p.engine, zynthian_engine_plugin_hostd) and p.engine.instance is not None:
                p.jackname = clients.get(p.engine.instance, p.engine.jackname)


def declare_chain(chain):
    members = tuple(p.engine.instance for slot in chain.synth_slots + chain.audio_slots for p in slot
                    if isinstance(p.engine, zynthian_engine_plugin_hostd) and p.engine.instance is not None)
    chain_id = chain.chain_id
    if len(members) < 2:
        members = ()
    if daemon.chains.get(chain_id, ()) == members or daemon.chain_refused.get(chain_id) == members:
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
    except (plugin_hostd_down, plugin_hostd_timeout) as e:
        logging.error(f"plugin-hostd chain {chain_id} => {e}")
        return
    daemon.clients.pop(chain_id, None)
    if daemon.chains.pop(chain_id, None):
        try:
            daemon.call(f"chain_clear {chain_id}")
        except (OSError, *FAILURES) as e:
            logging.debug(f"plugin-hostd chain_clear {chain_id} => {e}")


# ---- Controller pages from omx-panel's layout of the plugin's descriptor ----

layout_cache = {}  # (descriptor path, mtime) => (pages, layout controls by param)


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
        layout, pages = run(), run("--pages")
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
        self.preset_key = None
        self.pending_state = None
        self.bypass_zctrl = None
        self.zctrls = {}
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
        processor.jackname = self.jackname
        self.set_midi_chan(processor)
        processor.refresh_controllers()

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

    def pages(self):
        """[(page name, [(symbol, label, unit, descriptor control)])]: omx-panel's pages of eight,
        else the plain parameter order cut in eights."""
        path = self.descriptor_path()
        dlist, dcontrols = [], {}
        if path:
            try:
                with open(path) as f:
                    dlist = json.load(f).get("controls", [])
                for c in dlist:
                    dcontrols[str(c["id"])] = c
                    dcontrols.setdefault(str(c.get("name")), c)
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
                    rows.append((str(lc.get("symbol", pid)), lc.get("label") or dc.get("name"), lc.get("unit"), dc))
                if rows:
                    result.append((page.get("pageName") or page.get("sectionName"), rows))
            return result
        rows = [(s, n, None, c) for s, n, c in self.param_list(dlist) if not c.get("hidden") and not c.get("readonly")]
        if len(rows) <= 8:
            return [("Params", rows)] if rows else []
        return [(f"Page {i // 8 + 1}", rows[i:i + 8]) for i in range(0, len(rows), 8)]

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

    # ---- Presets: the ones the plugin lists through the daemon ----

    def get_bank_list(self, processor=None):
        return [("", None, "Presets", None)]

    def set_bank(self, processor, bank):
        return True

    def get_preset_list(self, bank, processor=None):
        presets = []
        try:
            for i in range(daemon.call(f"preset_list {self.instance}")[0]):
                key, _, name = daemon.call(f"preset_get {self.instance} {i}")[1].partition(" ")
                presets.append([key, None, name or key, ""])
        except FAILURES as e:
            log_once(("presets", self.nickname), "plugin-hostd lists no presets for %s: %s", self.plugin_name, e)
        return presets

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
