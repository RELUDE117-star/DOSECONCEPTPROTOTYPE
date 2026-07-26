"""Generate the 4 Dose Portable stickers, to scale with the Shapr3D
Dose Storage OBJ (60.5mm W x 150mm H x 33mm D), in the exact style of
the pharmacy label in dose.html (makeLabel canvas)."""
import os
import qrcode
from PIL import Image, ImageDraw, ImageFont

OUT = "/home/user/DOSECONCEPTPROTOTYPE/stickers"
os.makedirs(OUT, exist_ok=True)

DPI = 300
MM = DPI / 25.4  # px per mm

# Website label palette (from dose.html makeLabel)
BG = "#FBFAF6"
BORDER = "#E4E1D9"
GRAY = "#7c8291"
INK = "#111111"
NAVY = "#1B2A4A"
DARK = "#0E1626"
FAINT = "#b9bec7"

FB = "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"
FR = "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"


def font(path, px):
    return ImageFont.truetype(path, int(px))


# ── Front label: 50 x 58.3 mm — same 420:490 aspect as the website ──
FRONT_W_MM, FRONT_H_MM = 50.0, 58.3
FW, FH = int(FRONT_W_MM * MM), int(FRONT_H_MM * MM)
S = FW / 420.0  # scale factor from website-canvas units to pixels


def px(v):
    return int(v * S)


def label_base():
    img = Image.new("RGB", (FW, FH), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([px(5), px(5), FW - px(5), FH - px(5)],
                outline=BORDER, width=max(2, px(2)))
    return img, d


def barcode(d, x, y, w, h):
    bx = x
    while bx < x + w:
        bw = 1 + ((bx * 13) % 3)
        d.rectangle([bx, y, bx + max(1, px(bw)), y + h], fill=INK)
        bx += px(bw) + px(2) + 1


def atorvastatin_front():
    img, d = label_base()
    d.text((px(24), px(22)), "MISSION PHARMACY · Rx 7583104",
           font=font(FR, px(17)), fill=GRAY)
    d.line([px(24), px(52), FW - px(24), px(52)], fill=BORDER,
           width=max(1, px(2)))
    d.text((px(24), px(68)), "MILLER, JANE A.",
           font=font(FB, px(34)), fill=INK)
    d.text((px(24), px(116)), "ATORVASTATIN", font=font(FB, px(29)),
           fill=NAVY)
    d.text((px(24), px(152)), "20 MG TABLET", font=font(FB, px(29)),
           fill=NAVY)
    d.rectangle([px(24), px(198), FW - px(24), px(278)], fill=DARK)
    d.text((px(38), px(212)), "TAKE 1 TABLET BY MOUTH",
           font=font(FB, px(22)), fill="#FFFFFF")
    d.text((px(38), px(242)), "ONCE DAILY AT 9:00 PM",
           font=font(FB, px(22)), fill="#FFFFFF")
    d.text((px(24), px(296)), "Qty 30 · Refills 2 · Dr. R. Alvarez",
           font=font(FR, px(19)), fill=INK)
    barcode(d, px(24), px(340), FW - px(56), px(60))
    d.text((px(24), px(408)), "(01) 0 07583104 20255 7",
           font=font(FR, px(16)), fill=GRAY)
    d.text((px(24), FH - px(42)), "Filled 07/15/26 · Discard 07/27",
           font=font(FR, px(15)), fill=FAINT)
    return img


def blank_line(d, x1, x2, y):
    d.line([x1, y, x2, y], fill=FAINT, width=max(1, px(2)))


def newmed_front():
    img, d = label_base()
    d.text((px(24), px(22)), "DOSE PORTABLE · SELF-FILLED",
           font=font(FR, px(17)), fill=GRAY)
    d.line([px(24), px(52), FW - px(24), px(52)], fill=BORDER,
           width=max(1, px(2)))

    d.text((px(24), px(66)), "NAME", font=font(FR, px(15)), fill=GRAY)
    blank_line(d, px(24), FW - px(24), px(106))

    d.text((px(24), px(118)), "MEDICATION", font=font(FR, px(15)),
           fill=GRAY)
    blank_line(d, px(24), FW - px(24), px(158))

    d.text((px(24), px(170)), "STRENGTH / DOSE", font=font(FR, px(15)),
           fill=GRAY)
    blank_line(d, px(24), FW - px(24), px(210))

    # Directions box — outlined counterpart of the dark box on the Rx label
    d.rectangle([px(24), px(226), FW - px(24), px(306)],
                outline=DARK, width=max(2, px(2)))
    d.text((px(38), px(236)), "DIRECTIONS", font=font(FB, px(15)),
           fill=DARK)
    blank_line(d, px(38), FW - px(38), px(272))
    blank_line(d, px(38), FW - px(38), px(296))

    d.text((px(24), px(322)), "QTY", font=font(FR, px(15)), fill=GRAY)
    blank_line(d, px(58), px(170), px(344))
    d.text((px(190), px(322)), "TIME OF DAY", font=font(FR, px(15)),
           fill=GRAY)
    blank_line(d, px(290), FW - px(24), px(344))

    d.text((px(24), px(368)), "FILLED", font=font(FR, px(15)), fill=GRAY)
    blank_line(d, px(80), px(190), px(390))
    d.text((px(210), px(368)), "DISCARD", font=font(FR, px(15)),
           fill=GRAY)
    blank_line(d, px(286), FW - px(24), px(390))

    d.text((px(24), FH - px(42)),
           "Write clearly · Ask your pharmacist if unsure",
           font=font(FR, px(15)), fill=FAINT)
    return img


# ── Side QR sticker: 28 x 50 mm (side face is 33mm wide) ──
QR_W_MM, QR_H_MM = 28.0, 50.0
QW, QH = int(QR_W_MM * MM), int(QR_H_MM * MM)
Q = QW / 280.0  # local unit scale


def qx(v):
    return int(v * Q)


def make_qr(payload, box_px):
    q = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_H,
                      border=2)
    q.add_data(payload)
    q.make(fit=True)
    img = q.make_image(fill_color=DARK, back_color="white").convert("RGB")
    return img.resize((box_px, box_px), Image.NEAREST)


