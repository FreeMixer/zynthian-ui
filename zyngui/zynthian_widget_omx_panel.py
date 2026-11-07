#!/usr/bin/python3
# -*- coding: utf-8 -*-
# ******************************************************************************
# ZYNTHIAN PROJECT: Zynthian GUI
#
# Zynthian Widget: a page of an OpenMixer panel's controls on a Tk canvas
#
# Each control is a strip that moves with omx-panel's one gesture vocabulary (zynthian_panel_gestures): press and drag,
# Shift for fine, the wheel and the arrow keys for a step (Shift for five), double-click or Backspace for the declared
# default, a tap to select, a long press for `more`. The unit and the taper are the control's own; every number is
# read from omx-panel's schema/gestures.json at run time. Without it the strips are drawn and move with nothing.
#
# Copyright (C) 2026 Pau Aliagas <linuxnow@gmail.com>
#
# This program is free software; you can redistribute it and/or modify it under the terms
# of the GNU General Public License as published by the Free Software Foundation; either
# version 2 of the License, or any later version. See the LICENSE.txt file.
# ******************************************************************************

import logging
import tkinter

from zyngine import zynthian_panel_gestures as gestures
from zyngui import zynthian_gui_config
from zyngui.zynthian_widget_base import zynthian_widget_base

MARGIN = 10


class zynthian_widget_omx_panel(zynthian_widget_base):

    def __init__(self, parent):
        super().__init__(parent)
        self.widget_canvas = tkinter.Canvas(self, bd=0, highlightthickness=0, relief=tkinter.FLAT,
                                            bg=zynthian_gui_config.color_bg)
        self.widget_canvas.grid(sticky="news")
        self.panel = None
        self.table_error = None
        self.selected = None
        self.shown_until = {}
        c = self.widget_canvas
        c.bind("<ButtonPress-1>", self.on_press)
        c.bind("<B1-Motion>", self.on_motion)
        c.bind("<ButtonRelease-1>", self.on_release)
        c.bind("<MouseWheel>", self.on_wheel)
        c.bind("<Button-4>", self.on_wheel)
        c.bind("<Button-5>", self.on_wheel)
        c.bind("<Key>", self.on_key)

    def zctrls(self):
        return [z for z in self.zyngui_control.zcontrollers if not getattr(z, "not_on_gui", False)]

    def build(self):
        try:
            self.panel = gestures.Panel(self.zctrls(), self.width, self.height, MARGIN, on_word=self.on_word)
            self.table_error = None
        except gestures.GestureTableAbsent as e:
            self.panel, self.table_error = None, str(e)
            logging.error(e)

    def on_size(self, event):
        if super().on_size(event):
            self.build()
            return True
        return False

    def set_processor(self, processor):
        super().set_processor(processor)
        self.build()

    def on_word(self, zctrl, word):
        if word == "select":
            self.selected = zctrl.symbol
        elif word == "more":
            self.zyngui_control.show_menu()  # the surface's menu: never a value change

    def on_press(self, e):
        self.widget_canvas.focus_set()
        if self.panel:
            gestures.tk_press(_Routed(self.panel), e)
            self.refresh_gui()

    def on_motion(self, e):
        if self.panel:
            gestures.tk_motion(_Routed(self.panel), e)
            self.refresh_gui()

    def on_release(self, e):
        if self.panel:
            gestures.tk_release(_Routed(self.panel), e)
            self.refresh_gui()

    def on_wheel(self, e):
        row = self.panel and self.panel.row_at(e.y)
        if row:
            gestures.tk_wheel(row, e)
            self.refresh_gui()

    def on_key(self, e):
        row = self.panel and self.selected and next((g for z, g in self.panel.rows if z.symbol == self.selected), None)
        if row:
            gestures.tk_key(row, e)
            self.refresh_gui()

    def refresh_gui(self):
        c = self.widget_canvas
        c.delete("all")
        if not self.panel:
            c.create_text(self.width / 2, self.height / 2, text=self.table_error or "", fill=zynthian_gui_config.color_tx_off)
            return
        for zctrl, g in self.panel.rows:
            x, y, w, h = g.target
            pos = g.travel.position(zctrl.value)
            top, bottom = y + h * 0.3, y + h * 0.7
            c.create_rectangle(MARGIN, top, w - MARGIN, bottom, outline=zynthian_gui_config.color_tx_off)
            c.create_rectangle(MARGIN, top, MARGIN + pos * (w - 2 * MARGIN), bottom, fill=zynthian_gui_config.color_on,
                               outline="")
            label = zctrl.short_name if zctrl.symbol != self.selected else f"> {zctrl.short_name}"
            c.create_text(MARGIN, y + h * 0.15, anchor="w", text=label, fill=zynthian_gui_config.color_tx)
            c.create_text(w - MARGIN, y + h * 0.15, anchor="e", text=zctrl.get_value2label(), fill=zynthian_gui_config.color_tx)


class _Routed:
    """The panel as the one control Tk's helpers move: a press picks the row, the rest follow it."""

    def __init__(self, panel):
        self.panel = panel

    def press(self, x, y, t, pid, ptype):
        self.panel.press(x, y, t, pid, ptype)

    def motion(self, x, y, t, pid, fine=False):
        self.panel.motion(x, y, t, pid, fine)

    def release(self, x, y, t, pid):
        self.panel.release(x, y, t, pid)


# ------------------------------------------------------------------------------
