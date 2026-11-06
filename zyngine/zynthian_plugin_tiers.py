# -*- coding: utf-8 -*-
# ******************************************************************************
# ZYNTHIAN PROJECT: plugin tiers (zynthian_plugin_tiers)
#
# The rules that turn what qualification found about a plugin into its place in the plugin menus:
# a tier word, a short tag after its name, and whether it is below the bar.
#
#   pin / found                 RT-safe  worst latency  soak       tier        tag                       below bar
#   no record                   -        -              -          unverified  not qualified             no
#   verdict=refused             any      any            any        below       below bar · refused       yes
#   crashed in qualification    any      any            any        below       below bar · crashed       yes
#   hung in qualification       any      any            any        below       below bar · hung          yes
#   measured                    yes      <= live ms     soaked     live        live · <figures>          no
#   measured                    yes      <= live ms     unsoaked   live        live · <figures> · unsoaked  no
#   measured                    yes      > live ms      any        studio      studio · <figures>        no
#   measured                    no or unknown  any      any        studio      studio · <figures>        no
#
# <figures> are the record's, as it holds them, at the current rate and quantum, separated by a space
# and short enough that a row of half the 1024 px screen shows all three after a long title:
#   latency  64/0fr           measured/declared frames, 64fr when the record declares none; ?/64fr when only the
#                             plugin's declared figure is known (a note that did not sound); lat? unknown
#   RT       RT-ok            no allocation, lock, syscall or thread violation under the sweep;
#                             RT-alone: some, so plugin-hostd isolates it in its own worker, never
#                             grouped with RT-safe plugins (it is not refused); RT-?: no
#                             interposer count, or not swept
#   cost     19.4ns           the median ns per sample measured at this rate and quantum; cost? unknown
# The record read is the one of the format the row stands for: the LV2 uri's for an LV2 row, the
# clap:<path>#<id> one for a CLAP row. A run with interposer events or process errors is RT-alone
# even where the allocation, lock and syscall counts read zero.
#
# live is the gate of the plugin catalogue: RT-safe and a measured latency of at most LIVE_MAX_MS
# (5 ms, ZYNTHIAN_PLUGIN_LIVE_MS), read at the worst measured rate so a rate change does not move a
# plugin between tiers.
#
# The plugins below the bar are listed after the others, with their mark. A plugin-hostd pin judges plugin-hostd's
# rows only: a jalv row (JV/) is placed without it.
#
# A menu is ordered by tier (live, studio, the rest) or by one figure: name, cost, latency or RT safety,
# by the value shown, a figure the record does not hold after the ones it does. In every order the rows
# with figures come first, then the ones without (not qualified, entries that are not plugins), then the
# ones below the bar. hosted_only keeps the rows plugin-hostd runs (PH/).
#
# A plugin installed both as CLAP and as LV2 is one row, never two: formats are invisible to the user. A CLAP row and an
# LV2 row of one name are one plugin, and the one placed worse gives way: by tier
# (live, studio, not qualified, below the bar), then CLAP before LV2 (omx-clap-host's own format), then plugin-hostd
# before jalv. Rows of one format stay as they are: two plugins that share a name, and an LV2 plugin's jalv row beside
# its plugin-hostd one, which is a choice of engine and not of format.
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
RANK = {"live": 0, "studio": 1, "unverified": 2, None: 2, "below": 3}  # entries that are not plugins rank as unverified
SEP = " · "
ORDERS = ("tier", "name", "cost", "latency", "rt")
RT_WORDS = {True: "RT-ok", False: "RT-alone", None: "RT-?"}

Placement = namedtuple("Placement", "tier tag below reason")


def live_max_ms():
    try:
        return float(os.environ.get("ZYNTHIAN_PLUGIN_LIVE_MS", "").strip() or LIVE_MAX_MS)
    except ValueError:
        logging.warning(f"ZYNTHIAN_PLUGIN_LIVE_MS={os.environ['ZYNTHIAN_PLUGIN_LIVE_MS']} is not a number: using {LIVE_MAX_MS}")
        return LIVE_MAX_MS