def side_sticker(payload, line1, line2, line3):
    img = Image.new("RGB", (QW, QH), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([qx(5), qx(5), QW - qx(5), QH - qx(5)],
                outline=BORDER, width=max(2, qx(3)))
    d.text((QW // 2, qx(30)), "DOSE", font=font(FB, qx(30)), fill=DARK,
           anchor="mm")
    d.line([qx(24), qx(52), QW - qx(24), qx(52)], fill=BORDER,
           width=max(1, qx(2)))
    qr_size = QW - qx(44)
    qr = make_qr(payload, qr_size)
    img.paste(qr, ((QW - qr_size) // 2, qx(78)))
    base = qx(78) + qr_size + qx(34)
    d.text((QW // 2, base), line1, font=font(FB, qx(22)), fill=NAVY,
           anchor="mm")
    d.text((QW // 2, base + qx(34)), line2, font=font(FR, qx(19)),
           fill=GRAY, anchor="mm")
    d.text((QW // 2, base + qx(64)), line3, font=font(FR, qx(19)),
           fill=GRAY, anchor="mm")
    return img


ator_front = atorvastatin_front()
ator_qr = side_sticker('{"med":"Atorvastatin","slot":"yellow"}',
                       "ATORVASTATIN", "20 MG · SLOT YELLOW",
                       "SCAN TO LOAD")
new_front = newmed_front()
new_qr = side_sticker('{"med":"New Medication","slot":"demo"}',
                      "NEW MEDICATION", "SELF-FILLED",
                      "SCAN TO REGISTER")

ator_front.save(f"{OUT}/sticker_atorvastatin_front.png", dpi=(DPI, DPI))
ator_qr.save(f"{OUT}/sticker_atorvastatin_qr.png", dpi=(DPI, DPI))
new_front.save(f"{OUT}/sticker_newmed_front.png", dpi=(DPI, DPI))
new_qr.save(f"{OUT}/sticker_newmed_qr.png", dpi=(DPI, DPI))

# ── Print sheet: US Letter 8.5 x 11 in at 300 DPI — print at 100% ──
LETTER_W, LETTER_H = int(8.5 * DPI), int(11 * DPI)   # 2550 x 3300 px
sheet = Image.new("RGB", (LETTER_W, LETTER_H), "#FFFFFF")
d = ImageDraw.Draw(sheet)
cap = font(FR, int(3.2 * MM))
title_f = font(FB, int(5 * MM))

d.text((int(0.75 * DPI), int(0.55 * DPI)),
       "DOSE PORTABLE — STICKER SHEET", font=title_f, fill="#0E1626")
d.text((int(0.75 * DPI), int(0.55 * DPI) + int(6.5 * MM)),
       "Print on US Letter (8.5 x 11 in) at 100% scale / no fit-to-page. "
       "Sized for the Dose Storage (60.5 x 150 x 33 mm).",
       font=cap, fill="#555555")


def crop_marks(x, y, w, h):
    """Light cut guides just outside each sticker corner."""
    L = int(3 * MM)
    g = "#AAAAAA"
    for cx, cy, dx, dy in ((x, y, 1, 1), (x + w, y, -1, 1),
                           (x, y + h, 1, -1), (x + w, y + h, -1, -1)):
        d.line([cx - dx * L, cy, cx, cy], fill=g)
        d.line([cx, cy - dy * L, cx, cy], fill=g)


def place(img, x, y, caption):
    sheet.paste(img, (x, y))
    d.rectangle([x - 1, y - 1, x + img.width + 1, y + img.height + 1],
                outline="#BBBBBB")
    crop_marks(x, y, img.width, img.height)
    d.text((x, y + img.height + int(2 * MM)), caption, font=cap,
           fill="#555555")


MARGIN = int(0.75 * DPI)
top = int(1.15 * DPI)
row_gap = int(14 * MM)
col2 = MARGIN + FW + int(18 * MM)

place(ator_front, MARGIN, top,
      "ATORVASTATIN — FRONT (pharmacist-filled) · 50 x 58 mm")
place(ator_qr, col2, top,
      "ATORVASTATIN — SIDE QR · 28 x 50 mm")
row2 = top + FH + row_gap
place(new_front, MARGIN, row2,
      "NEW MEDICATION — FRONT (user-filled) · 50 x 58 mm")
place(new_qr, col2, row2,
      "NEW MEDICATION — SIDE QR · 28 x 50 mm")

# 1-inch calibration ruler so scale can be verified after printing
ry = row2 + FH + int(18 * MM)
rx = MARGIN
d.line([rx, ry, rx + DPI, ry], fill="#0E1626", width=3)
for i in range(5):
    tick = rx + int(i * DPI / 4)
    d.line([tick, ry - int(2 * MM), tick, ry], fill="#0E1626", width=3)
d.text((rx, ry + int(1.5 * MM)),
       'calibration: this bar must measure exactly 1 inch (25.4 mm)',
       font=cap, fill="#555555")

sheet.save(f"{OUT}/stickers_sheet.png", dpi=(DPI, DPI))

# verify the QRs decode
from pyzbar.pyzbar import decode
for name in ("sticker_atorvastatin_qr", "sticker_newmed_qr"):
    r = decode(Image.open(f"{OUT}/{name}.png"))
    print(name, "->", r[0].data.decode() if r else "NOT DECODED")
print("sheet:", sheet.size, "= 8.5x11in at", DPI, "DPI")
