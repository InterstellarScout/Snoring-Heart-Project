import json
import ipaddress
import os
import time

import adafruit_drv2605
import bitbangio
import board
import busio
import microcontroller
import socketpool
import watchdog
import wifi

from adafruit_httpserver import DELETE, GET, POST, Request, Response, Server


DRV2605_ADDRESS = 0x5A
LUB_DURATION = 0.070
DUB_DURATION = 0.090
MIN_BEAT_REST = 0.010
PREFERENCE_PREFIX = b"SH3|"
PREFERENCE_BYTES = 384
CREDENTIAL_PREFIX = b"SHC1|"
CREDENTIAL_OFFSET = PREFERENCE_BYTES
CREDENTIAL_BYTES = 128
HTTP_PORT = 5000
DEFAULT_AP_SSID = "Surrogate Heart"
DEFAULT_AP_IPV4 = "192.168.69.1"
DEFAULT_AP_NETMASK = "255.255.255.0"
CAPTIVE_DEBUG = False
HAPTIC_SAFETY_MARGIN_SECONDS = 0.350
HAPTIC_CONTINUOUS_LEASE_SECONDS = 0.150
APPLICATION_WATCHDOG_TIMEOUT_SECONDS = 5.0
UPSTREAM_CONNECT_TIMEOUT_SECONDS = 4
ACTIVE_MODES = ("heart", "purring", "snoring")
HEART_MOTOR_MODES = ("tandem", "simultaneous", "motor1", "motor2")
HEART_RATE_PRESETS = (
    ("very_slow", "Very Slow", 35),
    ("sleeping", "Sleeping", 45),
    ("relaxed", "Relaxed", 60),
    ("resting", "Resting", 72),
    ("alert", "Alert", 90),
    ("walking", "Walking", 110),
    ("active", "Active", 130),
    ("running", "Running", 160),
    ("sprinting", "Sprinting", 190),
    ("extreme", "Extreme", 220),
)
PURR_PATTERNS = ("alternate_left", "alternate_right", "both", "motor1", "motor2")
PURR_PLAY_MODES = ("slow", "normal", "fast")
PURR_RATE_MIN_PPM = 240
PURR_RATE_MAX_PPM = 1200
PURR_RATE_DEFAULT_PPM = 600
PURR_BASELINE_DEFAULT = 30
# duty, modulation depth, and transition fraction of one complete cycle.
# Soft keeps a narrow, smoothly changing band; Crisp has the deepest and
# sharpest contrast. All timing scales with the selected purr rate.
PURR_PLAY_MODE_CONFIG = {
    "slow": (0.80, 0.35, 0.20),
    "normal": (0.60, 0.60, 0.10),
    "fast": (0.375, 0.85, 0.025),
}
SNORE_MOTOR_MODES = ("motor1", "motor2", "both", "alternating")
SNORE_LEAD_RISE_END = 0.15
SNORE_LEAD_SUSTAIN_END = 0.30
API_KEYS = (
    "mode",
    "running",
    "master_power",
    "bpm",
    "preset",
    "motor_mode",
    "lub_intensity",
    "dub_intensity",
    "lub_dub_delay_ms",
    "intensity",
    "baseline",
    "rate",
    "rate_ppm",
    "play_mode",
    "speed",
    "motor_pattern",
    "duration",
    "taper",
    "gap",
    "seconds",
    "enabled",
    "ssid",
    "password",
    "behavior",
    "action",
)


def clamp(value, minimum, maximum):
    return max(minimum, min(maximum, value))


def scaled_strength(intensity, master_power):
    return int(clamp(intensity, 0, 100) * clamp(master_power, 0, 100) / 100)


def normalized_purr_rate(value):
    """Migrate legacy low-rate values to the new tactile default safely."""
    try:
        rate = int(value)
    except (TypeError, ValueError):
        return PURR_RATE_DEFAULT_PPM
    if PURR_RATE_MIN_PPM <= rate <= PURR_RATE_MAX_PPM:
        return rate
    return PURR_RATE_DEFAULT_PPM


def purr_modulation_strength(elapsed, interval, high_strength, play_mode):
    duty, modulation_depth, transition_fraction = PURR_PLAY_MODE_CONFIG[play_mode]
    high_strength = clamp(int(high_strength), 0, 100)
    low_strength = int(high_strength * (1.0 - modulation_depth))
    high_time = interval * duty
    low_time = max(0.0, interval - high_time)
    transition_time = min(
        interval * transition_fraction,
        high_time,
        low_time,
    )
    elapsed = clamp(elapsed, 0.0, interval)
    if transition_time > 0 and elapsed < transition_time:
        blend = elapsed / transition_time
        return int(low_strength + (high_strength - low_strength) * blend)
    if elapsed < high_time:
        return high_strength
    if transition_time > 0 and elapsed < high_time + transition_time:
        blend = (elapsed - high_time) / transition_time
        return int(high_strength - (high_strength - low_strength) * blend)
    return low_strength


def maximum_delay_ms(bpm):
    available = 60.0 / bpm - LUB_DURATION - DUB_DURATION - MIN_BEAT_REST
    return max(50, min(300, int(available * 1000)))


def preset_bpm(preset):
    for machine_name, _label, bpm in HEART_RATE_PRESETS:
        if machine_name == preset:
            return bpm
    return None


def bpm_preset(bpm):
    for machine_name, _label, preset_value in HEART_RATE_PRESETS:
        if preset_value == bpm:
            return machine_name
    return "custom"


def truthy(value):
    return value is True or str(value).lower() in ("true", "1", "on", "yes")


SAFETY_DEBUG = truthy(os.getenv("HEART_SAFETY_DEBUG"))


def single_snore_envelope(elapsed, duration, taper):
    rise_time = min(0.25, duration * 0.25)
    envelope = min(1.0, elapsed / rise_time)
    if taper > 0 and elapsed > duration - taper:
        envelope *= (duration - elapsed) / taper
    return max(0.0, envelope)


def alternating_snore_phase_fractions(duration, taper):
    """Return normalized staged-crossing phase lengths.

    Rise and lead-only sustain remain 15% each. The legacy taper control
    expands each motor's fade from 10% to 25%; the follow rise receives the
    remaining time. At the default 0.7 s taper / 1.4 s duration, the phases
    are exactly 15%, 15%, 20%, 25%, and 25%.
    """
    half_duration = max(0.001, duration * 0.5)
    taper_influence = clamp(taper / half_duration, 0.0, 1.0)
    each_taper = 0.10 + 0.15 * taper_influence
    follow_rise = 0.70 - each_taper * 2.0
    return (0.15, 0.15, follow_rise, each_taper, each_taper)


def alternating_snore_envelopes(elapsed, duration, taper):
    if duration <= 0:
        return 0.0, 0.0
    progress = clamp(elapsed / duration, 0.0, 1.0)
    lead_rise, lead_sustain, follow_rise, lead_taper, follow_taper = (
        alternating_snore_phase_fractions(duration, taper)
    )
    lead_rise_end = lead_rise
    lead_sustain_end = lead_rise_end + lead_sustain
    follow_rise_end = lead_sustain_end + follow_rise
    lead_taper_end = follow_rise_end + lead_taper
    if progress < SNORE_LEAD_RISE_END:
        return progress / lead_rise_end, 0.0
    if progress < lead_sustain_end:
        return 1.0, 0.0
    if progress < follow_rise_end:
        follow = (progress - lead_sustain_end) / follow_rise
        return 1.0, follow
    if progress < lead_taper_end:
        lead = 1.0 - (progress - follow_rise_end) / lead_taper
        return max(0.0, lead), 1.0
    follow = 1.0 - (progress - lead_taper_end) / follow_taper
    return 0.0, max(0.0, follow)


def snore_direction_name(direction):
    if direction == 0:
        return "motor1_to_motor2"
    return "motor2_to_motor1"


def json_response(request, value):
    return Response(request, json.dumps(value), content_type="application/json")


def request_data(request):
    data = {}
    try:
        body = request.json()
        if body:
            data.update(body)
    except Exception:
        pass
    for key in API_KEYS:
        value = request.query_params.get(key)
        if value is not None:
            data[key] = value
    return data


def scan_bus(i2c):
    while not i2c.try_lock():
        pass
    try:
        return i2c.scan()
    finally:
        i2c.unlock()


class PreferenceStore:
    def _load_region(self, offset, length, prefix):
        try:
            raw = bytes(microcontroller.nvm[offset:offset + length])
            if not raw.startswith(prefix):
                return {}
            end = raw.find(b"\x00", len(prefix))
            if end < 0:
                end = len(raw)
            return json.loads(raw[len(prefix):end].decode("utf-8"))
        except Exception as error:
            print("[PREFS] Load warning:", error)
            return {}

    def load_defaults(self):
        values = self._load_region(0, PREFERENCE_BYTES, PREFERENCE_PREFIX)
        if values:
            return values
        return self._load_region(0, PREFERENCE_BYTES, b"SH2|")

    def load_credentials(self):
        return self._load_region(
            CREDENTIAL_OFFSET,
            CREDENTIAL_BYTES,
            CREDENTIAL_PREFIX,
        )

    def _write_region(self, offset, length, prefix, values):
        try:
            if offset + length > len(microcontroller.nvm):
                raise ValueError("NVM region is unavailable on this board")
            data = prefix + json.dumps(values).encode("utf-8") + b"\x00"
            if len(data) > length:
                raise ValueError("stored data exceeds NVM allocation")
            microcontroller.nvm[offset:offset + len(data)] = data
            print("[PREFS] Saved explicit defaults")
            return True
        except Exception as error:
            print("[PREFS] Save warning:", error)
            return False

    def save_defaults(self, values, credentials):
        defaults_saved = self._write_region(
            0,
            PREFERENCE_BYTES,
            PREFERENCE_PREFIX,
            values,
        )
        credentials_saved = self._write_region(
            CREDENTIAL_OFFSET,
            CREDENTIAL_BYTES,
            CREDENTIAL_PREFIX,
            credentials,
        )
        return defaults_saved and credentials_saved


