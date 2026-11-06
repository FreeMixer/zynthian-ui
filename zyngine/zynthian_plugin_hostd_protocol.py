# -*- coding: utf-8 -*-
# ******************************************************************************
# ZYNTHIAN PROJECT: plugin-hostd protocol (zynthian_plugin_hostd_protocol)
#
# What plugin-hostd declares about its own protocol, read from the files its package installs:
# its reply codes and mod-host's, the defaults of its configuration, and the grammar of a word of its lines
# (quoted words, percent-encoded paths). Nothing here is typed twice: a code is looked up by its name.
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


# ---- The words of a line, as plugin-hostd-protocol's grammar (core.abnf: word, quoted, escaped-quote) and its
# "Pins and qualification" section write them: a word is bare or in quotation marks, \" is the only escape inside
# them, and the path of a clap: uri is percent-encoded (PHD_PIN_PATH_ESCAPE) so any file name is one bare word.
# The installed grammar file is checked against this by the tests (test_the_words_follow_the_installed_grammar).

CLAP_PREFIX = "clap:"
ESCAPE = "%"
EXCLUDED = ",= \t\r\n"  # PHD_PIN_PATH_EXCLUDED: the separators of pin_set's words, and whitespace


def words(line):
    """The words of a line as the daemon reads them: split at whitespace, a word that starts with a quotation mark
    runs to the next quotation mark that no backslash escapes, \" in it is a quote, the quotes are not part of the
    value. A quoted word that never closes runs to the end of the line, as the daemon's tokenizer ends it."""
    out, i, n = [], 0, len(line)
    while i < n:
        if line[i].isspace():
            i += 1
        elif line[i] == '"':
            i += 1
            word = []
            while i < n and line[i] != '"':
                if line[i] == "\\" and i + 1 < n and line[i + 1] == '"':
                    i += 1
                word.append(line[i])
                i += 1
            i += 1
            out.append("".join(word))
        else:
            j = i
            while j < n and not line[j].isspace():
                j += 1
            out.append(line[i:j])
            i = j
    return out


def quote(value):
    """A value as one word of a line: in quotation marks, \" for a quote, a backslash as it is (the grammar
    does not unescape it)."""
    return '"' + value.replace('"', '\\"') + '"'


def escaped(c):
    return ord(c) < 0x20 or ord(c) == 0x7F or c == ESCAPE or c == '"' or c == "\\" or c in EXCLUDED


def encode_path(path):
    """A path as the word of a pin: every byte PHD_PIN_PATH_EXCLUDED names, the escape, a quote, a backslash and a
    control byte as %XX (uppercase hex), the rest as it is."""
    return "".join("".join("%%%02X" % b for b in c.encode()) if escaped(c) else c for c in path)


def decode_path(word):
    """A percent-encoded path as it is; ValueError for a % not followed by two hex digits, as the daemon refuses it."""
    out, i = bytearray(), 0
    raw = word.encode()
    while i < len(raw):
        if raw[i] == ord(ESCAPE):
            digits = raw[i + 1:i + 3]
            if len(digits) != 2 or not all(chr(d) in "0123456789abcdefABCDEF" for d in digits):
                raise ValueError(f"'%' not followed by two hex digits in {word}")
            out.append(int(digits, 16))
            i += 3
        else:
            out.append(raw[i])
            i += 1
    return out.decode(errors="surrogateescape")


def split_uri(uri):
    """(prefix, path, id) of a clap: uri clap:<path>#<id>; None for any other uri (an LV2 one)."""
    if not uri.startswith(CLAP_PREFIX) or "#" not in uri:
        return None
    path, _, plugin_id = uri[len(CLAP_PREFIX):].rpartition("#")
    return CLAP_PREFIX, path, plugin_id


def uri_word(uri):
    """A plugin uri as the daemon takes it in an add, estimate, pin_accept or descriptor_get: the path of a clap: uri
    percent-encoded, so a path with spaces is one word; any other uri as it is."""
    parts = split_uri(uri)
    if parts is None:
        return uri
    return f"{parts[0]}{encode_path(parts[2 - 1])}#{parts[2]}"


def pin_key(uri):
    """What a pin is keyed by: a clap: uri's path decoded, so the spaced, the quoted and the encoded spelling of
    one uri are one key; any other uri as it is."""
    parts = split_uri(uri)
    if parts is None:
        return uri
    try:
        return f"{parts[0]}{decode_path(parts[1])}#{parts[2]}"
    except ValueError:
        return uri


def config_default(key, directory=None):
    """The default of a key of plugin-hostd's configuration, from the installed protocol file; None when the file
    or the key is absent."""
    directory, name = daemon_file(directory)
    for entry in read(name, directory)[0].get("config", []):
        if entry.get("key") == key:
            return entry.get("default")
    return None


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
            self.names[e["code"]] = e["name"]
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
