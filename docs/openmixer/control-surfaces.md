# Control surfaces

Issue: FreeMixer/zynthian-ui#26. Step 10 of the upstream patch series, after plugin-hostd is the default engine.

## Ruling

One surface contract, several backends; the omx-panel layout is the single source of the page a surface shows.

## The contract

A surface has a bank of strips and a row of encoders.

| Surface input | Zynthian action | Feedback to the surface |
|---|---|---|
| Fader (touch, move) | level of the chain on that strip | motor fader follows the level, whoever changed it |
| Select | active chain | select LED, chain number display |
| Mute, Solo | chain mute, solo | button LEDs |
| Bank left/right, Channel left/right | scroll the strips over the chains by eight or one | strip LCD |
| Encoder assign: Plugin | encoders drive the selected processor's current control page | LCD top row: control names; bottom row: values; encoder rings: positions |
| Channel left/right, in Plugin assign | previous/next control page | the control screen of the processor moves to the same page |
| Encoder turn, in Plugin assign | nudge that control by the turn's steps | ring and value |

Everything else stays on MIDI learn.

The page model is `zyngine/ctrldev/zynthian_ctrldev_base_surface.py` (`zynthian_ctrldev_surface_pages`). It knows
nothing of a protocol: it takes the processor's control groups in display order, eight controls a page. A
plugin-hostd processor groups its controls by omx-panel's layout pages (`group_symbol` `page<k>`, the same pages
the engine's control screens and the MOD GUI show), so the surface shows the layout's pages. Another engine shows
its own groups, a group wider than eight cut in pages. The surface's current page is the page of the processor's
current control screen, and paging the surface moves that screen, so the GUI and the surface never disagree.

## Backend 1: Mackie Control over MIDI

Behringer X-Touch, X-Touch One and every MCU-compatible surface. Zynthian already has a Mackie Control driver,
`zyngine/ctrldev/zynthian_ctrldev_mackiecontrol.py`, loaded by `zynthian_ctrldev_manager` like every control-device
driver, with per-desk yaml maps (`zyngine/ctrldev/mackiecontrol/`). It already does the faders with motor
feedback, select, mute, solo, bank and the LCD. Its Plugin encoder-assign button was mapped but did nothing.

Decision: the MCU backend is that driver, in the UI process; no second MCU driver and no daemon beside the UI. The
control-device manager loads Python drivers in-process and routes the device's MIDI to them, which is all a
surface needs; a daemon would have to reach the processors' controllers through a second API. The Plugin assign
mode reads `zynthian_ctrldev_surface_pages`:

- V-Pot turn: CC 16-23, bit 6 the direction (set is counterclockwise), bits 0-5 the steps.
- V-Pot ring: CC 48-55, mode 2 (wrap, lit from the left) in bits 4-5, LED 1-11 in bits 0-3, 0 for an empty slot.
  Out of Plugin assign the rings are dark.
- LCD: SysEx `F0 00 00 66 14 12 <offset> <text> F7`, seven characters a strip, top row from offset 0, bottom
  row from offset 0x38.

The driver is chosen for the device in the MIDI options (it is not autoloaded: its device id is the generic `*`).

## Backend 2, later: DigiNet

Digidesign Control|24 and ProControl through libdiginet over raw Ethernet on the Pi's port. It waits for a real desk
to prove the library (libdiginet#5: the surface daemon does not exist yet). It reads the same
`zynthian_ctrldev_surface_pages`; only the encoding of the inputs and the feedback is its own.

## Proof

`tests/test_ctrldev_mackiecontrol_plugin.py` runs the real driver, page model, plugin-hostd engine (against the
fake daemon and the omx-panel stand-in), chain, processor and controllers, and decodes the driver's MIDI output
into the surface's LCD, rings and motor faders:

- the LCD names the eight controls of the layout's first page and shows their values;
- a V-Pot turn moves the parameter in plugin-hostd and its ring;
- the channel buttons page through the layout's pages and the processor's control screen follows;
- a fader move lands on the chain level, and the motor follows a level changed on the UI.

Still to prove on a desk: captures of an X-Touch for these docs, and no xruns during surface traffic.

## Screens of four

A device with four encoders shows four controls at a time, so the engine cuts a layout page of more than four
controls into screens and names each screen by what is on it ("Band 1: Freq Gain Q Type", "Band 1: On"), never by a
numbered suffix.

Next step: an omx-panel `layout --pages` option for the number of controls per page, so a four-encoder
device asks for pages of four and the engine no longer cuts them.
