import time

import adafruit_drv2605
import bitbangio
import board
import busio


def scan(i2c):
    while not i2c.try_lock():
        pass
    try:
        return i2c.scan()
    finally:
        i2c.unlock()


def create_motor(number, sda, scl):
    try:
        i2c = busio.I2C(scl=scl, sda=sda, frequency=100000)
        print("Motor {} bus: busio.I2C".format(number))
    except Exception as error:
        print("Motor {} hardware I2C unavailable: {}".format(number, error))
        i2c = bitbangio.I2C(scl=scl, sda=sda, frequency=100000)
        print("Motor {} bus: bitbangio.I2C".format(number))
    if 0x5A not in scan(i2c):
        print("Motor {}: DRV2605L not detected".format(number))
        return i2c, None
    driver = adafruit_drv2605.DRV2605(i2c)
    driver.use_ERM()
    driver.realtime_value = 0
    driver.mode = adafruit_drv2605.MODE_REALTIME
    print("Motor {}: READY".format(number))
    return i2c, driver


def set_strength(driver, percent):
    if driver is not None:
        driver.realtime_value = int(127 * percent / 100)


def stop(driver):
    set_strength(driver, 0)


def pulse(driver, percent, duration):
    set_strength(driver, percent)
    time.sleep(duration)
    stop(driver)


def run():
    print("DUAL MOTOR DIAGNOSTIC")
    motor1_i2c, motor1 = create_motor(1, board.A0, board.A1)
    motor2_i2c, motor2 = create_motor(2, board.A2, board.A3)
    print("TEST MOTOR 1")
    pulse(motor1, 70, 0.25)
    time.sleep(0.75)
    print("TEST MOTOR 2")
    pulse(motor2, 70, 0.25)
    time.sleep(0.75)
    print("TEST BOTH")
    set_strength(motor1, 70)
    set_strength(motor2, 70)
    time.sleep(0.25)
    stop(motor1)
    stop(motor2)
    time.sleep(0.75)
    print("TEST TANDEM")
    pulse(motor1, 65, 0.07)
    time.sleep(0.10)
    pulse(motor2, 80, 0.09)
    print("DIAGNOSTIC COMPLETE")
    return motor1_i2c, motor2_i2c


run()
