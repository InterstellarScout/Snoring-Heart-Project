"""Temporary raw-level diagnostic for M5Stack Unit Dual Button U025.

Run from RAM. This script never imports the haptic driver and never sends a
motor command. Stop it with Ctrl-C after recording released, Blue, Red, and
both-button levels over at least 20 presses of each button.
"""

import digitalio
import board
import microcontroller
import time


blue = digitalio.DigitalInOut(board.TX)
red = digitalio.DigitalInOut(board.RX)
blue.switch_to_input(pull=None)
red.switch_to_input(pull=None)

last = (blue.value, red.value)
counts = {"tx0_rx0": 0, "tx0_rx1": 0, "tx1_rx0": 0, "tx1_rx1": 0}
print("M5 U025 RAW INPUT DIAGNOSTIC; NO MOTOR COMMANDS")
print("board.TX=BLUE/TOP board.RX=RED/BOTTOM")
print("TX={} RX={}".format(last[0], last[1]))

try:
    while True:
        try:
            microcontroller.watchdog.feed()
        except Exception:
            pass
        now = (blue.value, red.value)
        if now != last:
            label = "tx{}_rx{}".format(int(now[0]), int(now[1]))
            counts[label] += 1
            print("TX={} RX={} {} counts={}".format(now[0], now[1], label, counts))
            last = now
        time.sleep(0.005)
finally:
    blue.deinit()
    red.deinit()
    print("FINAL counts={}".format(counts))
