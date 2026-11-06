# -*- coding: utf-8 -*-
# ******************************************************************************
# ZYNTHIAN PROJECT: plugin-hostd catalogue (zynthian_plugin_hostd_catalogue)
#
# The plugins the plugin-hostd engine offers: the LV2 instruments and effects
# zynthian_lv2 lists from the installed bundles, and the CLAP ones omx-clap-scan finds.
#
# Copyright (C) 2026 Pau Aliagas <linuxnow@gmail.com>
#
# This program is free software; you can redistribute it and/or modify it under the terms
# of the GNU General Public License as published by the Free Software Foundation; either
# version 2 of the License, or any later version. See the LICENSE.txt file.
# ******************************************************************************

import os
import re
import json
import shutil
import logging
import subprocess

CONFIG_DIR = os.environ.get('ZYNTHIAN_CONFIG_DIR', "/zynthian/config")
ENGINE_TYPES = ("MIDI Synth", "Audio Effect")
PLUGIN_ENGINES = ("plugin-hostd", "jalv")
# CLAP features: the engine type a plugin takes, then its category
CLAP_TYPES = (("instrument", "MIDI Synth"), ("audio-effect", "Audio Effect"))
CLAP_CATS = {
    "synthesizer": "Synth", "sampler": "Sampler", "drum": "Percussion", "drum-machine": "Percussion",
    "piano": "Piano", "organ": "Organ", "delay": "Delay", "reverb": "Reverb", "distortion": "Distortion",
    "compressor": "Dynamics", "expander": "Dynamics", "gate": "Dynamics", "limiter": "Dynamics",
    "transient-shaper": "Dynamics", "equalizer": "Filter & EQ", "filter": "Filter & EQ", "chorus": "Modulation",
    "flanger": "Modulation", "phaser": "Modulation", "tremolo": "Modulation", "pitch-shifter": "Pitch",
    "pitch-correction": "Pitch", "analyzer": "Analyzer", "surround": "Spatial", "ambisonic": "Spatial"}
# omx-clap-scan's text output: a "file <path>" line, then per plugin a "  plugin" line and its
# indented features, param, port and error lines (omx-clap-core src/scan.c)
CLAP_PLUGIN_RE = re.compile(r'^  plugin (\S+) "((?:[^"\\]|\\.)*)" vendor (.*) version (.*)$')
CLAP_PARAM_RE = re.compile(r'^param (\d+) (.*) \[(\S+) \.\. (\S+)\] default (\S+)\s*(\S*)$')

clap_plugins = {}  # engine key => the plugin as parse_clap_scan reads it


def parse_clap_scan(text):
    """The plugins in the text output of omx-clap-scan, the ones that loaded."""
    plugins, path, cur = [], None, None
    for line in text.splitlines():
        if line.startswith("file "):
            path, cur = line[5:], None
            continue
        m = CLAP_PLUGIN_RE.match(line)
        if m:
            cur = {"path": path, "id": m[1], "name": m[2].replace('\\"', '"'), "vendor": m[3], "version": m[4],
                   "features": [], "params": []}
            plugins.append(cur)
            continue
        words = line.strip()
        if cur is None:
            continue
        if words.startswith("features "):
            cur["features"] = words.split()[1:]
        elif words.startswith("error:"):
            cur["error"] = words[6:].strip()
        else:
            m = CLAP_PARAM_RE.match(words)
            if m:
                cur["params"].append({"id": m[1], "name": m[2], "min": float(m[3]), "max": float(m[4]),
                                      "default": float(m[5]), "stepped": "stepped" in m[6].split(",")})
    return [p for p in plugins if "error" not in p]


def clap_dirs():
    dirs = [d for d in os.environ.get("CLAP_PATH", "").split(":") if d]
    dirs += [os.path.expanduser("~/.clap"), "/usr/lib/clap", "/usr/local/lib/clap"]
    return [d for d in dict.fromkeys(dirs) if os.path.isdir(d)]


def scan_clap(cache_path=None):
    """omx-clap-scan of each CLAP directory, kept in a cache until a directory changes:
    the scan loads every plugin, so it does not run at every start."""
    cache_path = cache_path or os.path.join(CONFIG_DIR, "plugin-hostd-clap.json")
    stamp = {d: os.stat(d).st_mtime for d in clap_dirs()}
    try:
        with open(cache_path) as f:
            cache = json.load(f)
        if cache["stamp"] == stamp:
            return cache["plugins"]
    except (OSError, ValueError, KeyError):
        pass
    scanner = shutil.which("omx-clap-scan")
    if not scanner:
        logging.warning("omx-clap-scan is not installed: no CLAP plugins for plugin-hostd")
        return []
    plugins = []
    for d in stamp:
        try:
            out = subprocess.run([scanner, d], capture_output=True, text=True, timeout=300).stdout
            plugins += parse_clap_scan(out)
        except (OSError, subprocess.SubprocessError) as e:
            logging.error(f"omx-clap-scan {d} => {e}")
    try:
        with open(cache_path, "w") as f:
            json.dump({"stamp": stamp, "plugins": plugins}, f)
    except OSError as e:
        logging.warning(f"Can't write {cache_path} => {e}")
    return plugins


def plugin_engine():
    """The engine the menus offer LV2 plugins through: ZYNTHIAN_PLUGIN_ENGINE, plugin-hostd by default."""
    engine = os.environ.get("ZYNTHIAN_PLUGIN_ENGINE", "").strip().lower() or PLUGIN_ENGINES[0]
    if engine not in PLUGIN_ENGINES:
        logging.warning(f"ZYNTHIAN_PLUGIN_ENGINE={engine} is not one of {', '.join(PLUGIN_ENGINES)}: using {PLUGIN_ENGINES[0]}")
        engine = PLUGIN_ENGINES[0]
    return engine


