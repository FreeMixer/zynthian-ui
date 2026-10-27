# -*- coding: utf-8 -*-
# ******************************************************************************
# ZYNTHIAN PROJECT: plugin qualification (zynthian_plugin_qualification)
#
# What plugin-qualify found about each plugin, for the plugin menus: the files in qualification_dir
# (the measurement documents, lv2-plugin-measurements format 1.x, and the per-plugin records
# plugin-qualify-sync writes, plugin-qualify.qualification-record/1) and the verdict word of each
# pin line (pin_set <uri> <files> <layout> verdict=<word>). A plugin with neither is simply not
# qualified yet. Nothing here makes up a figure the files do not hold.
#
# Copyright (C) 2026 Pau Aliagas <linuxnow@gmail.com>
#
# This program is free software; you can redistribute it and/or modify it under the terms
# of the GNU General Public License as published by the Free Software Foundation; either
# version 2 of the License, or any later version. See the LICENSE.txt file.
# ******************************************************************************

import os
import json
import logging
from collections import namedtuple

DOCUMENT_FORMAT = "lv2-plugin-measurements"
RECORD_SCHEMA = "plugin-qualify.qualification-record/1"
PIN_VERDICTS = ("in-process", "isolated", "refused")  # plugin-hostd's PHD_PIN_VERDICTS
CRASH_CODES = ("hosting.stability.crashed-attributed-plugin", "hosting.stability.crashed-live-on-this-rig",
               "hosting.stability.crash-unattributed")
UNSOAKED_CODE = "hosting.stability.unsoaked"
PROBE_OK = "probe.ok"
PROBE_CRASHED = "probe.crashed"
PROBE_TIMEOUT = "probe.timeout"
DIMENSIONS = ("latency", "cost", "stability", "rtSafety", "features")

# One plugin as the menus see it. record: a measurement document holds it. pin: its pin verdict word
# or None. latency_ms: at the current rate, None when not measured there and no scaling law carries
# it. worst_latency_ms: the worst measured rate. rt_safe, soaked: True, False or None (not measured).
Facts = namedtuple("Facts", "record pin deciding crashed hung latency_ms worst_latency_ms rt_safe soaked")
NO_FACTS = Facts(False, None, None, False, False, None, None, None, None)


def setting(name, default):
    return os.environ.get(name, "").strip() or default


def qualification_dir():
    """ZYNTHIAN_QUALIFICATION_DIR, else plugin-hostd's qualification_dir for a system service."""
    return setting("ZYNTHIAN_QUALIFICATION_DIR", setting("OMX_QUALIFICATION_DIR", "/var/lib/omx/qualification"))


def pin_paths():
    """ZYNTHIAN_QUALIFICATION_PINS: pin files or directories of *.pins, ':' between them."""
    return [p for p in setting("ZYNTHIAN_QUALIFICATION_PINS", "/usr/share/plugin-hostd/pins.d").split(":") if p]


# ---- Reading ----

def read_pins(paths):
    """{uri: verdict word or None}, the last line of a uri winning, as plugin-hostd reads them."""
    files = []
    for path in paths:
        if os.path.isdir(path):
            files += [os.path.join(path, n) for n in sorted(os.listdir(path)) if n.endswith(".pins")]
        elif os.path.isfile(path):
            files.append(path)
    pins = {}
    for path in files:
        try:
            with open(path) as f:
                lines = f.read().splitlines()
        except OSError as e:
            logging.warning(f"Can't read pin file {path} => {e}")
            continue
        for n, line in enumerate(lines, 1):
            words = line.split()
            if len(words) < 4 or words[0] != "pin_set":
                continue
            verdict = next((w[8:] for w in words[4:] if w.startswith("verdict=")), None)
            if verdict is not None and verdict not in PIN_VERDICTS:
                logging.warning(f"{path}:{n}: verdict={verdict} is not one of {', '.join(PIN_VERDICTS)}: not read")
                verdict = None
            pins[words[1]] = verdict
    return pins


