# -*- coding: utf-8 -*-
# ******************************************************************************
# ZYNTHIAN PROJECT: plugin-hostd grouping reasons (zynthian_plugin_hostd_grouping)
#
# The reason plugin-hostd gives for a plugin its plan does not run together with its neighbours, as
# a short text for the plugin's entry. The daemon declares the reason words (grouping.json, in the
# directory of its contract package); the texts are this file's and are looked up by word. A word
# the daemon declares and this file has no text for reads as the word itself, and the refusal of a
# whole plan is told by the name of the code the daemon refused it with.
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

CONTRACT_DIR = os.environ.get("ZYNTHIAN_PLUGIN_HOSTD_CONTRACT_DIR",
                              "/usr/share/omx-contract/contract.d/3/plugin-hostd")
PLAN_REFUSED = "plan-refused"

_declared = {}  # directory => {word: the daemon's sentence}


def declared(directory=None):
    """{reason word: the sentence the daemon declares for it} from the grouping.json its package installs; {} when
    it cannot be read."""
    directory = directory or CONTRACT_DIR
    if directory not in _declared:
        try:
            with open(os.path.join(directory, "grouping.json")) as f:
                doc = json.load(f)
            _declared[directory] = {w["word"]: w["doc"] for w in doc["PLUGIN_HOSTD_GROUPING_REASONS"]["words"].values()}
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            logging.debug("plugin-hostd grouping.json is not readable (%s)", e)
            return {}
    return _declared[directory]


def declared_words(directory=None):
    """The reason words the daemon declares; None when its declaration cannot be read."""
    return set(declared(directory)) or None


def text(reason, error_name=None):
    """The text for a reason as plan_get gives it. 'plan-refused:<code>' is told with error_name(<code>), the
    daemon's name for the code, when the caller has one."""
    word, _, code = reason.partition(":")
    if word == PLAN_REFUSED:
        try:
            name = error_name(int(code)) if error_name else code
        except ValueError:
            name = code
        return f"Not grouped: the daemon refused the plan ({name})"
    return declared().get(word) or f"Not grouped: {word}"
