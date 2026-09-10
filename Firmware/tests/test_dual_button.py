import importlib.util
import pathlib
import unittest


ROOT = pathlib.Path(__file__).parents[1]
MODULE_PATH = ROOT / "circuitpython" / "lib" / "dual_button.py"
SPEC = importlib.util.spec_from_file_location("dual_button", MODULE_PATH)
dual_button = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dual_button)


class Pin:
    def __init__(self, value=True):
        self.value = value


class GestureHarness:
    def __init__(self, blue=True, red=True):
        self.now = 0.0
        self.blue = Pin(blue)
        self.red = Pin(red)
        self.controller = dual_button.DualButtonController(
            self.blue,
            self.red,
            active_level=False,
            now=self.now,
            debounce_seconds=0.040,
            double_click_seconds=0.350,
            chord_seconds=0.120,
            long_hold_seconds=3.0,
        )
        self.events = []
        self.step(0.001)

    def step(self, seconds):
        self.now += seconds
        event = self.controller.update(self.now)
        if event:
            self.events.append(event)
        return event

    def set(self, blue=None, red=None):
        if blue is not None:
            self.blue.value = blue
        if red is not None:
            self.red.value = red
        self.step(0.001)
        self.step(0.041)

    def click(self, name):
        self.set(**{name: False})
        self.set(**{name: True})


class DualButtonTests(unittest.TestCase):
    def test_single_click_waits_for_double_window(self):
        harness = GestureHarness()
        harness.click("blue")
        self.assertEqual(harness.events, [])
        harness.step(0.349)
        self.assertEqual(harness.events, [])
        harness.step(0.002)
        self.assertEqual(harness.events, [dual_button.BLUE_SINGLE])

    def test_double_click_suppresses_single(self):
        for name, expected in (
            ("blue", dual_button.BLUE_DOUBLE),
            ("red", dual_button.RED_DOUBLE),
        ):
            harness = GestureHarness()
            harness.click(name)
            harness.step(0.100)
            harness.click(name)
            harness.step(0.500)
            self.assertEqual(harness.events, [expected])

    def test_chord_suppresses_individual_actions(self):
        harness = GestureHarness()
        harness.set(blue=False)
        harness.step(0.050)
        harness.set(red=False)
        harness.set(blue=True)
        harness.set(red=True)
        harness.step(0.500)
        self.assertEqual(harness.events, [dual_button.BOTH_SHORT])

    def test_long_hold_is_one_shot_and_has_no_short_release(self):
        harness = GestureHarness()
        harness.set(blue=False)
        harness.step(0.020)
        harness.set(red=False)
        harness.step(2.999)
        self.assertEqual(harness.events, [])
        harness.step(0.002)
        self.assertEqual(harness.events, [dual_button.BOTH_LONG])
        harness.step(7.0)
        harness.set(blue=True)
        harness.set(red=True)
        self.assertEqual(harness.events, [dual_button.BOTH_LONG])

    def test_boot_held_requires_release_before_arming(self):
        harness = GestureHarness(blue=False, red=False)
        harness.step(5.0)
        self.assertEqual(harness.events, [])
        self.assertFalse(harness.controller.armed)
        harness.set(blue=True, red=True)
        self.assertTrue(harness.controller.armed)
        self.assertEqual(harness.events, [])

    def test_twenty_clicks_each_have_no_cross_trigger(self):
        for name, expected in (
            ("blue", dual_button.BLUE_SINGLE),
            ("red", dual_button.RED_SINGLE),
        ):
            harness = GestureHarness()
            for _ in range(20):
                harness.click(name)
                harness.step(0.351)
            self.assertEqual(harness.events, [expected] * 20)

    def test_gesture_module_contains_no_blocking_sleep(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertNotIn("sleep(", source)


if __name__ == "__main__":
    unittest.main()