class HapticMotor:
    def __init__(self, number, sda, scl, pin_description):
        self.number = number
        self.available = False
        self.bus_kind = "unavailable"
        self.i2c = None
        self.driver = None
        self.command_count = 0
        self.activation_count = 0
        self.current_strength = 0
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
        percent = clamp(int(percent), 0, 100)
        if percent == self.current_strength:
            return
        try:
            self.driver.realtime_value = int(127 * percent / 100)
            self.command_count += 1
            self.current_strength = percent
            if percent > 0:
                self.activation_count += 1
        except Exception as error:
            self.available = False
            print("[HAPTIC] WARNING: Motor {} write failed: {}".format(self.number, error))

    def stop(self):
        if self.driver is None:
            self.current_strength = 0
            return
        if self.available and self.current_strength == 0:
            return
        try:
            self.driver.realtime_value = 0
            if self.current_strength != 0:
                self.command_count += 1
            self.current_strength = 0
        except Exception as error:
            print(
                "[HAPTIC] WARNING: Motor {} emergency stop failed: {}".format(
                    self.number,
                    error,
                )
            )


class HapticController:
    def __init__(self):
        self.simultaneous_activation_count = 0
        self.output_expected_until = None
        self.output_lease_deadline = None
        self.output_lease_reason = "off"
        self.failsafe_count = 0
        self.last_failsafe_reason = ""
        self.motor1 = HapticMotor(1, board.A0, board.A1, "A0/A1")
        self.motor2 = HapticMotor(2, board.A2, board.A3, "A2/A3")
        count = int(self.motor1.available) + int(self.motor2.available)
        print("[HAPTIC] {}/2 haptic drivers available".format(count))
        if count == 1:
            print("[HAPTIC] Continuing in single-motor mode")

    def _renew_output_lease(self, expected_until, reason):
        if self.motor1.current_strength <= 0 and self.motor2.current_strength <= 0:
            self.output_expected_until = None
            self.output_lease_deadline = None
            self.output_lease_reason = "off"
            return
        if expected_until is None:
            expected_until = time.monotonic() + HAPTIC_CONTINUOUS_LEASE_SECONDS
        self.output_expected_until = expected_until
        self.output_lease_deadline = expected_until + HAPTIC_SAFETY_MARGIN_SECONDS
        self.output_lease_reason = reason

    def set_motor_1(self, strength, expected_until=None, reason="motor1"):
        self.motor1.set_strength(strength)
        self._renew_output_lease(expected_until, reason)

    def set_motor_2(self, strength, expected_until=None, reason="motor2"):
        self.motor2.set_strength(strength)
        self._renew_output_lease(expected_until, reason)

    def set_both(self, strength1, strength2=None, expected_until=None, reason="both"):
        if strength2 is None:
            strength2 = strength1
        if strength1 > 0 and strength2 > 0:
            self.simultaneous_activation_count += 1
        self.motor1.set_strength(strength1)
        self.motor2.set_strength(strength2)
        self._renew_output_lease(expected_until, reason)

    def stop_all(self):
        self.motor1.stop()
        self.motor2.stop()
        self.output_expected_until = None
        self.output_lease_deadline = None
        self.output_lease_reason = "off"

    def check_failsafe(self, now):
        output_active = (
            self.motor1.current_strength > 0
            or self.motor2.current_strength > 0
        )
        expired = (
            self.output_lease_deadline is None
            or now > self.output_lease_deadline
        )
        if not output_active or not expired:
            return False
        reason = self.output_lease_reason
        overdue = (
            now - self.output_lease_deadline
            if self.output_lease_deadline is not None
            else 0.0
        )
        self.motor1.stop()
        self.motor2.stop()
        self.output_expected_until = None
        self.output_lease_deadline = None
        self.output_lease_reason = "off"
        self.failsafe_count += 1
        self.last_failsafe_reason = reason
        print(
            "[SAFETY] Expired haptic lease forced both motors OFF; "
            "reason={} overdue={:.3f}s".format(reason, overdue)
        )
        return True

    def set_pattern(
        self,
        motor_mode,
        phase,
        strength,
        alternating_index=0,
        expected_until=None,
        reason="pattern",
    ):
        self.stop_all()
        if motor_mode == "simultaneous":
            self.set_both(strength, expected_until=expected_until, reason=reason)
        elif motor_mode == "motor1":
            if self.motor1.available:
                self.set_motor_1(strength, expected_until, reason)
            else:
                self.set_motor_2(strength, expected_until, reason)
        elif motor_mode == "motor2":
            if self.motor2.available:
                self.set_motor_2(strength, expected_until, reason)
            else:
                self.set_motor_1(strength, expected_until, reason)
        elif motor_mode == "alternating":
            if alternating_index % 2 == 0:
                if self.motor1.available:
                    self.set_motor_1(strength, expected_until, reason)
                else:
                    self.set_motor_2(strength, expected_until, reason)
            else:
                if self.motor2.available:
                    self.set_motor_2(strength, expected_until, reason)
                else:
                    self.set_motor_1(strength, expected_until, reason)
        elif phase == "lub":
            if self.motor1.available:
                self.set_motor_1(strength, expected_until, reason)
            else:
                self.set_motor_2(strength, expected_until, reason)
        else:
            if self.motor2.available:
                self.set_motor_2(strength, expected_until, reason)
            else:
                self.set_motor_1(strength, expected_until, reason)

    def set_snore(
        self,
        motor_mode,
        strength,
        motor1_strength=0,
        motor2_strength=0,
        expected_until=None,
        reason="snore",
    ):
        if motor_mode == "motor1":
            self.set_both(strength, 0, expected_until, reason)
        elif motor_mode == "motor2":
            self.set_both(0, strength, expected_until, reason)
        elif motor_mode == "both":
            self.set_both(strength, expected_until=expected_until, reason=reason)
        else:
            self.set_both(
                motor1_strength,
                motor2_strength,
                expected_until,
                reason,
            )

    def set_purr(
        self,
        motor_mode,
        baseline_strength,
        modulation_strength,
        expected_until=None,
        reason="purr",
    ):
        if motor_mode == "alternate_left":
            self.set_both(
                baseline_strength,
                modulation_strength,
                expected_until,
                reason,
            )
        elif motor_mode == "alternate_right":
            self.set_both(
                modulation_strength,
                baseline_strength,
                expected_until,
                reason,
            )
        elif motor_mode == "both":
            self.set_both(
                modulation_strength,
                expected_until=expected_until,
                reason=reason,
            )
        elif motor_mode == "motor1":
            self.set_both(modulation_strength, 0, expected_until, reason)
        else:
            self.set_both(0, modulation_strength, expected_until, reason)

    def status(self):
        return {
            "motor1_available": self.motor1.available,
            "motor2_available": self.motor2.available,
            "motor1_bus": self.motor1.bus_kind,
            "motor2_bus": self.motor2.bus_kind,
            "motor1_commands": self.motor1.command_count,
            "motor2_commands": self.motor2.command_count,
            "motor1_activations": self.motor1.activation_count,
            "motor2_activations": self.motor2.activation_count,
            "simultaneous_activations": self.simultaneous_activation_count,
            "motor1_output": self.motor1.current_strength,
            "motor2_output": self.motor2.current_strength,
            "output_expected_until": self.output_expected_until,
            "output_lease_deadline": self.output_lease_deadline,
            "output_lease_reason": self.output_lease_reason,
            "failsafe_count": self.failsafe_count,
            "last_failsafe_reason": self.last_failsafe_reason,
            "safety_margin_seconds": HAPTIC_SAFETY_MARGIN_SECONDS,
        }


class HeartScheduler:
    WAITING = 0
    LUB_ACTIVE = 1
    INTER_PULSE = 2
    DUB_ACTIVE = 3

    def __init__(self, haptics, state):
        self.haptics = haptics
        self.state_values = state
        self.phase = self.WAITING
        self.deadline = 0
        self.next_beat = 0
        self.beat_started = 0
        self.first_activation_at = None

    def reset(self, now, preserve_output=False):
        if not preserve_output:
            self.haptics.stop_all()
        self.phase = self.WAITING
        self.next_beat = now + 60.0 / self.state_values["heart_bpm"]

    def arm(self, now):
        self.phase = self.WAITING
        self.next_beat = now

    def effective_delay(self):
        maximum = maximum_delay_ms(self.state_values["heart_bpm"])
        return min(self.state_values["heart_delay_ms"], maximum) / 1000.0

    def update(self, now):
        if self.phase == self.WAITING and now >= self.next_beat:
            self.beat_started = now
            if self.first_activation_at is None:
                self.first_activation_at = now
            strength = scaled_strength(
                self.state_values["heart_lub"],
                self.state_values["master_power"],
            )
            self.deadline = now + LUB_DURATION
            self.haptics.set_pattern(
                self.state_values["heart_motor_mode"],
                "lub",
                strength,
                expected_until=self.deadline,
                reason="heart_lub",
            )
            self.phase = self.LUB_ACTIVE
        elif self.phase == self.LUB_ACTIVE and now >= self.deadline:
            self.haptics.stop_all()
            self.deadline = now + self.effective_delay()
            self.phase = self.INTER_PULSE
        elif self.phase == self.INTER_PULSE and now >= self.deadline:
            strength = scaled_strength(
                self.state_values["heart_dub"],
                self.state_values["master_power"],
            )
            self.deadline = now + DUB_DURATION
            self.haptics.set_pattern(
                self.state_values["heart_motor_mode"],
                "dub",
                strength,
                expected_until=self.deadline,
                reason="heart_dub",
            )
            self.phase = self.DUB_ACTIVE
        elif self.phase == self.DUB_ACTIVE and now >= self.deadline:
            self.haptics.stop_all()
            self.phase = self.WAITING
            interval = 60.0 / self.state_values["heart_bpm"]
            self.next_beat = self.beat_started + interval
            if self.next_beat <= now:
                self.next_beat = now + interval


