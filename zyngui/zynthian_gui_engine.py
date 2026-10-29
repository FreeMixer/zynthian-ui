#!/usr/bin/python3
# -*- coding: utf-8 -*-
# ******************************************************************************
# ZYNTHIAN PROJECT: Zynthian GUI
#
# Zynthian GUI Engine Selector Class
#
# Copyright (C) 2015-2026 Fernando Moyano <jofemodo@zynthian.org>
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

import tkinter
import logging
import importlib
from time import sleep

# Zynthian specific modules
from zyngine import *
from zyngine import zynthian_lv2
from zyngine import zynthian_plugin_hostd_catalogue
from zyngine import zynthian_plugin_hostd_estimate
# The module: zyngine's package exports the engine class under the same name
plugin_hostd = importlib.import_module("zyngine.zynthian_engine_plugin_hostd")
from zyngine import zynthian_plugin_qualification
from zyngine import zynthian_plugin_tiers
from zyngui import zynthian_gui_config
from zyngui.zynthian_gui_base import zynthian_gui_base
from zyngui.zynthian_gui_selector import zynthian_gui_selector
from zyngui.zynthian_gui_controller import zynthian_gui_controller

# ------------------------------------------------------------------------------
# Zynthian Engine Selection GUI Class
# ------------------------------------------------------------------------------