def jalv_twin(key):
    """The jalv entry an LV2 PH entry was made from, None for a CLAP one."""
    return None if key.startswith("PH/clap:") or not key.startswith("PH/") else "JV/" + key[3:]


NOT_INSTALLED_WHY = "plugin-hostd is not installed, so it cannot run this plugin"


def short_meaning(meaning):
    """The first clause of a protocol meaning: what the code says, without how the daemon goes on."""
    return (meaning or "").split(": ")[0].strip()


def refusal_text(word, meaning=None):
    """Why plugin-hostd did not load a plugin: what the code means, then the daemon's word for it (PHD_ERR_ left out)."""
    word = (word or "").removeprefix("PHD_ERR_")
    return f"{short_meaning(meaning) or 'plugin-hostd refused it'} ({word})"


def plugin_name(info):
    """A plugin's name, case and spacing aside, as the menus match a CLAP row and an LV2 row of one plugin."""
    return " ".join((info.get("NAME") or "").casefold().split()) or None


def add_format_lines(added):
    """Give each entry its FORMAT_LINE, the line the detail panel shows: the format the row runs, as the menu titles
    leave it out, and the other build when the plugin is installed in both formats, e.g.
    "CLAP, run by plugin-hostd (an LV2 build is also installed)"."""
    formats = {}
    for info in added.values():
        formats.setdefault(plugin_name(info), set()).add(info["FORMAT"])
    for info in added.values():
        line = f"{info['FORMAT']}, run by plugin-hostd"
        others = sorted(formats.get(plugin_name(info), set()) - {info["FORMAT"]}) if plugin_name(info) else []
        for other in others:
            line += f" ({'an' if other == 'LV2' else 'a'} {other} build is also installed)"
        info["FORMAT_LINE"] = line


def add_catalogue(engines, clap=None, binary="plugin-hostd"):
    """Add a PH entry for each LV2 instrument and effect in engines and each CLAP one omx-clap-scan
    finds, to engines and to zynthian_lv2's lists by type. An LV2 one is enabled when its jalv entry is.
    With plugin-hostd as the plugin engine, the menus list the PH entry and not its jalv one; the jalv
    entry stays in engines, so a chain that still holds it loads as it was saved. Every plugin is listed,
    whatever its qualification found (the menus mark the ones below the bar): no plugin is moved to jalv, which
    the user chooses with ZYNTHIAN_PLUGIN_ENGINE. When plugin-hostd is not installed, every PH entry is marked
    NOT_WORKING, with why in NOT_WORKING_WHY, so the menu says so."""
    available = shutil.which(binary) is not None
    try:
        from zyngine import zynthian_lv2
        by_type = zynthian_lv2.engines_by_type or {}
    except ImportError:
        by_type = {}
    # Entries of an earlier catalogue, saved with the engine config, are made again or dropped
    for key in [k for k in engines if k.startswith("PH/")]:
        by_type.get(engines.pop(key).get("TYPE"), {}).pop(key, None)
    added = {}
    for key, info in list(engines.items()):
        if key.startswith("JV/") and info.get("TYPE") in ENGINE_TYPES:
            entry = {k: v for k, v in info.items() if k not in ("ENGINE", "NOT_WORKING", "NOT_WORKING_WHY")}
            entry.update({"ID": f"ph{info.get('ID', '')}", "TITLE": f"{info['TITLE']} (PH)", "FORMAT": "LV2",
                          "ENABLED": bool(info.get("ENABLED"))})
            added["PH/" + key[3:]] = entry
    first_dir = {}  # CLAP id => the directory it was first found in, in search order
    for plugin in (scan_clap() if clap is None else clap):
        if first_dir.setdefault(plugin["id"], os.path.dirname(plugin["path"])) != os.path.dirname(plugin["path"]):
            continue  # the same plugin in a later directory is the same entry
        etype = next((t for f, t in CLAP_TYPES if f in plugin["features"]), None)
        if not etype:
            continue
        # The pin URI names the file and the id: DPF-built files share one id
        url = f"clap:{plugin['path']}#{plugin['id']}"
        key = "PH/" + url
        clap_plugins[key] = plugin
        added[key] = {"ID": "phc" + url, "NAME": plugin["name"], "TITLE": f"{plugin['name']} (PH)",
                      "FORMAT": "CLAP", "TYPE": etype, "CAT": next((CLAP_CATS[f] for f in plugin["features"] if f in CLAP_CATS), "Other"),
                      "ENABLED": True, "INDEX": 9999, "URL": url, "UI": "",
                      "DESCR": f"Plugin by {plugin['vendor']}",
                      "QUALITY": 0, "COMPLEX": 0, "EDIT": 0}
    add_format_lines(added)
    if not available:
        for info in added.values():
            info.update(NOT_WORKING="not installed", NOT_WORKING_WHY=NOT_INSTALLED_WHY)
    engines.update(added)
    by_ph = plugin_engine() == "plugin-hostd"
    if by_ph and not available:
        logging.warning(f"ZYNTHIAN_PLUGIN_ENGINE is plugin-hostd but {binary} is not installed: "
                        "the menus list its plugins as not working until it is")
    for key, info in added.items():
        if info["TYPE"] in by_type:
            by_type[info["TYPE"]][key] = info
            twin = jalv_twin(key)
            if by_ph and twin:
                by_type[info["TYPE"]].pop(twin, None)
    return added


def set_enabled(engines, key, enabled):
    """Enable or disable an entry; a PH entry made from a jalv one carries the flag to it, as the
    catalogue makes the PH entry again from the jalv one on every load."""
    engines[key]["ENABLED"] = enabled
    twin = jalv_twin(key)
    if twin in engines:
        engines[twin]["ENABLED"] = enabled