class PurrScheduler:
    WAITING = 0
    ACTIVE = 1

    def __init__(self, haptics, state):
        self.haptics = haptics
        self.state_values = state
        self.phase = self.WAITING
        self.deadline = 0
        self.cycle_started = 0
        self.interval = 60.0 / PURR_RATE_DEFAULT_PPM
        self.pulse_index = 0
        self.play_mode = "normal"
        self.motor_mode = "alternate_left"
        self.baseline_strength = 0
        self.high_strength = 0

    def reset(self, now, preserve_output=False):
        if not preserve_output:
            self.haptics.stop_all()
        self.phase = self.WAITING
        self.deadline = now + 60.0 / self.state_values["purr_rate"]

    def arm(self, now):
        self.phase = self.ACTIVE
        self.cycle_started = now
        self.snapshot_cycle()
        self.deadline = self.cycle_started + self.interval

    def pulse_interval(self):
        return 60.0 / self.state_values["purr_rate"]

    def snapshot_cycle(self):
        self.interval = self.pulse_interval()
        self.play_mode = self.state_values["purr_play_mode"]
        self.motor_mode = self.state_values["purr_pattern"]
        self.baseline_strength = scaled_strength(
            self.state_values["purr_baseline"],
            self.state_values["master_power"],
        )
        self.high_strength = scaled_strength(
            self.state_values["purr_intensity"],
            self.state_values["master_power"],
        )

    def apply_output(self, now):
        elapsed = clamp(now - self.cycle_started, 0.0, self.interval)
        modulation = purr_modulation_strength(
            elapsed,
            self.interval,
            self.high_strength,
            self.play_mode,
        )
        self.haptics.set_purr(
            self.motor_mode,
            self.baseline_strength,
            modulation,
            now + HAPTIC_CONTINUOUS_LEASE_SECONDS,
            "purr",
        )

    def update(self, now):
        if self.phase == self.WAITING and now >= self.deadline:
            self.phase = self.ACTIVE
            self.cycle_started = now
            self.snapshot_cycle()
            self.deadline = self.cycle_started + self.interval
        if self.phase != self.ACTIVE:
            return
        if now >= self.deadline:
            # Advance from the prior boundary, never from a settings-change
            # request. This prevents a rate change from injecting an extra pulse.
            elapsed = now - self.cycle_started
            completed = max(1, int(elapsed / self.interval))
            self.cycle_started += completed * self.interval
            self.pulse_index += completed
            self.snapshot_cycle()
            if now - self.cycle_started >= self.interval:
                # A long loop stall must not replay a burst of missed cycles.
                self.cycle_started = now
            self.deadline = self.cycle_started + self.interval
        self.apply_output(now)


class SnoreScheduler:
    WAITING = 0
    ACTIVE = 1

    def __init__(self, haptics, state):
        self.haptics = haptics
        self.state_values = state
        self.phase = self.WAITING
        self.deadline = 0
        self.started = 0
        self.update_at = 0
        self.duration = 0
        self.taper = 0
        self.strength = 0
        self.motor_mode = "both"
        self.direction = 0
        self.next_direction = 0

    def reset(self, now, preserve_output=False):
        if not preserve_output:
            self.haptics.stop_all()
        self.phase = self.WAITING
        self.started = 0
        self.duration = 0
        self.strength = 0
        self.deadline = now + self.state_values["snore_gap"]

    def arm(self, now):
        self.phase = self.WAITING
        self.deadline = now

    def update(self, now):
        if self.phase == self.WAITING and now >= self.deadline:
            self.started = now
            self.duration = self.state_values["snore_duration"]
            self.taper = min(self.state_values["snore_taper"], self.duration)
            self.strength = scaled_strength(
                self.state_values["snore_intensity"],
                self.state_values["master_power"],
            )
            self.motor_mode = self.state_values["snore_motor_mode"]
            self.direction = self.next_direction
            self.update_at = now
            self.phase = self.ACTIVE
        elif self.phase == self.ACTIVE:
            elapsed = now - self.started
            if elapsed >= self.duration:
                self.haptics.stop_all()
                if self.motor_mode == "alternating":
                    self.next_direction = 1 - self.next_direction
                self.phase = self.WAITING
                self.deadline = now + self.state_values["snore_gap"]
            elif now >= self.update_at:
                rumble = 1.0 if int(elapsed * 18) % 2 == 0 else 0.72
                if self.motor_mode == "alternating":
                    lead, follow = alternating_snore_envelopes(
                        elapsed,
                        self.duration,
                        self.taper,
                    )
                    lead_strength = int(self.strength * lead * rumble)
                    follow_strength = int(self.strength * follow * rumble)
                    if self.direction == 0:
                        motor1_strength = lead_strength
                        motor2_strength = follow_strength
                    else:
                        motor1_strength = follow_strength
                        motor2_strength = lead_strength
                    self.haptics.set_snore(
                        self.motor_mode,
                        0,
                        motor1_strength,
                        motor2_strength,
                        now + HAPTIC_CONTINUOUS_LEASE_SECONDS,
                        "snore_alternating",
                    )
                else:
                    envelope = single_snore_envelope(
                        elapsed,
                        self.duration,
                        self.taper,
                    )
                    self.haptics.set_snore(
                        self.motor_mode,
                        int(self.strength * envelope * rumble),
                        expected_until=now + HAPTIC_CONTINUOUS_LEASE_SECONDS,
                        reason="snore",
                    )
                self.update_at = now + 0.04


class TestScheduler:
    def __init__(self, haptics, state):
        self.haptics = haptics
        self.state_values = state
        self.active = False
        self.kind = None
        self.phase = 0
        self.deadline = 0
        self.started = 0
        self.update_at = 0
        self.pulse_index = 0
        self.snore_duration = 0
        self.snore_taper = 0
        self.snore_strength = 0
        self.snore_motor_mode = "both"
        self.snore_direction = 0
        self.next_test_snore_direction = 0
        self.purr_cycle_started = 0
        self.purr_interval = 60.0 / PURR_RATE_DEFAULT_PPM

    def cancel(self):
        if self.active:
            self.haptics.stop_all()
        self.active = False
        self.kind = None

    def start(self, kind, now):
        self.cancel()
        self.kind = kind
        self.phase = 0
        self.deadline = now
        self.started = now
        self.update_at = now
        self.pulse_index = 0
        if kind == "purr":
            self.purr_rate = self.state_values["purr_rate"]
            self.purr_play_mode = self.state_values["purr_play_mode"]
            self.purr_pattern = self.state_values["purr_pattern"]
            self.purr_baseline = scaled_strength(
                self.state_values["purr_baseline"],
                self.state_values["master_power"],
            )
            self.purr_intensity = scaled_strength(
                self.state_values["purr_intensity"],
                self.state_values["master_power"],
            )
            self.purr_interval = 60.0 / self.purr_rate
            self.purr_cycle_started = now
        if kind == "snore":
            self.snore_duration = self.state_values["snore_duration"]
            self.snore_taper = min(
                self.state_values["snore_taper"],
                self.snore_duration,
            )
            self.snore_strength = scaled_strength(
                self.state_values["snore_intensity"],
                self.state_values["master_power"],
            )
            self.snore_motor_mode = self.state_values["snore_motor_mode"]
            self.snore_direction = self.next_test_snore_direction
        self.active = True

    def finish(self):
        self.haptics.stop_all()
        self.active = False
        self.kind = None

    def update(self, now):
        master = self.state_values["master_power"]
        if self.kind in ("motor1", "motor2", "both"):
            if self.phase == 0:
                strength = scaled_strength(75, master)
                if self.kind == "motor1":
                    self.haptics.set_motor_1(
                        strength,
                        self.deadline + 0.20,
                        "test_motor1",
                    )
                elif self.kind == "motor2":
                    self.haptics.set_motor_2(
                        strength,
                        self.deadline + 0.20,
                        "test_motor2",
                    )
                else:
                    self.haptics.set_both(
                        strength,
                        expected_until=self.deadline + 0.20,
                        reason="test_both",
                    )
                self.deadline = now + 0.20
                self.phase = 1
            elif now >= self.deadline:
                self.finish()
        elif self.kind == "heart":
            self.update_heart(now, master)
        elif self.kind == "purr":
            self.update_purr(now, master)
        elif self.kind == "snore":
            self.update_snore(now, master)

    def update_heart(self, now, master):
        if self.phase == 0:
            strength = scaled_strength(self.state_values["heart_lub"], master)
            self.deadline = now + LUB_DURATION
            self.haptics.set_pattern(
                self.state_values["heart_motor_mode"],
                "lub",
                strength,
                expected_until=self.deadline,
                reason="test_heart_lub",
            )
            self.phase = 1
        elif self.phase == 1 and now >= self.deadline:
            self.haptics.stop_all()
            self.deadline = now + min(
                self.state_values["heart_delay_ms"],
                maximum_delay_ms(self.state_values["heart_bpm"]),
            ) / 1000.0
            self.phase = 2
        elif self.phase == 2 and now >= self.deadline:
            strength = scaled_strength(self.state_values["heart_dub"], master)
            self.deadline = now + DUB_DURATION
            self.haptics.set_pattern(
                self.state_values["heart_motor_mode"],
                "dub",
                strength,
                expected_until=self.deadline,
                reason="test_heart_dub",
            )
            self.phase = 3
        elif self.phase == 3 and now >= self.deadline:
            self.finish()

    def update_purr(self, now, master):
        elapsed = now - self.purr_cycle_started
        if elapsed >= self.purr_interval:
            completed = max(1, int(elapsed / self.purr_interval))
            self.purr_cycle_started += completed * self.purr_interval
            self.pulse_index += completed
            if self.pulse_index >= 4:
                self.finish()
                return
        modulation = purr_modulation_strength(
            now - self.purr_cycle_started,
            self.purr_interval,
            self.purr_intensity,
            self.purr_play_mode,
        )
        self.haptics.set_purr(
            self.purr_pattern,
            self.purr_baseline,
            modulation,
            now + HAPTIC_CONTINUOUS_LEASE_SECONDS,
            "test_purr",
        )

    def update_snore(self, now, master):
        elapsed = now - self.started
        if elapsed >= self.snore_duration:
            if self.snore_motor_mode == "alternating":
                self.next_test_snore_direction = 1 - self.next_test_snore_direction
            self.finish()
        elif now >= self.update_at:
            rumble = 1.0 if int(elapsed * 18) % 2 == 0 else 0.72
            if self.snore_motor_mode == "alternating":
                lead, follow = alternating_snore_envelopes(
                    elapsed,
                    self.snore_duration,
                    self.snore_taper,
                )
                lead_strength = int(self.snore_strength * lead * rumble)
                follow_strength = int(self.snore_strength * follow * rumble)
                if self.snore_direction == 0:
                    motor1_strength = lead_strength
                    motor2_strength = follow_strength
                else:
                    motor1_strength = follow_strength
                    motor2_strength = lead_strength
                self.haptics.set_snore(
                    self.snore_motor_mode,
                    0,
                    motor1_strength,
                    motor2_strength,
                    now + HAPTIC_CONTINUOUS_LEASE_SECONDS,
                    "test_snore_alternating",
                )
            else:
                envelope = single_snore_envelope(
                    elapsed,
                    self.snore_duration,
                    self.snore_taper,
                )
                self.haptics.set_snore(
                    self.snore_motor_mode,
                    int(self.snore_strength * envelope * rumble),
                    expected_until=now + HAPTIC_CONTINUOUS_LEASE_SECONDS,
                    reason="test_snore",
                )
            self.update_at = now + 0.04


