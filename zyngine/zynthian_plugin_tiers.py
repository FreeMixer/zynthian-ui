# -*- coding: utf-8 -*-
# ******************************************************************************
# ZYNTHIAN PROJECT: plugin tiers (zynthian_plugin_tiers)
#
# The rules that turn what qualification found about a plugin into its place in the plugin menus:
# a tier word, a short tag after its name, and whether it is hidden.
#
#   pin / found                 RT-safe  worst latency  soak       tier        tag                       hidden
#   no record                   -        -              -          unverified  unverified                no
#   verdict=refused             any      any            any        -           hidden · refused          yes
#   crashed in qualification    any      any            any        -           hidden · crashed          yes
#   hung in qualification       any      any            any        -           hidden · hung             yes
#   measured                    yes      <= live ms     soaked     live        live · 1.2 ms · RT        no
#   measured                    yes      <= live ms     unsoaked   live        live · 1.2 ms · RT · unsoaked  no
#   measured                    yes      > live ms      any        studio      studio · 21 ms · RT       no
#   measured                    no       any            any        studio      studio · 0.0 ms · not RT  no
#   measured                    yes      unmeasured     any        studio      studio · RT               no
#   measured                    unknown  <= live ms     any        studio      studio · 1.0 ms           no
#
# live is the OpenMixer catalog's gate: RT-safe and a measured latency of at most LIVE_MAX_MS
# (5 ms, ZYNTHIAN_PLUGIN_LIVE_MS), read at the worst measured rate so a rate change does not move a
# plugin between tiers; the tag shows the latency at the current rate. A figure the record does not
# hold is left out of the tag, never guessed. Hidden ones are listed last, greyed, when
# ZYNTHIAN_PLUGIN_SHOW_HIDDEN=1.
#
# Copyright (C) 2026 Pau Aliagas <linuxnow@gmail.com>
#
# This program is free software; you can redistribute it and/or modify it under the terms
# of the GNU General Public License as published by the Free Software Foundation; either
# version 2 of the License, or any later version. See the LICENSE.txt file.
# ******************************************************************************

import os
import logging
from collections import namedtuple

LIVE_MAX_MS = 5.0
RANK = {"live": 0, "studio": 1, "unverified": 2, None: 2, "hidden": 3}  # entries that are not plugins rank as unverified
SEP = " · "

Placement = namedtuple("Placement", "tier tag hidden reason")


def live_max_ms():
    try:
        return float(os.environ.get("ZYNTHIAN_PLUGIN_LIVE_MS", "").strip() or LIVE_MAX_MS)
    except ValueError:
        logging.warning(f"ZYNTHIAN_PLUGIN_LIVE_MS={os.environ['ZYNTHIAN_PLUGIN_LIVE_MS']} is not a number: using {LIVE_MAX_MS}")
        return LIVE_MAX_MS


def show_hidden():
    return os.environ.get("ZYNTHIAN_PLUGIN_SHOW_HIDDEN", "").strip().lower() in ("1", "yes", "true", "on")


def ms_text(ms):
    return f"{ms:.1f} ms" if ms < 10 else f"{ms:.0f} ms"


def place(facts, live_ms=LIVE_MAX_MS):
    """The Placement of a plugin from its qualification Facts."""
    reason = "refused" if facts.pin == "refused" else "crashed" if facts.crashed else "hung" if facts.hung else None
    if reason:
        return Placement(None, SEP.join(("hidden", reason)), True, reason)
    if not facts.record:
        return Placement("unverified", "unverified", False, None)
    live = facts.rt_safe is True and facts.worst_latency_ms is not None and facts.worst_latency_ms <= live_ms
    tier = "live" if live else "studio"
    words = [tier]
    if facts.latency_ms is not None:
        words.append(ms_text(facts.latency_ms))
    if facts.rt_safe is not None:
        words.append("RT" if facts.rt_safe else "not RT")
    if facts.soaked is False:
        words.append("unsoaked")
    return Placement(tier, SEP.join(words), False, None)


def is_plugin(key):
    return key.startswith(("PH/", "JV/"))


def menu_entries(engines, facts_of, live_ms=LIVE_MAX_MS, show=False):
    """[(key, text, greyed)] of one menu's engines {key: info}, live first, then studio, then the
    rest in their own order; hidden ones dropped, or last and greyed when show is set.
    facts_of(url) gives a plugin's Facts; entries that are not plugins keep their title."""
    rows = []
    for n, (key, info) in enumerate(engines.items()):
        if not is_plugin(key):
            rows.append((RANK[None], n, key, info["TITLE"], False))
            continue
        p = place(facts_of(info.get("URL", "")), live_ms)
        if p.hidden and not show:
            continue
        tier = "hidden" if p.hidden else p.tier
        rows.append((RANK[tier], n, key, f"{info['TITLE']}  {p.tag}", p.hidden))
    return [(key, text, greyed) for _, _, key, text, greyed in sorted(rows)]
