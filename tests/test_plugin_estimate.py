# -*- coding: utf-8 -*-
#
# The chain's figures before an insert: the footer the plugin menu shows for the highlighted row, asked of a
# fake plugin-hostd that answers estimate from records, and the daemon without the verb. Plain
# `python3 -m unittest`.
#
# Copyright (C) 2026 Pau Aliagas <linuxnow@gmail.com>
# SPDX-License-Identifier: GPL-2.0-or-later

import time
import unittest

from test_engine_plugin_hostd import plugin_hostd_case, ph
from zyngine import zynthian_plugin_hostd_estimate as est


def wait_for(estimator, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if estimator.idle() and estimator.result:
            return estimator.result
        time.sleep(0.01)
    raise AssertionError("no answer")


class estimate_footer(plugin_hostd_case):

    def estimator(self):
        return est.estimator(ph.estimate, delay=0.0)

    def test_the_footer_says_the_chains_figures_with_the_candidate_in_it(self):
        fake = self.serve(estimator=True)
        chain = self.chain()
        self.add(chain, "JV/Amp")
        refs = ph.menu_refs(chain, "PH/NoPin", self.info["JV/NoPin"] | {"URL": "urn:test:nopin"})
        self.assertEqual(refs, ["urn:test:amp", "urn:test:nopin"])
        e = self.estimator()
        e.request("k", 48000, 256, refs)
        self.assertEqual(wait_for(e), ("k", "chain: +64 fr latency, 3.8 % CPU, 12.5 MB, 1 workers"))
        self.assertIn("estimate 48000 256 urn:test:amp urn:test:nopin", fake.lines)

    def test_a_plugin_the_records_lack_is_counted_unknown(self):
        self.serve(estimator=True)
        e = self.estimator()
        e.request("k", 96000, 128, ["urn:test:amp", "urn:test:big"])
        self.assertEqual(wait_for(e)[1], "chain: +0 fr latency, 1.5 % CPU, RAM unknown, 1 workers, 1 unknown (member 2: no record)")

    def test_a_daemon_without_the_verb_says_unavailable_once_and_is_not_asked_again(self):
        fake = self.serve()
        e = self.estimator()
        e.request("a", 48000, 256, ["urn:test:amp"])
        self.assertEqual(wait_for(e), ("a", "estimate unavailable"))
        e.request("b", 48000, 256, ["urn:test:nopin"])
        end = time.time() + 5
        while (e.result or (None,))[0] != "b" and time.time() < end:
            time.sleep(0.01)
        self.assertEqual(e.result, ("b", None), "said once, then nothing")
        self.assertEqual([l for l in fake.lines if l.startswith("estimate")], ["estimate 48000 256 urn:test:amp"])
        self.assertFalse(ph.daemon.estimate_verb)

    def test_a_row_the_daemon_does_not_run_asks_nothing(self):
        self.assertEqual(ph.menu_refs(None, "JV/Amp", self.info["JV/Amp"]), [])

    def test_the_newest_question_is_the_one_answered(self):
        fake = self.serve(estimator=True)
        e = est.estimator(ph.estimate, delay=0.2)
        for i, ref in enumerate(["urn:test:amp", "urn:test:nopin", "urn:test:big"]):
            e.request(i, 48000, 256, [ref])
        self.assertEqual(wait_for(e)[0], 2)
        self.assertEqual(len([l for l in fake.lines if l.startswith("estimate")]), 1, "debounced to one request")

    def test_the_chain_view_has_the_figures_of_the_whole_chain_after_an_insert(self):
        self.serve(estimator=True)
        self.sm.get_jackd_samplerate.return_value = 48000
        self.sm.get_jackd_blocksize.return_value = 256
        chain = self.chain()
        self.add(chain, "JV/Amp")
        self.add(chain, "JV/NoPin")
        end = time.time() + 5
        while len(ph.daemon.chain_figures.get(chain.chain_id, ([],))[0]) < 2 and time.time() < end:
            time.sleep(0.01)
            ph.figure_chain(chain, self.sm)
        self.assertEqual(ph.daemon.chain_figures[chain.chain_id][3],
                         "chain: +64 fr latency, 3.8 % CPU, 12.5 MB, 1 workers")


if __name__ == "__main__":
    unittest.main()
