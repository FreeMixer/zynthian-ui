# -*- coding: utf-8 -*-
# ******************************************************************************
# ZYNTHIAN PROJECT: plugin-hostd protocol (zynthian_plugin_hostd_protocol)
#
# What plugin-hostd declares about its own protocol, read from the files its package installs:
# its reply codes and mod-host's. Nothing here is typed twice: a code is looked up by its name.
# The line the daemon prints when it is ready is read by zynthian-sys's sbin/plugin-hostd-start.sh,
# which starts every plugin-hostd on a Zynthian.
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

# Where the files are: libplugin-hostd-protocol0 (plugin-hostd 0.16.0 on) installs them in its own directory, the
# daemon's as plugin-hostd.json; plugin-hostd before 0.16.0 installed that one as protocol.json in its directory.
# The first place that holds the daemon's file is the one read.
PLACES = (("/usr/share/plugin-hostd-protocol", "plugin-hostd.json"), ("/usr/share/plugin-hostd", "protocol.json"))
DAEMON_FILES = tuple(name for _, name in PLACES)
HOST_FILE = "mod-host.json"
PROTOCOL_DIR = os.environ.get("ZYNTHIAN_PLUGIN_HOSTD_PROTOCOL_DIR")


def daemon_file(directory=None):
    """(directory, name) of the daemon's protocol file: in the given directory (or ZYNTHIAN_PLUGIN_HOSTD_PROTOCOL_DIR)
    under either name, else the first of PLACES that holds it; the first candidate when none does."""
    directory = directory or PROTOCOL_DIR
    places = [(directory, name) for name in DAEMON_FILES] if directory else list(PLACES)
    for d, name in places:
        if os.path.isfile(os.path.join(d, name)):
            return d, name
    return places[0]


def read(name, directory=None):
    """The JSON file plugin-hostd installs under that name; (contents, None), or ({}, why it is not there)."""
    path = os.path.join(directory or daemon_file()[0], name)
    try:
        with open(path) as f:
            return json.load(f), None
    except (OSError, ValueError) as e:
        return {}, f"plugin-hostd's protocol file {path} is not readable ({e})"


class protocol:
    """plugin-hostd's codes by name and by number, read once from both files.
    needs: the names the caller branches on. A file that is missing or unreadable, or a name neither file
    declares, leaves loaded False and failure saying which, logged once as an error here."""

    def __init__(self, directory=None, needs=()):
        directory, name = daemon_file(directory)
        (daemon, daemon_failure), (host, host_failure) = (read(n, directory) for n in (name, HOST_FILE))
        self.codes = {}  # name => code
        self.names = {}  # code => name, the first name a code has
        self.meanings = {}  # code => what the code means, as plugin-hostd's protocol file says it
        # mod-host's codes, then the ones plugin-hostd adds
        for e in host.get("host_errors", []):
            self.codes.setdefault(e["name"], e["value"])
            self.names.setdefault(e["value"], e["name"])
        for e in daemon.get("errors", []):
            self.codes[e["name"]] = e["code"]
            self.names[e["code"]] = e["macro"]
            if e.get("meaning"):
                self.meanings[e["code"]] = e["meaning"]
        missing = [n for n in needs if n not in self.codes]
        self.failure = daemon_failure or host_failure or (
            f"plugin-hostd's protocol files in {directory} do not declare {', '.join(missing)}"
            if missing else None)
        self.loaded = self.failure is None
        if self.failure:
            logging.error("%s: the plugin-hostd engine will not start", self.failure)

    def code(self, name):
        """The number a name stands for; KeyError for a name the protocol does not declare."""
        return self.codes[name]

    def name(self, code):
        return self.names.get(code, f"ERR_{-code}")

    def meaning(self, code):
        """What a code means, from the protocol file; its name where the file says nothing."""
        return self.meanings.get(code) or self.name(code)
