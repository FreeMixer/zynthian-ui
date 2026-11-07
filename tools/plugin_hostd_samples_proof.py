#!/usr/bin/env python3
"""Pi proof of lane z-samples. usage: drive.py <play|reload> <samplv1|drumkv1> <rate> [file]
Runs the fork's plugin-hostd engine (zyngine/zynthian_engine_plugin_hostd.py, real zynthian_lv2 over the Pi's lilv, real
chain and processor) against a plugin-hostd of its own (port 17561) on a JACK server of its own (zsamples); a probe client
plays a note into the instance and measures the instance's outputs."""
import os, sys, types, json, time, math
os.environ.update(ZYNTHIAN_PLUGIN_HOSTD_PORT="17561", ZYNTHIAN_PLUGIN_ENGINE="plugin-hostd",
                  ZYNTHIAN_CONFIG_DIR="/root/lane-z/cfg", LV2_PATH="/usr/lib/lv2", JACK_DEFAULT_SERVER="zsamples",
                  HOME="/root/lane-z/home", XDG_STATE_HOME="/root/lane-z/state", TMPDIR="/root/lane-z/tmp")
ROOT = "/root/lane-z/zui"
sys.path[:0] = [ROOT + "/tests", ROOT]
import numpy as np, jack
from unittest import mock
for n in ("pexpect", "pyliblo3", "zynconf", "zynautoconnect", "zyncoder", "zyncoder.zyncore"):
    sys.modules.setdefault(n, mock.MagicMock())
pkg = types.ModuleType("zyngine"); pkg.__path__ = [ROOT + "/zyngine"]; sys.modules["zyngine"] = pkg
# the real zynthian_lv2, but not its import-time load_engines(), which would rewrite Zynthian's engine config
src = open(ROOT + "/zyngine/zynthian_lv2.py").read()
assert src.count("\nload_engines()\n") == 1
src = src.replace("\nload_engines()\n", "\ninit_lilv()\nengines_by_type = {}\n")
lv2 = types.ModuleType("zyngine.zynthian_lv2"); lv2.__file__ = ROOT + "/zyngine/zynthian_lv2.py"
sys.modules["zyngine.zynthian_lv2"] = lv2
exec(compile(src, lv2.__file__, "exec"), lv2.__dict__)
import test_engine_plugin_hostd as base
ph = base.ph
from zyngine.zynthian_chain import zynthian_chain
from zyngine.zynthian_processor import zynthian_processor
ph.zynthian_engine.my_data_dir = "/root/lane-z/mydata"
os.makedirs("/root/lane-z/mydata", exist_ok=True)

phase, which, rate = sys.argv[1], sys.argv[2], int(sys.argv[3])
FILE = sys.argv[4] if len(sys.argv) > 4 else "/zynthian/zynthian-data/presets/puredata/sampler/Midi_File_Sampler/sound.wav"
PLUGINS = {"samplv1": ("http://samplv1.sourceforge.net/lv2", 60), "drumkv1": ("http://drumkv1.sourceforge.net/lv2", 36)}
uri, note = PLUGINS[which]
snap_path = f"/root/lane-z/snap/{which}-{rate}.json"
os.makedirs("/root/lane-z/snap", exist_ok=True)

ph.daemon = ph.plugin_hostd_daemon(17561)
ph.daemon.start_script = "/nonexistent"
lv2.engines_by_type.update({"MIDI Synth": {}, "Audio Effect": {}})
info = {"JV/X": base.lv2_info(which, uri, "MIDI Synth")}
base.cat.add_catalogue(info, clap=[], binary="plugin-hostd")
sm = mock.MagicMock(); sm.chain_manager.engine_info = info; sm.chain_manager.chains = {}
sys.modules["zynautoconnect"].get_ports.return_value = []
chain = zynthian_chain(1, midi_chan=0); sm.chain_manager.chains[1] = chain
nextid = [1]

def add(xconfig=None):
    proc = zynthian_processor("PH/X", info["PH/X"], nextid[0]); nextid[0] += 1
    chain.insert_processor(proc)
    engine = ph.zynthian_engine_plugin_hostd("PH/X", sm)
    if xconfig:
        engine.set_extended_config(xconfig)
    proc.set_engine(engine)
    chain.rebuild_graph()
    return proc

