import json
import os
import time

import adafruit_drv2605
import bitbangio
import board
import busio
import microcontroller
import socketpool
import wifi

from adafruit_httpserver import DELETE, GET, POST, Request, Response, Server


DRV2605_ADDRESS = 0x5A
LUB_DURATION = 0.070
DUB_DURATION = 0.090
MIN_BEAT_REST = 0.010
PREFERENCE_WRITE_DELAY = 2.0
PREFERENCE_PREFIX = b"SH2|"
PREFERENCE_BYTES = 256

DEFAULTS = {
    "bpm": 72,
    "motor_mode": "tandem",
    "lub_intensity": 65,
    "dub_intensity": 80,
    "lub_dub_delay_ms": 100,
}

MOTOR_MODES = ("tandem", "simultaneous", "motor1", "motor2")


def clamp(value, minimum, maximum):
    return max(minimum, min(maximum, value))


def maximum_delay_ms(bpm):
    available = 60.0 / bpm - LUB_DURATION - DUB_DURATION - MIN_BEAT_REST
    return max(50, min(300, int(available * 1000)))


def json_response(request, value):
    return Response(
        request,
        json.dumps(value),
        content_type="application/json",
    )


def scan_bus(i2c):
    while not i2c.try_lock():
        pass
    try:
        return i2c.scan()
    finally:
        i2c.unlock()


class PreferenceStore:
    def __init__(self):
        self.pending = False
        self.write_at = 0

    def load(self):
        preferences = DEFAULTS.copy()
        try:
            raw = bytes(microcontroller.nvm[0:PREFERENCE_BYTES])
            if not raw.startswith(PREFERENCE_PREFIX):
                return preferences
            end = raw.find(b"\x00", len(PREFERENCE_PREFIX))
            if end < 0:
                end = len(raw)
            saved = json.loads(raw[len(PREFERENCE_PREFIX):end].decode("utf-8"))
            for key in DEFAULTS:
                if key in saved:
                    preferences[key] = saved[key]
        except Exception as error:
            print("[PREFS] Load warning:", error)
        return preferences

    def schedule(self, now):
        self.pending = True
        self.write_at = now + PREFERENCE_WRITE_DELAY

    def update(self, now, values):
        if not self.pending or now < self.write_at:
            return
        try:
            data = PREFERENCE_PREFIX + json.dumps(values).encode("utf-8") + b"\x00"
            if len(data) > PREFERENCE_BYTES:
                raise ValueError("preferences exceed NVM allocation")
            microcontroller.nvm[0:len(data)] = data
            self.pending = False
            print("[PREFS] Saved")
        except Exception as error:
            self.pending = False
            print("[PREFS] Save warning:", error)


class HapticMotor:
    def __init__(self, number, sda, scl, pin_description):
        self.number = number
        self.available = False
        self.bus_kind = "unavailable"
        self.i2c = None
        self.driver = None
        print("[HAPTIC] Initializing Motor {} bus {}...".format(number, pin_description))
        try:
            self.i2c = busio.I2C(scl=scl, sda=sda, frequency=100000)
            self.bus_kind = "busio.I2C"
            print("[HAPTIC] Motor {} using hardware I2C".format(number))
        except Exception as hardware_error:
            print("[HAPTIC] Motor {} hardware I2C unavailable: {}".format(number, hardware_error))
            try:
                self.i2c = bitbangio.I2C(scl=scl, sda=sda, frequency=100000)
                self.bus_kind = "bitbangio.I2C"
                print("[HAPTIC] Motor {} using software I2C".format(number))
            except Exception as software_error:
                print("[HAPTIC] WARNING: Motor {} bus unavailable: {}".format(number, software_error))
                return

        try:
            if DRV2605_ADDRESS not in scan_bus(self.i2c):
                print("[HAPTIC] WARNING: Motor {} DRV2605L not detected at 0x5A".format(number))
                return
            self.driver = adafruit_drv2605.DRV2605(self.i2c)
            self.driver.use_ERM()
            self.driver.realtime_value = 0
            self.driver.mode = adafruit_drv2605.MODE_REALTIME
            self.available = True
            print("[HAPTIC] Motor {} DRV2605L detected at 0x5A".format(number))
        except Exception as error:
            print("[HAPTIC] WARNING: Motor {} initialization failed: {}".format(number, error))

    def set_strength(self, percent):
        if not self.available:
            return
        try:
            self.driver.realtime_value = int(127 * clamp(int(percent), 0, 100) / 100)
        except Exception as error:
            self.available = False
            print("[HAPTIC] WARNING: Motor {} write failed: {}".format(self.number, error))

    def stop(self):
        self.set_strength(0)


