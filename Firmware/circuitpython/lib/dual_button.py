"""Non-blocking, debounce-safe gesture recognition for a dual GPIO button."""

BLUE_SINGLE = "BLUE_SINGLE"
BLUE_DOUBLE = "BLUE_DOUBLE"
RED_SINGLE = "RED_SINGLE"
RED_DOUBLE = "RED_DOUBLE"
BOTH_SHORT = "BOTH_SHORT"
BOTH_LONG = "BOTH_LONG"


class DebouncedButton:
    def __init__(self, initial_pressed, now, debounce_seconds):
        self.stable = bool(initial_pressed)
        self.candidate = self.stable
        self.candidate_since = now
        self.debounce_seconds = debounce_seconds

    def update(self, pressed, now):
        pressed = bool(pressed)
        if pressed != self.candidate:
            self.candidate = pressed
            self.candidate_since = now
        if (
            self.candidate != self.stable
            and now - self.candidate_since >= self.debounce_seconds
        ):
            self.stable = self.candidate
            return True
        return False


class DualButtonController:
    """Turn two digital levels into semantic click/chord events.

    GPIO polarity is supplied explicitly. Gesture recognition remains disarmed
    until both inputs have first been observed released after boot.
    """

    def __init__(
        self,
        blue_input,
        red_input,
        active_level,
        now=0.0,
        debounce_seconds=0.040,
        double_click_seconds=0.350,
        chord_seconds=0.120,
        long_hold_seconds=3.000,
    ):
        self.blue_input = blue_input
        self.red_input = red_input
        self.active_level = bool(active_level)
        self.debounce_seconds = debounce_seconds
        self.double_click_seconds = double_click_seconds
        self.chord_seconds = chord_seconds
        self.long_hold_seconds = long_hold_seconds

        blue_pressed = self._pressed(self.blue_input.value)
        red_pressed = self._pressed(self.red_input.value)
        self.blue = DebouncedButton(blue_pressed, now, debounce_seconds)
        self.red = DebouncedButton(red_pressed, now, debounce_seconds)
        self._previous_blue = blue_pressed
        self._previous_red = red_pressed
        self._press_times = {"blue": None, "red": None}
        self._pending_deadlines = {"blue": None, "red": None}
        self._consumed = {"blue": False, "red": False}
        self._events = []
        self._armed = False
        self._chord_active = False
        self._chord_started = None
        self._chord_long_fired = False
        self._overlap_lockout = False

    @property
    def armed(self):
        return self._armed

    def _pressed(self, level):
        return bool(level) == self.active_level

    def _queue(self, event):
        self._events.append(event)

    def _clear_pending(self):
        self._pending_deadlines["blue"] = None
        self._pending_deadlines["red"] = None

    def _reset_after_chord(self):
        self._chord_active = False
        self._chord_started = None
        self._chord_long_fired = False
        self._press_times["blue"] = None
        self._press_times["red"] = None
        self._consumed["blue"] = False
        self._consumed["red"] = False

    def _individual_release(self, name, now):
        if self._consumed[name]:
            self._consumed[name] = False
            self._press_times[name] = None
            return
        deadline = self._pending_deadlines[name]
        if deadline is not None and now <= deadline:
            self._pending_deadlines[name] = None
            self._queue(BLUE_DOUBLE if name == "blue" else RED_DOUBLE)
        else:
            self._pending_deadlines[name] = now + self.double_click_seconds
        self._press_times[name] = None

    def update(self, now, blue_level=None, red_level=None):
        if blue_level is None:
            blue_level = self.blue_input.value
        if red_level is None:
            red_level = self.red_input.value

        self.blue.update(self._pressed(blue_level), now)
        self.red.update(self._pressed(red_level), now)
        blue_pressed = self.blue.stable
        red_pressed = self.red.stable
        blue_down = blue_pressed and not self._previous_blue
        red_down = red_pressed and not self._previous_red
        blue_up = not blue_pressed and self._previous_blue
        red_up = not red_pressed and self._previous_red
        self._previous_blue = blue_pressed
        self._previous_red = red_pressed

        if not self._armed:
            if not blue_pressed and not red_pressed:
                self._armed = True
                self._clear_pending()
                self._press_times["blue"] = None
                self._press_times["red"] = None
            return self._events.pop(0) if self._events else None

        if blue_down:
            self._press_times["blue"] = now
            self._consumed["blue"] = False
        if red_down:
            self._press_times["red"] = now
            self._consumed["red"] = False

        if not self._chord_active and blue_pressed and red_pressed:
            blue_at = self._press_times["blue"]
            red_at = self._press_times["red"]
            if (
                blue_at is not None
                and red_at is not None
                and abs(blue_at - red_at) <= self.chord_seconds
            ):
                self._chord_active = True
                self._chord_started = max(blue_at, red_at)
                self._chord_long_fired = False
                self._consumed["blue"] = True
                self._consumed["red"] = True
                self._clear_pending()
            else:
                # Overlapping presses outside the chord window are ambiguous.
                # Suppress them instead of risking unintended adjustments.
                self._overlap_lockout = True
                self._consumed["blue"] = True
                self._consumed["red"] = True
                self._clear_pending()

        if self._overlap_lockout:
            if not blue_pressed and not red_pressed:
                self._overlap_lockout = False
                self._reset_after_chord()
            return self._events.pop(0) if self._events else None

        if self._chord_active:
            if (
                not self._chord_long_fired
                and blue_pressed
                and red_pressed
                and now - self._chord_started >= self.long_hold_seconds
            ):
                self._chord_long_fired = True
                self._queue(BOTH_LONG)
            if not blue_pressed and not red_pressed:
                if not self._chord_long_fired:
                    self._queue(BOTH_SHORT)
                self._reset_after_chord()
            return self._events.pop(0) if self._events else None

        if blue_up:
            self._individual_release("blue", now)
        if red_up:
            self._individual_release("red", now)

        for name, single_event in (
            ("blue", BLUE_SINGLE),
            ("red", RED_SINGLE),
        ):
            deadline = self._pending_deadlines[name]
            if deadline is not None and now >= deadline:
                self._pending_deadlines[name] = None
                self._queue(single_event)

        return self._events.pop(0) if self._events else None

