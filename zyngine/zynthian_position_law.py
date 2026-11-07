# -*- coding: utf-8 -*-
# ******************************************************************************
# ZYNTHIAN PROJECT: position law of a plugin-hostd controller (zynthian_position_law)
#
# A Python twin of omx-contract's scalePosition and scaleValue (lib/scale.mjs): where a control sits
# on its travel, 0 at the minimum and 1 at the maximum, for each scale word. linear is
# (v - min) / (max - min); log is ln(v / min) / ln(max / min) over a positive floor (a log travel
# whose floor is not positive is linear); stepped is the step's index over the steps; map is linear
# between its [plain value, quantity] points. 20 Hz to 20 kHz puts 1 kHz at 0.5663.
#
# Only the plugin-hostd engine's controllers use it (they carry a position_law travel). Every other
# engine keeps upstream's is_logarithmic law. The twin is held to data/scale-vectors.json of the
# omx-contract package, both directions within one ulp (tests/test_position_law.py).
#
# Copyright (C) 2026 Pau Aliagas <linuxnow@gmail.com>
#
# This program is free software; you can redistribute it and/or modify it under the terms
# of the GNU General Public License as published by the Free Software Foundation; either
# version 2 of the License, or any later version. See the LICENSE.txt file.
# ******************************************************************************

import math
import os

VECTORS_PATH = os.environ.get("ZYNTHIAN_OMX_SCALE_VECTORS", "/usr/share/omx-contract/scale-vectors.json")
LAWS = ("linear", "log", "stepped", "map")


def _num(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _law(travel):
    word = travel.get("scale") or "linear"
    if word == "unknown":
        return "linear"
    if word not in LAWS:
        raise ValueError(f"no position law for the scale {word!r}")
    if word == "log" and not travel.get("min", 0) > 0:
        return "linear"
    return word


def _points(travel):
    pts = travel.get("points")
    if not isinstance(pts, (list, tuple)) or len(pts) < 2 or not all(
            isinstance(p, (list, tuple)) and len(p) == 2 and _num(p[0]) and _num(p[1]) for p in pts):
        raise ValueError("a map travel carries at least two [plain value, quantity] points")
    return pts


def _ends(travel, law):
    if law == "map":
        pts = _points(travel)
        lo, hi = pts[0][0], pts[-1][0]
    else:
        lo, hi = travel.get("min"), travel.get("max")
    if not (_num(lo) and _num(hi) and hi > lo):
        raise ValueError(f"a travel runs upward from its min to its max, not {lo} to {hi}")
    return lo, hi


def _steps(travel, lo, hi):
    if not travel.get("step", 0) > 0:
        raise ValueError("a stepped travel states a positive step")
    n = round((hi - lo) / travel["step"]) + 1
    if n < 2:
        raise ValueError("a stepped travel has at least two steps")
    return n


def _piece(pts, col, x):
    direction = 1 if pts[-1][col] > pts[0][col] else -1
    i = 0
    while i < len(pts) - 2 and (x - pts[i + 1][col]) * direction > 0:
        i += 1
    return pts[i], pts[i + 1]


def _round(x):
    # Math.round: halves go up
    return math.floor(x + 0.5)


def effective_law(travel):
    """The law that serves travel: log over a floor that is not positive is linear."""
    return _law(travel)


def scale_position(travel, value):
    """The position of value on travel ({scale, min, max, step?} or {scale: 'map', points}); not clamped."""
    law = _law(travel)
    lo, hi = _ends(travel, law)
    if law == "map":
        a, b = _piece(_points(travel), 1, value)
        plain = a[0] + ((value - a[1]) / (b[1] - a[1])) * (b[0] - a[0])
        return (plain - lo) / (hi - lo)
    if value == lo:
        return 0
    if value == hi:
        return 1
    if law == "log":
        return math.log(value / lo) / math.log(hi / lo)
    if law == "stepped":
        n = _steps(travel, lo, hi)
        k = min(n - 1, max(0, _round((value - lo) / travel["step"])))
        return k / (n - 1)
    return (value - lo) / (hi - lo)


def scale_value(travel, p):
    """The value at position p on travel: the inverse of scale_position (a stepped one lands on the nearest step)."""
    law = _law(travel)
    lo, hi = _ends(travel, law)
    if law == "map":
        plain = lo + p * (hi - lo)
        a, b = _piece(_points(travel), 0, plain)
        if plain == lo:
            return a[1]
        if plain == hi:
            return b[1]
        return a[1] + ((plain - a[0]) / (b[0] - a[0])) * (b[1] - a[1])
    if p == 0:
        return lo
    if p == 1:
        return hi
    if law == "log":
        return lo * math.pow(hi / lo, p)
    if law == "stepped":
        n = _steps(travel, lo, hi)
        k = min(n - 1, max(0, _round(p * (n - 1))))
        return lo + k * travel["step"]
    return lo + p * (hi - lo)


def load_vectors(path=None):
    """The installed golden vectors; raises OSError when the omx-contract data package is not installed."""
    import json
    with open(path or VECTORS_PATH) as f:
        return json.load(f)
