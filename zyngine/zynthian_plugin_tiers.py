# -*- coding: utf-8 -*-
# ******************************************************************************
# ZYNTHIAN PROJECT: plugin tiers (zynthian_plugin_tiers)
#
# The rules that turn what qualification found about a plugin into its place in the plugin menus:
# a tier word, a short tag after its name, and whether it is hidden.
#
#   pin / found                 RT-safe  worst latency  soak       tier        tag                       hidden
#   no record                   -        -              -          unverified  not qualified             no
#   verdict=refused             any      any            any        -           hidden · refused          yes
#   verdict=refused, jalv row   any      any            any        refused     refused                   no
#   crashed in qualification    any      any            any        -           hidden · crashed          yes
#   hung in qualification       any      any            any        -           hidden · hung             yes
#   measured                    yes      <= live ms     soaked     live        live · <figures>          no
#   measured                    yes      <= live ms     unsoaked   live        live · <figures> · unsoaked  no
#   measured                    yes      > live ms      any        studio      studio · <figures>        no
#   measured                    no or unknown  any      any        studio      studio · <figures>        no
#
# <figures> are the record's, as it holds them, at the current rate and quantum, separated by a space
# and short enough that a row of half the 1024 px screen shows all three after a long title:
#   latency  64/0fr           measured/declared frames, 64fr when the record declares none; lat? unknown
#   RT       RT-ok            no allocation, lock, syscall or thread violation under the sweep;
#                             RT-alone: some, so plugin-hostd isolates it in its own worker, never
#                             grouped with RT-safe plugins (it is not refused); RT-?: no
#                             interposer count, or not swept
#   cost     19.4ns           the median ns per sample measured at this rate and quantum; cost? unknown
# The record read is the one of the format the row stands for: the LV2 uri's for an LV2 row, the
# clap:<path>#<id> one for a CLAP row. A record whose verdict is hosting.rt-safety.violations-in-run
# is RT-alone whatever its counts say.
#
# live is the OpenMixer catalog's gate: RT-safe and a measured latency of at most LIVE_MAX_MS
# (5 ms, ZYNTHIAN_PLUGIN_LIVE_MS), read at the worst measured rate so a rate change does not move a
# plugin between tiers. A refused plugin's jalv row (JV/) is listed, since jalv runs what plugin-hostd
# refuses: after the qualified ones, tagged refused. Hidden ones are listed last, greyed, when
# ZYNTHIAN_PLUGIN_SHOW_HIDDEN=1.
#
# A menu is ordered by tier (live, studio, the rest) or by one figure: name, cost, latency or RT safety,
# by the value shown, a figure the record does not hold after the ones it does. In every order the rows
# with figures come first, then the ones without (not qualified, entries that are not plugins), then the
# refused jalv rows, then the hidden ones. hosted_only keeps the rows plugin-hostd runs (PH/).
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
RANK = {"live": 0, "studio": 1, "unverified": 2, None: 2, "refused": 3, "hidden": 4}  # entries that are not plugins rank as unverified
SEP = " · "
ORDERS = ("tier", "name", "cost", "latency", "rt")
RT_WORDS = {True: "RT-ok", False: "RT-alone", None: "RT-?"}

Placement = namedtuple("Placement", "tier tag hidden reason")


def live_max_ms():
    try:
        return float(os.environ.get("ZYNTHIAN_PLUGIN_LIVE_MS", "").strip() or LIVE_MAX_MS)
    except ValueError:
        logging.warning(f"ZYNTHIAN_PLUGIN_LIVE_MS={os.environ['ZYNTHIAN_PLUGIN_LIVE_MS']} is not a number: using {LIVE_MAX_MS}")
        return LIVE_MAX_MS


def show_hidden():
    return os.environ.get("ZYNTHIAN_PLUGIN_SHOW_HIDDEN", "").strip().lower() in ("1", "yes", "true", "on")


def number_text(n):
    return f"{n:.1f}" if isinstance(n, float) and n < 100 and n != int(n) else f"{n:.0f}"


def latency_text(facts):
    if facts.frames is None:
        return "lat?"
    declared = "" if facts.declared_frames is None else f"/{number_text(facts.declared_frames)}"
    return f"{number_text(facts.frames)}{declared}fr"


def cost_text(facts):
    return "cost?" if facts.cost_ns is None else f"{number_text(facts.cost_ns)}ns"


def place(facts, live_ms=LIVE_MAX_MS):
    """The Placement of a plugin from its qualification Facts."""
    reason = "refused" if facts.pin == "refused" else "crashed" if facts.crashed else "hung" if facts.hung else None
    if reason:
        return Placement(None, SEP.join(("hidden", reason)), True, reason)
    if not facts.record:
        return Placement("unverified", "not qualified", False, None)
    live = facts.rt_safe is True and facts.worst_latency_ms is not None and facts.worst_latency_ms <= live_ms
    tier = "live" if live else "studio"
    words = [tier, latency_text(facts), RT_WORDS[facts.rt_safe], cost_text(facts)]
    if facts.soaked is False:
        words.append("unsoaked")
    return Placement(tier, " ".join(words), False, None)


def figure(facts, order):
    """The shown value a menu is sorted by, None when the row does not show it."""
    if order == "cost":
        return facts.cost_ns
    if order == "latency":
        return facts.frames
    if order == "rt":
        return {True: 0, False: 1}.get(facts.rt_safe)
    return None


def is_plugin(key):
    return key.startswith(("PH/", "JV/"))


def menu_entries(engines, facts_of, live_ms=LIVE_MAX_MS, show=False, order="tier", hosted_only=False):
    """[(key, text, greyed)] of one menu's engines {key: info}, in order (one of ORDERS): by tier, live
    first, then studio, then the rest in their own order; or by name or by a figure, the rows with
    figures first, then the rest. Then the jalv rows of the plugins plugin-hostd refuses, tagged
    refused; hidden ones dropped, or last and greyed when show is set. hosted_only keeps the PH rows.
    facts_of(url) gives a plugin's Facts; entries that are not plugins keep their title."""
    rows = []
    for n, (key, info) in enumerate(engines.items()):
        if hosted_only and not key.startswith("PH/"):
            continue
        title = info["TITLE"]
        if not is_plugin(key):
            rows.append((RANK[None], n, key, title, False, None))
            continue
        facts = facts_of(info.get("URL", ""))
        p = place(facts, live_ms)
        if p.reason == "refused" and key.startswith("JV/"):
            rows.append((RANK["refused"], n, key, f"{title}  refused", False, None))
            continue
        if p.hidden and not show:
            continue
        tier = "hidden" if p.hidden else p.tier
        rows.append((RANK[tier], n, key, f"{title}  {p.tag}", p.hidden, figure(facts, order) if tier != "unverified" else None))
    if order == "tier":
        rows.sort(key=lambda r: r[:2])
    else:
        # live and studio are one group: the figure orders them
        rows.sort(key=lambda r: (max(r[0], RANK["studio"]), r[5] is None, r[5] or 0, r[3].lower(), r[1]))
    return [(key, text, greyed) for _, _, key, text, greyed, _ in rows]
