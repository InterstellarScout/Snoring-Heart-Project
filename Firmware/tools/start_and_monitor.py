"""Exit raw REPL, start code.py, and print the boot log."""

import argparse
import sys
import time

import serial


parser = argparse.ArgumentParser()
parser.add_argument("--port", default="COM7")
parser.add_argument("--seconds", type=float, default=20.0)
parser.add_argument("--native-usb", action="store_true")
args = parser.parse_args()

port = serial.Serial(args.port, 115200, timeout=0.1)
if not args.native_usb:
    port.dtr = False
    port.rts = False
try:
    time.sleep(0.2)
    port.write(b"\x02")
    port.flush()
    time.sleep(0.2)
    port.write(b"\x04")
    port.flush()
    deadline = time.monotonic() + args.seconds
    while time.monotonic() < deadline:
        data = port.read(port.in_waiting or 1)
        if data:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
finally:
    port.close()
