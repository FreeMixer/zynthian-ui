#!/usr/bin/python3
# -*- coding: utf-8 -*-
# ******************************************************************************
# ZYNTHIAN PROJECT: Zynthian Control Device Driver
#
# Control surface pages: what a surface's row of encoders shows for the selected processor
#
# Copyright (C) 2026 Pau Aliagas <linuxnow@gmail.com>
#
# ******************************************************************************
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License as
# published by the Free Software Foundation; either version 2 of
# the License, or any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# For a full copy of the GNU General Public License see the LICENSE.txt file.
#
# ******************************************************************************

import logging

# ------------------------------------------------------------------------------------------------------------------
# Surface pages: backend neutral, a MCU driver and a DigiNet one read the same pages
# ------------------------------------------------------------------------------------------------------------------


class zynthian_ctrldev_surface_pages:
    """The pages of a surface's encoders for a processor: its control groups in display order, `width` controls a
    page. A plugin-hostd processor groups its controls by omx-panel's layout pages (group_symbol "page<k>"), so
    the surface shows the layout's pages, the same the control screens show; any other engine shows its own groups.
    The current page follows the processor's current control screen, and paging the surface moves that screen."""

    def __init__(self, width=8):
        self.width = width
        self.processor = None
        self.index = 0

    def pages(self, processor=None):
        """[(page name, [zctrl])]: one page per control group, a group wider than the surface cut in pages."""
        processor = processor or self.processor
        if processor is None:
            return []
        zctrls = sorted(processor.controllers_dict.values(), key=lambda z: z.display_priority, reverse=True)
        groups = {}
        for zctrl in zctrls:
            if zctrl.not_on_gui:
                continue
            if zctrl.group_symbol not in groups:
                groups[zctrl.group_symbol] = [zctrl.group_name or zctrl.group_symbol or "Params", []]
            groups[zctrl.group_symbol][1].append(zctrl)
        result = []
        for name, group in groups.values():
            if len(group) <= self.width:
                result.append((name, group))
            else:
                n = (len(group) + self.width - 1) // self.width
                for i in range(n):
                    result.append((f"{name} {i + 1}/{n}", group[i * self.width:(i + 1) * self.width]))
        return result

    def select(self, processor):
        """Show `processor`, on the page of its current control screen."""
        self.processor = processor
        self.index = self.screen_page(processor)

    def screen_page(self, processor):
        """Index of the page holding the first control of the processor's current control screen, else 0."""
        if processor is None:
            return 0
        try:
            screens = list(processor.get_ctrl_screens().values())
            first = screens[processor.get_current_screen_index()][0]
        except (IndexError, TypeError, AttributeError):
            return 0
        for i, (name, zctrls) in enumerate(self.pages(processor)):
            if first in zctrls:
                return i
        return 0

    def page(self):
        """(page name, [zctrl]) of the current page, ("", []) when there is nothing to show."""
        pages = self.pages()
        if not pages:
            return "", []
        self.index = max(0, min(self.index, len(pages) - 1))
        return pages[self.index]

    def move(self, delta):
        """Page by `delta`; the processor's current control screen follows. Returns True if the page changed."""
        pages = self.pages()
        index = max(0, min(self.index + delta, len(pages) - 1)) if pages else 0
        if index == self.index:
            return False
        self.index = index
        try:
            first = pages[index][1][0]
            for i, screen in enumerate(self.processor.get_ctrl_screens().values()):
                if first in screen:
                    self.processor.set_current_screen_index(i)
                    break
        except (IndexError, AttributeError) as e:
            logging.debug(f"Surface page {index} has no control screen => {e}")
        return True

    def zctrl(self, i):
        zctrls = self.page()[1]
        return zctrls[i] if 0 <= i < len(zctrls) else None

    def turn(self, i, steps):
        """Turn encoder `i` by `steps` (negative is counterclockwise). Returns the zctrl it moved, or None."""
        zctrl = self.zctrl(i)
        if zctrl is None or not steps:
            return None
        zctrl.nudge(steps)
        return zctrl

    def label(self, i):
        zctrl = self.zctrl(i)
        return zctrl.short_name if zctrl else ""

    def value_text(self, i):
        zctrl = self.zctrl(i)
        if zctrl is None:
            return ""
        if zctrl.ticks:
            return str(zctrl.get_value2label())
        return f"{zctrl.value:.4g}"

    def position(self, i):
        """Position of control `i` in its travel, 0.0 to 1.0; None for an empty slot."""
        zctrl = self.zctrl(i)
        if zctrl is None:
            return None
        if zctrl.ticks:
            n = len(zctrl.ticks) - 1
            return zctrl.get_value2index() / n if n > 0 else 0.0
        span = zctrl.value_max - zctrl.value_min
        return max(0.0, min(1.0, (zctrl.value - zctrl.value_min) / span)) if span else 0.0

# ------------------------------------------------------------------------------------------------------------------
