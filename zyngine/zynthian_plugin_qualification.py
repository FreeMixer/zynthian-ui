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
# The figures a menu row shows, as the record holds them and None where it does not: frames and
# declared_frames, the measured and the declared latency at the current rate; cost_ns, the median
# cost in ns per sample measured at the current rate and quantum.
Facts = namedtuple("Facts", "record pin deciding crashed hung latency_ms worst_latency_ms rt_safe soaked "
                            "frames declared_frames cost_ns", defaults=(None, None, None))
NO_FACTS = Facts(False, None, None, False, False, None, None, None, None)


def setting(name, default):
    return os.environ.get(name, "").strip() or default


def qualification_dir():
    """ZYNTHIAN_QUALIFICATION_DIR, else plugin-hostd's qualification_dir for a system service."""
    return setting("ZYNTHIAN_QUALIFICATION_DIR", setting("OMX_QUALIFICATION_DIR", "/var/lib/omx/qualification"))


PINS_DIR = "/usr/share/plugin-hostd/pins.d"


def conf_pin_file():
    """The pin_file of plugin-hostd's config, where the qualification sync writes its verdicts and the daemon
    reads them: ZYNTHIAN_PLUGIN_HOSTD_CONF, else plugin-hostd.conf in the config dir, as the PH engine
    finds it. None when the file or the key is absent."""
    conf = setting("ZYNTHIAN_PLUGIN_HOSTD_CONF",
                   os.path.join(setting("ZYNTHIAN_CONFIG_DIR", "/zynthian/config"), "plugin-hostd.conf"))
    try:
        with open(conf) as f:
            for line in f:
                words = line.split()
                if len(words) >= 2 and words[0] == "pin_file":
                    return words[1]
    except OSError:
        pass
    return None


def pin_paths():
    """ZYNTHIAN_QUALIFICATION_PINS: pin files or directories of *.pins, ':' between them. Unset, the pins
    plugin-hostd reads: its package's pins.d and the pin_file of its config."""
    paths = setting("ZYNTHIAN_QUALIFICATION_PINS", "")
    if paths:
        return [p for p in paths.split(":") if p]
    pin_file = conf_pin_file()
    return [PINS_DIR] + ([pin_file] if pin_file else [])


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
            reading = {"frames": lat["measuredFrames"]}
            if isinstance(lat.get("declaredFrames"), (int, float)):
                reading["declaredFrames"] = lat["declaredFrames"]
            per_rate.setdefault(str(p["rate"]), reading)
    plugin["latency"] = {"perRate": per_rate}
    plugin["cost"] = {"grid": [{"rate": p.get("rate"), "quantum": p.get("quantum"), "median": p["cost"].get("median")}
                               for p in ok if isinstance(p.get("cost"), dict)]}
    points = [p["rtSafety"] for p in ok if isinstance(p.get("rtSafety"), dict)]
    if points and all(r.get("interposer") is True for r in points):
        counts = {}
        for key, name in (("allocations", "allocationsInRun"), ("locks", "locksInRun"), ("syscalls", "syscallsInRun"),
                          ("threadViolations", "threadViolationsInRun")):
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


def file_entries(fpath):
    """[(key, measuredAt, plugin, [verdicts])] of one file: a qualification record or a measurement document."""
    try:
        with open(fpath) as f:
            doc = json.load(f)
    except (OSError, ValueError) as e:
        logging.warning(f"Can't read qualification file {fpath} => {e}")
        return []
    if not isinstance(doc, dict):
        return []
    if doc.get("schema") == RECORD_SCHEMA:
        entry = record_entry(doc)
        return [(record_key(doc), *entry)] if entry else []
    if doc.get("format") != DOCUMENT_FORMAT:
        return []
    when = str(doc.get("run", {}).get("measuredAt", ""))
    verdicts = {}
    for v in doc.get("verdicts", []):
        verdicts.setdefault(v.get("uri"), []).append(v)
    return [(plugin.get("uri"), when, plugin, verdicts.get(plugin.get("uri"), []))
            for plugin in doc.get("plugins", [])]


