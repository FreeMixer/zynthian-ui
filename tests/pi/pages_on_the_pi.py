# -*- coding: utf-8 -*-
#
# The Zynthian screens of a REAL plugin on the Pi, by hand (not collected by CI): the engine of this tree adds the plugin
# to a plugin-hostd already listening on PHD_PORT (a jackd of its own name and a daemon the caller started), and the
# four controls of every screen are read back (name, value from the daemon) and compared with what the omx-panel
# named by OMX_PANEL prints for the descriptor the daemon scanned: `describe --pages --per-page 4`.
#
#   OMX_PANEL=/root/z-pages/bin-omx-panel PHD_PORT=17556 PHD_URI=http://lsp-plug.in/plugins/lv2/para_equalizer_x8_mono \
#   JACK_DEFAULT_SERVER=zpages python3 -m pytest -s tests/pi/pages_on_the_pi.py
#
# Copyright (C) 2026 Pau Aliagas <linuxnow@gmail.com>
# SPDX-License-Identifier: GPL-2.0-or-later

import json
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
from test_engine_plugin_hostd import plugin_hostd_case, ph  # noqa: E402
from zyngine import zynthian_plugin_hostd_catalogue as cat  # noqa: E402

PORT = int(os.environ.get("PHD_PORT", "17556"))
URI = os.environ.get("PHD_URI", "http://lsp-plug.in/plugins/lv2/para_equalizer_x8_mono")
PANEL = os.environ.get("OMX_PANEL", "/root/z-pages/bin-omx-panel")


class OnThePi(plugin_hostd_case):

    def setUp(self):
        super().setUp()
        os.unlink(os.path.join(self.bin, "omx-panel"))  # the stub of the tests; the Pi's own omx-panel instead
        os.symlink(PANEL, os.path.join(self.bin, "omx-panel"))
        os.environ["PATH"] = self.bin + ":/usr/bin:/bin"
        self.port = PORT
        ph.daemon = ph.plugin_hostd_daemon(PORT)
        ph.daemon.start_script = os.path.join(self.tmp, "not-installed")
        self.code = "PH/Real"
        if URI.startswith("clap:"):  # clap:<file>#<id>, as the catalogue's scan names it
            path, _, ident = URI[5:].partition("#")
            scan = (f'file {path}\n  plugin {ident} "{ident}" vendor Test version 1\n    features instrument synthesizer stereo\n'
                    '    out main 1 channels unknown "out" out_1\n')
            cat.add_catalogue(self.info, clap=cat.parse_clap_scan(scan), binary="/usr/bin/plugin-hostd")
            self.code = "PH/" + URI
        else:
            self.info["JV/Real"] = {**self.info["JV/Amp"], "ID": "Real", "NAME": "Real", "TITLE": "Real", "URL": URI}
            cat.add_catalogue(self.info, clap=[], binary="/usr/bin/plugin-hostd")

    def test_the_screens_are_omx_panels_pages_of_four(self):
        proc = self.add(self.chain(), self.code)
        engine = proc.engine
        path = engine.descriptor_path()
        with open(path) as f:
            remote = engine.remote_ids({str(c["id"]): c for c in json.load(f)["controls"]})  # what the host reports
        extra = []
        if remote:
            rfile = os.path.join(self.tmp, "remote.json")
            with open(rfile, "w") as f:
                json.dump(remote, f)
            extra = ["--remote", rfile]
        print(f"the host reports {len(remote)} remote-controls pages", flush=True)
        cut = json.loads(subprocess.run([PANEL, "describe", "--descriptor", path, "--pages", "--per-page", "4", *extra],
                                        capture_output=True, text=True, check=True).stdout)
        want = [(p.get("pageName") or p["sectionName"], [None if i is None else str(i) for i in p["ids"]]) for p in cut]
        got = [(name, keys) for name, keys in engine._ctrl_screens]
        print(f"\n{URI}: {len(got)} screens; omx-panel prints {len(want)} pages", flush=True)
        for name, keys in got[:int(os.environ.get('SHOW', '12'))]:
            cells = []
            for k in keys:
                z = proc.controllers_dict.get(k) if k else None
                cells.append("-" if z is None else f"{z.short_name}={ph.daemon.call(f'param_get {engine.instance} {k}')[1].split()[0]}")
            print(f"  {name[:32]:32} | " + " | ".join(f"{c:22}" for c in cells), flush=True)
        # blank slots in omx-panel's cut are blank here; the others are the controls, in its order
        self.assertEqual([n for n, _ in got], [n for n, _ in want])
        for (name, keys), (_, ids) in zip(got, want):
            self.assertEqual(len(keys), 4, name)
            self.assertEqual([k for k in keys if k], [i for i in ids if i is not None and i in proc.controllers_dict], name)
            for k, i in zip(keys, ids):
                self.assertTrue(k is None or k == i, f"{name}: slot {i} is shown at its place")


if __name__ == "__main__":
    unittest.main()
