# -*- coding: utf-8 -*-
# ******************************************************************************
# ZYNTHIAN PROJECT: plugin-hostd catalogue (zynthian_plugin_hostd_catalogue)
#
# The plugins the plugin-hostd engine offers: the LV2 instruments and effects
# zynthian_lv2 lists from the installed bundles, and CLAP plugins as omx-clap-scan reports them.
#
# Copyright (C) 2026 Pau Aliagas <linuxnow@gmail.com>
#
# This program is free software; you can redistribute it and/or modify it under the terms
# of the GNU General Public License as published by the Free Software Foundation; either
# version 2 of the License, or any later version. See the LICENSE.txt file.
# ******************************************************************************

import os
import re
import shutil
import logging

ENGINE_TYPES = ("MIDI Synth", "Audio Effect")
PLUGIN_ENGINES = ("plugin-hostd", "jalv")
# What the menus offer LV2 plugins through when ZYNTHIAN_PLUGIN_ENGINE is unset
DEFAULT_PLUGIN_ENGINE = "jalv"
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


def plugin_engine():
    """The engine the menus offer LV2 plugins through: ZYNTHIAN_PLUGIN_ENGINE, else DEFAULT_PLUGIN_ENGINE."""
    engine = os.environ.get("ZYNTHIAN_PLUGIN_ENGINE", "").strip().lower() or DEFAULT_PLUGIN_ENGINE
    if engine not in PLUGIN_ENGINES:
        logging.warning(f"ZYNTHIAN_PLUGIN_ENGINE={engine} is not one of {', '.join(PLUGIN_ENGINES)}: using {DEFAULT_PLUGIN_ENGINE}")
        engine = DEFAULT_PLUGIN_ENGINE
    return engine


def jalv_twin(key):
    """The jalv entry an LV2 PH entry was made from, None for a CLAP one."""
    return None if key.startswith("PH/clap:") or not key.startswith("PH/") else "JV/" + key[3:]


def add_catalogue(engines, clap=None, binary="plugin-hostd"):
    """Add a PH entry for each LV2 instrument and effect in engines, and for each CLAP plugin in clap
    (omx-clap-scan's plugins; none are offered by default yet), to engines. They are enabled when
    plugin-hostd is installed; an LV2 one only when its jalv entry is enabled too.
    The entries reach zynthian_lv2's lists by type, the menus, only with plugin-hostd as the plugin
    engine (ZYNTHIAN_PLUGIN_ENGINE=plugin-hostd): then the menus list the PH entry and not its jalv
    one. The jalv entry stays in engines, and the PH ones always are, so a chain saved with either
    loads as it was saved whatever the setting."""
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
    for plugin in (clap or []):
        etype = next((t for f, t in CLAP_TYPES if f in plugin["features"]), None)
        if not etype:
            continue
        # The pin URI names the file and the id: DPF-built files share one id
        url = f"clap:{plugin['path']}#{plugin['id']}"
        key = "PH/" + url
        clap_plugins[key] = plugin
        added[key] = {"ID": "phc" + url, "NAME": plugin["name"], "TITLE": f"{plugin['name']} (PH)",
                      "TYPE": etype, "CAT": next((CLAP_CATS[f] for f in plugin["features"] if f in CLAP_CATS), "Other"),
                      "ENABLED": available, "INDEX": 9999, "URL": url, "UI": "",
                      "DESCR": f"CLAP plugin by {plugin['vendor']}, run by plugin-hostd",
                      "QUALITY": 0, "COMPLEX": 0, "EDIT": 0}
    engines.update(added)
    by_ph = plugin_engine() == "plugin-hostd"
    if by_ph and not available:
        logging.warning(f"ZYNTHIAN_PLUGIN_ENGINE is plugin-hostd but {binary} is not installed: the menus offer jalv")
    if not (by_ph and available):
        return added
    for key, info in added.items():
        if info["TYPE"] in by_type:
            by_type[info["TYPE"]][key] = info
            if jalv_twin(key):
                by_type[info["TYPE"]].pop(jalv_twin(key), None)
    return added


def set_enabled(engines, key, enabled):
    """Enable or disable an entry; a PH entry made from a jalv one carries the flag to it, as the
    catalogue makes the PH entry again from the jalv one on every load."""
    engines[key]["ENABLED"] = enabled
    twin = jalv_twin(key)
    if twin in engines:
        engines[twin]["ENABLED"] = enabled
