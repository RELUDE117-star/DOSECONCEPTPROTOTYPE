#!/usr/bin/env python3
"""Quick test: decode qr_blue.png with pyzbar."""

from PIL import Image
from pyzbar.pyzbar import decode

img = Image.open("qr_blue.png")
codes = decode(img)
for code in codes:
    print(f"  Type: {code.type}")
    print(f"  Data: {code.data.decode('utf-8')}")
if not codes:
    print("  No QR code found!")
