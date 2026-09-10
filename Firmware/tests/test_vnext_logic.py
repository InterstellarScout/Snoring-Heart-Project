import ast
import pathlib
import re
import json
import types
import unittest

SOURCE_PATH = (
    pathlib.Path(__file__).parents[1]
    / "circuitpython"
    / "lib"
    / "surrogate_heart_v03.py"
)
SOURCE = SOURCE_PATH.read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)
dual_button = types.SimpleNamespace(
    BLUE_SINGLE="BLUE_SINGLE",
    BLUE_DOUBLE="BLUE_DOUBLE",
    RED_SINGLE="RED_SINGLE",
    RED_DOUBLE="RED_DOUBLE",
    BOTH_SHORT="BOTH_SHORT",
    BOTH_LONG="BOTH_LONG",
)


def load_definitions(names, namespace=None):
    selected = [
        node
        for node in TREE.body
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names
    ]
    values = {} if namespace is None else dict(namespace)
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(SOURCE_PATH), "exec"), values)
    return values


class FakeHaptics:
    def __init__(self):
        self.outputs = []
        self.stop_count = 0

    def set_purr(self, mode, baseline, modulation, expected_until, reason):
        self.outputs.append(
            (mode, baseline, modulation, expected_until, reason)
        )

    def stop_all(self):
        self.stop_count += 1


class FakeScheduler:
    def __init__(self, haptics, state):
        self.arm_times = []
        self.reset_times = []

    def arm(self, now):
        self.arm_times.append(now)

    def reset(self, now, preserve_output=False):
        self.reset_times.append((now, preserve_output))

    def update(self, now):
        pass


class FakeTestScheduler(FakeScheduler):
    def __init__(self, haptics, state):
        super().__init__(haptics, state)
        self.active = False

    def cancel(self):
        self.active = False

    def start(self, kind, now):
        self.active = True