def number_text(n):
    return f"{n:.1f}" if isinstance(n, float) and n < 100 and n != int(n) else f"{n:.0f}"


def latency_text(facts):
    if facts.frames is None:
        return "lat?" if facts.declared_frames is None else f"?/{number_text(facts.declared_frames)}fr"
    declared = "" if facts.declared_frames is None else f"/{number_text(facts.declared_frames)}"
    return f"{number_text(facts.frames)}{declared}fr"


def cost_text(facts):
    return "cost?" if facts.cost_ns is None else f"{number_text(facts.cost_ns)}ns"


def place(facts, live_ms=LIVE_MAX_MS):
    """The Placement of a plugin from its qualification Facts."""
    reason = "refused" if facts.pin == "refused" else "crashed" if facts.crashed else "hung" if facts.hung else None
    if reason:
        return Placement("below", SEP.join(("below bar", reason)), True, reason)
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


def plugin_identity(key, info):
    """(name, format) of a plugin row: its name, case and spacing aside, None when it has none; clap or lv2."""
    name = " ".join((info.get("NAME") or "").casefold().split()) or None
    return name, "clap" if key.startswith("PH/clap:") else "lv2"


def format_rank(key):
    """CLAP before LV2, plugin-hostd before jalv: the order a tie between two rows of one plugin is broken in."""
    return 0 if key.startswith("PH/clap:") else 1 if key.startswith("PH/") else 2


def one_row_per_plugin(rows):
    """rows as menu_entries builds them, (rank, n, key, text, figure, identity), with each plugin's rows reduced to
    the best placed: by rank, then by format_rank, then in their own order. A row gives way only to a better one of
    the same name in the other format."""
    order = lambda r: (r[0], format_rank(r[2]), r[1])
    by_name = {}
    for row in rows:
        if row[5] is not None and row[5][0] is not None:
            by_name.setdefault(row[5][0], []).append(row)
    dropped = set()
    for group in by_name.values():
        for row in group:
            if any(order(other) < order(row) and other[5][1] != row[5][1] for other in group):
                dropped.add(id(row))
    return [row for row in rows if id(row) not in dropped]


def menu_entries(engines, facts_of, live_ms=LIVE_MAX_MS, order="tier", hosted_only=False):
    """[(key, text)] of one menu's engines {key: info}, in order (one of ORDERS): by tier, live
    first, then studio, then the rest in their own order; or by name or by a figure, the rows with
    figures first, then the rest. Then the plugins below the bar, with their mark. hosted_only keeps the PH rows.
    A plugin present in more than one format is one row, the best placed (one_row_per_plugin).
    facts_of(url) gives a plugin's Facts; entries that are not plugins keep their title."""
    rows = []
    for n, (key, info) in enumerate(engines.items()):
        if hosted_only and not key.startswith("PH/"):
            continue
        title = info["TITLE"]
        if not is_plugin(key):
            rows.append((RANK[None], n, key, title, None, None))
            continue
        facts = facts_of(info.get("URL", ""))
        if not key.startswith("PH/"):
            facts = facts._replace(pin=None)  # plugin-hostd's pin does not judge jalv
        p = place(facts, live_ms)
        rows.append((RANK[p.tier], n, key, f"{title}  {p.tag}",
                     figure(facts, order) if p.tier not in ("unverified", "below") else None, plugin_identity(key, info)))
    rows = one_row_per_plugin(rows)
    if order == "tier":
        rows.sort(key=lambda r: r[:2])
    else:
        # live and studio are one group: the figure orders them
        rows.sort(key=lambda r: (max(r[0], RANK["studio"]), r[4] is None, r[4] or 0, r[3].lower(), r[1]))
    return [(key, text) for _, _, key, text, _, _ in rows]
