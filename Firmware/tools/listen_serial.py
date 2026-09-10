"""Print serial output without sending control characters or changing DTR/RTS."""

import argparse
import sys
import time

import serial


parser = argparse.ArgumentParser()
parser.add_argument("--port", required=True)
parser.add_argument("--seconds", type=float, default=30.0)
args = parser.parse_args()

port = serial.Serial(args.port, 115200, timeout=0.1)
try:
    deadline = time.monotonic() + args.seconds
    while time.monotonic() < deadline:
        data = port.read(port.in_waiting or 1)
        if data:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
finally:
    port.close()
