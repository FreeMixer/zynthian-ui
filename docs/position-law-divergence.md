# Divergence: the position law of plugin-hostd controllers

This fork draws and nudges the controllers of the plugin-hostd engine by the position law of
omx-contract (`scalePosition` / `scaleValue`, FreeMixer/omx-contract#207), not by upstream's
`is_logarithmic` law.

- Where: `zyngine/zynthian_position_law.py` is a Python twin of the two functions. The plugin-hostd
  engine hands each linear or log controller its travel (`position_law`); `zynthian_controller.py`
  (nudge) and `zynthian_gui_controller.py` (drawing) branch on it. A 20 Hz to 20 kHz frequency
  control puts 1 kHz at 0.5663 of the arc; upstream's `log10(1 + 9p)` law puts it at 0.5.
- What stays upstream's: every other engine (jalv and the rest), and the MIDI CC mapping of
  every controller. A controller without `position_law` takes exactly the code path upstream has.
- Held to: `data/scale-vectors.json` from the omx-contract-data package
  (`/usr/share/omx-contract/scale-vectors.json`, or `ZYNTHIAN_OMX_SCALE_VECTORS`), both
  directions within one ulp, in `tests/test_position_law.py`. The tests fail if the file is absent.
- End: the divergence ends when upstream adopts the law for its controllers. The twin, the
  `position_law` option and the branches then go.
- Not proven here: the drawing of an omx-eq frequency at 1 kHz on a Pi's screen.
