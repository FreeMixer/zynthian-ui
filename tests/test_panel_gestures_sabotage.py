# -*- coding: utf-8 -*-
#
# Sabotage arms of the gesture vocabulary: each arm breaks one thing in a COPY of zyngine/ and tests/ and runs
# test_panel_gestures.py there; every arm must turn it red, and the unbroken copy must stay green (the control, which
# proves the copy runs the same tests). Three kinds of arm: a number the declaration owns written into the widget
# (a literal), a gesture of the table dropped, and the claim of a control not kept.
#
# Copyright (C) 2026 Pau Aliagas <linuxnow@gmail.com>
# SPDX-License-Identifier: GPL-2.0-or-later

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MODULE = os.path.join("zyngine", "zynthian_panel_gestures.py")

ARMS = {
    # a literal where the declaration is read
    "literal dragStartPx": ('G["dragStartPx"]', "10"),
    "literal fineRatio": ('G["fineRatio"]', "0.3"),
    "literal stepFraction": ('["thresholds"])["stepFraction"]', '["thresholds"])["stepFraction"] * 0 + 0.01'),
    "literal coarseSteps": ('G["coarseSteps"]', "5"),
    "literal defaultTravelPx": ('G["defaultTravelPx"]', "200"),
    "literal longPressMs": ('T["longPressMs"]["default"]', "500"),
    "literal doubleTapMs": ('T["doubleTapMs"]["default"]', "300"),
    "literal claimMs": ('T["claimMs"]["default"]', "300"),
    # a gesture of the table dropped
    "drop drag": ('            if not self.word("drag"):\n                return', "            if True:\n                return"),
    "drop fine": ('fine=bool(e.state & SHIFT))\n\n\ndef tk_release', "fine=False)\n\n\ndef tk_release"),
    "drop double-click reset": ('if o["second"] and self.word("doubleTap") == "reset":', "if False:"),
    "drop backspace reset": ('if self.word("backspace") == "reset":', "if False:"),
    "drop wheel step": ('        word = self.word(inp)\n        if not word:\n            return self.refuse("GESTURE_NOT_IN_TABLE", t, f"a {self.widget} has no {inp}")\n        if word == "second" and self.second',
                        '        word = self.word(inp)\n        return\n        if word == "second" and self.second'),
    "drop coarse": ("count = abs(n) * (", "count = abs(n) * 1 or ("),
    "drop toggle": ('if word == "toggle":', "if False:"),
    "drop long-press more": ('if self.word("longPress") == "more":', "if False:"),
    "tap changes the value": ('self.on_word("select")  # a tap selects and never changes a value',
                              'self.on_word("select"); self.change(g, self.travel.default)'),
    "pinch ignored": ('self.word("pinch") == "second"', 'self.word("pinch") == "never"'),
    "literal palmGuardMs": ('T["palmGuardMs"]["default"]', "300"),
    "pinch does not scale by the spread": ('self.pinch["q0"] * (', 'self.pinch["q0"] + 0 * ('),
    "pinch ignores the range": ('min(q["max"], max(q["min"], self.pinch', 'min(float("inf"), max(q["min"], self.pinch'),
    "encoder 3 dropped": ('"encoder3" if source == "encoder3" else', '"detent" if source == "encoder3" else'),
    "the touch layer drops the moves": ('elif s["moved"]:', 'elif False:', os.path.join("zyngine", "zynthian_touch_layer.py")),
    "the touch layer drops the lift": ('self.emit("up", s["id"], x, y, t)', 'pass', os.path.join("zyngine", "zynthian_touch_layer.py")),
    "the claim is not kept": ('self.claim = {"source": source, "until": t + T["claimMs"]["default"]}', "pass"),
    "a drag does not start from where it is": ('o["acc"] = self.travel.position(self.get())', 'o["acc"] = 0.0'),
}


def run_copy(mutation=None):
    with tempfile.TemporaryDirectory() as tmp:
        for d in ("zyngine", "tests"):
            shutil.copytree(os.path.join(ROOT, d), os.path.join(tmp, d), ignore=shutil.ignore_patterns("__pycache__"))
        if mutation:
            old, new, *module = mutation
            path = os.path.join(tmp, module[0] if module else MODULE)
            with open(path) as f:
                code = f.read()
            assert code.count(old) >= 1, f"the arm did not land: {old!r} is not in the module"
            with open(path, "w") as f:
                f.write(code.replace(old, new))
        return subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider",
                               os.path.join(tmp, "tests", "test_panel_gestures.py"), os.path.join(tmp, "tests", "test_touch_pinch.py")],
                              cwd=tmp, capture_output=True, text=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))


class SabotageArms(unittest.TestCase):

    def test_the_unbroken_copy_is_green(self):
        r = run_copy()
        self.assertEqual(r.returncode, 0, r.stdout[-2000:])

    def test_every_arm_turns_the_gestures_red(self):
        for name, mutation in ARMS.items():
            with self.subTest(name):
                r = run_copy(mutation)
                self.assertNotEqual(r.returncode, 0, f"the arm '{name}' left the gesture tests green")


if __name__ == "__main__":
    unittest.main()