class HapticController:
    def __init__(self):
        self.motor1 = HapticMotor(1, board.A0, board.A1, "A0/A1")
        self.motor2 = HapticMotor(2, board.A2, board.A3, "A2/A3")
        count = int(self.motor1.available) + int(self.motor2.available)
        print("[HAPTIC] {}/2 haptic drivers available".format(count))
        if count == 1:
            print("[HAPTIC] Continuing in single-motor mode")

    def set_motor_1(self, strength):
        self.motor1.set_strength(strength)

    def set_motor_2(self, strength):
        self.motor2.set_strength(strength)

    def set_both(self, strength1, strength2=None):
        if strength2 is None:
            strength2 = strength1
        self.set_motor_1(strength1)
        self.set_motor_2(strength2)

    def stop_motor_1(self):
        self.motor1.stop()

    def stop_motor_2(self):
        self.motor2.stop()

    def stop_all(self):
        self.stop_motor_1()
        self.stop_motor_2()

    def pulse_phase(self, motor_mode, phase, strength):
        self.stop_all()
        if motor_mode == "simultaneous":
            self.set_both(strength)
        elif motor_mode == "motor1":
            if self.motor1.available:
                self.set_motor_1(strength)
            else:
                self.set_motor_2(strength)
        elif motor_mode == "motor2":
            if self.motor2.available:
                self.set_motor_2(strength)
            else:
                self.set_motor_1(strength)
        elif phase == "lub":
            if self.motor1.available:
                self.set_motor_1(strength)
            else:
                self.set_motor_2(strength)
        elif self.motor2.available:
            self.set_motor_2(strength)
        else:
            self.set_motor_1(strength)

    def status(self):
        return {
            "motor1_available": self.motor1.available,
            "motor2_available": self.motor2.available,
            "motor1_bus": self.motor1.bus_kind,
            "motor2_bus": self.motor2.bus_kind,
        }


class HeartbeatScheduler:
    WAITING = 0
    LUB_ACTIVE = 1
    INTER_PULSE = 2
    DUB_ACTIVE = 3

    def __init__(self, haptics, state):
        self.haptics = haptics
        self.state_values = state
        self.phase = self.WAITING
        self.deadline = time.monotonic()
        self.next_beat = self.deadline
        self.one_shot = False

    def abort(self, now=None):
        self.haptics.stop_all()
        self.phase = self.WAITING
        self.one_shot = False
        self.next_beat = time.monotonic() if now is None else now

    def trigger_once(self, now):
        self.abort(now)
        self.one_shot = True

    def effective_delay(self):
        interval = 60.0 / self.state_values["bpm"]
        maximum = int((interval - LUB_DURATION - DUB_DURATION - MIN_BEAT_REST) * 1000)
        return clamp(self.state_values["lub_dub_delay_ms"], 50, max(50, maximum)) / 1000.0

    def update(self, now):
        if not self.state_values["enabled"] and not self.one_shot:
            if self.phase != self.WAITING:
                self.abort(now)
            return

        interval = 60.0 / self.state_values["bpm"]

        if self.phase == self.WAITING and now >= self.next_beat:
            self.haptics.pulse_phase(
                self.state_values["motor_mode"],
                "lub",
                self.state_values["lub_intensity"],
            )
            self.deadline = now + LUB_DURATION
            self.phase = self.LUB_ACTIVE
        elif self.phase == self.LUB_ACTIVE and now >= self.deadline:
            self.haptics.stop_all()
            self.deadline = now + self.effective_delay()
            self.phase = self.INTER_PULSE
        elif self.phase == self.INTER_PULSE and now >= self.deadline:
            self.haptics.pulse_phase(
                self.state_values["motor_mode"],
                "dub",
                self.state_values["dub_intensity"],
            )
            self.deadline = now + DUB_DURATION
            self.phase = self.DUB_ACTIVE
        elif self.phase == self.DUB_ACTIVE and now >= self.deadline:
            self.haptics.stop_all()
            self.phase = self.WAITING
            if self.one_shot:
                self.one_shot = False
                self.next_beat = now + interval
            else:
                self.next_beat += interval
                if self.next_beat <= now:
                    self.next_beat = now + interval