class zynthian_gui_engine(zynthian_gui_selector):

    engine_type_title = {
        "MIDI Synth": "MIDI Instrument",
        "Audio Effect": "Audio Effect",
        "MIDI Tool": "MIDI tool",
        "Audio Generator": "Audio Generator",
        "Special": "Special"
    }

    # Custom layout for GUI engine
    layout = {
        'name': 'gui_engine',
        'columns': 3,
        'rows': 4,
        'ctrl_pos': [
            (0, 2),
            (1, 2),
            (2, 2),
            (3, 2)
        ],
        'list_pos': (0, 1),
        'list_width': 0.5,
        'list2_pos': (0, 0),
        'list2_width': 0.21,
        'ctrl_orientation': 'horizontal',
        'ctrl_order': zynthian_gui_config.layout['ctrl_order'],
        'ctrl_width': 0.29
    }

    def __init__(self):
        self.proc_type = None
        self.zsel2 = None
        self.cat_index = 0
        self.engine_cats = None
        self.context_index = {}
        self.show_all = False
        self.info_canvas = None
        self.engine_info_dirty = False
        self.xswipe_sens = 10
        self.qualification = zynthian_plugin_qualification.qualification()
        self.menu_entries = {}
        self.list_order = "tier"  # one of zynthian_plugin_tiers.ORDERS
        self.hosted_only = False  # list only the plugins plugin-hostd runs
        # The chain's figures with the highlighted plugin in it, asked of plugin-hostd off this thread
        self.estimator = zynthian_plugin_hostd_estimate.estimator(plugin_hostd.estimate)
        self.estimate_key = None
        self.estimate_text = ""

        super().__init__('Engine', True, False)

        # ListBox for Categories
        self.lb2_bg = zynthian_gui_config.color_panel_bg
        self.lb2_fg = zynthian_gui_config.color_panel_tx
        self.listbox2_y0 = None
        self.listbox2_dragging = False
        self.listbox2 = tkinter.Listbox(
            self.main_frame,
            font=zynthian_gui_config.font_listbox,
            bd=7,
            highlightthickness=0,
            relief='flat',
            bg=self.lb2_bg,
            fg=self.lb2_fg,
            selectbackground=self.lb2_bg,
            selectforeground=self.lb2_fg,
            selectmode=tkinter.SINGLE)
        self.listbox2.bind("<Button-1>", self.cb_listbox2_push)
        self.listbox2.bind("<ButtonRelease-1>", self.cb_listbox2_release)
        self.listbox2.bind("<B1-Motion>", self.cb_listbox2_motion)
        self.listbox2.bind("<Button-4>", self.cb_listbox2_wheel)
        self.listbox2.bind("<Button-5>", self.cb_listbox2_wheel)
        self.listbox2.grid(
            row=self.layout['list2_pos'][0],
            column=self.layout['list2_pos'][1],
            rowspan=self.layout['rows'],
            padx=self.padx,
            pady=self.pady,
            sticky="news")

        # One line under the lists: what the chain costs with the highlighted plugin in it
        self.estimate_label = tkinter.Label(
            self.main_frame, text="", anchor="w", bd=0,
            font=("sans-serif", int(0.8 * zynthian_gui_config.font_size)),
            bg=zynthian_gui_config.color_bg, fg=zynthian_gui_config.color_panel_tx)
        self.estimate_label.grid(row=self.layout['rows'], column=0, columnspan=self.layout['columns'],
                                 padx=self.padx, sticky="we")

        # Canvas for engine info
        self.info_canvas = tkinter.Canvas(
            self.main_frame,
            width=1,  # zynthian_gui_config.fw2, #self.width // 4 - 2,
            height=1,  # zynthian_gui_config.fh2, #self.height // 2 - 1,
            bd=0,
            highlightthickness=0,
            bg=zynthian_gui_config.color_bg)
        self.info_canvas.bind('<ButtonRelease-1>', self.cb_info_press)
        # Position at top of column containing selector
        self.info_canvas.grid(row=0, column=self.layout['list_pos'][1] + 1, rowspan=4, sticky="news")

        # Marker for category page
        # self.cat_marker_greyline = self.info_canvas.create_rectangle(0, 2, 0.25 * self.width, 4, fill=zynthian_gui_config.color_off)
        # self.cat_marker_marker = self.info_canvas.create_rectangle(0, 0, 0, 0, fill=zynthian_gui_config.color_on)

        # Info layout
        ctrl_width = int(self.layout['ctrl_width'] * self.width)
        star_fs = int(ctrl_width * 0.16)
        # color_star = zynthian_gui_config.color_ml
        color_star = zynthian_gui_config.color_on
        color_star_off = zynthian_gui_config.color_off
        xpos = int(0.1 * star_fs)
        ypos = int(-0.3 * star_fs)
        info_width = ctrl_width - xpos
        self.quality_stars_bg_label = self.info_canvas.create_text(
            xpos,
            ypos,
            anchor=tkinter.NW,
            justify=tkinter.CENTER,
            width=info_width,
            text="★★★★★",
            # text="✱✱✱✱✱",
            font=(zynthian_gui_config.font_family, star_fs),
            fill=color_star_off,
            tags="stars")
        self.quality_stars_label = self.info_canvas.create_text(
            xpos,
            ypos,
            anchor=tkinter.NW,
            justify=tkinter.CENTER,
            width=info_width,
            text="",
            font=(zynthian_gui_config.font_family, star_fs),
            fill=color_star,
            tags="stars")
        ypos += int(1.2 * star_fs)
        self.complexity_stars_bg_label = self.info_canvas.create_text(
            xpos,
            ypos,
            anchor=tkinter.NW,
            justify=tkinter.CENTER,
            width=info_width,
            #text="⚈⚈⚈⚈⚈",
            text="⚙️⚙️⚙️⚙️⚙️",
            font=(zynthian_gui_config.font_family, star_fs),
            fill=color_star_off,
            tags="stars")
        self.complexity_stars_label = self.info_canvas.create_text(
            xpos,
            ypos,
            anchor=tkinter.NW,
            justify=tkinter.CENTER,
            width=info_width,
            text="",
            font=(zynthian_gui_config.font_family, star_fs),
            fill=color_star,
            tags="stars")
        ypos += int(1.6 * star_fs)

        self.description_label = self.info_canvas.create_text(
            xpos,
            ypos,
            anchor=tkinter.NW,
            justify=tkinter.LEFT,
            width=info_width,
            text="",
            # font=(zynthian_gui_config.font_family, int(0.8 * zynthian_gui_config.font_size)),
            font=("sans-serif", int(0.8 * zynthian_gui_config.font_size)),
            fill=zynthian_gui_config.color_panel_tx)

    def update_layout(self):
        # Call grandpa's method
        zynthian_gui_base.update_layout(self)
        ctrl_width = int(self.width * self.layout['ctrl_width'] * self.sidebar_shown)
        lb_width = int(self.width * self.layout['list_width'])
        lb2_width = int(self.width * self.layout['list2_width'])
        lb_weight = 3
        self.main_frame.columnconfigure(0, minsize=lb2_width, weight=lb_weight)
        self.main_frame.columnconfigure(1, minsize=lb_width, weight=lb_weight)
        self.main_frame.columnconfigure(2, minsize=ctrl_width, weight=self.sidebar_shown)
        if self.info_canvas:
            self.info_canvas.configure(height=int(0.6 * self.height))
            ctrl_width = int(self.layout['ctrl_width'] * self.width)
            star_fs = int(ctrl_width * 0.16)
            xpos = int(0.1 * star_fs)
            info_width = ctrl_width - xpos
            self.info_canvas.itemconfigure("stars", font=(zynthian_gui_config.font_family, star_fs))
            self.info_canvas.itemconfigure(self.description_label, width=info_width)

    def get_info(self, eng_code=None):
        if not eng_code:
            eng_code = self.list_data[self.index][0]
        try:
            return self.chain_manager.engine_info[eng_code]
        except:
            #logging.warning(f"Can't get info for engine '{eng_code}'")
            return {"QUALITY": 0, "COMPLEX": 0, "DESCR": ""}

    def update_info(self):
        eng_info = self.get_info()
        quality_stars = "★" * eng_info["QUALITY"]
        self.info_canvas.itemconfigure(self.quality_stars_label, text=quality_stars)
        complexity_stars = "⚙️" * eng_info["COMPLEX"]
        self.info_canvas.itemconfigure(self.complexity_stars_label, text=complexity_stars)
        self.info_canvas.itemconfigure(self.description_label, text=eng_info["DESCR"])

    def show_details(self, eng_code=None):
        eng_info = self.get_info(eng_code)
        try:
            path = zynthian_lv2.engine_type_title[eng_info["TYPE"]]
        except:
            path = eng_info["TYPE"]
        if self.engine_cats:
            path = path + "/" + eng_info["CAT"]
        if eng_info:
            description = eng_info["DESCR"].replace("\n", "</p>\n<p>")
            html = f"""<html>
 <head>
  <link rel="stylesheet" href="style_details.css">
 </head>
 <body class="help_ui">
 <div class="details_container">
  <h1>{eng_info['NAME']}</h1>
  <div class="engine_path">{path}</div>
  <div class="quality">Quality: <span class="stars">{"★" * eng_info["QUALITY"]}</span></div>
  <div class="complexity">Complexity: <span class="stars">{"⚙️" * eng_info["COMPLEX"]}</span></div>
  <p class="description">{description}</p>
 </body>
</html>
"""
            self.zyngui.screens['help'].set_html(html)

    def get_engines_by_cat(self):
        self.chain_manager.get_engine_info()
        self.proc_type = self.zyngui.modify_chain_status["type"]
        self.engines_by_cat = self.chain_manager.filtered_engines_by_cat(self.proc_type, all=self.show_all)
        for exclude in ["MI", "MR", "MX"]:
            try:
                self.engines_by_cat["Other"].pop(exclude)
            except:
                pass
        # Each menu as qualification orders it; a category whose plugins are all hidden goes too
        self.menu_entries = self.qualified_entries(self.engines_by_cat)
        self.engines_by_cat = {cat: engines for cat, engines in self.engines_by_cat.items()
                               if self.menu_entries[cat] or not engines}
        self.engine_cats = list(self.engines_by_cat.keys())
        self.cat_index = min(self.cat_index, len(self.engine_cats) - 1)
        # Fill category list
        self.listbox2.delete(0, tkinter.END)
        for cat in self.engine_cats:
            self.listbox2.insert(tkinter.END, cat)
        self.listbox2.itemconfig(self.cat_index, {'bg': self.lb2_fg, 'fg': self.lb2_bg})
        # self.engines_by_cat = sorted(self.engines_by_cat.items(), key=lambda kv: "!" if kv[0] is None else kv[0])

    def qualified_entries(self, engines_by_cat):
        """{cat: [(engine, text, greyed)]}: the plugins tagged with their tier and their figures at the current
        rate and quantum, in the chosen order, the hidden ones dropped or greyed. Plain titles in their own
        order if the qualification can't be read."""
        try:
            rate = self.zyngui.state_manager.get_jackd_samplerate()
            quantum = self.zyngui.state_manager.get_jackd_blocksize()
        except Exception:
            rate = quantum = None
        try:
            q = self.qualification.refresh()
            live_ms, show = zynthian_plugin_tiers.live_max_ms(), zynthian_plugin_tiers.show_hidden()
            return {cat: zynthian_plugin_tiers.menu_entries(engines, lambda url: q.facts(url, rate, quantum), live_ms,
                                                             show, self.list_order, self.hosted_only)
                    for cat, engines in engines_by_cat.items()}
        except Exception as e:
            logging.error(f"Can't read the plugin qualification => {e}")
            return {cat: [(eng, info["TITLE"], False) for eng, info in engines.items()]
                    for cat, engines in engines_by_cat.items()}

    def recall_context_index(self):
        try:
            self.index = self.context_index[self.proc_type + "#" + str(self.cat_index)]
        except:
            self.index = 0
            self.update_context_index()

    def update_context_index(self):
        self.context_index[self.proc_type + "#" + str(self.cat_index)] = self.index

    def build_view(self):
        self.show_all = False
        self.get_engines_by_cat()
        self.recall_context_index()
        return super().build_view()

    def hide(self):
        try:
            self.context_index[self.zyngui.modify_chain_status["type"]] = self.index
        except:
            pass
        super().hide()

    def fill_list(self):
        self.list_data = []

        if self.proc_type in ("MIDI Tool", "Audio Effect"):
            self.list_data.append(("None", 0, "None", "None"))

        greyed = []
        if self.engine_cats:
            # Fill engine list
            cat = self.engine_cats[self.cat_index]
            engines_info = self.engines_by_cat[cat]
            for eng, text, grey in self.menu_entries[cat]:
                i = len(self.list_data)
                info = engines_info[eng]
                if grey:
                    greyed.append(i)
                if self.show_all:
                    if info["ENABLED"]:
                        self.list_data.append((eng, i, "\u2612 " + text, info["NAME"]))
                    else:
                        self.list_data.append((eng, i, "\u2610 " + text, info["NAME"]))
                else:
                    self.list_data.append((eng, i, text, info["NAME"]))

        # Display help if no engines are enabled ...
        if len(self.list_data) == 0:
            self.list_data.append((None, len(self.list_data), "Bold-push to enable some engines"))
            self.index = 0
            self.update_context_index()

        if not self.show_all:
            self.engine_info_dirty = False

        super().fill_list()
        for i in greyed:
            self.listbox.itemconfig(i, {'fg': zynthian_gui_config.color_tx_off})

    def select(self, index=None, set_zctrl=True):
        super().select(index, set_zctrl)
        self.update_info()
        self.update_context_index()
        self.ask_estimate()

    def ask_estimate(self):
        """The chain's figures with the highlighted plugin in it, asked of plugin-hostd while the highlight
        rests; the answer is shown when it comes, and the list is never waited for."""
        refs = []
        try:
            fmt = plugin_hostd.audio_format(self.zyngui.state_manager)
            status = self.zyngui.modify_chain_status
            if fmt and not self.show_all and self.list_data and self.list_data[self.index][0]:
                eng_code = self.list_data[self.index][0]
                chain = self.chain_manager.get_chain(status["chain_id"]) if "chain_id" in status else None
                refs = plugin_hostd.menu_refs(
                    chain, eng_code, self.chain_manager.engine_info[eng_code], status.get("processor"))
        except Exception as e:
            logging.debug(f"No chain figures for the highlighted plugin => {e}")
        self.estimate_key = (self.proc_type, self.cat_index, self.index, tuple(refs))
        self.set_estimate_text("")
        if refs:
            self.estimator.request(self.estimate_key, fmt[0], fmt[1], refs)
            self.main_frame.after(150, self.poll_estimate)

    def poll_estimate(self):
        result = self.estimator.result
        if result and result[0] == self.estimate_key:
            self.set_estimate_text(result[1] or "")
        elif self.shown and self.estimate_key[3]:
            self.main_frame.after(150, self.poll_estimate)

    def set_estimate_text(self, text):
        self.estimate_text = text
        self.estimate_label.configure(text=text)

    def select_action(self, i, t='S'):
        if t == 'S':
            if i is not None and self.list_data[i][0]:
                engine = self.list_data[i][0]
                if self.show_all:
                    info = self.chain_manager.engine_info[engine]
                    zynthian_plugin_hostd_catalogue.set_enabled(self.chain_manager.engine_info, engine, not info['ENABLED'])
                    if info['EDIT'] == 0:
                        info['EDIT'] = 1
                    self.engine_info_dirty = True
                    self.update_list()
                else:
                    self.zyngui.modify_chain_status["engine"] = engine
                    if "chain_id" in self.zyngui.modify_chain_status:
                        # Modifying existing chain
                        pass
                    else:
                        # Adding engine to new chain
                        if engine == "AP":
                            # TODO: Better done with engine flag
                            self.zyngui.modify_chain_status["audio_thru"] = False
                        if self.zyngui.modify_chain_status["type"] in ("Audio Effect", "Audio Generator") and not self.zyngui.modify_chain_status["midi_thru"]:
                            self.zyngui.modify_chain_status["midi_chan"] = None
                    self.zyngui.modify_chain()
        elif t == 'B':
            if not self.back_action():
                self.show_all = True
                self.get_engines_by_cat()
                self.update_list()

    # --------------------------------------------------------------------------
    # List options: the order and the plugin-hostd filter
    # --------------------------------------------------------------------------

    order_titles = {"tier": "Tier", "name": "Name", "cost": "Cost", "latency": "Latency", "rt": "RT safety"}

    def show_menu(self):
        options = {"Sort by": None}
        for order, title in self.order_titles.items():
            mark = "\u2612" if order == self.list_order else "\u2610"
            options[f"{mark} {title}"] = ("order", order)
        options["Filter"] = None
        mark = "\u2612" if self.hosted_only else "\u2610"
        options[f"{mark} plugin-hostd plugins only"] = ("hosted_only", not self.hosted_only)
        self.zyngui.screens['option'].config("Plugin list", options, self.list_options_cb)
        self.zyngui.show_screen('option')

    def toggle_menu(self):
        if self.shown:
            self.show_menu()
        elif self.zyngui.get_current_screen() == "option":
            self.close_screen()

    def list_options_cb(self, option, value):
        what, value = value
        if what == "order":
            self.list_order = value
        else:
            self.hosted_only = value
        self.index = 0
        self.get_engines_by_cat()
        self.update_context_index()
        self.update_list()

    def back_action(self):
        if self.show_all:
            if self.engine_info_dirty:
                self.chain_manager.save_engine_info()
                self.engine_info_dirty = False
            self.show_all = False
            self.get_engines_by_cat()
            self.update_list()
            return True
        else:
            return False

    def arrow_right(self):
        self.zynpot_cb(2, 1)

    def arrow_left(self):
        self.zynpot_cb(2, -1)

    def switch(self, swi, t='S'):
        if swi == 2:
            if t == 'S':
                self.show_details()
                return True

    def cuia_v5_zynpot_switch(self, params):
        i = params[0]
        t = params[1].upper()
        if i == 2 and t == 'S':
            self.show_details()
            return True
        return False

    def set_selector(self, zs_hidden=True):
        super().set_selector(zs_hidden)
        self.zselector.zctrl.engine = self
        if self.zsel2:
            self.zsel2.zctrl.set_options({'symbol': "cat_index", 'name': "Category", 'short_name': "Category",
                                          'value_min': 0, 'value_max': len(self.engine_cats) - 1,
                                          'value': self.cat_index})
            self.zsel2.config(self.zsel2.zctrl)
            self.zsel2.show()
        else:
            zsel2_ctrl = zynthian_controller(self, "cat_index",
                                             {'name': "Category", 'short_name': "Category", 'value_min': 0,
                                              'value_max': len(self.engine_cats) - 1, 'value': self.cat_index})
            self.zsel2 = zynthian_gui_controller(zynthian_gui_config.select_ctrl - 1, self.main_frame,
                                                 zsel2_ctrl, zs_hidden,
                                                 selcounter=True,
                                                 orientation=self.layout['ctrl_orientation'])
        if not self.zselector_hidden:
            self.zsel2.grid(row=self.layout['ctrl_pos'][2][0],
                            column=self.layout['ctrl_pos'][2][1], sticky="news", pady=(0, 1))

    def plot_zctrls(self):
        super().plot_zctrls()
        if self.zsel2.zctrl.is_dirty:
            self.zsel2.calculate_plot_values()
            self.zsel2.plot_value()
            self.zsel2.zctrl.is_dirty = False

    def set_cat(self, cat_index):
        # Highlight category in category lisbox
        self.listbox2.itemconfig(self.cat_index, {'bg': self.lb2_bg, 'fg': self.lb2_fg})
        self.cat_index = max(0, min(cat_index, len(self.engine_cats) - 1))
        self.listbox2.itemconfig(self.cat_index, {'bg': self.lb2_fg, 'fg': self.lb2_bg})
        self.listbox2.see(self.cat_index)
        # Load engines for the category
        self.recall_context_index()
        self.update_list()
        # Update header breadcrumb
        self.set_select_path()
        if self.zyngui.tts:
            self.zyngui.tts.announce(f"Category: {self.engine_cats[cat_index]}, Engine: {self.list_data[self.index][2]}")

    def zynpot_cb(self, i, dval):
        if not self.shown:
            return False
        # Use secondary selector to move across categories
        if self.zsel2 and self.zsel2.index == i:
            self.zsel2.zynpot_cb(dval)
            if self.cat_index != self.zsel2.zctrl.value:
                self.set_cat(self.zsel2.zctrl.value)
            return True
        else:
            return super().zynpot_cb(i, dval)

    def send_controller_value(self, zctrl):
        if not self.shown:
            return
        if zctrl.symbol == "cat_index":
            if self.cat_index != zctrl.value:
                self.set_cat(zctrl.value)
        if zctrl.symbol == "Engine":
            self.select(zctrl.value)

    # --------------------------------------------------------------------------
    # Keyboard & Mouse/Touch Callbacks
    # --------------------------------------------------------------------------

    def cb_listbox2_push(self, event):
        if self.zyngui.cb_touch(event):
            return "break"
        cursel = self.listbox2.nearest(event.y)
        if cursel != self.cat_index:
            self.set_cat(cursel)
        return "break"

    def cb_listbox2_motion(self, event):
        cursel = self.listbox2.nearest(event.y)
        if cursel != self.cat_index:
            self.set_cat(cursel)

    def cb_listbox2_release(self, event):
        if self.zyngui.cb_touch_release(event):
            return "break"

    def cb_listbox2_wheel(self, event):
        if event.num == 5 or event.delta == -120:
            self.set_cat(self.cat_index + 1)
        elif event.num == 4 or event.delta == 120:
            self.set_cat(self.cat_index - 1)
        return "break"  # Consume event to stop scrolling of listbox

    def cb_info_press(self, event):
        self.show_details()

    # -------------------------------------------------------------------------

    def set_select_path(self):
        path = ""
        try:
            path = zynthian_lv2.engine_type_title[self.zyngui.modify_chain_status["type"]]
            # chain = self.chain_manager.chains[self.zyngui.modify_chain_status["chain_id"]].get_name()
            # path = f"{chain}#{path}"
        except:
            pass
        if self.engine_cats:
            path = path + "/" + self.engine_cats[self.cat_index]
        self.select_path.set(path)

    # --------------------------------------------------------------------------
    # ZynVoice TTS
    # --------------------------------------------------------------------------

    def tts_info(self):
        if not self.zyngui.tts:
            return
        eng_info = self.get_info()
        self.zyngui.tts.announce(f"View: Engine")
        self.zyngui.tts.announce(f"Category: {self.engine_cats[self.cat_index]}", False, False, False)
        self.zyngui.tts.announce(f"Engine: {self.list_data[self.index][2]}", False, False, False)
        complex = ["No", "Minimal", "Low", "Medium", "High", "Maximum"][eng_info["COMPLEX"]]
        self.zyngui.tts.announce(f"{eng_info['QUALITY']} stars. {complex} complexity. {eng_info['DESCR']}", False, False, False)
        self.zyngui.tts.announce(f"{self.index + 1} of {len(self.list_data)}", False, False, False)

# ------------------------------------------------------------------------------
