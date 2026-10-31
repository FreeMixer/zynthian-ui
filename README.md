# Zynthian User Interface

> **This is the FreeMixer fork** of [zynthian/zynthian-ui](https://github.com/zynthian/zynthian-ui), branch `vangelis`.
> It adds an engine, `PH`, that runs a chain's LV2 and CLAP plugins through
> [plugin-hostd](https://github.com/FreeMixer/plugin-hostd): the plugins of a chain that run in series in one worker process, a plugin qualified as not
> real-time safe in a worker of its own, and a lost worker restarted and re-wired (a plugin that refuses a state load while it processes loses its values, [omx-clap-core#438](https://github.com/FreeMixer/omx-clap-core/issues/438)). JACK stays as it is, and jalv stays
> for what plugin-hostd refuses. Plugin menus are ordered from the measurements of
> [plugin-qualify](https://github.com/FreeMixer/plugin-qualify), every plugin has a Factory and a User preset bank, and
> controller pages follow the plugin's own sections. Its tests run in CI on every change.
>
> Tried on one Raspberry Pi 5: chains play, with levels and behaviour measured digitally at the JACK ports. The grouped figures measure plugin-hostd directly; a run through this engine on the Pi is next. On this fork the engine is the default; in the series offered upstream it stays off until a user sets it, and
> setting `ZYNTHIAN_PLUGIN_ENGINE=jalv` goes back. What is proven and what is still open:
> [plugin-hostd's Zynthian status](https://github.com/FreeMixer/plugin-hostd/blob/main/docs/zynthian/status.md).
> The changes are offered upstream one step at a time, starting with
> [zynthian-sys#228](https://github.com/zynthian/zynthian-sys/pull/228); the companion fork is
> [FreeMixer/zynthian-sys](https://github.com/FreeMixer/zynthian-sys). Install the daemon and the packages from the
> [FreeMixer channel](https://www.openmixer.org).

The text below is Zynthian's own.

[Zynthian](http://zynthian.org) is an Open Synth Platform based on Raspberry Pi, Linux and Free Software Synthesizers.

The [ZynthianOS SD-image](https://os.zynthian.org/zynthianos-last-stable.zip) has all the software you need for building a ZynthianBox, including a good amount of sound libraries and presets.

This repository contains the software for the Engine Manager & User Interface.

![Image of Zynthian Box Design](https://wiki.zynthian.org/images/e/ed/Zynthian_v5_render_desplegado.jpg)

You can learn more about the Zynthian Project in any of our sites: 

+ [website](https://zynthian.org)
+ [wiki](https://wiki.zynthian.org)
+ [user's guide](https://help.zynthian.org)
+ [blog](https://blog.zynthian.org)
+ [forum](https://discourse.zynthian.org) => Join the conversation!!

You can buy official kits in the zynthian shop:

+ [shop](https://shop.zynthian.org)
