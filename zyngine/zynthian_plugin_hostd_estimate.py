# -*- coding: utf-8 -*-
# ******************************************************************************
# ZYNTHIAN PROJECT: plugin-hostd estimate (zynthian_plugin_hostd_estimate)
#
# What a chain will cost, as plugin-hostd's estimate verb answers it from the qualification records:
#   estimate <rate> <quantum> <plugin-ref>...  =>  resp 0 <latency_frames> <cpu_percent> <ram_bytes> <workers> <unknown_count>
# The figures are the daemon's; this file parses the reply, words it for a one-line footer, and asks for it
# off the UI thread: debounced, one request in flight, the newest question the only one answered.
#
# Copyright (C) 2026 Pau Aliagas <linuxnow@gmail.com>
#
# This program is free software; you can redistribute it and/or modify it under the terms
# of the GNU General Public License as published by the Free Software Foundation; either
# version 2 of the License, or any later version. See the LICENSE.txt file.
# ******************************************************************************

import threading

UNAVAILABLE = "estimate unavailable"
DEBOUNCE = 0.25


class estimate_unavailable(Exception):
    """The daemon has no estimate verb (a plugin-hostd older than the one that brought it)."""


def parse(text):
    """The reply after 'resp 0': the first line holds the five figures, continuation lines are not read.
    {'latency': frames, 'cpu': percent, 'ram': bytes, 'workers': n, 'unknown': n}; ValueError if it does not."""
    words = (text.strip().splitlines() or [""])[0].split()
    if len(words) < 5:
        raise ValueError(f"estimate reply '{text[:64]}' has fewer than five figures")
    latency, cpu, ram, workers, unknown = words[:5]
    return {"latency": int(float(latency)), "cpu": float(cpu), "ram": int(float(ram)),
            "workers": int(workers), "unknown": int(unknown)}


def footer(est):
    """'chain: +64 fr latency, 12.5 % CPU, 48.0 MB, 2 workers[, 3 unknown]'"""
    text = "chain: +%d fr latency, %.1f %% CPU, %.1f MB, %d workers" % (
        est["latency"], est["cpu"], est["ram"] / 1e6, est["workers"])
    if est["unknown"]:
        text += ", %d unknown" % est["unknown"]
    return text


class estimator:
    """Asks for the estimate of a list of plugin refs without blocking its caller.
    fetch(rate, quantum, refs) returns the figures or raises estimate_unavailable; any other exception is
    no figure for that question. request() waits delay seconds for the next one before asking; while a
    question is out, only the newest later one is kept. result is (key, text) for the newest answered
    question, text None for no figure; the unavailable text is given once, then None."""

    def __init__(self, fetch, delay=DEBOUNCE):
        self.fetch = fetch
        self.delay = delay
        self.lock = threading.Lock()
        self.timer = None
        self.busy = False
        self.pending = None
        self.unavailable = False
        self.said = False
        self.result = None
        self.asked = 0  # the questions that reached the daemon

    def request(self, key, rate, quantum, refs):
        with self.lock:
            if self.timer:
                self.timer.cancel()
            self.pending = (key, rate, quantum, tuple(refs))
            self.timer = threading.Timer(self.delay, self.launch)
            self.timer.daemon = True
            self.timer.start()

    def launch(self):
        with self.lock:
            if self.busy or self.pending is None:
                return  # the running worker picks the pending one up
            self.busy = True
        threading.Thread(target=self.work, daemon=True).start()

    def work(self):
        while True:
            with self.lock:
                question, self.pending = self.pending, None
                if question is None:
                    self.busy = False
                    return
            key, rate, quantum, refs = question
            text = self.answer(rate, quantum, refs)
            with self.lock:
                if self.pending is None:
                    self.result = (key, text)

    def answer(self, rate, quantum, refs):
        if self.unavailable:
            return self.unavailable_text()
        self.asked += 1
        try:
            return footer(self.fetch(rate, quantum, refs))
        except estimate_unavailable:
            self.unavailable = True
            return self.unavailable_text()
        except Exception:
            return None

    def unavailable_text(self):
        if self.said:
            return None
        self.said = True
        return UNAVAILABLE

    def idle(self):
        with self.lock:
            return not self.busy and self.pending is None
