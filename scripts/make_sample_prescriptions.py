"""
Generate synthetic prescriptions for trying the scanner.

Writes printed-font and handwriting-font PNGs plus a text-layer PDF to
samples/. These are clean renders; real phone photos of handwritten
prescriptions are much harder for OCR.
"""

from __future__ import annotations

import random
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "samples"
FONTS = Path("C:/Windows/Fonts")

HEADER = [
    "Dr. Anil Mehta  MBBS, MD (Medicine)",
    "City Care Clinic, Kolkata   Reg No. 45821",
    "Patient: Ravi Kumar   Age: 42 / M   Date: 12/10/2026",
]
MEDICINES = [
    "Rx",
    "1. Tab Augmentin 625 Duo   1-0-1 x 5 days",
    "2. Tab Pan 40   1-0-0 before food",
    "3. Tab Dolo 650   SOS",
    "4. Syp Ascoril LS   10ml TDS",
    "5. Tab Telma 40   0-0-1",
    "6. Tab Metformin 500   1-0-1 after food",
]


def render(font_file: str, size: int, out_name: str, jitter: bool) -> None:
    font = ImageFont.truetype(str(FONTS / font_file), size)
    small = ImageFont.truetype(str(FONTS / "arial.ttf"), 26)
    img = Image.new("RGB", (1240, 1100), (252, 251, 246))
    draw = ImageDraw.Draw(img)
    y = 50
    for line in HEADER:
        draw.text((60, y), line, font=small, fill=(30, 30, 90))
        y += 42
    draw.line((60, y + 10, 1180, y + 10), fill=(120, 120, 160), width=2)
    y += 50
    rng = random.Random(7)
    for line in MEDICINES:
        x = 80 + (rng.randint(-10, 25) if jitter else 0)
        draw.text((x, y), line, font=font, fill=(20, 30, 110))
        y += int(size * 1.9)
    if jitter:
        img = img.rotate(1.2, expand=False, fillcolor=(252, 251, 246))
        img = img.filter(ImageFilter.GaussianBlur(0.6))
    img.save(OUT / out_name)


def render_pdf(out_name: str) -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    y = 72
    for line in HEADER + [""] + MEDICINES:
        page.insert_text((60, y), line, fontsize=12)
        y += 22
    doc.save(OUT / out_name)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    render("arial.ttf", 38, "prescription_printed.png", jitter=False)
    render("segoesc.ttf", 38, "prescription_handwriting_style.png", jitter=True)
    render_pdf("prescription_digital.pdf")
    print(f"Wrote samples to {OUT}")


if __name__ == "__main__":
    main()
