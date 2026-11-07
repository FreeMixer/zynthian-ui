# -*- coding: utf-8 -*-
# ******************************************************************************
# ZYNTHIAN PROJECT: the touch layer of the panel gestures (zynthian_touch_layer)
#
# Tk 8.6 sees one pointer. A multi-touch screen reports every finger on the evdev multi-touch protocol (type B: a slot
# per finger, ABS_MT_SLOT, ABS_MT_TRACKING_ID, ABS_MT_POSITION_X/Y, closed by SYN_REPORT). This module turns that
# stream into pointers (down, move, up, each with its slot's id, its place on the canvas and the time in ms) and gives
# them to the panel, which reads a two-finger spread as the gesture table's `pinch`. It holds no gesture law: the
# word, the palm guard and the scaling are the table's and zynthian_panel_gestures'.
#
# Facts of the Pi it was written on (read-only, 2026-11-07): the Zynthian there has a generic HDMI display and no touch
# device (/proc/bus/input/devices lists the power button and the two HDMI ports), no X session runs, X uses
# xserver-xorg-input-evdev, python3-evdev is installed, and /dev/input/event* are root:input, rw for root, which is the
# user Zynthian runs as. So no multi-touch device was read here: the parser is proved on synthetic slot streams, and a
# machine with no device or no permission makes EvdevSource raise NoTouchDevice with the system's own words.
#
# The source of events is separate from the parser: `Slots` takes (type, code, value) triples, so the tests feed it
# synthetic two-slot streams; `EvdevSource` reads a real device and refuses, with the system's own words, a device it
# may not open or a machine with none.
#
# Copyright (C) 2026 Pau Aliagas <linuxnow@gmail.com>
#
# This program is free software; you can redistribute it and/or modify it under the terms
# of the GNU General Public License as published by the Free Software Foundation; either
# version 2 of the License, or any later version. See the LICENSE.txt file.
# ******************************************************************************

EV_SYN, EV_ABS = 0x00, 0x03
SYN_REPORT = 0x00
ABS_MT_SLOT, ABS_MT_POSITION_X, ABS_MT_POSITION_Y, ABS_MT_TRACKING_ID = 0x2F, 0x35, 0x36, 0x39


class NoTouchDevice(OSError):
    """No input device of this machine reports multi-touch slots, or none may be opened; the message says which."""


class Slots:
    """The multi-touch protocol's slots as pointers. `emit(kind, id, x, y, t)` gets kind 'down', 'move' or 'up'; (x, y) are
    on the canvas: the device's range (`ranges` = ((xmin, xmax), (ymin, ymax))) scaled to `size` = (width, height)."""

    def __init__(self, emit, ranges, size):
        self.emit, self.ranges, self.size = emit, ranges, size
        self.slot = 0
        self.live = {}  # slot => {id, x, y, moved, new}

    def feed(self, etype, code, value, t_ms=0):
        if etype == EV_ABS:
            if code == ABS_MT_SLOT:
                self.slot = value
            elif code == ABS_MT_TRACKING_ID:
                if value < 0:
                    s = self.live.get(self.slot)
                    if s:
                        s["gone"] = True
                else:
                    self.live[self.slot] = {"id": f"touch{value}", "x": None, "y": None, "new": True, "moved": False}
            elif code in (ABS_MT_POSITION_X, ABS_MT_POSITION_Y) and self.slot in self.live:
                s = self.live[self.slot]
                s["x" if code == ABS_MT_POSITION_X else "y"] = value
                s["moved"] = True
        elif etype == EV_SYN and code == SYN_REPORT:
            self.report(t_ms)

    def canvas(self, s):
        (x0, x1), (y0, y1) = self.ranges
        return ((s["x"] - x0) / ((x1 - x0) or 1) * self.size[0], (s["y"] - y0) / ((y1 - y0) or 1) * self.size[1])

    def report(self, t):
        for slot in sorted(self.live):
            s = self.live[slot]
            if s["x"] is None or s["y"] is None:
                continue  # a finger is a pointer from the report that gives both its coordinates
            x, y = self.canvas(s)
            if s["new"]:
                s["new"], s["moved"] = False, False
                self.emit("down", s["id"], x, y, t)
            elif s["moved"]:
                s["moved"] = False
                self.emit("move", s["id"], x, y, t)
            if s.get("gone"):
                self.emit("up", s["id"], x, y, t)
                del self.live[slot]


def route(panel):
    """Slots' pointers into a panel of rows (zynthian_panel_gestures.Panel): the type is touch, never a mouse."""
    def emit(kind, pid, x, y, t):
        if kind == "down":
            panel.press(x, y, t, pid, "touch")
        elif kind == "move":
            panel.motion(x, y, t, pid)
        else:
            panel.release(x, y, t, pid)
    return emit


class EvdevSource:
    """A multi-touch device through python3-evdev. Raises NoTouchDevice with the reason when there is none to read."""

    def __init__(self, path=None):
        try:
            import evdev
        except ImportError as e:
            raise NoTouchDevice(f"python3-evdev is not installed ({e})")
        self.evdev = evdev
        paths = [path] if path else evdev.list_devices()
        refused = []
        for p in paths:
            try:
                dev = evdev.InputDevice(p)
            except OSError as e:
                refused.append(f"{p}: {e}")  # e.g. [Errno 13] Permission denied
                continue
            caps = dev.capabilities().get(EV_ABS, [])
            codes = {c[0] if isinstance(c, tuple) else c: c[1] if isinstance(c, tuple) else None for c in caps}
            if ABS_MT_SLOT in codes and ABS_MT_POSITION_X in codes:
                self.device = dev
                self.ranges = ((codes[ABS_MT_POSITION_X].min, codes[ABS_MT_POSITION_X].max),
                               (codes[ABS_MT_POSITION_Y].min, codes[ABS_MT_POSITION_Y].max))
                self.slots = codes[ABS_MT_SLOT].max + 1  # the number of fingers the device tells apart
                return
            dev.close()
        raise NoTouchDevice("no input device reports multi-touch slots" + (f" ({'; '.join(refused)})" if refused else ""))

    def poll(self, slots):
        """Feed Slots what the device has now and return at once (for a Tk `after`); returns whether anything came."""
        import os
        os.set_blocking(self.device.fd, False)
        got = False
        try:
            for e in self.device.read():
                got = True
                slots.feed(e.type, e.code, e.value, int(e.timestamp() * 1000))
        except BlockingIOError:
            pass
        return got
