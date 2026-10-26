#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ******************************************************************************
# ZYNTHIAN PROJECT: convert_snapshots_to_ph
#
# Run once by hand: rewrites the jalv (JV) processors of saved snapshots into plugin-hostd (PH)
# processors of the same plugin, with the same controller values and preset. Each result is written
# beside its snapshot, foo.zss as foo.ph.zss, and nothing is ever overwritten. Zynthian itself never
# converts a snapshot when it loads one.
#
#   convert_snapshots_to_ph.py [--engine-config FILE] [--pins PATHS] [--suffix .ph] SNAPSHOT_OR_DIR...
#
# Copyright (C) 2026 Pau Aliagas <linuxnow@gmail.com>
#
# This program is free software; you can redistribute it and/or modify it under the terms
# of the GNU General Public License as published by the Free Software Foundation; either
# version 2 of the License, or any later version. See the LICENSE.txt file.
# ******************************************************************************

import os
import sys
import json
import argparse
import importlib.util

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA_VERSION = 5  # zynthian_legacy_snapshot.SNAPSHOT_SCHEMA_VERSION
PH_BANK = ["", None, "Presets", None]  # the one bank zynthian_engine_plugin_hostd lists


def load_module(name):
    """A zyngine module by its path: the zyngine package imports every engine and its libraries."""
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "zyngine", name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


catalogue = load_module("zynthian_plugin_hostd_catalogue")
qualification = load_module("zynthian_plugin_qualification")


def default_engine_config():
    return os.path.join(os.environ.get("ZYNTHIAN_CONFIG_DIR") or "/zynthian/config", "engine_config.json")


def why_not(eng_code, engines, pins=None):
    """None when plugin-hostd can host the processor's plugin, else the reason it stays."""
    if not eng_code.startswith("JV/"):
        return f"{eng_code} is not a jalv plugin"
    info = engines.get(eng_code)
    if not info or not info.get("URL"):
        return f"{eng_code} is not in the engine config, so its plugin URL is unknown"
    if info.get("TYPE") not in catalogue.ENGINE_TYPES:
        return f"{eng_code} is a {info.get('TYPE')}: plugin-hostd hosts instruments and effects"
    if (pins or {}).get(info["URL"]) == "refused":
        return f"{eng_code}: plugin-hostd refuses {info['URL']} (pin verdict=refused)"
    return None


def ph_processor_state(state):
    """A jalv processor's zs3 state for the PH engine: controllers as they are, the preset by its URL."""
    state = dict(state)
    preset = state.get("preset_info")
    if preset and preset[0]:
        state["preset_info"] = [preset[0], None, preset[2] if len(preset) > 2 else preset[0], ""]
    else:
        state["preset_info"] = None
    state["bank_info"] = list(PH_BANK)
    state["bank_subdir_info"] = None
    state["preset_subdir_info"] = None
    return state


def convert_state(snapshot, engines, pins=None):
    """(converted snapshot, report rows). A row is (processor id, engine code, new code or None, reason)."""
    rows = []
    if snapshot.get("schema_version") != SCHEMA_VERSION:
        return None, [(None, None, None, f"schema {snapshot.get('schema_version')} is not {SCHEMA_VERSION}: "
                                         "load and save it once in Zynthian, then convert")]
    snapshot = json.loads(json.dumps(snapshot))
    moved = {}
    for chain_id, chain in snapshot.get("chains", {}).items():
        for slot in chain.get("slots", []):
            for proc_id, eng_code in slot.items():
                reason = why_not(eng_code, engines, pins)
                if reason:
                    rows.append((proc_id, eng_code, None, reason))
                    continue
                slot[proc_id] = moved[proc_id] = "PH/" + eng_code[3:]
                rows.append((proc_id, eng_code, slot[proc_id], engines[eng_code]["URL"]))
    for zs3 in snapshot.get("zs3", {}).values():
        processors = zs3.get("processors", {})
        for proc_id in moved:
            if proc_id in processors:
                processors[proc_id] = ph_processor_state(processors[proc_id])
    return (snapshot if moved else None), rows


def target_path(path, suffix):
    base, ext = os.path.splitext(path)
    return f"{base}{suffix}{ext}"


def snapshot_files(paths, suffix):
    for path in paths:
        if os.path.isdir(path):
            for top, dirs, files in os.walk(path):
                dirs.sort()
                for name in sorted(files):
                    if name.endswith(".zss") and not name.endswith(suffix + ".zss"):
                        yield os.path.join(top, name)
        else:
            yield path


def convert_file(path, engines, suffix, out, pins=None):
    """Convert one snapshot. Returns (converted, left, written)."""
    target = target_path(path, suffix)
    try:
        with open(path) as f:
            snapshot = json.load(f)
    except (OSError, ValueError) as e:
        print(f"{path}: not read ({e})", file=out)
        return 0, 0, False
    converted, rows = convert_state(snapshot, engines, pins)
    if converted is None:
        print(f"{path}: left as it is", file=out)
    elif os.path.exists(target):
        print(f"{path}: {target} exists, left as it is (nothing is overwritten)", file=out)
        converted = None
    else:
        print(f"{path} -> {target}", file=out)
    for proc_id, eng_code, new_code, reason in rows:
        if proc_id is None:
            print(f"  left       {reason}", file=out)
        elif new_code and converted is not None:
            print(f"  converted  processor {proc_id} {eng_code} -> {new_code} ({reason})", file=out)
        elif new_code:
            print(f"  left       processor {proc_id} {eng_code}: would become {new_code}", file=out)
        else:
            print(f"  left       processor {proc_id}: {reason}", file=out)
    n_converted = sum(1 for r in rows if r[2]) if converted is not None else 0
    n_left = sum(1 for r in rows if r[0] is not None) - n_converted
    if converted is None:
        return n_converted, n_left, False
    try:
        # "x" mode: a file that appeared since the check is not overwritten either
        with open(target, "x") as f:
            json.dump(converted, f)
    except OSError as e:
        print(f"  not written: {e}", file=out)
        return 0, n_left + n_converted, False
    return n_converted, n_left, True


def main(argv=None, out=sys.stdout):
    parser = argparse.ArgumentParser(description="Write a plugin-hostd copy of each snapshot that holds jalv plugins.")
    parser.add_argument("paths", nargs="+", help="snapshot files or directories of them")
    parser.add_argument("--engine-config", default=default_engine_config(),
                        help="Zynthian's engine config, for each jalv plugin's URL and type (default: %(default)s)")
    parser.add_argument("--pins", default=":".join(qualification.pin_paths()),
                        help="pin files or directories, ':' between them; a plugin pinned verdict=refused stays "
                             "with jalv (default: %(default)s)")
    parser.add_argument("--suffix", default=".ph", help="added before .zss in the name of each result (default: %(default)s)")
    args = parser.parse_args(argv)
    try:
        with open(args.engine_config) as f:
            engines = json.load(f)
    except (OSError, ValueError) as e:
        print(f"Can't read the engine config {args.engine_config}: {e}", file=out)
        return 2
    pins = qualification.read_pins([p for p in args.pins.split(":") if p])
    totals = [0, 0, 0, 0]
    for path in snapshot_files(args.paths, args.suffix):
        converted, left, written = convert_file(path, engines, args.suffix, out, pins)
        totals = [totals[0] + 1, totals[1] + written, totals[2] + converted, totals[3] + left]
    print(f"{totals[0]} snapshots read, {totals[1]} written; {totals[2]} processors converted, {totals[3]} left as they are",
          file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
