# -*- coding: utf-8 -*-
#
# A screen is driven through the GUI's table (self.screens['x'] in zynthian_gui.py, self.zyngui.screens["x"]
# elsewhere): every method called through it must be one the screen's class defines or inherits. Source level,
# so it holds where tkinter and the native libraries are absent. CUIA CHAIN_OPTIONS called setup(), a method the
# chain options screen no longer has (#22).
#
# Copyright (C) 2026 Pau Aliagas <linuxnow@gmail.com>
# SPDX-License-Identifier: GPL-2.0-or-later

import os
import ast
import glob
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def zyngui_classes():
    """class name -> (method names, base names) for every class under zyngui/"""
    classes = {}
    for path in glob.glob(os.path.join(ROOT, "zyngui", "*.py")):
        for node in ast.walk(ast.parse(open(path).read())):
            if isinstance(node, ast.ClassDef):
                methods = {n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
                methods |= {t.id for n in node.body if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Name)}
                bases = [b.id if isinstance(b, ast.Name) else b.attr if isinstance(b, ast.Attribute) else None for b in node.bases]
                classes[node.name] = (methods, bases)
    return classes


# tkinter's Frame, the base of every screen: its methods from tkinter where it is installed, else the widget
# methods a screen can be asked for (CI runs without tkinter)
TK_PREFIXES = ("winfo_", "grid_", "pack_", "place_", "event_", "wm_", "tk_", "after", "bind", "focus", "grab_", "image_",
               "option_", "selection_", "clipboard_", "update", "wait_")
TK_METHODS = {"pack", "grid", "place", "configure", "config", "cget", "keys", "destroy", "lift", "lower", "tkraise",
              "forget", "quit", "mainloop", "unbind", "nametowidget", "register", "children"}
try:
    import tkinter
    TK_FRAME = set(dir(tkinter.Frame))
except ImportError:
    TK_FRAME = None


def frame_has(method):
    if TK_FRAME is not None:
        return method in TK_FRAME
    return method in TK_METHODS or method.startswith(TK_PREFIXES)


def has_method(classes, name, method):
    """True or False when the class and its bases are all under zyngui or tkinter's Frame, None when another
    base is outside it"""
    if name == "Frame":
        return frame_has(method)
    if name not in classes:
        return None
    methods, bases = classes[name]
    if method in methods:
        return True
    answers = [has_method(classes, b, method) for b in bases if b != "object"]
    if True in answers:
        return True
    return None if None in answers else False


def screen_key(node):
    """'x' for self.screens['x'] or self.zyngui.screens['x'], else None"""
    if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute) and node.value.attr == "screens"
            and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str)):
        return node.slice.value
    return None


class screen_calls(unittest.TestCase):

    def setUp(self):
        self.classes = zyngui_classes()
        self.screens = {}
        tree = ast.parse(open(os.path.join(ROOT, "zyngui", "zynthian_gui.py")).read())
        for node in ast.walk(tree):
            if (isinstance(node, ast.Assign) and len(node.targets) == 1 and screen_key(node.targets[0])
                    and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name)):
                self.screens[screen_key(node.targets[0])] = node.value.func.id

    def calls(self):
        for path in sorted(glob.glob(os.path.join(ROOT, "zyngui", "*.py"))):
            for node in ast.walk(ast.parse(open(path).read())):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    key = screen_key(node.func.value)
                    if key in self.screens:
                        yield os.path.basename(path), node.lineno, key, node.func.attr

    def test_the_table_is_read(self):
        self.assertEqual(self.screens.get("chain_options"), "zynthian_gui_chain_options")
        self.assertTrue(any(key == "chain_options" for _, _, key, _ in self.calls()))

    def test_every_method_called_through_the_screen_table_exists(self):
        missing = [f"{f}:{line}: screens['{key}'].{method}() on {self.screens[key]}"
                   for f, line, key, method in self.calls()
                   if has_method(self.classes, self.screens[key], method) is False]
        self.assertEqual(missing, [])

    def test_the_chain_options_screen_is_given_a_chain_not_an_id(self):
        """set_chain keeps what it is given as the chain the screen shows: an id there is a chain of no attributes"""
        wrong = []
        for path in sorted(glob.glob(os.path.join(ROOT, "zyngui", "*.py"))):
            for node in ast.walk(ast.parse(open(path).read())):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "set_chain"
                        and screen_key(node.func.value) == "chain_options" and node.args
                        and isinstance(node.args[0], (ast.Constant, ast.Name)) and not (
                            isinstance(node.args[0], ast.Constant) and node.args[0].value is None)):
                    if isinstance(node.args[0], ast.Constant) or node.args[0].id.endswith("_id"):
                        wrong.append(f"{os.path.basename(path)}:{node.lineno}")
        self.assertEqual(wrong, [])


if __name__ == "__main__":
    unittest.main()
