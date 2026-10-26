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


def add_catalogue(engines, clap=None, binary="plugin-hostd"):
    """Add a PH entry for each LV2 instrument and effect in engines and each CLAP one omx-clap-scan
    finds, to engines and to zynthian_lv2's lists by type. They are enabled when plugin-hostd is
    installed; an LV2 one only when its jalv entry is enabled too."""
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
            entry = {k: v for k, v in info.items() if k != "ENGINE"}
            entry.update({"ID": f"ph{info.get('ID', '')}", "TITLE": f"{info['TITLE']} (PH)",
                          "ENABLED": bool(info.get("ENABLED")) and available})
            added["PH/" + key[3:]] = entry
    for plugin in (scan_clap() if clap is None else clap):
        etype = next((t for f, t in CLAP_TYPES if f in plugin["features"]), None)
        if not etype:
            continue
        key = f"PH/clap:{plugin['id']}"
        clap_plugins[key] = plugin
        added[key] = {"ID": "phc" + plugin["id"], "NAME": plugin["name"], "TITLE": f"{plugin['name']} (PH)",
                      "TYPE": etype, "CAT": next((CLAP_CATS[f] for f in plugin["features"] if f in CLAP_CATS), "Other"),
                      "ENABLED": available, "INDEX": 9999, "URL": f"clap:{plugin['path']}#{plugin['id']}", "UI": "",
                      "DESCR": f"CLAP plugin by {plugin['vendor']}, run by plugin-hostd",
                      "QUALITY": 0, "COMPLEX": 0, "EDIT": 0}
    engines.update(added)
    for key, info in added.items():
        if info["TYPE"] in by_type:
            by_type[info["TYPE"]][key] = info
    return added