class OutputArbiter:
    def __init__(self, haptics, state):
        self.haptics = haptics
        self.state_values = state
        self.heart = HeartScheduler(haptics, state)
        self.purr = PurrScheduler(haptics, state)
        self.snore = SnoreScheduler(haptics, state)
        self.test = TestScheduler(haptics, state)

    def scheduler(self):
        if self.state_values["active_mode"] == "heart":
            return self.heart
        if self.state_values["active_mode"] == "purring":
            return self.purr
        return self.snore

    def stop_all(self, now):
        self.test.cancel()
        self.state_values["running"] = False
        self.haptics.stop_all()
        self.heart.reset(now, True)
        self.purr.reset(now, True)
        self.snore.reset(now, True)

    def switch_mode(self, mode, now):
        if mode not in ACTIVE_MODES:
            return False
        if mode == self.state_values["active_mode"]:
            if not self.state_values["running"]:
                self.set_running(True, now)
            return False
        self.stop_all(now)
        self.state_values["active_mode"] = mode
        self.set_running(True, now)
        return True

    def set_running(self, running, now):
        if not running:
            self.stop_all(now)
            return
        self.test.cancel()
        self.haptics.stop_all()
        self.state_values["running"] = True
        self.scheduler().arm(now)

    def start_test(self, kind, now):
        self.haptics.stop_all()
        self.heart.reset(now, True)
        self.purr.reset(now, True)
        self.snore.reset(now, True)
        self.test.start(kind, now)

    def update(self, now):
        if self.test.active:
            was_active = True
            self.test.update(now)
            if was_active and not self.test.active and self.state_values["running"]:
                self.scheduler().reset(now, True)
            return
        if self.state_values["running"]:
            self.scheduler().update(now)


class RunTimer:
    def __init__(self):
        self.deadline = None
        self.completed = False

    def start(self, seconds, now):
        self.deadline = now + clamp(int(seconds), 1, 86400)
        self.completed = False

    def cancel(self):
        self.deadline = None
        self.completed = False

    def update(self, now, arbiter):
        if self.deadline is not None and now >= self.deadline:
            self.deadline = None
            self.completed = True
            arbiter.set_running(False, now)

    def status(self, now):
        remaining = 0
        if self.deadline is not None:
            remaining = max(0, int(self.deadline - now + 0.999))
        return {
            "active": self.deadline is not None,
            "remaining_seconds": remaining,
            "action": "stop_haptics",
            "complete": self.completed,
        }


class NetworkManager:
    """Manage station mode without ever disabling the direct-control AP."""

    def __init__(self, radio, live_values):
        self.radio = radio
        self.live_values = live_values
        self.status = "Disabled" if not live_values["upstream_enabled"] else "Failed"
        self.error = "" if not live_values["upstream_enabled"] else "Not connected"
        self.station_ip = None
        self.rssi = None
        self.internet_status = "Not tested"

    def refresh(self):
        if not self.live_values["upstream_enabled"]:
            self.status = "Disabled"
            self.station_ip = None
            self.rssi = None
            return
        if self.radio.connected:
            self.status = "Connected"
            self.station_ip = getattr(self.radio, "ipv4_address", None)
            info = getattr(self.radio, "ap_info", None)
            self.rssi = getattr(info, "rssi", None) if info else None
        elif self.status != "Connecting":
            self.status = "Failed"
            if not self.error:
                self.error = "Not connected"
            self.station_ip = None
            self.rssi = None

    def disconnect(self):
        try:
            self.radio.stop_station()
        except Exception as error:
            print("[WIFI] Station stop warning:", error)
        self.live_values["upstream_enabled"] = False
        self.status = "Disabled"
        self.error = ""
        self.station_ip = None
        self.rssi = None
        self.internet_status = "Not tested"
        print("[WIFI] Upstream station disabled; AP active =", self.radio.ap_active)

    def connect(self):
        ssid = self.live_values["upstream_ssid"]
        password = self.live_values["upstream_password"]
        if not ssid:
            self.status = "Failed"
            self.error = "Network name is required"
            return False
        self.live_values["upstream_enabled"] = True
        self.status = "Connecting"
        self.error = ""
        self.internet_status = "Not tested"
        print("[WIFI] Connecting Heart to upstream Wi-Fi:", ssid)
        try:
            current_info = getattr(self.radio, "ap_info", None)
            current_ssid = getattr(current_info, "ssid", "") if current_info else ""
            if isinstance(current_ssid, bytes):
                current_ssid = current_ssid.decode("utf-8")
            if self.radio.connected and current_ssid != ssid:
                self.radio.stop_station()
            if not self.radio.connected:
                self.radio.start_station()
                self.radio.connect(
                    ssid,
                    password,
                    timeout=UPSTREAM_CONNECT_TIMEOUT_SECONDS,
                )
            self.refresh()
            if not self.radio.connected:
                raise RuntimeError("station did not acquire a connection")
            print("[WIFI] Upstream connected")
            print("[WIFI] Station IP:", self.station_ip)
            print("[WIFI] Direct AP still active:", self.radio.ap_active)
            print("[WIFI] Direct AP IP:", self.radio.ipv4_address_ap)
            return True
        except Exception as error:
            self.status = "Failed"
            self.error = str(error)
            self.station_ip = None
            print("[WIFI] Upstream connection failed:", error)
            print("[WIFI] Direct AP still active:", self.radio.ap_active)
            return False

    def test_internet(self):
        if not self.radio.connected:
            self.internet_status = "Unavailable: station not connected"
            return False
        try:
            reply = self.radio.ping(ipaddress.ip_address("1.1.1.1"), timeout=1.0)
            self.internet_status = "Reachable" if reply is not None else "No ping reply"
            return reply is not None
        except Exception as error:
            self.internet_status = "Test failed: {}".format(error)
            return False

    def public_status(self):
        self.refresh()
        return {
            "enabled": self.live_values["upstream_enabled"],
            "status": self.status,
            "ssid": self.live_values["upstream_ssid"],
            "station_ip": str(self.station_ip) if self.station_ip else None,
            "signal_strength": self.rssi,
            "error": self.error,
            "internet_status": self.internet_status,
        }


def saved_value(saved, compact_key, old_key, default):
    if compact_key in saved:
        return saved[compact_key]
    if old_key in saved:
        return saved[old_key]
    return default


boot_started = time.monotonic()
print("[BOOT] Surrogate Heart vNext")
print("[BOOT] Runtime:", os.uname().version)
print("[PREFS] NVM bytes:", len(microcontroller.nvm))
preferences = PreferenceStore()
saved_defaults = preferences.load_defaults()
saved_credentials = preferences.load_credentials()
settings_upstream_ssid = (
    os.getenv("HEART_UPSTREAM_WIFI_SSID")
    or os.getenv("CIRCUITPY_WIFI_SSID")
    or ""
)
settings_upstream_password = (
    os.getenv("HEART_UPSTREAM_WIFI_PASSWORD")
    or os.getenv("CIRCUITPY_WIFI_PASSWORD")
    or ""
)
settings_upstream_enabled = truthy(
    os.getenv("HEART_UPSTREAM_WIFI_ENABLED")
    or bool(settings_upstream_ssid)
)

# live_state is the authoritative user-session configuration. Scheduler phase,
# deadlines, event snapshots, and direction counters stay inside the scheduler
# objects and are never persisted. saved_defaults changes only via Save Defaults.
live_state = {
    "active_mode": saved_value(saved_defaults, "m", "effect_mode", "heart"),
    "running": False,
    "master_power": clamp(int(saved_value(saved_defaults, "p", "master_power", 100)), 0, 100),
    "heart_bpm": clamp(int(saved_value(saved_defaults, "b", "bpm", 72)), 30, 240),
    "heart_motor_mode": saved_value(saved_defaults, "hm", "motor_mode", "tandem"),
    "heart_lub": clamp(int(saved_value(saved_defaults, "li", "lub_intensity", 65)), 0, 100),
    "heart_dub": clamp(int(saved_value(saved_defaults, "di", "dub_intensity", 80)), 0, 100),
    "heart_delay_ms": clamp(int(saved_value(saved_defaults, "d", "lub_dub_delay_ms", 100)), 50, 300),
    "purr_intensity": clamp(int(saved_value(saved_defaults, "pi", "purr_intensity", 45)), 0, 100),
    "purr_baseline": clamp(int(saved_value(saved_defaults, "pb", "purr_baseline", PURR_BASELINE_DEFAULT)), 0, 100),
    "purr_rate": normalized_purr_rate(
        saved_value(
            saved_defaults,
            "pr",
            "purr_rate",
            PURR_RATE_DEFAULT_PPM,
        )
    ),
    "purr_play_mode": saved_value(saved_defaults, "pc", "purr_play_mode", saved_value(saved_defaults, "ps", "purr_speed", "normal")),
    "purr_pattern": saved_value(saved_defaults, "pm", "purr_pattern", "alternate_left"),
    "snore_intensity": clamp(int(saved_value(saved_defaults, "si", "snore_intensity", 70)), 0, 100),
    "snore_motor_mode": saved_value(saved_defaults, "sm", "snore_motor_mode", "both"),
    "snore_duration": clamp(float(saved_value(saved_defaults, "sd", "snore_duration", 1.4)), 0.2, 5.0),
    "snore_taper": clamp(float(saved_value(saved_defaults, "st", "snore_taper", 0.7)), 0.0, 3.0),
    "snore_gap": clamp(float(saved_value(saved_defaults, "sg", "snore_gap", 3.6)), 0.0, 10.0),
    "upstream_enabled": truthy(saved_value(saved_defaults, "ne", "upstream_enabled", settings_upstream_enabled)),
    "upstream_ssid": saved_value(saved_defaults, "ns", "upstream_ssid", settings_upstream_ssid),
    "upstream_password": saved_credentials.get("up", settings_upstream_password),
    "network_behavior": saved_value(saved_defaults, "nb", "network_behavior", "ap_plus_sta"),
}
state = live_state
if state["active_mode"] not in ACTIVE_MODES:
    state["active_mode"] = "heart"
if state["heart_motor_mode"] not in HEART_MOTOR_MODES:
    state["heart_motor_mode"] = "tandem"
if state["purr_play_mode"] not in PURR_PLAY_MODES:
    state["purr_play_mode"] = "normal"
if state["purr_pattern"] == "alternating":
    state["purr_pattern"] = "alternate_left"
