#!/usr/bin/env python3
"""Quick test: capture one frame and save it."""

from picamera2 import Picamera2
from PIL import Image

cam = Picamera2()
config = cam.create_preview_configuration(main={"size": (1280, 720), "format": "RGB888"})
cam.configure(config)
cam.start()
try:
    cam.set_controls({"AfMode": 2})
except Exception:
    pass

import time
time.sleep(1)

arr = cam.capture_array()
img = Image.fromarray(arr[:, :, ::-1])
img.save("test_capture.jpg")
cam.stop()
print(f"Saved test_capture.jpg ({img.size})")