def record_key(rec):
    """What the menus and the pins call a record's plugin: the LV2 uri, or clap:<path>#<id> for a CLAP
    file, since DPF-built files share one CLAP id. None when the record does not say."""
    plugin, binary = rec.get("plugin"), rec.get("binary")
    if not isinstance(plugin, dict) or not plugin.get("id"):
        return None
    if plugin.get("format") == "clap":
        path = binary.get("path") if isinstance(binary, dict) else None
        return f"clap:{path}#{plugin['id']}" if path else None
    return plugin["id"]


def record_entry(rec):
    """(measuredAt, plugin, verdicts) of a qualification record in the shape of a measurement document's
    plugin, from the figures the record holds: the latency each rate measured, the interposer's
    counts over the grid, whether the soak was left unmeasured, and a run that crashed or timed out.
    None when the record holds no figure and no crash or timeout: it says nothing about the plugin."""
    grid = rec.get("measurements", {}).get("grid", [])
    ok = [p for p in grid if isinstance(p, dict) and p.get("outcome") == PROBE_OK]
    outcome = rec.get("probe", {}).get("outcome")
    if not ok and outcome not in (PROBE_CRASHED, PROBE_TIMEOUT):
        return None
    plugin = {}
    per_rate = {}
    for p in ok:
        lat = p.get("latency")
        if isinstance(lat, dict) and isinstance(lat.get("measuredFrames"), (int, float)) and isinstance(p.get("rate"), int):
            per_rate.setdefault(str(p["rate"]), {"frames": lat["measuredFrames"]})
    plugin["latency"] = {"perRate": per_rate}
    points = [p["rtSafety"] for p in ok if isinstance(p.get("rtSafety"), dict)]
    if points and all(r.get("interposer") is True for r in points):
        counts = {}
        for key, name in (("allocations", "allocationsInRun"), ("locks", "locksInRun"), ("syscalls", "syscallsInRun")):
            values = [r.get(key) for r in points]
            if all(isinstance(v, (int, float)) for v in values):
                counts[name] = sum(values)
        plugin["rtSafety"] = {"interposer": True, "swept": all(r.get("swept") is True for r in points),
                              "sweepApplicable": any(r.get("sweepApplicable") is True for r in points), **counts}
    stability = {}
    if any(n.get("code") == UNSOAKED_CODE for n in rec.get("notMeasured", []) if isinstance(n, dict)):
        stability["unmeasuredReason"] = "unsoaked"
    if outcome == PROBE_CRASHED:
        stability["crashes"] = [PROBE_CRASHED]
    if stability:
        plugin["stability"] = stability
    if outcome == PROBE_TIMEOUT:
        plugin["latency"]["unmeasuredReason"] = "probe-timeout"
    verdicts = [v for v in rec.get("verdicts", []) if isinstance(v, dict)]
    return str(rec.get("measuredAt", "")), plugin, verdicts


def read_documents(path):
    """{key: (measuredAt, plugin, [verdicts])} of both formats in path, the latest run of each plugin
    winning: the measurement documents by LV2 uri, the qualification records by record_key."""
    found = {}

    def keep(key, when, plugin, verdicts):
        if key and (key not in found or found[key][0] <= when):
            found[key] = (when, plugin, verdicts)

    for top, dirs, names in os.walk(path):
        dirs.sort()
        for name in sorted(n for n in names if n.endswith(".json")):
            fpath = os.path.join(top, name)
            try:
                with open(fpath) as f:
                    doc = json.load(f)
            except (OSError, ValueError) as e:
                logging.warning(f"Can't read qualification file {fpath} => {e}")
                continue
            if not isinstance(doc, dict):
                continue
            if doc.get("schema") == RECORD_SCHEMA:
                entry = record_entry(doc)
                if entry:
                    keep(record_key(doc), *entry)
                continue
            if doc.get("format") != DOCUMENT_FORMAT:
                continue
            when = str(doc.get("run", {}).get("measuredAt", ""))
            verdicts = {}
            for v in doc.get("verdicts", []):
                verdicts.setdefault(v.get("uri"), []).append(v)
            for plugin in doc.get("plugins", []):
                uri = plugin.get("uri")
                keep(uri, when, plugin, verdicts.get(uri, []))
    return found


# ---- One plugin's facts ----

