# -*- coding: utf-8 -*-
# ******************************************************************************
# ZYNTHIAN PROJECT: one gesture vocabulary of a panel's controls (zynthian_panel_gestures)
#
# omx-panel declares the gestures of every surface once, in schema/gestures.json (its docs/panel-gestures.md): the
# timings, the thresholds and a table of what each input means to each kind of control. This module maps Zynthian's
# pointer, wheel, encoder and key inputs onto that table and moves a control in the control's OWN unit and taper
# (the position law of zynthian_position_law), reading every number from the declaration. It keeps no timing, no
# threshold and no table of its own: without the installed omx-panel (or the file ZYNTHIAN_OMX_GESTURES names) it
# refuses, it does not fall back to a copy.
#
# Zynthian takes every word but `second` (a handle's Q, refused as GESTURE_NOT_TAKEN) and `paint` (a bar group's
# sweep, which is not an input read here). A bar or a handle is a one-value control read from the same table by its
# key; the fork's pages draw none, and no control of it is two-dimensional, so an xy has no surface. `second`, a
# handle's Q, is taken where a row is given the controller that is its Q (Panel's `seconds`): by encoder 3, by
# Shift with the arrows, by the wheel and by a pinch of two touches (zynthian_touch_layer gives the second finger).
#
# Time is given by the caller (a Tk event's time, a script's timestamp), so a gesture is the same on the Pi and in
# a test.
#
# Copyright (C) 2026 Pau Aliagas <linuxnow@gmail.com>
#
# This program is free software; you can redistribute it and/or modify it under the terms
# of the GNU General Public License as published by the Free Software Foundation; either
# version 2 of the License, or any later version. See the LICENSE.txt file.
# ******************************************************************************

import json
import math
import os
import shutil

from zyngine import zynthian_position_law as law

ENV_TABLE = "ZYNTHIAN_OMX_GESTURES"


class GestureTableAbsent(OSError):
    """The omx-panel installed here carries no schema/gestures.json (a release before the gesture vocabulary)."""


def table_path():
    """The declaration: the file ZYNTHIAN_OMX_GESTURES names, else schema/gestures.json beside the installed omx-panel."""
    named = os.environ.get(ENV_TABLE)
    if named:
        return named
    panel = shutil.which("omx-panel")
    if panel:
        here = os.path.dirname(os.path.realpath(panel))
        for _ in range(6):
            for sub in ("schema", os.path.join("dist", "schema")):
                p = os.path.join(here, sub, "gestures.json")
                if os.path.isfile(p):
                    return p
            here = os.path.dirname(here)
    raise GestureTableAbsent("omx-panel's schema/gestures.json is not installed: no gestures (no copy of the table is kept)")


_loaded = {}


def load_table(path=None):
    """The declaration, read once per file and modification time."""
    path = path or table_path()
    key = (path, os.stat(path).st_mtime_ns)
    if key not in _loaded:
        with open(path) as f:
            _loaded[key] = json.load(f)
    return _loaded[key]


# ---- A control's travel, in its own unit ------------------------------------------------------------------------


