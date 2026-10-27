# -*- coding: utf-8 -*-
# ******************************************************************************
# ZYNTHIAN PROJECT: plugin-hostd protocol (zynthian_plugin_hostd_protocol)
#
# What plugin-hostd declares about its own protocol, read from the files its package installs:
# its reply codes, mod-host's reply codes and the line it prints when it is ready.
# Nothing here is typed twice: a code is looked up by its name.
#
# Copyright (C) 2026 Pau Aliagas <linuxnow@gmail.com>
#
# This program is free software; you can redistribute it and/or modify it under the terms
# of the GNU General Public License as published by the Free Software Foundation; either
# version 2 of the License, or any later version. See the LICENSE.txt file.
# ******************************************************************************

import os
import json
import logging

PROTOCOL_DIR = os.environ.get("ZYNTHIAN_PLUGIN_HOSTD_PROTOCOL_DIR", "/usr/share/plugin-hostd")


def read(name, directory=None):
    """The JSON file plugin-hostd installs under that name; {} and one warning when it is not there."""
    path = os.path.join(directory or PROTOCOL_DIR, name)
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        logging.warning("plugin-hostd protocol file %s is not readable (%s)", path, e)
        return {}


class protocol:
    """plugin-hostd's codes by name and by number, and its readiness line."""

    def __init__(self, directory=None):
        daemon = read("protocol.json", directory)
        host = read("mod-host.json", directory)
        self.loaded = bool(daemon.get("errors"))
        self.codes = {}  # name => code
        self.names = {}  # code => name, the first name a code has
        # mod-host's codes, then the ones plugin-hostd adds
        for e in host.get("host_errors", []):
            self.codes.setdefault(e["name"], e["value"])
            self.names.setdefault(e["value"], e["name"])
        for e in daemon.get("errors", []):
            self.codes[e["name"]] = e["code"]
            self.names[e["code"]] = e["macro"]
        self.ready_line = daemon.get("readiness", {}).get("daemon_line")

    def code(self, name):
        """The number a name stands for; KeyError for a name the protocol does not declare."""
        return self.codes[name]

    def name(self, code):
        return self.names.get(code, f"ERR_{-code}")