elif state["purr_pattern"] == "simultaneous":
    state["purr_pattern"] = "both"
if state["purr_pattern"] not in PURR_PATTERNS:
    state["purr_pattern"] = "alternate_left"
if state["snore_motor_mode"] not in SNORE_MOTOR_MODES:
    state["snore_motor_mode"] = "both"
state["heart_delay_ms"] = min(state["heart_delay_ms"], maximum_delay_ms(state["heart_bpm"]))

haptics = HapticController()
arbiter = OutputArbiter(haptics, state)
run_timer = RunTimer()

print("[HAPTIC] Active mode:", state["active_mode"])
print("[HAPTIC] Running: OFF")
ap_ssid = os.getenv("HEART_AP_SSID") or DEFAULT_AP_SSID
ap_password = os.getenv("HEART_AP_PASSWORD") or None
print("[WIFI] Starting dedicated control network")
print("[WIFI] SSID:")
print(ap_ssid)
try:
    if ap_password is None:
        wifi.radio.start_ap(ap_ssid)
        ap_security = "Open"
    else:
        wifi.radio.start_ap(ap_ssid, ap_password)
        ap_security = "WPA2"
    ap_ipv4 = ipaddress.ip_address(DEFAULT_AP_IPV4)
    wifi.radio.set_ipv4_address_ap(
        ipv4=ap_ipv4,
        netmask=ipaddress.ip_address(DEFAULT_AP_NETMASK),
        gateway=ap_ipv4,
    )
except Exception as error:
    haptics.stop_all()
    state["running"] = False
    print("[WIFI] ERROR: Access point failed to start:", error)
    print("[HEART] Running remains OFF")
    while True:
        time.sleep(1)
ip = ap_ipv4
print("[WIFI] Access point ready")
print("[WIFI] Security:", ap_security)
print("[WIFI] IP:", ip)
network_manager = NetworkManager(wifi.radio, state)
if state["upstream_enabled"]:
    if network_manager.connect():
        network_manager.test_internet()
        print("[WIFI] Heart Internet:", network_manager.internet_status)
elif wifi.radio.connected:
    network_manager.disconnect()
pool = socketpool.SocketPool(wifi.radio)
server = Server(pool, "/")
captive_server = Server(pool, "/")
captive_available = False
nat_api_names = [
    name for name in dir(wifi.radio)
    if "nat" in name.lower() or "napt" in name.lower() or "forward" in name.lower()
]
internet_passthrough_available = False
defaults_status = {"message": "", "saved": False}
runtime_metrics = {
    "loop_count": 0,
    "last_loop_gap_ms": 0.0,
    "max_loop_gap_ms": 0.0,
}
print("[NETWORK] NAT/NAPT/IP-forwarding bindings:", nat_api_names or "none")


HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Surrogate Heart</title>
<style>
:root{color-scheme:dark;--red:#ff4d67;--panel:#202126;--muted:#a9abb4;--green:#52d273;--blue:#5e8cff}*{box-sizing:border-box}body{margin:0;background:#101115;color:#fff;font-family:system-ui,sans-serif}main{max-width:480px;margin:auto;padding:16px}.card{background:var(--panel);border-radius:20px;padding:18px;margin-bottom:14px}h1{text-align:center;margin:2px 0 4px;font-size:28px}h2{font-size:14px;letter-spacing:.12em;color:var(--muted);margin:0 0 14px}.connection{text-align:center;color:var(--green);font-size:14px}.segments{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;margin:16px 0}.segments button.active{background:var(--red)}button,select,input[type=number],input[type=text],input[type=password]{min-height:48px;border:0;border-radius:12px;padding:10px 12px;font-size:16px;background:#383a43;color:#fff;font-weight:650}button.primary{background:var(--red)}button:disabled{opacity:.35}.row{display:flex;align-items:center;justify-content:space-between;gap:10px}.run{font-size:18px}.stat{text-align:center;font-size:48px;font-weight:750;margin:10px 0}.stat small{font-size:17px}.master{border:1px solid #555866;background:#292b33}.master .value{font-size:30px}.label{margin-top:13px}.value{text-align:center;font-size:21px;font-weight:700}input[type=range]{width:100%;height:40px;accent-color:var(--red)}select,input[type=number],input[type=text],input[type=password]{width:100%;background:#30323a}.hidden{display:none}.hint,.status{color:var(--muted);font-size:14px;line-height:1.5}.tests{display:grid;grid-template-columns:1fr 1fr;gap:8px}.timer-buttons{display:grid;grid-template-columns:1fr 1fr;gap:8px}.timer{text-align:center;font-size:27px;font-variant-numeric:tabular-nums;margin:12px}.dot,.notice{color:var(--green)}.bad,.error{color:#ff8798}.settingsButton{width:100%;margin-top:10px}
</style></head><body><main>
<section class="card"><h1>Surrogate Heart</h1><div class="connection">Connection: Direct Wi-Fi</div><div class="segments"><button id="modeHeart">Heart</button><button id="modePurr">Purring</button><button id="modeSnore">Snoring</button></div><div class="row run"><strong>Running</strong><button id="runToggle">OFF</button></div></section>
<section class="card master"><h2>MASTER POWER</h2><div id="masterValue" class="value">100%</div><input id="master" type="range" min="0" max="100" value="100"></section>

<div id="heartPanel">
<section class="card"><h2>HEART</h2><div class="stat">&#10084;&#65039; <span id="bpmStat">72</span> <small>BPM</small></div><div class="label">Heart Rate Preset</div><select id="heartPreset"><option value="custom">Custom</option></select><div class="label">BPM</div><div class="row"><button id="minus">&minus;</button><span id="bpmValue" class="value">72</span><button id="plus">+</button></div><input id="bpm" type="range" min="30" max="240" value="72"><p class="hint">Fictional tactile rhythm presets; not medical classifications.</p><div class="label">Motor Mode</div><select id="heartMotor"><option value="tandem">Tandem / Lub-Dub</option><option value="simultaneous">Both Motors</option><option value="motor1">Motor 1 Only</option><option value="motor2">Motor 2 Only</option></select><p id="heartHelp" class="hint"></p></section>
<section class="card"><h2>ADVANCED HEART TUNING</h2><div class="label">LUB Strength</div><div id="lubValue" class="value">65%</div><input id="lub" type="range" min="0" max="100" value="65"><div class="label">DUB Strength</div><div id="dubValue" class="value">80%</div><input id="dub" type="range" min="0" max="100" value="80"><div class="label">LUB &rarr; DUB Delay</div><div id="delayValue" class="value">100 ms</div><input id="delay" type="range" min="50" max="300" value="100"></section>
<section class="card"><h2>HEART TEST</h2><button id="testHeart" style="width:100%">Test One Heartbeat</button></section>
<section class="card"><h2>HEARTBEAT TIMER</h2><div class="timer-buttons"><button data-minutes="15">15 minutes</button><button data-minutes="30">30 minutes</button><button data-minutes="60">1 hour</button><button data-minutes="120">2 hours</button></div><div class="row label"><input id="customTime" type="number" min="1" value="30"><select id="customUnit"><option value="60">minutes</option><option value="3600">hours</option><option value="1">seconds (test)</option></select></div><button id="startTimer" class="primary" style="width:100%;margin-top:8px">Start Timer</button><div id="timerText" class="timer">No timer active</div><button id="cancelTimer" class="hidden" style="width:100%">Cancel Timer</button></section>
</div>

<div id="purrPanel" class="hidden"><section class="card"><h2>PURRING</h2><div class="stat">PURR <small id="purrStat">10.0 Hz</small></div><div class="label">Purr Intensity</div><div id="purrValue" class="value">45%</div><input id="purrIntensity" type="range" min="0" max="100" value="45"><div id="baselineControl"><div class="label">Baseline Power</div><div id="baselineValue" class="value">30%</div><input id="purrBaseline" type="range" min="0" max="100" value="30"><p class="hint">Continuous Left/Right baseline before Master Power scaling.</p></div><div class="label">Purr Speed</div><div id="purrRateValue" class="value">600 pulses/min &middot; 10.0 Hz</div><input id="purrRate" type="range" min="240" max="1200" step="10" value="600"><div class="label">Purr Play Mode</div><select id="purrPlayMode"><option value="slow">Soft</option><option value="normal">Normal</option><option value="fast">Crisp</option></select><p class="hint">Soft is smooth and shallow; Normal is balanced; Crisp is sharper and deeper. Each envelope scales with Purr Speed.</p><div class="label">Motor Mode</div><select id="purrPattern"><option value="alternate_left">Alternate Left</option><option value="alternate_right">Alternate Right</option><option value="both">Both Motors</option><option value="motor1">Motor 1 Only</option><option value="motor2">Motor 2 Only</option></select></section><section class="card"><h2>PURR TEST</h2><button id="testPurr" style="width:100%">Test Purr</button></section></div>

<div id="snorePanel" class="hidden"><section class="card"><h2>SNORING</h2><div class="stat">SNORE <small id="snoreStat">1.4 s</small></div><div class="label">Snore Intensity</div><div id="snoreValue" class="value">70%</div><input id="snoreIntensity" type="range" min="0" max="100" value="70"><div class="label">Motor Routing</div><select id="snoreMotor"><option value="motor1">Motor 1</option><option value="motor2">Motor 2</option><option value="both">Both Motors</option><option value="alternating">Alternating</option></select><p id="snoreHelp" class="hint"></p><div class="label">Snore Duration</div><div id="durationValue" class="value">1.4 s</div><input id="snoreDuration" type="range" min="0.2" max="5" step="0.05" value="1.4"><div class="label">Taper Time</div><div id="taperValue" class="value">0.7 s</div><input id="snoreTaper" type="range" min="0" max="3" step="0.05" value="0.7"><div class="label">Time Between Snores</div><div id="gapValue" class="value">3.6 s</div><input id="snoreGap" type="range" min="0" max="10" step="0.05" value="3.6"></section><section class="card"><h2>SNORE TEST</h2><button id="testSnore" style="width:100%">Test Snore</button></section></div>

<section class="card"><h2>MOTOR TESTS</h2><div class="tests"><button id="test1">Test Motor 1</button><button id="test2">Test Motor 2</button><button id="testBoth">Test Both</button></div></section>
<section class="card"><h2>HARDWARE</h2><div class="status"><div>Motor 1 <span id="motor1Status"></span></div><div>Motor 2 <span id="motor2Status"></span></div><div id="busStatus"></div></div></section>
<section class="card"><h2>BATTERY</h2><div class="status">Battery monitoring is not installed.</div></section>
<section id="connectionLauncher" class="card"><button id="connectionToggle" class="settingsButton">Connection Settings</button></section>
<section id="connectionPanel" class="card hidden"><h2>CONNECTION SETTINGS</h2><div class="status"><strong>Direct Control Network</strong><div id="networkName"></div><div id="directAddress"></div><p>The direct AP always remains available.</p></div><div class="row label"><label for="upstreamEnabled">Connect Heart to Wi-Fi</label><input id="upstreamEnabled" type="checkbox"></div><div id="upstreamFields"><div class="label">Network</div><input id="upstreamSsid" type="text" autocomplete="off" placeholder="Wi-Fi network name"><div class="label">Password</div><input id="upstreamPassword" type="password" autocomplete="new-password" placeholder="Leave blank to keep saved password"><button id="connectUpstream" class="primary settingsButton">Connect</button><button id="testInternet" class="settingsButton">Test Heart Internet</button><p class="hint">Turn Running OFF before Connect or Test. Manual SSID entry avoids a blocking scan that could stall haptic scheduling.</p></div><div id="upstreamStatus" class="status"></div><div id="passthroughStatus" class="status"></div><h2 style="margin-top:20px">SAVED DEFAULTS</h2><p class="hint">Live adjustments last for this powered session. They are written to persistent storage only when you press this button.</p><button id="saveDefaults" class="settingsButton">Save Current Settings as Defaults</button><p id="defaultsMessage" class="notice"></p><button id="connectionBack" class="settingsButton">Back to Controls</button></section>
</main><script>
const $=id=>document.getElementById(id);let current=null;let sending=false;const modeHelp={tandem:"Motor 1 = LUB / Motor 2 = DUB",simultaneous:"Both motors produce both contractions",motor1:"Heartbeat uses Motor 1 only",motor2:"Heartbeat uses Motor 2 only"};const snoreHelp={motor1:"Snoring uses Motor 1 only.",motor2:"Snoring uses Motor 2 only.",both:"Both motors reproduce the same snore together.",alternating:"The follow motor reaches full output before the lead motor fades. Direction reverses after each complete snore."};
async function post(path,values={}){sending=true;try{await fetch(path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(values)})}finally{sending=false}await refresh()}
function localValues(){$("masterValue").textContent=$("master").value+"%";$("bpmValue").textContent=$("bpm").value;$("bpmStat").textContent=$("bpm").value;$("lubValue").textContent=$("lub").value+"%";$("dubValue").textContent=$("dub").value+"%";$("delayValue").textContent=$("delay").value+" ms";$("heartHelp").textContent=modeHelp[$("heartMotor").value];$("purrValue").textContent=$("purrIntensity").value+"%";$("baselineValue").textContent=$("purrBaseline").value+"%";const ppm=Number($("purrRate").value),hz=(ppm/60).toFixed(1);$("purrRateValue").textContent=ppm+" pulses/min · "+hz+" Hz";$("purrStat").textContent=hz+" Hz";$("baselineControl").classList.toggle("hidden",!$("purrPattern").value.startsWith("alternate_"));$("snoreValue").textContent=$("snoreIntensity").value+"%";$("snoreHelp").textContent=snoreHelp[$("snoreMotor").value];$("durationValue").textContent=$("snoreDuration").value+" s";$("snoreStat").textContent=$("snoreDuration").value+" s";$("taperValue").textContent=$("snoreTaper").value+" s";$("gapValue").textContent=$("snoreGap").value+" s"}
function showMode(mode){$("heartPanel").classList.toggle("hidden",mode!=="heart");$("purrPanel").classList.toggle("hidden",mode!=="purring");$("snorePanel").classList.toggle("hidden",mode!=="snoring");$("modeHeart").classList.toggle("active",mode==="heart");$("modePurr").classList.toggle("active",mode==="purring");$("modeSnore").classList.toggle("active",mode==="snoring")}
function formatTime(seconds){const m=Math.floor(seconds/60),s=seconds%60;return m+":"+String(s).padStart(2,"0")}
render=data=>{current=data;if($("heartPreset").options.length===1)data.heart.rate_presets.forEach(p=>{const option=new Option(p.label,p.name);option.dataset.bpm=p.bpm;$("heartPreset").add(option)});if(document.activeElement?.type!=="range"){$("master").value=data.master_power;$("bpm").value=data.heart.bpm;$("lub").value=data.heart.lub_intensity;$("dub").value=data.heart.dub_intensity;$("delay").value=data.heart.lub_dub_delay_ms;$("purrIntensity").value=data.purring.intensity;$("purrBaseline").value=data.purring.baseline;$("purrRate").value=data.purring.rate_ppm;$("snoreIntensity").value=data.snoring.intensity;$("snoreDuration").value=data.snoring.duration;$("snoreTaper").value=data.snoring.taper;$("snoreGap").value=data.snoring.gap}$("heartPreset").value=data.heart.bpm_preset;$("heartMotor").value=data.heart.motor_mode;$("purrPlayMode").value=data.purring.play_mode;$("purrPattern").value=data.purring.motor_pattern;$("snoreMotor").value=data.snoring.motor_mode;$("runToggle").textContent=data.running?"ON":"OFF";$("runToggle").classList.toggle("primary",data.running);showMode(data.active_mode);localValues();const h=data.hardware;$("motor1Status").innerHTML=h.motor1_available?'<span class="dot">Ready</span>':'<span class="bad">Not detected</span>';$("motor2Status").innerHTML=h.motor2_available?'<span class="dot">Ready</span>':'<span class="bad">Not detected</span>';$("test1").disabled=!h.motor1_available;$("test2").disabled=!h.motor2_available;$("testBoth").disabled=!h.motor1_available&&!h.motor2_available;$("busStatus").textContent="Bus 1: "+h.motor1_bus+" / Bus 2: "+h.motor2_bus;$("networkName").textContent=data.network.ssid;$("directAddress").textContent=data.network.ip+":"+data.network.port;const u=data.network.upstream;if(document.activeElement!==$("upstreamSsid"))$("upstreamSsid").value=u.ssid||"";$("upstreamEnabled").checked=u.enabled;$("upstreamFields").classList.toggle("hidden",!u.enabled);$("upstreamStatus").innerHTML="<p><strong>Status:</strong> "+u.status+"</p>"+(u.station_ip?"<p>Station IP: "+u.station_ip+"</p>":"")+(u.signal_strength!==null?"<p>Signal: "+u.signal_strength+" dBm</p>":"")+(u.error?'<p class="error">'+u.error+"</p>":"")+"<p>Heart Internet: "+u.internet_status+"</p>";const pass=data.network.internet_passthrough;$("passthroughStatus").innerHTML="<p><strong>AP-client Internet passthrough:</strong> "+(pass.verified?"Verified":pass.available?"Capability found; not verified":"Not available in current CircuitPython API")+"</p>";$("defaultsMessage").textContent=data.defaults.message||"";const t=data.timer;$("timerText").textContent=t.active?"Stops in "+formatTime(t.remaining_seconds):(t.complete?"Timer complete":"No timer active");$("cancelTimer").classList.toggle("hidden",!t.active)};
async function refresh(){if(sending)return;try{const response=await fetch("/api/v1/state");render(await response.json())}catch(error){}}
function slider(id,endpoint,key){$(id).addEventListener("input",localValues);$(id).addEventListener("change",()=>post(endpoint,{[key]:$(id).value}))}
slider("master","/api/v1/master","master_power");slider("bpm","/api/v1/heart","bpm");slider("lub","/api/v1/heart","lub_intensity");slider("dub","/api/v1/heart","dub_intensity");slider("delay","/api/v1/heart","lub_dub_delay_ms");slider("purrIntensity","/api/v1/purring","intensity");slider("purrBaseline","/api/v1/purring","baseline");slider("purrRate","/api/v1/purring","rate_ppm");slider("snoreIntensity","/api/v1/snoring","intensity");slider("snoreDuration","/api/v1/snoring","duration");slider("snoreTaper","/api/v1/snoring","taper");slider("snoreGap","/api/v1/snoring","gap");
$("bpm").addEventListener("input",()=>{const match=Array.from($("heartPreset").options).find(option=>Number(option.dataset.bpm)===Number($("bpm").value));$("heartPreset").value=match?match.value:"custom"});
$("heartPreset").onchange=()=>{if($("heartPreset").value!=="custom")post("/api/v1/heart",{preset:$("heartPreset").value})};
$("heartMotor").onchange=()=>post("/api/v1/heart",{motor_mode:$("heartMotor").value});$("purrPlayMode").onchange=()=>post("/api/v1/purring",{play_mode:$("purrPlayMode").value});$("purrPattern").onchange=()=>{localValues();post("/api/v1/purring",{motor_pattern:$("purrPattern").value})};$("snoreMotor").onchange=()=>post("/api/v1/snoring",{motor_mode:$("snoreMotor").value});
$("minus").onclick=()=>{const value=Math.max(30,Number($("bpm").value)-1);$("bpm").value=value;localValues();post("/api/v1/heart",{bpm:value})};$("plus").onclick=()=>{const value=Math.min(240,Number($("bpm").value)+1);$("bpm").value=value;localValues();post("/api/v1/heart",{bpm:value})};
$("modeHeart").onclick=()=>post("/api/v1/mode",{mode:"heart"});$("modePurr").onclick=()=>post("/api/v1/mode",{mode:"purring"});$("modeSnore").onclick=()=>post("/api/v1/mode",{mode:"snoring"});$("runToggle").onclick=()=>post("/api/v1/run",{running:!current.running});
$("test1").onclick=()=>post("/api/v1/haptic/motor1/test");$("test2").onclick=()=>post("/api/v1/haptic/motor2/test");$("testBoth").onclick=()=>post("/api/v1/haptic/both/test");$("testHeart").onclick=()=>post("/api/v1/heartbeat/test");$("testPurr").onclick=()=>post("/api/v1/purring/test");$("testSnore").onclick=()=>post("/api/v1/snoring/test");$("connectionToggle").onclick=()=>{$("connectionPanel").classList.remove("hidden");$("connectionLauncher").classList.add("hidden");$("connectionPanel").scrollIntoView({behavior:"smooth"})};$("connectionBack").onclick=()=>{$("connectionPanel").classList.add("hidden");$("connectionLauncher").classList.remove("hidden")};$("upstreamEnabled").onchange=()=>post("/api/v1/network",{enabled:$("upstreamEnabled").checked,action:$("upstreamEnabled").checked?"configure":"disconnect"});$("connectUpstream").onclick=()=>post("/api/v1/network",{enabled:true,ssid:$("upstreamSsid").value,password:$("upstreamPassword").value,action:"connect"});$("testInternet").onclick=()=>post("/api/v1/network",{action:"test_internet"});$("saveDefaults").onclick=()=>post("/api/v1/defaults");
document.querySelectorAll("[data-minutes]").forEach(button=>button.onclick=()=>post("/api/v1/heartbeat/timer",{seconds:Number(button.dataset.minutes)*60}));$("startTimer").onclick=()=>post("/api/v1/heartbeat/timer",{seconds:Number($("customTime").value)*Number($("customUnit").value)});$("cancelTimer").onclick=async()=>{await fetch("/api/v1/heartbeat/timer",{method:"DELETE"});await refresh()};refresh();setInterval(refresh,1500);
</script></body></html>"""


def compact_preferences():
    return {
        "m": state["active_mode"],
        "p": state["master_power"],
        "b": state["heart_bpm"],
        "hm": state["heart_motor_mode"],
        "li": state["heart_lub"],
        "di": state["heart_dub"],
        "d": state["heart_delay_ms"],
        "pi": state["purr_intensity"],
        "pb": state["purr_baseline"],
        "pr": state["purr_rate"],
        "pc": state["purr_play_mode"],
        "pm": state["purr_pattern"],
        "si": state["snore_intensity"],
        "sm": state["snore_motor_mode"],
        "sd": state["snore_duration"],
        "st": state["snore_taper"],
        "sg": state["snore_gap"],
        "ne": state["upstream_enabled"],
        "ns": state["upstream_ssid"],
        "nb": state["network_behavior"],
    }


def compact_credentials():
    return {"up": state["upstream_password"]}


def public_state(now):
    return {
        "active_mode": state["active_mode"],
        "running": state["running"],
        "master_power": state["master_power"],
        "heart": {
            "bpm": state["heart_bpm"],
            "bpm_preset": bpm_preset(state["heart_bpm"]),
            "rate_presets": [
                {"name": name, "label": label, "bpm": bpm}
                for name, label, bpm in HEART_RATE_PRESETS
            ],
            "motor_mode": state["heart_motor_mode"],
            "lub_intensity": state["heart_lub"],
            "dub_intensity": state["heart_dub"],
            "lub_dub_delay_ms": state["heart_delay_ms"],
            "effective_delay_ms": int(arbiter.heart.effective_delay() * 1000),
        },
        "purring": {
            "intensity": state["purr_intensity"],
            "baseline": state["purr_baseline"],
            "rate": state["purr_rate"],
            "rate_ppm": state["purr_rate"],
            "rate_hz": round(state["purr_rate"] / 60.0, 1),
            "rate_min": PURR_RATE_MIN_PPM,
            "rate_max": PURR_RATE_MAX_PPM,
            "rate_min_ppm": PURR_RATE_MIN_PPM,
            "rate_max_ppm": PURR_RATE_MAX_PPM,
            "play_mode": state["purr_play_mode"],
            "motor_pattern": state["purr_pattern"],
            "play_mode_profiles": {
                "soft": {"duty": 0.80, "depth": 0.35, "transition": 0.20},
                "normal": {"duty": 0.60, "depth": 0.60, "transition": 0.10},
                "crisp": {"duty": 0.375, "depth": 0.85, "transition": 0.025},
            },
        },
        "snoring": {
            "intensity": state["snore_intensity"],
            "motor_mode": state["snore_motor_mode"],
            "duration": state["snore_duration"],
            "taper": state["snore_taper"],
            "gap": state["snore_gap"],
        },
        "snoring_runtime": {
            "normal_active": arbiter.snore.phase == SnoreScheduler.ACTIVE,
            "normal_direction": snore_direction_name(arbiter.snore.direction),
            "normal_next_direction": snore_direction_name(arbiter.snore.next_direction),
            "test_active": arbiter.test.active and arbiter.test.kind == "snore",
            "test_direction": snore_direction_name(arbiter.test.snore_direction),
            "test_next_direction": snore_direction_name(
                arbiter.test.next_test_snore_direction
            ),
        },
        "hardware": haptics.status(),
        "network": {
            "mode": "ap+sta" if state["upstream_enabled"] else "ap",
            "ssid": ap_ssid,
            "security": ap_security,
            "ip": str(ip),
            "port": HTTP_PORT,
            "captive_helper_available": captive_available,
            "direct_ap_active": wifi.radio.ap_active,
            "upstream": network_manager.public_status(),
            "network_behavior": state["network_behavior"],
            "internet_passthrough": {
                "available": internet_passthrough_available,
                "verified": False,
                "binding_candidates": nat_api_names,
                "reason": "No CircuitPython NAT/NAPT/IP-forwarding API is exposed" if not nat_api_names else "Candidate bindings require physical verification",
            },
        },
        "defaults": defaults_status,
        "timer": run_timer.status(now),
        "runtime": runtime_metrics,
    }


def changed(key, value):
    if state[key] == value:
        return False
    state[key] = value
    return True


def apply_master(params, now):
    if params.get("master_power") is not None:
        changed("master_power", clamp(int(params.get("master_power")), 0, 100))


def apply_heart(params, now):
    dirty = False
    selected_bpm = preset_bpm(params.get("preset"))
    if selected_bpm is not None:
        dirty = changed("heart_bpm", selected_bpm) or dirty
    if params.get("bpm") is not None:
        dirty = changed("heart_bpm", clamp(int(params.get("bpm")), 30, 240)) or dirty
    if params.get("motor_mode") in HEART_MOTOR_MODES:
        dirty = changed("heart_motor_mode", params.get("motor_mode")) or dirty
    if params.get("lub_intensity") is not None:
        dirty = changed("heart_lub", clamp(int(params.get("lub_intensity")), 0, 100)) or dirty
    if params.get("dub_intensity") is not None:
        dirty = changed("heart_dub", clamp(int(params.get("dub_intensity")), 0, 100)) or dirty
    if params.get("lub_dub_delay_ms") is not None:
        dirty = changed("heart_delay_ms", clamp(int(params.get("lub_dub_delay_ms")), 50, 300)) or dirty
    return dirty


def apply_purr(params, now):
    dirty = False
    if params.get("intensity") is not None:
        dirty = changed("purr_intensity", clamp(int(params.get("intensity")), 0, 100)) or dirty
    if params.get("baseline") is not None:
        dirty = changed("purr_baseline", clamp(int(params.get("baseline")), 0, 100)) or dirty
    requested_rate = params.get("rate_ppm")
    if requested_rate is None:
        requested_rate = params.get("rate")
    if requested_rate is not None:
        dirty = changed(
            "purr_rate",
            clamp(
                int(requested_rate),
                PURR_RATE_MIN_PPM,
                PURR_RATE_MAX_PPM,
            ),
        ) or dirty
    play_mode = params.get("play_mode") or params.get("speed")
    if play_mode in PURR_PLAY_MODES:
        dirty = changed("purr_play_mode", play_mode) or dirty
    if params.get("motor_pattern") in PURR_PATTERNS:
        dirty = changed("purr_pattern", params.get("motor_pattern")) or dirty
    return dirty


def apply_snore(params, now):
    dirty = False
    if params.get("intensity") is not None:
        dirty = changed("snore_intensity", clamp(int(params.get("intensity")), 0, 100)) or dirty
    if params.get("motor_mode") in SNORE_MOTOR_MODES:
        dirty = changed("snore_motor_mode", params.get("motor_mode")) or dirty
    if params.get("duration") is not None:
        dirty = changed("snore_duration", clamp(float(params.get("duration")), 0.2, 5.0)) or dirty
    if params.get("taper") is not None:
        dirty = changed("snore_taper", clamp(float(params.get("taper")), 0.0, 3.0)) or dirty
    if params.get("gap") is not None:
        dirty = changed("snore_gap", clamp(float(params.get("gap")), 0.0, 10.0)) or dirty
    return dirty


def apply_network(params):
    ssid_changed = False
    if params.get("ssid") is not None:
        ssid_changed = changed("upstream_ssid", str(params.get("ssid")).strip())
    # A blank password means "keep the current credential" so polling and
    # form rendering can never erase or disclose it accidentally. For a new
    # SSID, blank intentionally selects an open network.
    if params.get("password"):
        changed("upstream_password", str(params.get("password")))
    elif params.get("password") is not None and ssid_changed:
        changed("upstream_password", "")
    if params.get("behavior") == "ap_plus_sta":
        changed("network_behavior", "ap_plus_sta")
    if params.get("enabled") is not None:
        changed("upstream_enabled", truthy(params.get("enabled")))

    action = params.get("action")
    if action == "disconnect" or not state["upstream_enabled"]:
        network_manager.disconnect()
        return {"accepted": True}
    if action in ("connect", "test_internet") and (
        state["running"] or arbiter.test.active
    ):
        network_manager.error = "Turn Running OFF before a blocking Wi-Fi operation"
        return {
            "accepted": False,
            "error": network_manager.error,
        }
    if action == "connect":
        return {"accepted": network_manager.connect(), "error": network_manager.error}
    if action == "test_internet":
        return {"accepted": network_manager.test_internet()}
    return {"accepted": True}


@server.route("/", GET)
def home(request: Request):
    return Response(request, HTML, content_type="text/html")


@server.route("/ping", GET)
def ping(request: Request):
    return Response(request, "pong", content_type="text/plain")


@server.route("/api/v1/ping", GET)
def api_ping(request: Request):
    return json_response(request, {"ok": True})


@server.route("/api/v1/state", GET)
def api_state(request: Request):
    return json_response(request, public_state(time.monotonic()))


@server.route("/api/v1/mode", POST)
def api_mode(request: Request):
    now = time.monotonic()
    mode = request_data(request).get("mode")
    if arbiter.switch_mode(mode, now):
        run_timer.cancel()
    return json_response(request, public_state(now))


@server.route("/api/v1/run", POST)
def api_run(request: Request):
    now = time.monotonic()
    running_value = request_data(request).get("running")
    running = running_value is True or running_value in ("true", "1", "on")
    arbiter.set_running(running, now)
    return json_response(request, public_state(now))


@server.route("/api/v1/master", POST)
def api_master(request: Request):
    apply_master(request_data(request), time.monotonic())
    return json_response(request, public_state(time.monotonic()))


@server.route("/api/v1/heart", POST)
def api_heart(request: Request):
    apply_heart(request_data(request), time.monotonic())
    return json_response(request, public_state(time.monotonic()))


@server.route("/api/v1/purring", POST)
def api_purring(request: Request):
    apply_purr(request_data(request), time.monotonic())
    return json_response(request, public_state(time.monotonic()))


@server.route("/api/v1/snoring", POST)
def api_snoring(request: Request):
    data = request_data(request)
    motor_mode = data.get("motor_mode")
    if motor_mode is not None and motor_mode not in SNORE_MOTOR_MODES:
        return json_response(request, {
            "error": "invalid motor_mode",
            "valid": SNORE_MOTOR_MODES,
        })
    apply_snore(data, time.monotonic())
    return json_response(request, public_state(time.monotonic()))


@server.route("/api/v1/defaults", POST)
def api_save_defaults(request: Request):
    saved = preferences.save_defaults(compact_preferences(), compact_credentials())
    defaults_status["saved"] = saved
    defaults_status["message"] = "Defaults saved" if saved else "Defaults could not be saved"
    print("[PREFS] Save Defaults requested; motor output unchanged")
    return json_response(request, public_state(time.monotonic()))


@server.route("/api/v1/network", POST)
def api_network(request: Request):
    result = apply_network(request_data(request))
    response = public_state(time.monotonic())
    response["network_action"] = result
    return json_response(request, response)


def begin_test(kind):
    now = time.monotonic()
    arbiter.start_test(kind, now)
    return now


@server.route("/api/v1/haptic/motor1/test", POST)
def test_motor1(request: Request):
    begin_test("motor1")
    return json_response(request, {"accepted": haptics.motor1.available})


@server.route("/api/v1/haptic/motor2/test", POST)
def test_motor2(request: Request):
    begin_test("motor2")
    return json_response(request, {"accepted": haptics.motor2.available})


@server.route("/api/v1/haptic/both/test", POST)
def test_both(request: Request):
    begin_test("both")
    return json_response(request, {"accepted": haptics.motor1.available or haptics.motor2.available})


@server.route("/api/v1/heartbeat/test", POST)
def test_heartbeat(request: Request):
    begin_test("heart")
    return json_response(request, {"accepted": haptics.motor1.available or haptics.motor2.available})


@server.route("/api/v1/purring/test", POST)
def test_purring(request: Request):
    begin_test("purr")
    return json_response(request, {"accepted": haptics.motor1.available or haptics.motor2.available})


@server.route("/api/v1/snoring/test", POST)
def test_snoring(request: Request):
    begin_test("snore")
    return json_response(request, {"accepted": haptics.motor1.available or haptics.motor2.available})


if SAFETY_DEBUG:
    def debug_haptic_fault(request: Request):
        """Development-only lease-expiry test; never registered in production."""
        now = time.monotonic()
        haptics.set_motor_1(
            scaled_strength(30, state["master_power"]),
            now + 0.050,
            "debug_network_exception",
        )
        raise RuntimeError("intentional haptic fail-safe diagnostic")

    server.route("/api/v1/debug/haptic-fault", POST)(debug_haptic_fault)


@server.route("/api/v1/heartbeat/timer", GET)
def get_timer(request: Request):
    return json_response(request, run_timer.status(time.monotonic()))


@server.route("/api/v1/heartbeat/timer", POST)
def start_timer(request: Request):
    seconds = request_data(request).get("seconds")
    if seconds is None:
        return json_response(request, {"error": "seconds is required"})
    run_timer.start(int(float(seconds)), time.monotonic())
    return json_response(request, run_timer.status(time.monotonic()))


@server.route("/api/v1/heartbeat/timer", DELETE)
def cancel_timer(request: Request):
    run_timer.cancel()
    return json_response(request, run_timer.status(time.monotonic()))


@server.route("/api/v1/heartbeat", POST)
def compatibility_heartbeat(request: Request):
    now = time.monotonic()
    params = request.query_params
    if params.get("effect_mode") in ACTIVE_MODES and params.get("effect_mode") != state["active_mode"]:
        arbiter.switch_mode(params.get("effect_mode"), now)
    apply_heart(params, now)
    if params.get("snore_intensity") is not None:
        apply_snore({"intensity": params.get("snore_intensity")}, now)
    return json_response(request, public_state(now))


@server.route("/toggle", POST)
def legacy_toggle(request: Request):
    arbiter.set_running(not state["running"], time.monotonic())
    return Response(request, "on" if state["running"] else "off")


@server.route("/settings", POST)
def legacy_settings(request: Request):
    now = time.monotonic()
    params = request.query_params
    mode = params.get("mode")
    if mode in ACTIVE_MODES and mode != state["active_mode"]:
        arbiter.switch_mode(mode, now)
    apply_heart({"bpm": params.get("bpm")}, now)
    apply_snore({
        "intensity": params.get("intensity"),
        "duration": params.get("duration"),
        "taper": params.get("taper"),
        "gap": params.get("gap"),
    }, now)
    return Response(request, "ok")


@server.route("/test", POST)
def legacy_test(request: Request):
    return test_both(request)


CAPTIVE_HTML = """<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><title>Surrogate Heart</title></head><body style="font-family:sans-serif;text-align:center;padding:2rem"><h1>Surrogate Heart</h1><p>You're connected directly to the device.</p><p><a style="display:inline-block;padding:1rem;background:#c33;color:white;border-radius:.7rem;text-decoration:none" href="http://{}:{}/">Open Controller</a></p><p>No Internet is expected on this network.</p></body></html>""".format(ip, HTTP_PORT)


