#!/usr/bin/env python3
"""Generate demo QR codes for each medication slot."""

import json
import qrcode

SLOTS = {
    "blue":   {"med": "Sertraline",   "slot": "blue"},
    "red":    {"med": "Metformin",    "slot": "red"},
    "green":  {"med": "Atorvastatin", "slot": "green"},
    "yellow": {"med": "Vitamin D",    "slot": "yellow"},
}

for key, payload in SLOTS.items():
    data = json.dumps(payload, separators=(",", ":"))
    img = qrcode.make(data, box_size=10, border=4)
    path = f"qr_{key}.png"
    img.save(path)
    print(f"  {path}  →  {data}")

print("\nDone. Show these on a phone screen for the camera to scan.")