class SimpleTestScheduler:
    def __init__(self, haptics):
        self.haptics = haptics
        self.active = False
        self.deadline = 0

    def start(self, target, strength, now):
        self.haptics.stop_all()
        if target == "motor1":
            self.haptics.set_motor_1(strength)
        elif target == "motor2":
            self.haptics.set_motor_2(strength)
        else:
            self.haptics.set_both(strength)
        self.deadline = now + 0.20
        self.active = True

    def update(self, now):
        if self.active and now >= self.deadline:
            self.haptics.stop_all()
            self.active = False


class HeartbeatTimer:
    def __init__(self):
        self.deadline = None
        self.completed = False

    def start(self, seconds, now):
        self.deadline = now + clamp(int(seconds), 1, 86400)
        self.completed = False

    def cancel(self):
        self.deadline = None
        self.completed = False

    def update(self, now, state, haptics):
        if self.deadline is not None and now >= self.deadline:
            self.deadline = None
            self.completed = True
            state["enabled"] = False
            haptics.stop_all()

    def status(self, now):
        remaining = 0
        if self.deadline is not None:
            remaining = max(0, int(self.deadline - now + 0.999))
        return {
            "active": self.deadline is not None,
            "remaining_seconds": remaining,
            "action": "stop_heartbeat",
            "complete": self.completed,
        }


print("[BOOT] Surrogate Heart v0.2")

preferences = PreferenceStore()
saved = preferences.load()
state = {
    "enabled": True,
    "effect_mode": "heart",
    "bpm": clamp(int(saved["bpm"]), 30, 240),
    "motor_mode": saved["motor_mode"] if saved["motor_mode"] in MOTOR_MODES else "tandem",
    "lub_intensity": clamp(int(saved["lub_intensity"]), 0, 100),
    "dub_intensity": clamp(int(saved["dub_intensity"]), 0, 100),
    "lub_dub_delay_ms": clamp(int(saved["lub_dub_delay_ms"]), 50, 300),
    "snore_intensity": 70,
    "snore_duration": 1.4,
    "snore_taper": 0.7,
    "snore_gap": 3.6,
}
state["lub_dub_delay_ms"] = min(
    state["lub_dub_delay_ms"],
    maximum_delay_ms(state["bpm"]),
)

haptics = HapticController()
heartbeat = HeartbeatScheduler(haptics, state)
simple_test = SimpleTestScheduler(haptics)
heartbeat_timer = HeartbeatTimer()

print("[HAPTIC] Motor mode:", state["motor_mode"])
print("[WIFI] Connecting...")
wifi.radio.connect(
    os.getenv("CIRCUITPY_WIFI_SSID"),
    os.getenv("CIRCUITPY_WIFI_PASSWORD"),
)
ip = wifi.radio.ipv4_address
print("[WIFI] Connected")
print("[WIFI]", ip)

pool = socketpool.SocketPool(wifi.radio)
server = Server(pool, "/")


HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Surrogate Heart</title>
<style>
:root{color-scheme:dark;--red:#ff4d67;--panel:#202126;--muted:#a9abb4;--green:#52d273}
*{box-sizing:border-box}body{margin:0;background:#101115;color:#fff;font-family:system-ui,sans-serif}
main{max-width:480px;margin:auto;padding:18px}.card{background:var(--panel);border-radius:20px;padding:20px;margin:0 0 16px}
h1{text-align:center;margin:4px 0 2px;font-size:30px}.bpm{text-align:center;font-size:58px;font-weight:700}.bpm small{font-size:18px}
h2{font-size:14px;letter-spacing:.12em;color:var(--muted);margin:0 0 16px}.row{display:flex;gap:10px;align-items:center;justify-content:space-between}
button,select,input[type=number]{min-height:48px;border:0;border-radius:12px;padding:10px 14px;font-size:17px}
button{background:#383a43;color:#fff;font-weight:650}button.primary{background:var(--red)}button:disabled{opacity:.35}
select,input[type=number]{width:100%;background:#30323a;color:#fff}input[type=range]{width:100%;height:38px;accent-color:var(--red)}
.value{text-align:center;font-size:22px;font-weight:700}.label{margin-top:14px}.hint,.status{color:var(--muted);font-size:14px;line-height:1.45}
.tests{display:grid;grid-template-columns:1fr 1fr;gap:10px}.tests button{margin:0}.dot{color:var(--green)}.bad{color:#777}
.hidden{display:none}.timer-buttons{display:grid;grid-template-columns:1fr 1fr;gap:8px}.timer{font-size:30px;text-align:center;font-variant-numeric:tabular-nums}
</style>
</head>
<body><main>
<section class="card">
<h1>&#10084;&#65039; <span id="title">HEART</span></h1>
<div class="bpm"><span id="bpmText">72</span> <small>BPM</small></div>
<div class="row"><strong>Heartbeat</strong><button id="toggle" class="primary">ON</button></div>
<div class="label">BPM</div><div class="row"><button id="minus">&minus;</button><span id="bpmNumber" class="value">72</span><button id="plus">+</button></div>
<input id="bpm" type="range" min="30" max="240" value="72">
<div class="label">Heart Mode: <span id="heartPresetValue">Off / Manual</span></div><input id="heartPreset" type="range" min="0" max="5" step="1" value="0">
<div class="row"><button id="heartTab">Heart</button><button id="snoreTab">Snoring</button></div>
</section>

<div id="heartPanel">
<section class="card"><h2>MOTOR MODE</h2>
<select id="motorMode"><option value="tandem">Tandem / Lub-Dub</option><option value="simultaneous">Simultaneous</option><option value="motor1">Motor 1 Only</option><option value="motor2">Motor 2 Only</option></select>
<p id="modeHelp" class="hint"></p></section>

<section class="card"><h2>CONTRACTIONS</h2>
<div class="label">LUB Strength</div><div id="lubValue" class="value">65%</div><input id="lub" type="range" min="0" max="100" value="65">
<div class="label">DUB Strength</div><div id="dubValue" class="value">80%</div><input id="dub" type="range" min="0" max="100" value="80">
<div class="label">LUB &rarr; DUB Delay</div><div id="delayValue" class="value">100 ms</div><input id="delay" type="range" min="50" max="300" value="100">
</section>
</div>

<section id="snorePanel" class="card hidden"><h2>SNORING</h2>
<div class="label">Intensity</div><div id="snoreIntensityValue" class="value">70%</div><input id="snoreIntensity" type="range" min="0" max="100" value="70">
<div class="label">Snore Duration</div><div id="snoreDurationValue" class="value">1.4 s</div><input id="snoreDuration" type="range" min="0.2" max="5" step="0.05" value="1.4">
<div class="label">Taper</div><div id="snoreTaperValue" class="value">0.7 s</div><input id="snoreTaper" type="range" min="0" max="3" step="0.05" value="0.7">
<div class="label">Time Between Snores</div><div id="snoreGapValue" class="value">3.6 s</div><input id="snoreGap" type="range" min="0" max="10" step="0.05" value="3.6">
<div class="label">Breathing Speed: <span id="breathingValue">Off / Manual</span></div><input id="breathingPreset" type="range" min="0" max="5" step="1" value="0">
</section>

<section class="card"><h2>TEST</h2><div class="tests">
<button id="test1">Test Motor 1</button><button id="test2">Test Motor 2</button><button id="testBoth">Test Both</button><button id="testHeart">Test Heartbeat</button>
</div></section>

<section class="card"><h2>HEARTBEAT TIMER</h2>
<div class="timer-buttons"><button data-minutes="15">15 minutes</button><button data-minutes="30">30 minutes</button><button data-minutes="60">1 hour</button><button data-minutes="120">2 hours</button></div>
<div class="row label"><input id="customTime" type="number" min="1" value="30"><select id="customUnit"><option value="60">minutes</option><option value="3600">hours</option><option value="1">seconds (test)</option></select></div>
<button id="startTimer" class="primary" style="width:100%;margin-top:10px">Start Timer</button>
<div id="timerText" class="timer">No timer active</div><button id="cancelTimer" class="hidden" style="width:100%">Cancel Timer</button>
</section>

<section class="card"><h2>HARDWARE</h2><div class="status"><div>Motor 1 <span id="motor1Status"></span></div><div>Motor 2 <span id="motor2Status"></span></div><div id="busStatus"></div></div></section>
<section class="card"><h2>BATTERY</h2><div class="status">Battery monitoring is not installed.</div></section>
</main>
<script>
const $=id=>document.getElementById(id);let current=null;let sendTimer=null;
const modeHelp={tandem:"Motor 1 = LUB / Motor 2 = DUB",simultaneous:"Both motors produce LUB and DUB",motor1:"Heartbeat uses Motor 1 only",motor2:"Heartbeat uses Motor 2 only"};
const presetNames=["Off / Manual","Sleeping","Walking","Running","Jackrabbit","Hummingbird"];const heartRates=[72,50,90,140,200,240];const breathingPresets=[null,[1.4,.7,3.6],[1.1,.5,1.4],[.7,.3,.6],[.45,.2,.35],[.25,.1,.25]];
function query(values){return Object.keys(values).map(k=>encodeURIComponent(k)+"="+encodeURIComponent(values[k])).join("&")}
async function post(path,values={}){await fetch(path+(Object.keys(values).length?"?"+query(values):""),{method:"POST"});await refresh()}
function scheduleSettings(){clearTimeout(sendTimer);sendTimer=setTimeout(()=>post("/api/v1/heartbeat",{bpm:$("bpm").value,motor_mode:$("motorMode").value,lub_intensity:$("lub").value,dub_intensity:$("dub").value,lub_dub_delay_ms:$("delay").value,effect_mode:current.effect_mode,snore_intensity:$("snoreIntensity").value,snore_duration:$("snoreDuration").value,snore_taper:$("snoreTaper").value,snore_gap:$("snoreGap").value}),180)}
function showValues(){$("bpmText").textContent=$("bpm").value;$("bpmNumber").textContent=$("bpm").value;$("lubValue").textContent=$("lub").value+"%";$("dubValue").textContent=$("dub").value+"%";$("delayValue").textContent=$("delay").value+" ms";$("snoreIntensityValue").textContent=$("snoreIntensity").value+"%";$("snoreDurationValue").textContent=$("snoreDuration").value+" s";$("snoreTaperValue").textContent=$("snoreTaper").value+" s";$("snoreGapValue").textContent=$("snoreGap").value+" s";$("modeHelp").textContent=modeHelp[$("motorMode").value]}
function showMode(mode){const heart=mode==="heart";$("heartPanel").classList.toggle("hidden",!heart);$("snorePanel").classList.toggle("hidden",heart);$("title").textContent=heart?"HEART":"SNORING"}
function formatTime(seconds){const m=Math.floor(seconds/60),s=seconds%60;return m+":"+String(s).padStart(2,"0")}
function render(data){current=data;$("bpm").value=data.bpm;$("motorMode").value=data.motor_mode;$("lub").value=data.lub_intensity;$("dub").value=data.dub_intensity;$("delay").value=data.lub_dub_delay_ms;$("snoreIntensity").value=data.snore_intensity;$("snoreDuration").value=data.snore_duration;$("snoreTaper").value=data.snore_taper;$("snoreGap").value=data.snore_gap;$("toggle").textContent=data.enabled?"ON":"OFF";$("toggle").classList.toggle("primary",data.enabled);showMode(data.effect_mode);showValues();const h=data.hardware;$("motor1Status").innerHTML=h.motor1_available?'<span class="dot">● Ready</span>':'<span class="bad">○ Not detected</span>';$("motor2Status").innerHTML=h.motor2_available?'<span class="dot">● Ready</span>':'<span class="bad">○ Not detected</span>';$("test1").disabled=!h.motor1_available;$("test2").disabled=!h.motor2_available;$("testBoth").disabled=!h.motor1_available&&!h.motor2_available;$("busStatus").textContent="Bus 1: "+h.motor1_bus+" · Bus 2: "+h.motor2_bus;const t=data.timer;$("timerText").textContent=t.active?"Heartbeat stops in "+formatTime(t.remaining_seconds):(t.complete?"Timer complete":"No timer active");$("cancelTimer").classList.toggle("hidden",!t.active)}
async function refresh(){try{const r=await fetch("/api/v1/state");render(await r.json())}catch(e){}}
[$("lub"),$("dub"),$("delay"),$("snoreIntensity")].forEach(el=>{el.addEventListener("input",showValues);el.addEventListener("change",scheduleSettings)});$("motorMode").addEventListener("change",scheduleSettings);
$("bpm").addEventListener("input",showValues);$("bpm").addEventListener("change",()=>{$("heartPreset").value=0;$("heartPresetValue").textContent=presetNames[0];scheduleSettings()});
$("heartPreset").addEventListener("input",()=>$("heartPresetValue").textContent=presetNames[Number($("heartPreset").value)]);$("heartPreset").addEventListener("change",()=>{const level=Number($("heartPreset").value);if(level>0){$("bpm").value=heartRates[level];showValues()}scheduleSettings()});
[$("snoreDuration"),$("snoreTaper"),$("snoreGap")].forEach(el=>{el.addEventListener("input",showValues);el.addEventListener("change",()=>{$("breathingPreset").value=0;$("breathingValue").textContent=presetNames[0];scheduleSettings()})});
$("breathingPreset").addEventListener("input",()=>$("breathingValue").textContent=presetNames[Number($("breathingPreset").value)]);$("breathingPreset").addEventListener("change",()=>{const preset=breathingPresets[Number($("breathingPreset").value)];if(preset){$("snoreDuration").value=preset[0];$("snoreTaper").value=preset[1];$("snoreGap").value=preset[2];showValues()}scheduleSettings()});
function manualHeart(){$("heartPreset").value=0;$("heartPresetValue").textContent=presetNames[0]}$("minus").onclick=()=>{manualHeart();$("bpm").value=Math.max(30,Number($("bpm").value)-1);showValues();scheduleSettings()};$("plus").onclick=()=>{manualHeart();$("bpm").value=Math.min(240,Number($("bpm").value)+1);showValues();scheduleSettings()};
$("heartTab").onclick=()=>post("/api/v1/heartbeat",{effect_mode:"heart"});$("snoreTab").onclick=()=>post("/api/v1/heartbeat",{effect_mode:"snoring"});$("toggle").onclick=()=>post("/toggle");
$("test1").onclick=()=>post("/api/v1/haptic/motor1/test");$("test2").onclick=()=>post("/api/v1/haptic/motor2/test");$("testBoth").onclick=()=>post("/api/v1/haptic/both/test");$("testHeart").onclick=()=>post("/api/v1/heartbeat/test");
document.querySelectorAll("[data-minutes]").forEach(button=>button.onclick=()=>post("/api/v1/heartbeat/timer",{seconds:Number(button.dataset.minutes)*60}));$("startTimer").onclick=()=>post("/api/v1/heartbeat/timer",{seconds:Number($("customTime").value)*Number($("customUnit").value)});$("cancelTimer").onclick=async()=>{await fetch("/api/v1/heartbeat/timer",{method:"DELETE"});await refresh()};
refresh();setInterval(refresh,1000);
</script></body></html>"""


def preference_values():
    return {
        "bpm": state["bpm"],
        "motor_mode": state["motor_mode"],
        "lub_intensity": state["lub_intensity"],
        "dub_intensity": state["dub_intensity"],
        "lub_dub_delay_ms": state["lub_dub_delay_ms"],
    }


def timer_state(now):
    return heartbeat_timer.status(now)


def public_state(now):
    result = state.copy()
    result["hardware"] = haptics.status()
    result["timer"] = timer_state(now)
    result["effective_lub_dub_delay_ms"] = int(heartbeat.effective_delay() * 1000)
    return result


def apply_settings(params, now):
    persistent_changed = False
    if params.get("bpm") is not None:
        value = clamp(int(params.get("bpm")), 30, 240)
        persistent_changed = persistent_changed or value != state["bpm"]
        state["bpm"] = value
    if params.get("motor_mode") in MOTOR_MODES:
        value = params.get("motor_mode")
        persistent_changed = persistent_changed or value != state["motor_mode"]
        state["motor_mode"] = value
    if params.get("lub_intensity") is not None:
        value = clamp(int(params.get("lub_intensity")), 0, 100)
        persistent_changed = persistent_changed or value != state["lub_intensity"]
        state["lub_intensity"] = value
    if params.get("dub_intensity") is not None:
        value = clamp(int(params.get("dub_intensity")), 0, 100)
        persistent_changed = persistent_changed or value != state["dub_intensity"]
        state["dub_intensity"] = value
    if params.get("lub_dub_delay_ms") is not None:
        value = clamp(int(params.get("lub_dub_delay_ms")), 50, 300)
        persistent_changed = persistent_changed or value != state["lub_dub_delay_ms"]
        state["lub_dub_delay_ms"] = value
    valid_delay = min(state["lub_dub_delay_ms"], maximum_delay_ms(state["bpm"]))
    persistent_changed = persistent_changed or valid_delay != state["lub_dub_delay_ms"]
    state["lub_dub_delay_ms"] = valid_delay
    if params.get("effect_mode") in ("heart", "snoring"):
        if state["effect_mode"] != params.get("effect_mode"):
            state["effect_mode"] = params.get("effect_mode")
            heartbeat.abort(now)
    if params.get("snore_intensity") is not None:
        state["snore_intensity"] = clamp(int(params.get("snore_intensity")), 0, 100)
    if params.get("snore_duration") is not None:
        state["snore_duration"] = clamp(float(params.get("snore_duration")), 0.2, 5.0)
    if params.get("snore_taper") is not None:
        state["snore_taper"] = clamp(float(params.get("snore_taper")), 0.0, 3.0)
    if params.get("snore_gap") is not None:
        state["snore_gap"] = clamp(float(params.get("snore_gap")), 0.0, 10.0)
    if persistent_changed:
        preferences.schedule(now)


@server.route("/", GET)
def home(request: Request):
    return Response(request, HTML, content_type="text/html")


@server.route("/api/v1/state", GET)
def api_state(request: Request):
    return json_response(request, public_state(time.monotonic()))


@server.route("/api/v1/heartbeat", POST)
def api_heartbeat(request: Request):
    apply_settings(request.query_params, time.monotonic())
    return json_response(request, public_state(time.monotonic()))


@server.route("/api/v1/haptic/motor1/test", POST)
def test_motor1(request: Request):
    now = time.monotonic()
    heartbeat.abort(now)
    simple_test.start("motor1", state["lub_intensity"], now)
    return json_response(request, {"accepted": haptics.motor1.available})


@server.route("/api/v1/haptic/motor2/test", POST)
def test_motor2(request: Request):
    now = time.monotonic()
    heartbeat.abort(now)
    simple_test.start("motor2", state["dub_intensity"], now)
    return json_response(request, {"accepted": haptics.motor2.available})


@server.route("/api/v1/haptic/both/test", POST)
def test_both(request: Request):
    now = time.monotonic()
    heartbeat.abort(now)
    simple_test.start("both", max(state["lub_intensity"], state["dub_intensity"]), now)
    return json_response(request, {"accepted": haptics.motor1.available or haptics.motor2.available})


@server.route("/api/v1/heartbeat/test", POST)
def test_heartbeat(request: Request):
    now = time.monotonic()
    simple_test.update(now + 1)
    heartbeat.trigger_once(now)
    return json_response(request, {"accepted": haptics.motor1.available or haptics.motor2.available})


@server.route("/api/v1/heartbeat/timer", GET)
def get_timer(request: Request):
    return json_response(request, timer_state(time.monotonic()))


@server.route("/api/v1/heartbeat/timer", POST)
def start_timer(request: Request):
    seconds = request.query_params.get("seconds")
    if seconds is None:
        return json_response(request, {"error": "seconds is required"})
    heartbeat_timer.start(int(float(seconds)), time.monotonic())
    return json_response(request, timer_state(time.monotonic()))


@server.route("/api/v1/heartbeat/timer", DELETE)
def cancel_timer(request: Request):
    heartbeat_timer.cancel()
    return json_response(request, timer_state(time.monotonic()))


@server.route("/toggle", POST)
def legacy_toggle(request: Request):
    state["enabled"] = not state["enabled"]
    if not state["enabled"]:
        heartbeat.abort(time.monotonic())
    return Response(request, "on" if state["enabled"] else "off")


@server.route("/settings", POST)
def legacy_settings(request: Request):
    params = request.query_params
    translated = {
        "bpm": params.get("bpm"),
        "effect_mode": params.get("mode"),
        "snore_intensity": params.get("intensity"),
        "snore_duration": params.get("duration"),
        "snore_taper": params.get("taper"),
        "snore_gap": params.get("gap"),
    }
    apply_settings(translated, time.monotonic())
    return Response(request, "ok")


@server.route("/test", POST)
def legacy_test(request: Request):
    return test_both(request)


server.start(str(ip))
print("[HTTP] Server ready at http://{}:5000/".format(ip))
print("[HEART] {} BPM".format(state["bpm"]))
print("[HEART]", state["motor_mode"])
print("[HEART] LUB {}% / DUB {}%".format(state["lub_intensity"], state["dub_intensity"]))

snore_started = time.monotonic()
snore_update = 0

while True:
    now = time.monotonic()
    try:
        server.poll()
    except OSError as error:
        print("[HTTP]", error)

    heartbeat_timer.update(now, state, haptics)
    preferences.update(now, preference_values())

    if simple_test.active:
        simple_test.update(now)
        continue

    if not state["enabled"]:
        haptics.stop_all()
        continue

    if state["effect_mode"] == "heart" or heartbeat.one_shot:
        heartbeat.update(now)
        snore_started = now
        continue

    snore_interval = state["snore_duration"] + state["snore_gap"]
    if now - snore_started >= snore_interval:
        snore_started = now
    elapsed = now - snore_started
    if now >= snore_update:
        if elapsed < state["snore_duration"]:
            rise_time = min(0.25, state["snore_duration"] * 0.25)
            envelope = min(1.0, elapsed / rise_time)
            taper_time = min(state["snore_taper"], state["snore_duration"])
            if taper_time > 0 and elapsed > state["snore_duration"] - taper_time:
                envelope *= (state["snore_duration"] - elapsed) / taper_time
            rumble = 1.0 if int(elapsed * 18) % 2 == 0 else 0.72
            haptics.pulse_phase(
                state["motor_mode"],
                "lub",
                int(state["snore_intensity"] * envelope * rumble),
            )
        else:
            haptics.stop_all()
        snore_update = now + 0.04