class Travel:
    """Where a value sits on its travel (0..1) and back, by the position law; the declared default; the declared step."""

    def __init__(self, travel, default=None, ticks=None, thresholds=None):
        self.travel = dict(travel)
        self.default = default
        self.ticks = list(ticks) if ticks else None
        self.stepped = self.ticks is not None or travel.get("scale") == "stepped"
        self.fraction = (thresholds or load_table()["thresholds"])["stepFraction"]  # the declaration's, never a default here

    @classmethod
    def of(cls, zctrl, thresholds=None):
        """A plugin-hostd controller's travel; any other controller's own range, linear."""
        if zctrl.ticks:
            return cls({"scale": "stepped", "min": 0, "max": len(zctrl.ticks) - 1, "step": 1},
                       zctrl.value_default, zctrl.ticks, thresholds)
        if zctrl.is_toggle:
            return cls({"scale": "stepped", "min": zctrl.value_min, "max": zctrl.value_max, "step": zctrl.value_max - zctrl.value_min},
                       zctrl.value_default, None, thresholds)
        t = dict(zctrl.position_law) if zctrl.position_law else {"scale": "linear"}
        t.update(min=zctrl.value_min, max=zctrl.value_max)
        if zctrl.is_integer and t["scale"] == "linear":
            t.update(scale="stepped", step=1)
        return cls(t, zctrl.value_default, None, thresholds)

    # the value of the ticked travel is an index
    def position(self, value):
        if self.ticks:
            value = self.ticks.index(value) if value in self.ticks else self.nearest(value)
        return min(1.0, max(0.0, law.scale_position(self.travel, value)))

    def nearest(self, value):
        return min(range(len(self.ticks)), key=lambda i: abs(self.ticks[i] - value))

    def value(self, pos):
        v = law.scale_value(self.travel, min(1.0, max(0.0, pos)))
        if self.ticks:
            return self.ticks[int(v)]
        if self.travel.get("scale") == "stepped" and float(v).is_integer() and float(self.travel.get("step", 0.5)).is_integer():
            return int(v)
        return v

    def step_norm(self):
        """One declared step on the 0..1 travel: a stepped travel's, a linear travel's declared step, else 1% of the travel."""
        t = self.travel
        if self.stepped:
            n = round((t["max"] - t["min"]) / t["step"]) + 1
            return 1.0 / (n - 1)
        if t.get("step") and t.get("scale", "linear") == "linear":
            return t["step"] / (t["max"] - t["min"])
        return self.fraction

    def step_of(self, value):
        """One declared step in the control's unit at `value`: the tolerance of the proof."""
        p = self.position(value)
        return abs(law.scale_value(self.travel, min(1.0, p + self.step_norm())) - law.scale_value(self.travel, p)) if not self.ticks else 1


# ---- The gestures of one control --------------------------------------------------------------------------------