def read_documents(path, cache=None):
    """{key: (measuredAt, plugin, [verdicts])} of both formats in path, the latest run of each plugin
    winning: the measurement documents by LV2 uri, the qualification records by record_key.
    cache, {file: (mtime, size, entries)}, keeps what each file held: a file is read again only when
    its mtime or size changed, and a file gone from path leaves the cache."""
    cache = {} if cache is None else cache
    found = {}
    seen = set()
    for top, dirs, names in os.walk(path):
        dirs.sort()
        for name in sorted(n for n in names if n.endswith(".json")):
            fpath = os.path.join(top, name)
            try:
                st = os.stat(fpath)
            except OSError:
                continue
            seen.add(fpath)
            stamp = (st.st_mtime_ns, st.st_size)
            if cache.get(fpath, (None,))[:2] != stamp:
                cache[fpath] = (*stamp, file_entries(fpath))
            for key, when, plugin, verdicts in cache[fpath][2]:
                if key and (key not in found or found[key][0] <= when):
                    found[key] = (when, plugin, verdicts)
    for fpath in set(cache) - seen:
        del cache[fpath]
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


def latency_frames(plugin, rate):
    """(measured, declared) frames the record holds at rate, each None when it holds none; no other
    rate's reading and no scaling law stands in for them."""
    r = plugin.get("latency", {}).get("perRate", {}).get(str(rate), {})
    if r.get("excludedReason"):
        return None, None
    frames, declared = r.get("frames"), r.get("declaredFrames")
    return (frames if isinstance(frames, (int, float)) else None,
            declared if isinstance(declared, (int, float)) else None)


def cost_at(plugin, rate, quantum):
    """The median cost in ns per sample measured at rate and quantum: a record's grid point, or a
    measurement document's reading at rate taken in blocks of quantum frames. None when not measured there."""
    for p in plugin.get("cost", {}).get("grid", []):
        if p.get("rate") == rate and p.get("quantum") == quantum and isinstance(p.get("median"), (int, float)):
            return p["median"]
    r = plugin.get("cost", {}).get("perRate", {}).get(str(rate), {})
    if r.get("blockFrames") == quantum and isinstance(r.get("nsPerSampleMedian"), (int, float)):
        return r["nsPerSampleMedian"]
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
    if any(counts) or rt.get("threadViolationsInRun"):
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


def facts_of(entry, pin, rate, quantum=None):
    """Facts from a document entry (measuredAt, plugin, verdicts) or None, and a pin word or None,
    at the current rate and quantum."""
    if entry is None:
        return NO_FACTS._replace(pin=pin)
    plugin, verdicts = entry[1], entry[2]
    # The code that decided the worst verdict: the one a refusal or a crash is explained by
    order = ("unsuitable", "unknown", "conditional", "suitable")
    worst = sorted(verdicts, key=lambda v: order.index(v.get("rating")) if v.get("rating") in order else 0)
    deciding = next((v.get("deciding") for v in worst if v.get("deciding")), None)
    return Facts(True, pin, deciding, crashed(plugin, deciding), hung(plugin), latency_at(plugin, rate),
                 worst_latency(plugin), rt_safe(plugin), soaked(plugin, deciding),
                 *latency_frames(plugin, rate), cost_at(plugin, rate, quantum))


class qualification:
    """Every plugin's facts, read again when a file changes: a record once per mtime, never loading a plugin."""

    def __init__(self, path=None, pins=None):
        self.path = path
        self.pins = pins
        self.stamp = None
        self.documents = {}
        self.pin_verdicts = {}
        self.cache = {}

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
            self.documents = read_documents(path, self.cache) if os.path.isdir(path) else {}
            self.pin_verdicts = read_pins(pins)
            self.stamp = stamp
        return self

    def facts(self, uri, rate, quantum=None):
        return facts_of(self.documents.get(uri), self.pin_verdicts.get(uri), rate, quantum)
