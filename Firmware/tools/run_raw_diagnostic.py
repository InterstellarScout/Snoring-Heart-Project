"""Run a CircuitPython diagnostic from RAM over USB serial.

The target filesystem is not modified. Ctrl-C stops the remote diagnostic,
returns the board to its normal application, and closes the serial port.
"""

import argparse
import pathlib
import sys
import time

import serial


def read_until(port, marker, timeout):
    deadline = time.monotonic() + timeout
    data = bytearray()
    while time.monotonic() < deadline:
        waiting = port.in_waiting
        if waiting:
            data.extend(port.read(waiting))
            if marker in data:
                return bytes(data)
        else:
            time.sleep(0.01)
    raise RuntimeError("Timed out waiting for {!r}: {!r}".format(marker, bytes(data[-300:])))


def send_chunked(port, payload):
    for start in range(0, len(payload), 128):
        port.write(payload[start:start + 128])
        port.flush()
        time.sleep(0.004)


def enter_raw_repl(port, timeout=12.0):
    deadline = time.monotonic() + timeout
    data = bytearray()
    while time.monotonic() < deadline:
        port.write(b"\x03\x03")
        port.flush()
        time.sleep(0.10)
        port.write(b"\x01")
        port.flush()
        until = time.monotonic() + 0.35
        while time.monotonic() < until:
            waiting = port.in_waiting
            if waiting:
                data.extend(port.read(waiting))
                if b"raw REPL" in data:
                    return bytes(data)
            else:
                time.sleep(0.01)
    raise RuntimeError("Could not enter raw REPL: {!r}".format(bytes(data[-500:])))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("script", type=pathlib.Path)
    parser.add_argument("--port", default="COM7")
    parser.add_argument("--seconds", type=float, default=120.0)
    parser.add_argument("--native-usb", action="store_true")
    args = parser.parse_args()
    source = args.script.read_bytes().replace(b"\r\n", b"\n")

    port = serial.Serial(args.port, 115200, timeout=0.05, write_timeout=2)
    if not args.native_usb:
        port.dtr = False
        port.rts = False
    try:
        port.reset_input_buffer()
        enter_raw_repl(port)
        send_chunked(port, source)
        port.write(b"\x04")
        response = read_until(port, b"OK", 4.0)
        if b"OK" not in response:
            raise RuntimeError("Raw execution was not accepted")
        print("[HOST] Diagnostic running from RAM on {} for up to {} seconds".format(args.port, args.seconds), flush=True)
        deadline = time.monotonic() + args.seconds
        while time.monotonic() < deadline:
            data = port.read(port.in_waiting or 1)
            if data:
                sys.stdout.write(data.decode("utf-8", "replace"))
                sys.stdout.flush()
    except KeyboardInterrupt:
        print("\n[HOST] Stopping diagnostic", flush=True)
    finally:
        try:
            port.write(b"\x03")
            time.sleep(0.15)
            port.write(b"\x02")
            time.sleep(0.15)
        finally:
            port.close()


if __name__ == "__main__":
    main()