class Probe:
    def __init__(self):
        self.c = jack.Client("zprobe", no_start_server=True)
        self.mid = self.c.midi_outports.register("midi_out")
        self.ins = [self.c.inports.register(f"in{i}") for i in range(2)]
        self.q, self.rec, self.peak, self.sumsq, self.n = [], False, 0.0, 0.0, 0
        self.c.set_process_callback(self.process)
        self.c.activate()
    def process(self, frames):
        self.mid.clear_buffer()
        if self.q:
            self.mid.write_midi_event(0, self.q.pop(0))
        if self.rec:
            x = np.concatenate([p.get_array() for p in self.ins])
            self.peak = max(self.peak, float(np.abs(x).max())); self.sumsq += float((x * x).sum()); self.n += x.size
    def window(self, seconds):
        self.peak, self.sumsq, self.n, self.rec = 0.0, 0.0, 0, True
        time.sleep(seconds); self.rec = False
        db = lambda v: round(20 * math.log10(v), 1) if v > 0 else float("-inf")
        return db(self.peak), db(math.sqrt(self.sumsq / max(self.n, 1)))
    def connect(self, jackname):
        self.c.connect(self.mid, f"{jackname}:midi_in")
        outs = self.c.get_ports(f"{jackname}:", is_output=True, is_audio=True)
        print("  outputs:", [p.name for p in outs])
        for p, q in zip(outs, self.ins):
            self.c.connect(p, q)
    def play(self, label):
        quiet = self.window(1.0)
        self.q.append(bytes([0x90, note, 100])); time.sleep(0.05)
        on = self.window(2.0)
        self.q.append(bytes([0x80, note, 0])); time.sleep(0.1)
        print(f"CELL {which} {label} rate={rate} frames=128: before note peak/rms {quiet} dBFS, note {note} vel 100 peak/rms {on} dBFS")
        return quiet, on

def path_ctrl(proc):
    cs = [z for z in proc.controllers_dict.values() if z.is_path]
    print("  path controllers:", [(z.symbol, z.path_file_types, proc.engine.path_uris[z.symbol]) for z in cs])
    return cs

probe = Probe()
try:
    if phase == "play":
        proc = add()
        eng = proc.engine
        cs = path_ctrl(proc)
        sample = next(z for z in cs if "SAMPLE_FILE" in z.symbol)
        probe.connect(eng.jackname)
        probe.play("no file")
        sample.set_value(FILE)           # the user's file choice, through the controller: the engine sends the patch_set
        print("  patch_get:", repr(eng.read_path(sample.symbol)), "== chosen:", eng.read_path(sample.symbol) == FILE)
        time.sleep(1.0)
        probe.play("file chosen")
        state = proc.get_state(); xconfig = eng.get_extended_config()
        json.dump({"state": state, "xconfig": xconfig}, open(snap_path, "w"))
        print("  snapshot saved:", snap_path, "controller value:", state["controllers"][sample.symbol]["value"])
        # a user preset keeps the file too
        saved = eng.save_preset(None, f"LaneZ {which}")
        print("  preset saved:", saved, json.load(open(saved)).get("patch"))
        eng.stop(); chain.remove_processor(proc)
        proc2 = add()
        probe.connect(proc2.engine.jackname)
        cs2 = path_ctrl(proc2)
        probe.play("fresh instance, no file")
        proc2.engine.set_preset(proc2, [saved, None, "x", "user"])
        time.sleep(1.0)
        probe.play("user preset loaded")
        proc2.engine.stop(); chain.remove_processor(proc2)
    else:
        snap = json.load(open(snap_path))
        proc = add(snap["xconfig"])
        eng = proc.engine
        cs = path_ctrl(proc)
        probe.connect(eng.jackname)
        proc.set_state(snap["state"])    # what a snapshot reload does
        sample = next(z for z in cs if "SAMPLE_FILE" in z.symbol)
        print("  after reload, controller:", sample.value, "patch_get:", repr(eng.read_path(sample.symbol)))
        time.sleep(1.0)
        probe.play("snapshot reloaded, new daemon")
        eng.stop(); chain.remove_processor(proc)
finally:
    probe.c.deactivate(); probe.c.close()
