#!/usr/bin/env python3
"""
Generate QR code images for DOSE demo medications.

Creates a PNG for each medication and an all-in-one printable sheet.
Run on any machine with Python 3 and the qrcode + Pillow packages:

    pip install qrcode[pil]
    python3 generate_qr_codes.py

Output goes into a qr_codes/ directory.
"""

import os

try:
    import qrcode
except ImportError:
    print("Install the qrcode package:  pip install qrcode[pil]")
    raise SystemExit(1)

from PIL import Image, ImageDraw, ImageFont

MEDICATIONS = ["blue", "red", "green", "yellow"]
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qr_codes")
QR_SIZE = 400


def generate():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    images = []
    for med in MEDICATIONS:
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_H,
            box_size=12,
            border=4,
        )
        qr.add_data(med)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        img = img.resize((QR_SIZE, QR_SIZE), Image.NEAREST)

        # Add label below
        labeled = Image.new("RGB", (QR_SIZE, QR_SIZE + 60), "white")
        labeled.paste(img, (0, 0))
        draw = ImageDraw.Draw(labeled)
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 28)
        except OSError:
            font = ImageFont.load_default()
        bbox = draw.textbbox((0, 0), med.upper(), font=font)
        tw = bbox[2] - bbox[0]
        draw.text(((QR_SIZE - tw) // 2, QR_SIZE + 10), med.upper(), fill="black", font=font)

        path = os.path.join(OUTPUT_DIR, f"{med}.png")
        labeled.save(path)
        images.append(labeled)
        print(f"  saved {path}")

    # All-in-one sheet (2×2 grid)
    margin = 40
    sheet_w = QR_SIZE * 2 + margin * 3
    sheet_h = (QR_SIZE + 60) * 2 + margin * 3
    sheet = Image.new("RGB", (sheet_w, sheet_h), "white")
    positions = [
        (margin, margin),
        (margin * 2 + QR_SIZE, margin),
        (margin, margin * 2 + QR_SIZE + 60),
        (margin * 2 + QR_SIZE, margin * 2 + QR_SIZE + 60),
    ]
    for img, pos in zip(images, positions):
        sheet.paste(img, pos)

    sheet_path = os.path.join(OUTPUT_DIR, "all_codes_printable.png")
    sheet.save(sheet_path)
    print(f"  saved {sheet_path}")
    print(f"\nDone! Print {sheet_path} and cut out the codes.")


if __name__ == "__main__":
    generate()