def captive_landing(request: Request):
    if CAPTIVE_DEBUG:
        print("[CAPTIVE] Probe request")
    return Response(request, CAPTIVE_HTML, content_type="text/html")


captive_server.route("/", GET)(captive_landing)
captive_server.route("/hotspot-detect.html", GET)(captive_landing)
captive_server.route("/library/test/success.html", GET)(captive_landing)
captive_server.route("/generate_204", GET)(captive_landing)
captive_server.route("/gen_204", GET)(captive_landing)
captive_server.route("/connecttest.txt", GET)(captive_landing)
captive_server.route("/ncsi.txt", GET)(captive_landing)


server.start(str(ip), HTTP_PORT)
print("[HTTP] Main server: {}:{}".format(ip, HTTP_PORT))
print("[HTTP] Ping: http://{}:{}/ping".format(ip, HTTP_PORT))
try:
    captive_server.start(str(ip), 80)
    captive_available = True
    print("[CAPTIVE] Port 80 helper ready")
except Exception as error:
    print("[CAPTIVE] WARNING: Port 80 helper unavailable:", error)

application_watchdog = None
try:
    application_watchdog = microcontroller.watchdog
    application_watchdog.timeout = APPLICATION_WATCHDOG_TIMEOUT_SECONDS
    application_watchdog.mode = watchdog.WatchDogMode.RESET
    application_watchdog.feed()
    print(
        "[SAFETY] Hardware watchdog armed at {:.1f}s".format(
            APPLICATION_WATCHDOG_TIMEOUT_SECONDS
        )
    )
