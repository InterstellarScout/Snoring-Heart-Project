"""Scan likely QT Py I2C pin pairs without configuring or driving a motor."""

import bitbangio
import board
import busio
import digitalio


def idle_level(pin):
    line = digitalio.DigitalInOut(pin)
    try:
        line.switch_to_input(pull=None)
        return "HIGH" if line.value else "LOW"
    except Exception as error:
        return "ERROR: {}".format(error)
    finally:
        line.deinit()


def scan(i2c):
    while not i2c.try_lock():
        pass
    try:
        return ["0x{:02X}".format(address) for address in i2c.scan()]
    finally:
        i2c.unlock()


def probe(label, sda, scl):
    print(
        "{}: SDA {} {}, SCL {} {}".format(
            label,
            sda,
            idle_level(sda),
            scl,
            idle_level(scl),
        )
    )
    i2c = None
    try:
        try:
            i2c = busio.I2C(scl=scl, sda=sda, frequency=100000)
            kind = "hardware"
        except Exception as hardware_error:
            print("{}: hardware unavailable: {}".format(label, hardware_error))
            i2c = bitbangio.I2C(scl=scl, sda=sda, frequency=100000)
            kind = "software"
        print("{}: {} scan {}".format(label, kind, scan(i2c)))
    except Exception as error:
        print("{}: scan failed: {}".format(label, error))
    finally:
        if i2c is not None:
            i2c.deinit()


print("QT PY NON-ACTUATING I2C PROBE")
probe("Motor 1 A0/A1", board.A0, board.A1)
probe("Motor 2 A2/A3", board.A2, board.A3)
probe("Motor 2 reversed A3/A2", board.A3, board.A2)
probe("Pad I2C SDA/SCL", board.SDA, board.SCL)
probe("STEMMA I2C SDA1/SCL1", board.SDA1, board.SCL1)
print("PROBE COMPLETE; NO MOTOR COMMANDS SENT")