class VNextLogicTests(unittest.TestCase):
    def test_scd43_detection_starts_cooperative_polling(self):
        class Bus:
            def try_lock(self):
                return True

            def scan(self):
                return [0x62]

            def unlock(self):
                pass

        class Sensor:
            def __init__(self, i2c):
                self.started = False
                self.data_ready = True
                self.CO2 = 777
                self.temperature = 23.5
                self.relative_humidity = 44.25

            def start_periodic_measurement(self):
                self.started = True

        clock = types.SimpleNamespace(monotonic=lambda: 10.0)
        values = load_definitions(
            {"scan_bus", "SCD43Monitor"},
            {
                "board": types.SimpleNamespace(STEMMA_I2C=lambda: Bus()),
                "adafruit_scd4x": types.SimpleNamespace(SCD4X=Sensor),
                "SCD43_ADDRESS": 0x62,
                "SCD43_POLL_SECONDS": 1.0,
                "time": clock,
            },
        )
        monitor = values["SCD43Monitor"]()
        self.assertTrue(monitor.detected)
        self.assertTrue(monitor.initialized)
        monitor.update(11.0)
        self.assertEqual(monitor.public_status()["co2_ppm"], 777)
        self.assertEqual(monitor.read_count, 1)

    def test_button_events_use_live_state_setters_and_preserve_settings(self):
        state = {
            "active_mode": "heart",
            "running": True,
            "master_power": 50,
            "heart_bpm": 72,
            "heart_delay_ms": 100,
            "snore_duration": 1.4,
            "snore_gap": 3.6,
            "breath_rate": 12,
            "purr_rate": 600,
            "button_inputs_inverted": False,
        }

        class Arbiter:
            def switch_mode(self, mode, now):
                if mode == state["active_mode"]:
                    return False
                state["active_mode"] = mode
                return True

            def set_running(self, running, now):
                state["running"] = running

        class Timer:
            def cancel(self):
                pass

        values = load_definitions(
            {
                "clamp", "maximum_delay_ms", "changed", "truthy", "set_master_power",
                "set_heart_bpm", "set_snore_gap", "set_breath_rate",
                "set_mode", "set_running", "next_button_mode",
                "adjust_active_mode_speed", "effective_button_event",
                "handle_button_event", "apply_controls",
            },
            {
                "state": state,
                "arbiter": Arbiter(),
                "run_timer": Timer(),
                "dual_button": dual_button,
                "BUTTON_MODE_SEQUENCE": ("heart", "purring", "snoring", "breathing"),
                "MASTER_STEP": 5,
                "HEART_BUTTON_BPM_STEP": 5,
                "SNORE_BUTTON_CADENCE_STEP": 0.10,
                "BREATH_BUTTON_RATE_STEP_BPM": 1,
                "BREATH_RATE_MIN_BPM": 6,
                "BREATH_RATE_MAX_BPM": 30,
                "PURR_BUTTON_RATE_STEP_PPM": 10,
                "PURR_RATE_MIN_PPM": 240,
                "PURR_RATE_MAX_PPM": 1200,
                "LUB_DURATION": 0.070,
                "DUB_DURATION": 0.090,
                "MIN_BEAT_REST": 0.010,
            },
        )
        handle = values["handle_button_event"]
        handle(dual_button.BLUE_SINGLE, 1.0)
        self.assertEqual(state["master_power"], 55)
        handle(dual_button.RED_SINGLE, 1.1)
        self.assertEqual(state["master_power"], 50)

        # Inversion swaps individual inputs but never changes BOTH semantics.
        values["apply_controls"]({"inverted": True}, 1.2)
        handle(dual_button.RED_SINGLE, 1.3)
        self.assertEqual(state["master_power"], 55)
        values["apply_controls"]({"inverted": False}, 1.4)

        # A web-side value is authoritative for the next physical adjustment.
        state["heart_bpm"] = 95
        handle(dual_button.RED_DOUBLE, 2.0)
        self.assertEqual(state["heart_bpm"], 90)
        handle(dual_button.BLUE_DOUBLE, 2.1)
        self.assertEqual(state["heart_bpm"], 95)

        retained = (state["master_power"], state["heart_bpm"])
        handle(dual_button.BOTH_LONG, 3.0)
        self.assertFalse(state["running"])
        self.assertEqual((state["master_power"], state["heart_bpm"]), retained)
        handle(dual_button.BOTH_SHORT, 3.1)
        self.assertEqual(state["active_mode"], "purring")
        self.assertFalse(state["running"])
        handle(dual_button.BOTH_SHORT, 3.15)
        self.assertEqual(state["active_mode"], "snoring")
        handle(dual_button.BLUE_DOUBLE, 3.2)
        self.assertLess(state["snore_gap"], 3.6)
        handle(dual_button.BOTH_SHORT, 3.3)
        self.assertEqual(state["active_mode"], "breathing")
        handle(dual_button.BLUE_DOUBLE, 3.4)
        self.assertEqual(state["breath_rate"], 13)
        handle(dual_button.RED_DOUBLE, 3.5)
        self.assertEqual(state["breath_rate"], 12)
        handle(dual_button.BOTH_LONG, 6.5)
        self.assertTrue(state["running"])
        self.assertEqual((state["master_power"], state["heart_bpm"]), retained)

    def test_breathing_scheduler_has_zero_start_and_boundary_updates(self):
        class BreathingHaptics:
            def __init__(self):
                self.outputs = []
                self.stops = 0

            def stop_all(self):
                self.stops += 1

            def set_breathing(self, mode, strength, direction, expected_until, reason):
                self.outputs.append((mode, strength, direction, expected_until, reason))

        values = load_definitions(
            {"clamp", "scaled_strength", "breathing_envelope", "BreathingScheduler"},
            {
                "BREATH_RATE_DEFAULT_BPM": 12,
                "HAPTIC_CONTINUOUS_LEASE_SECONDS": 0.15,
            },
        )
        state = {
            "breath_rate": 12,
            "breath_intensity": 80,
            "breath_motor_mode": "both",
            "master_power": 50,
        }
        haptics = BreathingHaptics()
        scheduler = values["BreathingScheduler"](haptics, state)
        scheduler.arm(0.0)
        scheduler.update(0.0)
        self.assertEqual(haptics.outputs[-1][1], 0)
        scheduler.update(2.5)
        self.assertEqual(haptics.outputs[-1][1], 40)
        original_deadline = scheduler.deadline
        state["breath_rate"] = 15
        scheduler.update(3.0)
        self.assertEqual(scheduler.deadline, original_deadline)
        scheduler.update(5.0)
        self.assertEqual(scheduler.interval, 4.0)
        self.assertEqual(haptics.outputs[-1][1], 0)

    def test_default_snore_phases_and_full_follow_handoff(self):
        values = load_definitions(
            {"clamp", "alternating_snore_phase_fractions", "alternating_snore_envelopes"},
            {"SNORE_LEAD_RISE_END": 0.15},
        )
        fractions = values["alternating_snore_phase_fractions"](1.4, 0.7)
        self.assertEqual(tuple(round(value, 4) for value in fractions), (0.15, 0.15, 0.20, 0.25, 0.25))

        envelope = values["alternating_snore_envelopes"]
        lead, follow = envelope(0.50 * 1.4, 1.4, 0.7)
        self.assertAlmostEqual(lead, 1.0)
        self.assertEqual(follow, 1.0)
        lead, follow = envelope(0.625 * 1.4, 1.4, 0.7)
        self.assertAlmostEqual(lead, 0.5)
        self.assertEqual(follow, 1.0)
        lead, follow = envelope(0.875 * 1.4, 1.4, 0.7)
        self.assertEqual(lead, 0.0)
        self.assertAlmostEqual(follow, 0.5)

        for index in range(101):
            lead, follow = envelope(index * 1.4 / 100, 1.4, 0.7)
            if lead < 1.0 and lead > 0.0 and index > 50:
                self.assertEqual(follow, 1.0)

    def test_taper_control_changes_normalized_fade_length(self):
        values = load_definitions(
            {"clamp", "alternating_snore_phase_fractions"}
        )
        abrupt = values["alternating_snore_phase_fractions"](2.0, 0.0)
        smooth = values["alternating_snore_phase_fractions"](2.0, 1.0)
        self.assertEqual(tuple(round(value, 2) for value in abrupt), (0.15, 0.15, 0.50, 0.10, 0.10))
        self.assertEqual(tuple(round(value, 2) for value in smooth), (0.15, 0.15, 0.20, 0.25, 0.25))
        self.assertAlmostEqual(sum(smooth), 1.0)

    def test_directional_purr_scaling_and_rate_change_do_not_force_pulse(self):
        values = load_definitions(
            {
                "clamp",
                "scaled_strength",
                "purr_modulation_strength",
                "PurrScheduler",
            },
            {
                "PURR_RATE_DEFAULT_PPM": 600,
                "PURR_PLAY_MODE_CONFIG": {
                    "slow": (0.80, 0.35, 0.20),
                    "normal": (0.60, 0.60, 0.10),
                    "fast": (0.375, 0.85, 0.025),
                },
                "HAPTIC_CONTINUOUS_LEASE_SECONDS": 0.15,
            },
        )
        state = {
            "purr_rate": 600,
            "purr_play_mode": "normal",
            "purr_baseline": 30,
            "purr_intensity": 70,
            "purr_pattern": "alternate_left",
            "master_power": 50,
        }
        haptics = FakeHaptics()
        scheduler = values["PurrScheduler"](haptics, state)
        scheduler.arm(0.0)
        scheduler.update(0.0)
        self.assertEqual(haptics.outputs[-1][:3], ("alternate_left", 15, 14))
        scheduler.update(0.05)
        self.assertEqual(haptics.outputs[-1][:3], ("alternate_left", 15, 35))
        original_deadline = scheduler.deadline
        state["purr_rate"] = 900
        scheduler.update(0.075)
        self.assertEqual(scheduler.deadline, original_deadline)
        self.assertEqual(scheduler.interval, 0.1)
        scheduler.update(0.10)
        self.assertAlmostEqual(scheduler.interval, 60.0 / 900)
        self.assertEqual(scheduler.pulse_index, 1)

    def test_left_and_right_outputs_are_mirrored(self):
        values = load_definitions({"HapticController"})
        controller = object.__new__(values["HapticController"])
        calls = []
        controller.set_both = (
            lambda left, right=None, expected_until=None, reason=None:
            calls.append((left, right))
        )
        controller.set_purr("alternate_left", 30, 70, 1.0, "test")
        controller.set_purr("alternate_right", 30, 70, 1.0, "test")
        self.assertEqual(calls, [(30, 70), (70, 30)])

    def test_purr_rate_migration_and_all_target_frequencies(self):
        values = load_definitions(
            {"normalized_purr_rate"},
            {
                "PURR_RATE_MIN_PPM": 240,
                "PURR_RATE_MAX_PPM": 1200,
                "PURR_RATE_DEFAULT_PPM": 600,
            },
        )
        normalize = values["normalized_purr_rate"]
        self.assertEqual(normalize("slow"), 600)
        self.assertEqual(normalize(72), 600)
        for rate in (240, 360, 600, 900, 1200):
            self.assertEqual(normalize(rate), rate)
            self.assertAlmostEqual(60.0 / rate, 1.0 / (rate / 60.0))

    def test_purr_profiles_have_rate_relative_duty_and_depth(self):
        config = {
            "slow": (0.80, 0.35, 0.20),
            "normal": (0.60, 0.60, 0.10),
            "fast": (0.375, 0.85, 0.025),
        }
        values = load_definitions(
            {"clamp", "purr_modulation_strength"},
            {"PURR_PLAY_MODE_CONFIG": config},
        )
        output = values["purr_modulation_strength"]
        interval = 0.1
        self.assertEqual(output(0.05, interval, 60, "slow"), 60)
        self.assertEqual(output(0.05, interval, 60, "normal"), 60)
        self.assertEqual(output(0.05, interval, 60, "fast"), 9)
        self.assertEqual(output(0.095, interval, 60, "slow"), 44)
        self.assertEqual(output(0.095, interval, 60, "normal"), 24)
        self.assertEqual(output(0.095, interval, 60, "fast"), 9)

        scheduler_node = next(
            node for node in TREE.body
            if isinstance(node, ast.ClassDef) and node.name == "PurrScheduler"
        )
        scheduler_source = ast.get_source_segment(SOURCE, scheduler_node)
        self.assertNotIn("sleep(", scheduler_source)

    def test_haptic_lease_forces_both_outputs_off_after_margin(self):
        values = load_definitions(
            {"HapticController"},
            {"HAPTIC_SAFETY_MARGIN_SECONDS": 0.35},
        )

        class Motor:
            def __init__(self):
                self.current_strength = 50
                self.stop_count = 0

            def stop(self):
                self.current_strength = 0
                self.stop_count += 1

        controller = object.__new__(values["HapticController"])
        controller.motor1 = Motor()
        controller.motor2 = Motor()
        controller.output_expected_until = 1.0
        controller.output_lease_deadline = 1.35
        controller.output_lease_reason = "unit_test"
        controller.failsafe_count = 0
        controller.last_failsafe_reason = ""
        self.assertFalse(controller.check_failsafe(1.35))
        self.assertTrue(controller.check_failsafe(1.351))
        self.assertEqual(controller.motor1.current_strength, 0)
        self.assertEqual(controller.motor2.current_strength, 0)
        self.assertEqual(controller.failsafe_count, 1)

    def test_mode_switch_preserves_running_state(self):
        values = load_definitions(
            {"OutputArbiter"},
            {
                "ACTIVE_MODES": ("heart", "purring", "snoring", "breathing"),
                "HeartScheduler": FakeScheduler,
                "PurrScheduler": FakeScheduler,
                "SnoreScheduler": FakeScheduler,
                "BreathingScheduler": FakeScheduler,
                "TestScheduler": FakeTestScheduler,
            },
        )
        state = {"active_mode": "heart", "running": True}
        haptics = FakeHaptics()
        arbiter = values["OutputArbiter"](haptics, state)
        self.assertTrue(arbiter.switch_mode("purring", 4.0))
        self.assertEqual(state, {"active_mode": "purring", "running": True})
        self.assertEqual(arbiter.purr.arm_times, [4.0])
        self.assertGreaterEqual(haptics.stop_count, 2)

        arbiter.set_running(False, 5.0)
        self.assertTrue(arbiter.switch_mode("breathing", 6.0))
        self.assertEqual(state, {"active_mode": "breathing", "running": False})
        self.assertEqual(arbiter.breathing.arm_times, [])

    def test_live_changes_have_no_implicit_preference_write(self):
        self.assertNotIn("preferences.schedule", SOURCE)
        self.assertNotIn("preferences.update", SOURCE)
        self.assertIn('@server.route("/api/v1/defaults", POST)', SOURCE)
        self.assertIn("preferences.save_defaults(compact_preferences(), compact_credentials())", SOURCE)

    def test_defaults_and_credentials_round_trip_in_separate_regions(self):
        fake_microcontroller = types.SimpleNamespace(nvm=bytearray(512))
        values = load_definitions(
            {"PreferenceStore"},
            {
                "microcontroller": fake_microcontroller,
                "json": json,
                "PREFERENCE_PREFIX": b"SH3|",
                "PREFERENCE_BYTES": 384,
                "CREDENTIAL_PREFIX": b"SHC1|",
                "CREDENTIAL_OFFSET": 384,
                "CREDENTIAL_BYTES": 128,
            },
        )
        store = values["PreferenceStore"]()
        defaults = {"p": 47, "b": 91, "pr": 76, "pm": "alternate_left"}
        credentials = {"up": "private-test-password"}
        self.assertTrue(store.save_defaults(defaults, credentials))
        self.assertEqual(store.load_defaults(), defaults)
        self.assertEqual(store.load_credentials(), credentials)
        self.assertNotIn(b"private-test-password", bytes(fake_microcontroller.nvm[:384]))
        self.assertNotIn(b'"p": 47', bytes(fake_microcontroller.nvm[384:]))

    def test_password_is_not_part_of_public_state(self):
        public_node = next(
            node for node in TREE.body
            if isinstance(node, ast.FunctionDef) and node.name == "public_state"
        )
        public_source = ast.get_source_segment(SOURCE, public_node)
        self.assertNotIn("upstream_password", public_source)

    def test_every_javascript_element_reference_exists(self):
        html_node = next(
            node for node in TREE.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "HTML" for target in node.targets)
        )
        html = ast.literal_eval(html_node.value)
        ids = set(re.findall(r'id="([^"]+)"', html))
        references = set(re.findall(r'\$\("([^"]+)"\)', html))
        self.assertEqual(references - ids, set())

    def test_button_instructions_are_collapsed_colorless_table(self):
        html_node = next(
            node for node in TREE.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "HTML" for target in node.targets)
        )
        html = ast.literal_eval(html_node.value)
        match = re.search(
            r'<details class="card instructions"([^>]*)>(.*?)</details>',
            html,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        self.assertNotIn("open", match.group(1))
        panel = match.group(2)
        self.assertIn("<summary>Button Instructions</summary>", panel)
        self.assertIn("<table>", panel)
        self.assertIn('id="invertButtons"', panel)
        self.assertNotRegex(panel.lower(), r"\bblue\b|\bred\b")

    def test_discovery_and_connection_button_layout(self):
        self.assertNotIn("import " + "mdns", SOURCE)
        self.assertNotIn("." + "local", SOURCE)
        html_node = next(
            node for node in TREE.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "HTML" for target in node.targets)
        )
        html = ast.literal_eval(html_node.value)
        self.assertEqual(html.count('id="connectionToggle"'), 1)
        self.assertGreater(html.index('id="connectionLauncher"'), html.index("HARDWARE"))

    def test_instructions_are_collapsed_and_match_button_controls(self):
        html_node = next(
            node for node in TREE.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "HTML" for target in node.targets)
        )
        html = ast.literal_eval(html_node.value)
        self.assertIn(
            '<details class="card instructions"><summary>Button Instructions</summary><table>',
            html,
        )
        self.assertNotIn('<details open', html)
        self.assertIn("Heart &rarr; Purring &rarr; Snoring &rarr; Breathing", html)
        self.assertIn("Top: single-click", html)
        self.assertIn("Bottom: double-click", html)
        self.assertIn("resume the last mode and settings", html)
        self.assertIn('id="invertButtons"', html)
        self.assertNotIn("Blue/top", html)
        self.assertNotIn("Red/bottom", html)

    def test_settings_and_network_actions_have_no_motor_commands(self):
        guarded = {"api_save_defaults", "api_network", "apply_network"}
        for node in TREE.body:
            if isinstance(node, ast.FunctionDef) and node.name in guarded:
                text = ast.get_source_segment(SOURCE, node)
                self.assertNotIn("set_motor", text)
                self.assertNotIn("set_pattern", text)
                self.assertNotIn("set_purr", text)
                self.assertNotIn("set_snore", text)


if __name__ == "__main__":
    unittest.main()