def latency_readings(plugin):
    """{rate: frames} of the readings a consumer may resolve a rate to."""
    readings = {}
    for rate, r in plugin.get("latency", {}).get("perRate", {}).items():
        if not r.get("excludedReason") and isinstance(r.get("frames"), (int, float)):
            readings[int(rate)] = r["frames"]
    return readings


def latency_at(plugin, rate):
    """ms at rate: its own reading, else what the fitted scaling law says, else None."""
    readings = latency_readings(plugin)
    if not rate:
        return None
    if rate in readings:
        return readings[rate] * 1000.0 / rate
    scaling = plugin.get("latency", {}).get("scalingClass")
    if not readings:
        return None
    if scaling == "zero":
        return 0.0
    first = min(readings)
    if scaling == "fixed-frame":
        return readings[first] * 1000.0 / rate
    if scaling == "fixed-time":
        return readings[first] * 1000.0 / first
    return None


def worst_latency(plugin):
    readings = latency_readings(plugin)
    return max(frames * 1000.0 / rate for rate, frames in readings.items()) if readings else None


def rt_safe(plugin):
    """True on a clean count of a loaded interposer over swept controls, False on any count, None unmeasured."""
    rt = plugin.get("rtSafety", {})
    counts = [rt.get(k) for k in ("allocationsInRun", "locksInRun", "syscallsInRun")]
    if not rt.get("interposer") or any(c is None for c in counts):
        return None
    if any(counts):
        return False
    return True if rt.get("swept") or rt.get("sweepApplicable") is False else None


def soaked(plugin, deciding):
    stability = plugin.get("stability")
    if deciding == UNSOAKED_CODE:
        return False
    if not stability:
        return None
    rates = stability.get("soak", {}).get("perRate", {})
    if not rates:
        return False
    return all(not r.get("died") and r.get("seconds", 0) > 0 for r in rates.values())


def crashed(plugin, deciding):
    stability = plugin.get("stability", {})
    return bool(stability.get("crashes")) or deciding in CRASH_CODES \
        or any(r.get("died") for r in stability.get("soak", {}).get("perRate", {}).values()) \
        or any(plugin.get(d, {}).get("unmeasuredReason") == "probe-crashed" for d in DIMENSIONS)


def hung(plugin):
    return any(plugin.get(d, {}).get("unmeasuredReason") == "probe-timeout" for d in DIMENSIONS)


def facts_of(entry, pin, rate):
    """Facts from a document entry (measuredAt, plugin, verdicts) or None, and a pin word or None."""
    if entry is None:
        return NO_FACTS._replace(pin=pin)
    plugin, verdicts = entry[1], entry[2]
    # The code that decided the worst verdict: the one a refusal or a crash is explained by
    order = ("unsuitable", "unknown", "conditional", "suitable")
    worst = sorted(verdicts, key=lambda v: order.index(v.get("rating")) if v.get("rating") in order else 0)
    deciding = next((v.get("deciding") for v in worst if v.get("deciding")), None)
    return Facts(True, pin, deciding, crashed(plugin, deciding), hung(plugin), latency_at(plugin, rate),
                 worst_latency(plugin), rt_safe(plugin), soaked(plugin, deciding))


class qualification:
    """Every plugin's facts, read again when a file changes."""

    def __init__(self, path=None, pins=None):
        self.path = path
        self.pins = pins
        self.stamp = None
        self.documents = {}
        self.pin_verdicts = {}

    def files(self):
        path = self.path or qualification_dir()
        pins = self.pins if self.pins is not None else pin_paths()
        stamp = []
        for p in [path] + pins:
            for top, dirs, names in os.walk(p) if os.path.isdir(p) else [(os.path.dirname(p), [], [os.path.basename(p)])]:
                for n in sorted(names):
                    try:
                        st = os.stat(os.path.join(top, n))
                        stamp.append((top, n, st.st_mtime_ns, st.st_size))
                    except OSError:
                        pass
        return path, pins, stamp

    def refresh(self):
        path, pins, stamp = self.files()
        if stamp != self.stamp:
            self.documents = read_documents(path) if os.path.isdir(path) else {}
            self.pin_verdicts = read_pins(pins)
            self.stamp = stamp
        return self

    def facts(self, uri, rate):
        return facts_of(self.documents.get(uri), self.pin_verdicts.get(uri), rate)
