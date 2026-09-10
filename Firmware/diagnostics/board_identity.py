"""Print non-mutating CircuitPython board identity and relevant pin aliases."""

import board
import microcontroller
import os


print("UNAME", os.uname())
print("CPU", microcontroller.cpu.uid)
print(
    "PINS",
    [
        name
        for name in ("TX", "RX", "A0", "A1", "A2", "A3", "SDA", "SCL")
        if hasattr(board, name)
    ],
)
for name in ("TX", "RX", "A0", "A1", "A2", "A3", "SDA", "SCL"):
    if hasattr(board, name):
        print(name, getattr(board, name))