except Exception as error:
    application_watchdog = None
    print("[SAFETY] WARNING: Hardware watchdog unavailable:", error)
print("[HTTP] Controller:")
print("http://{}:{}/".format(ip, HTTP_PORT))
boot_haptic_status = haptics.status()
print(
    "[SAFETY] Initialization motor activations: {}/{}".format(
        boot_haptic_status["motor1_activations"],
        boot_haptic_status["motor2_activations"],
    )
)
if (
    boot_haptic_status["motor1_available"]
    or boot_haptic_status["motor2_available"]
):
    startup_complete = time.monotonic()
    arbiter.set_running(True, startup_complete)
    print("[STARTUP] Complete after {:.2f} seconds".format(startup_complete - boot_started))
    print("[MODE] {} / ON (saved default)".format(state["active_mode"]))
    print("[HAPTIC] First event armed through normal scheduler")
    startup_report_pending = True
else:
    arbiter.stop_all(time.monotonic())
    print("[HAPTIC] ERROR: No haptic drivers available")
    print("[MODE] {} / OFF".format(state["active_mode"]))
    startup_report_pending = False

try:
    last_loop_at = time.monotonic()
    network_refresh_at = last_loop_at
    while True:
        loop_now = time.monotonic()
        loop_gap_ms = (loop_now - last_loop_at) * 1000.0
        last_loop_at = loop_now
        runtime_metrics["loop_count"] += 1
        runtime_metrics["last_loop_gap_ms"] = round(loop_gap_ms, 3)
        if loop_gap_ms > runtime_metrics["max_loop_gap_ms"]:
            runtime_metrics["max_loop_gap_ms"] = round(loop_gap_ms, 3)
        if application_watchdog is not None:
            application_watchdog.feed()
        if haptics.check_failsafe(loop_now):
            arbiter.stop_all(loop_now)
        try:
            server.poll()
        except Exception as error:
            print("[HTTP] Request recovered:", error)
        now = time.monotonic()
        if haptics.check_failsafe(now):
            arbiter.stop_all(now)
        if captive_available:
            try:
                captive_server.poll()
            except Exception as error:
                print("[CAPTIVE] Request recovered:", error)
        now = time.monotonic()
        if haptics.check_failsafe(now):
            arbiter.stop_all(now)
        run_timer.update(now, arbiter)
        if now >= network_refresh_at:
            network_manager.refresh()
            network_refresh_at = now + 0.50
        arbiter.update(now)
        if startup_report_pending:
            current_haptic_status = haptics.status()
            motor1_delta = (
                current_haptic_status["motor1_activations"]
                - boot_haptic_status["motor1_activations"]
            )
            motor2_delta = (
                current_haptic_status["motor2_activations"]
                - boot_haptic_status["motor2_activations"]
            )
            if motor1_delta > 0 or motor2_delta > 0:
                print(
                    "[HAPTIC] First intentional {} output at {:.2f} seconds; activations {}/{}".format(
                        state["active_mode"],
                        now - boot_started,
                        motor1_delta,
                        motor2_delta,
                    )
                )
                startup_report_pending = False
except BaseException as error:
    state["running"] = False
    try:
        haptics.stop_all()
    except Exception as stop_error:
        print("[FATAL] Motor shutdown warning:", stop_error)
    print("[FATAL] Both motors commanded OFF:", repr(error))
    print("[FATAL] Runtime metrics:", runtime_metrics)
    print("[FATAL] Haptic status after shutdown:", haptics.status())
    raise