class Gestures:
    """The gestures of one control: `widget` is the table's key (knob, fader, switch, selector), `travel` a Travel,
    `get` returns the control's value, `put(value)` moves it, and the three words that move nothing are told to
    `on_word(word)`: select, more. Inputs carry their time in ms."""

    def __init__(self, widget, travel, get, put, target, orientation="vertical", travel_px=None,
                 on_word=None, readonly=False, table=None, second=None):
        self.table = table or load_table()
        self.widget, self.travel, self.get, self.put = widget, travel, get, put
        self.target = target  # (x, y, w, h)
        self.horizontal = orientation == "horizontal"
        self.travel_px = travel_px
        self.on_word = on_word or (lambda word: None)
        self.readonly = readonly or not self.table["table"].get(widget)
        self.log = []  # every gesture: {word, input, begin, end, changes}
        self.refused = []
        self.owner = None  # {id, ptype, t0, x0, y0, x, y, dragging, fired}
        self.claim = None  # {source, until}
        self.last_tap = None
        self.step_gesture = None
        self.second = second  # (Travel, get, put) of the handle's Q, else None: `second` then has no surface
        self.pointers = {}  # id => {x0, y0, x, y, t0, ptype}: every pointer of the gesture
        self.pinch = None  # {a, b, d0, q0, g}

    # -- helpers

    def word(self, inp):
        return self.table["table"].get(self.widget, {}).get(inp)

    def refuse(self, code, t, why):
        self.refused.append({"code": code, "t": t, "why": why})

    def open(self, word, inp, t):
        g = {"word": word, "input": inp, "begin": t, "end": t, "changes": []}
        self.log.append(g)
        return g

    def change(self, g, value):
        g["changes"].append(value)
        self.put(value)

    def inside(self, x, y):
        tx, ty, tw, th = self.target
        return tx <= x < tx + tw and ty <= y < ty + th

    def tick(self, t):
        """A press held the long-press time without a drag is `more`."""
        o = self.owner
        T = self.table["timings"]
        if o and not o["dragging"] and not o["fired"] and t - o["t0"] >= T["longPressMs"]["default"]:
            o["fired"] = True
            if self.word("longPress") == "more":
                g = self.open("more", "longPress", o["t0"] + T["longPressMs"]["default"])
                self.on_word("more")
                return g

    # -- the pointer

    def press(self, x, y, t, pid="p", ptype="mouse"):
        self.tick(t)
        if not self.inside(x, y):
            return
        if self.readonly:
            return self.refuse("GESTURE_READ_ONLY", t, f"a {self.widget} that is read-only takes no gesture")
        if self.owner and pid != self.owner["id"] and pid not in self.pointers:
            return self.press_second(x, y, t, pid, ptype)
        if (self.claim and t < self.claim["until"]) or self.owner:
            return self.refuse("GESTURE_CONTROL_OWNED", t, "the control belongs to another input")
        T = self.table["timings"]
        second = self.last_tap is not None and self.last_tap["ptype"] == ptype and t - self.last_tap["t"] <= T["doubleTapMs"]["default"]
        self.owner = {"id": pid, "ptype": ptype, "t0": t, "x0": x, "y0": y, "x": x, "y": y, "dragging": False,
                      "fired": False, "second": second}
        self.pointers = {pid: {"x0": x, "y0": y, "x": x, "y": y, "t0": t, "ptype": ptype}}

    def press_second(self, x, y, t, pid, ptype):
        """A later press while one holds the control: a pinch when the table gives this control one, the two pointers are of
        one type, the first began inside the target and this one begins within the palm guard of it; else owned."""
        o = self.owner
        first = self.pointers.get(o["id"])
        T = self.table["timings"]
        if not (self.word("pinch") == "second" and self.second and not self.pinch and ptype == o["ptype"]
                and t - o["t0"] <= T["palmGuardMs"]["default"] and first and self.inside(first["x0"], first["y0"])):
            return self.refuse("GESTURE_CONTROL_OWNED", t, "the control belongs to another input")
        d0 = math.hypot(x - first["x"], y - first["y"]) or 1.0
        o["dragging"] = o["fired"] = True  # the first finger no longer drags or taps
        self.pointers[pid] = {"x0": x, "y0": y, "x": x, "y": y, "t0": t, "ptype": ptype}
        q = self.second[1]()
        self.pinch = {"a": o["id"], "b": pid, "d0": d0, "q0": q, "g": self.open("second", "pinch", t)}

    def pinch_move(self, t):
        """The spread of the two pointers scales Q, kept in the Q's own range."""
        a, b = self.pointers[self.pinch["a"]], self.pointers[self.pinch["b"]]
        q = self.second[0].travel
        scaled = min(q["max"], max(q["min"], self.pinch["q0"] * (math.hypot(a["x"] - b["x"], a["y"] - b["y"]) / self.pinch["d0"])))
        self.pinch["g"]["changes"].append(scaled)
        self.second[2](scaled)
        self.pinch["g"]["end"] = t

    def motion(self, x, y, t, pid="p", fine=False):
        self.tick(t)
        o = self.owner
        if self.pinch and pid in self.pointers:
            self.pointers[pid].update(x=x, y=y)
            return self.pinch_move(t)
        if not o or o["id"] != pid:
            return
        o["x"], o["y"] = x, y
        self.pointers[pid].update(x=x, y=y)
        G = self.table["thresholds"]
        if not o["dragging"]:
            if math.hypot(x - o["x0"], y - o["y0"]) <= G["dragStartPx"]:
                return
            if not self.word("drag"):
                return
            o["dragging"] = o["fired"] = True
            o["acc"] = self.travel.position(self.get())
            o["g"] = self.open("fine" if fine else "drag", "dragFine" if fine else "drag", o["t0"])
            o["px"], o["py"] = x, y  # the threshold is dead travel: the value moves from here
        ratio = G["fineRatio"] if fine else 1
        if fine:
            o["g"].update(word="fine", input="dragFine")
        dx, dy = x - o["px"], y - o["py"]
        o["px"], o["py"] = x, y
        px = self.travel_px or G["defaultTravelPx"]
        o["acc"] += ((dx if self.horizontal else -dy) / px) * ratio
        self.change(o["g"], self.travel.value(o["acc"]))
        o["g"]["end"] = t

    def release(self, x, y, t, pid="p"):
        self.tick(t)
        o = self.owner
        if self.pinch and pid in self.pointers:
            self.pointers.pop(pid)
            self.pinch["g"]["end"] = t
            self.pinch = None
            if len(self.pointers) <= 1:
                self.owner, self.pointers = None, {}  # the finger left does not carry on as a drag
            return
        if not o or o["id"] != pid:
            return
        self.owner = None
        self.pointers = {}
        if o["dragging"]:
            o["g"]["end"] = t
            return
        if o["fired"]:
            return
        if not self.inside(x, y):
            return  # a release outside the target is nothing
        if o["second"] and self.word("doubleTap") == "reset":
            g = self.open("reset", "doubleTap", o["t0"])
            self.last_tap = None
            g["end"] = t
            return self.reset(g, t)
        word = self.word("tap")
        if not word:
            return self.refuse("GESTURE_NOT_IN_TABLE", t, f"a {self.widget} has no tap")
        g = self.open(word, "tap", o["t0"])
        if word == "toggle":
            lo, hi = self.travel.travel["min"], self.travel.travel["max"]
            self.change(g, lo if self.get() == hi else hi)
        elif word == "select":
            self.on_word("select")  # a tap selects and never changes a value
        g["end"] = t
        self.last_tap = {"t": t, "ptype": o["ptype"]}

    # -- wheel, encoder, key

    def reset(self, g, t):
        if self.travel.default is None:
            self.log.pop()
            return self.refuse("GESTURE_NO_DEFAULT", t, "the control declares no default")
        self.change(g, self.travel.default)  # the declared default itself, exact

    def step(self, n, t, coarse=False, source="wheel"):
        """A wheel notch (n = +1 / -1), an encoder detent or an arrow key: one step; coarse is five."""
        self.tick(t)
        if source == "key" and coarse and self.widget == "handle":
            coarse, source = False, "arrowSecond"  # Shift with the arrows is the handle's Q
        inp = "arrowSecond" if source == "arrowSecond" else "encoder3" if source == "encoder3" else ("detentCoarse" if coarse else "detent") if source == "detent" else ("wheelCoarse" if coarse else "wheel") if source == "wheel" else ("arrowCoarse" if coarse else "arrow")
        if self.readonly:
            return self.refuse("GESTURE_READ_ONLY", t, f"a {self.widget} that is read-only takes no gesture")
        if self.owner or (self.claim and t < self.claim["until"] and self.claim["source"] != source):
            return self.refuse("GESTURE_CONTROL_OWNED", t, "the control belongs to another input")
        word = self.word(inp)
        if not word:
            return self.refuse("GESTURE_NOT_IN_TABLE", t, f"a {self.widget} has no {inp}")
        if word == "second" and self.second is None or word not in ("step", "second"):
            return self.refuse("GESTURE_NOT_TAKEN", t, f"the {word} of a {self.widget} has no surface here")  # no Q
        G = self.table["thresholds"]
        T = self.table["timings"]
        travel, get, put = (self.second if word == "second" else (self.travel, self.get, self.put))
        count = abs(n) * (G["coarseSteps"] if coarse else 1)
        # steps of one source within the claim are one gesture
        if not (self.step_gesture and self.claim and self.claim["source"] == source and t < self.claim["until"]):
            self.step_gesture = self.open(word, inp, t)
        pos = travel.position(get()) + (1 if n > 0 else -1) * count * travel.step_norm()
        self.step_gesture["changes"].append(travel.value(pos))
        put(travel.value(pos))
        self.claim = {"source": source, "until": t + T["claimMs"]["default"]}
        self.step_gesture["end"] = t

    def key(self, name, t, shift=False):
        self.tick(t)
        if name == "BackSpace" or name == "Backspace":
            if self.readonly:
                return self.refuse("GESTURE_READ_ONLY", t, "read-only")
            if self.word("backspace") == "reset":
                return self.reset(self.open("reset", "backspace", t), t)
            return self.refuse("GESTURE_NOT_IN_TABLE", t, f"a {self.widget} has no backspace")
        sign = {"Up": 1, "Right": 1, "Down": -1, "Left": -1}.get(name.replace("Arrow", ""))
        if sign:
            self.step(sign, t, coarse=shift, source="key")

    def shown(self, t):
        """Whether the control shows its value text: from press to release and through the claim after a step."""
        return self.owner is not None or bool(self.claim and t < self.claim["until"])


