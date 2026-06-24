#!/usr/bin/env python3
"""Quick test: print MPR121 touched pads continuously."""

import time
import board
import busio
import adafruit_mpr121

i2c = busio.I2C(board.SCL, board.SDA)
mpr = adafruit_mpr121.MPR121(i2c, address=0x5A)

print("MPR121 connected. Touch a pad...")
while True:
    touched = []
    for i in range(12):
        if mpr[i].value:
            touched.append(i)
    if touched:
        print(f"  Touched: {touched}")
    time.sleep(0.1)
