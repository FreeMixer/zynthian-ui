# Control surfaces

The page a surface shows is an omx-panel layout page. A device with four encoders shows four
controls at a time, so the engine cuts a page of more than four controls into screens and names each
screen by what is on it ("Band 1: Freq Gain Q Type", "Band 1: On"), never by a numbered suffix.

Next step: an omx-panel `layout --pages` option for the number of controls per page, so a four-encoder
device asks for pages of four and the engine no longer cuts them.