# ---- Tk's events onto the inputs ---------------------------------------------------------------------------------

SHIFT = 0x1


def tk_press(gestures, e):
    gestures.press(e.x, e.y, e.time, "mouse1", "mouse")


def tk_motion(gestures, e):
    gestures.motion(e.x, e.y, e.time, "mouse1", fine=bool(e.state & SHIFT))


def tk_release(gestures, e):
    gestures.release(e.x, e.y, e.time, "mouse1")


def tk_wheel(gestures, e):
    """X11 wheel is buttons 4 and 5; elsewhere delta's sign. Shift is the coarse step."""
    up = getattr(e, "num", None) == 4 or (getattr(e, "delta", 0) or 0) > 0
    gestures.step(1 if up else -1, e.time, coarse=bool(e.state & SHIFT), source="wheel")


def tk_key(gestures, e):
    gestures.key(e.keysym, e.time, shift=bool(e.state & SHIFT))


# ---- A page of controls: one row each, the pointer routed to the row it pressed ---------------------------------


def widget_of(zctrl):
    """The table's key of a controller: a toggle is a switch, one with labels a selector, the rest a fader."""
    if zctrl.is_toggle:
        return "switch"
    return "selector" if zctrl.ticks else "fader"


class Panel:
    """The rows of a page. A row is a horizontal strip of the canvas, `margin` px in from each side, whose drawn length is
    the travel of its drag; a press belongs to the row it lands in, and the motion and the release follow that row."""

    def __init__(self, zctrls, width, height, margin, table=None, on_word=None, seconds=None):
        self.table = table or load_table()
        self.rows = []
        self.on_word = on_word or (lambda zctrl, word: None)
        self.pressed = None
        n = max(1, len(zctrls))
        for i, zctrl in enumerate(zctrls):
            travel = Travel.of(zctrl, self.table["thresholds"])
            q = (seconds or {}).get(zctrl.symbol)  # the controller that is this row's Q: the row is then a handle
            self.rows.append((zctrl, Gestures(
                "handle" if q is not None else widget_of(zctrl), travel, lambda z=zctrl: z.value, lambda v, z=zctrl: z.set_value(v),
                (0, i * height / n, width, height / n), orientation="horizontal", travel_px=max(1, width - 2 * margin),
                on_word=lambda word, z=zctrl: self.on_word(z, word), readonly=bool(zctrl.readonly), table=self.table,
                second=None if q is None else (Travel.of(q, self.table["thresholds"]), lambda z=q: z.value, lambda v, z=q: z.set_value(v)))))

    def row_at(self, y):
        for zctrl, g in self.rows:
            if g.inside(0, y):
                return g

    def press(self, x, y, t, pid="mouse1", ptype="mouse"):
        if self.pressed and pid not in self.pressed.pointers and self.pressed.owner:
            return self.pressed.press(x, y, t, pid, ptype)  # a second finger belongs to the row the first holds
        self.pressed = self.row_at(y)
        if self.pressed:
            self.pressed.press(x, y, t, pid, ptype)

    def motion(self, x, y, t, pid="mouse1", fine=False):
        if self.pressed:
            self.pressed.motion(x, y, t, pid, fine)

    def release(self, x, y, t, pid="mouse1"):
        row = self.pressed
        if row:
            row.release(x, y, t, pid)
            if row.owner is None:
                self.pressed = None

    def tick(self, t):
        for _, g in self.rows:
            g.tick(t)
