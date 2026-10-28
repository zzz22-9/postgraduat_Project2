"""Deterministic Pillow PNGs; labels are synthetic metadata, not vision results."""
import json

from PIL import Image, ImageDraw, ImageFont, PngImagePlugin


def render_frame(path, metadata):
    image = Image.new("RGB", (960, 600), "#111c2d")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=19)
    title = ImageFont.load_default(size=27)
    fault = metadata["synthetic_label"] == "fault"
    color = "#f46767" if fault else "#59d5aa"
    draw.text((28, 22), f"{metadata['equipment_id']} / {metadata['process_id']} / {metadata['stage']}", font=title, fill=color)
    draw.rounded_rectangle((35, 85, 445, 400), radius=18, fill="#263b55", outline=color, width=4)
    draw.rectangle((170, 110, 310, 150), fill="#7f97ad")
    draw.rectangle((222, 150, 258, 245), fill="#d5e2ef")
    draw.ellipse((210, 230, 270, 265), fill=color)
    draw.polygon(((110, 310), (285, 275), (385, 320), (210, 357)), fill="#b0c6d9")
    draw.polygon(((110, 310), (210, 357), (210, 383), (110, 336)), fill="#6f8da7")
    if fault:
        draw.line(((188, 316), (222, 306), (233, 327), (280, 318)), fill="#f44949", width=5)
    y = 105
    for key, value in metadata["signals"].items():
        draw.text((480, y), f"{key}: {value:.4f}", font=font, fill="#e4edf6")
        y += 52
    for y, text in ((430, f"Workpiece {metadata['order_id']} / Work order {metadata['work_order_id']}"),
                    (465, metadata["timestamp"]),
                    (500, f"Simulation {metadata['sim_time_s']:.3f}s / label={metadata['synthetic_label']}"),
                    (550, "SYNTHETIC DEMO / labels from generator, not image recognition")):
        draw.text((28, y), text, font=font, fill="#b6c8db")
    info = PngImagePlugin.PngInfo()
    info.add_text("task21_metadata", json.dumps(metadata, sort_keys=True, ensure_ascii=False, allow_nan=False))
    image.save(path, format="PNG", pnginfo=info)
