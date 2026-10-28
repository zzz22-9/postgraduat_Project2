"""Deterministic educational 3-D renderer. Requires numpy and Pillow.

No photographs, diffusion model, Blender, MES database, or SimPy run are used.
Run: python render_assembly.py
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont
import PIL

WIDTH, HEIGHT = 800, 600
EPS = 1e-5
STATES = {"unfastened": [], "complete": [0, 1, 2, 3], "missing_bolt": [0, 1, 2]}
POSITIONS = [(-.94, .58), (.94, .58), (-.94, -.58), (.94, -.58)]
VIEWS = {"front": [4.5, -6.5, 6.2], "side": [-5.5, -5.8, 6.7]}


def unit(a):
    return a / np.linalg.norm(a, axis=-1, keepdims=True)


def scene(state):
    """Lengths are arbitrary scene units; dimensions are teaching assumptions."""
    items = []

    def box(name, lo, hi, color, label):
        items.append(dict(kind="box", name=name, lo=lo, hi=hi, color=color, label=label))

    def cylinder(name, x, y, radius, low, high, color, label):
        items.append(dict(kind="cylinder", name=name, center=[x, y], radius=radius,
                          low=low, high=high, color=color, label=label))

    box("work_surface", [-200, -200, -.16], [200, 200, 0], [.29, .34, .39], 1)
    box("blue_base", [-1.58, -1.17, .02], [1.58, 1.17, .24], [.045, .27, .36], 2)
    box("cover_plate", [-1.37, -.98, .24], [1.37, .98, .38], [.59, .66, .70], 3)
    cylinder("central_housing", 0, 0, .49, .38, .88, [.045, .31, .42], 4)
    cylinder("housing_cap", 0, 0, .51, .88, .97, [.55, .64, .70], 4)
    cylinder("center_inset", 0, 0, .31, .9701, .974, [.10, .16, .21], 4)
    for i, (x, y) in enumerate(POSITIONS):
        # A dark inset surface approximates an empty threaded recess visually.
        # This is not CAD Boolean geometry or a physics model of a through-hole.
        cylinder(f"hole_{i}", x, y, .115, .3801, .382, [.018, .025, .030], 5)
        if i in STATES[state]:
            label = 10 + i
            cylinder(f"washer_{i}", x, y, .19, .382, .415, [.36, .43, .49], label)
            cylinder(f"bolt_head_{i}", x, y, .135, .415, .59, [.64, .69, .73], label)
            cylinder(f"socket_{i}", x, y, .058, .5901, .592, [.028, .037, .043], label)
    return items


def intersect(origins, directions, obj):
    """Nearest positive ray hit and outward normal for boxes / capped cylinders."""
    count = len(origins)
    normals = np.zeros((count, 3))
    if obj["kind"] == "box":
        lo, hi = np.array(obj["lo"]), np.array(obj["hi"])
        parallel = np.abs(directions) < 1e-12
        safe = np.where(parallel, 1, directions)
        a, b = (lo - origins) / safe, (hi - origins) / safe
        near, far = np.minimum(a, b), np.maximum(a, b)
        near = np.where(parallel, -np.inf, near)
        far = np.where(parallel, np.inf, far)
        enter, leave = near.max(1), far.min(1)
        outside = (parallel & ((origins < lo) | (origins > hi))).any(1)
        distance = np.where(enter > EPS, enter, leave)
        valid = (~outside) & (leave >= np.maximum(enter, EPS))
        axis = np.where(enter > EPS, near.argmax(1), far.argmin(1))
        sign = np.where(enter > EPS, -1, 1) * np.sign(directions[np.arange(count), axis])
        normals[np.arange(count), axis] = sign
        return np.where(valid, distance, np.inf), normals
    center = np.array(obj["center"])
    relative = origins[:, :2] - center
    dxy = directions[:, :2]
    a = (dxy * dxy).sum(1)
    b = 2 * (relative * dxy).sum(1)
    c = (relative * relative).sum(1) - obj["radius"] ** 2
    disc = b * b - 4 * a * c
    denom = np.where(a > 1e-12, 2 * a, 1)
    root = np.sqrt(np.maximum(disc, 0))
    best = np.full(count, np.inf)
    for t in [(-b - root) / denom, (-b + root) / denom]:
        z = origins[:, 2] + t * directions[:, 2]
        valid = (a > 1e-12) & (disc >= 0) & (t > EPS) & (z >= obj["low"]) & (z <= obj["high"]) & (t < best)
        best[valid] = t[valid]
        normals[valid, :2] = (relative[valid] + t[valid, None] * dxy[valid]) / obj["radius"]
        normals[valid, 2] = 0
    dz = directions[:, 2]
    for z, sign in [(obj["low"], -1), (obj["high"], 1)]:
        t = (z - origins[:, 2]) / np.where(np.abs(dz) > 1e-12, dz, 1)
        xy = relative + t[:, None] * dxy
        valid = (np.abs(dz) > 1e-12) & (t > EPS) & ((xy * xy).sum(1) <= obj["radius"] ** 2) & (t < best)
        best[valid] = t[valid]
        normals[valid] = [0, 0, sign]
    return best, normals


def render(objects, camera):
    # Fixed orthographic projection; deterministic directional light + shadows.
    eye, target = np.array(camera), np.array([0, 0, .35])
    forward = unit(target - eye)
    right = unit(np.cross(forward, [0, 0, 1]))
    up = np.cross(right, forward)
    x, y = np.meshgrid((np.arange(WIDTH) + .5 - WIDTH / 2) / WIDTH * 4.8,
                       (HEIGHT / 2 - np.arange(HEIGHT) - .5) / WIDTH * 4.8)
    origins = (eye + x[..., None] * right + y[..., None] * up).reshape(-1, 3)
    directions = np.broadcast_to(forward, origins.shape)
    nearest = np.full(len(origins), np.inf)
    normals = np.zeros_like(origins)
    object_ids = np.full(len(origins), -1, dtype=int)
    for index, obj in enumerate(objects):
        t, n = intersect(origins, directions, obj)
        visible = t < nearest
        nearest[visible], normals[visible], object_ids[visible] = t[visible], n[visible], index
    assert (object_ids >= 0).all(), "Work surface should cover every ray"
    points = origins + nearest[:, None] * directions
    colors = np.array([o["color"] for o in objects])[object_ids]
    # Neutral workbench with restrained grid texture, no state labels in RGB.
    floor = object_ids == 0
    grid = (np.mod(points[:, 0] + .01, .5) < .012) | (np.mod(points[:, 1] + .01, .5) < .012)
    colors[floor & grid] *= .91
    light = unit(np.array([-3., -4., 8.]))
    shadow_origins = points + normals * .0005
    shadow_directions = np.broadcast_to(light, origins.shape)
    shadow = np.zeros(len(origins), dtype=bool)
    for obj in objects[1:]:
        t, _ = intersect(shadow_origins, shadow_directions, obj)
        shadow |= np.isfinite(t)
    diffuse = np.maximum(normals @ light, 0)
    half_vector = unit(light - forward)
    specular = np.maximum(normals @ half_vector, 0) ** 55
    lighting = .34 + .66 * diffuse * np.where(shadow, .10, 1.)
    rgb = colors * lighting[:, None] + (.24 * specular * (~shadow))[:, None]
    rgb = np.clip(rgb, 0, 1) ** (1 / 1.7)
    pixels = (rgb.reshape(HEIGHT, WIDTH, 3) * 255 + .5).astype(np.uint8)
    labels = np.array([o["label"] for o in objects], dtype=np.uint8)[object_ids].reshape(HEIGHT, WIDTH)
    return Image.fromarray(pixels), Image.fromarray(labels)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def preview(out, records):
    board = Image.new("RGB", (1200, 1010), "#edf1f5")
    draw = ImageDraw.Draw(board)
    font_path = Path("C:/Windows/Fonts/msyh.ttc")
    def font(size):
        return ImageFont.truetype(str(font_path), size) if font_path.exists() else ImageFont.load_default()
    draw.text((28, 16), "装配图像样本 · 程序化三维渲染", font=font(27), fill="#192e40")
    draw.text((28, 56), "合成教学数据 / 同一几何场景 / 三种状态 × 两个视角", font=font(18), fill="#54697c")
    titles = {"unfastened": "未紧固：0 / 4 颗螺栓", "complete": "完整装配：4 / 4 颗螺栓", "missing_bolt": "缺件：3 / 4 颗螺栓"}
    for i, rec in enumerate(records):
        row, col = divmod(i, 2)
        x, y = 20 + col * 600, 100 + row * 300
        with Image.open(out / rec["image_path"]) as im:
            board.paste(im.resize((384, 288), Image.Resampling.LANCZOS), (x + 190, y))
        draw.text((x, y + 65), titles[rec["state"]].split("：")[0], font=font(26), fill="#192e40")
        draw.text((x, y + 106), titles[rec["state"]].split("：")[1], font=font(20), fill="#425c73")
        draw.text((x, y + 145), "视角 " + rec["view"], font=font(17), fill="#617689")
    board.save(out / "preview.jpg", quality=94)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(__file__).parent / "rendered_v1")
    parser.add_argument("--verify-repeat", action="store_true", help="Render one view twice and compare pixels")
    args = parser.parse_args()
    out = args.output.resolve()
    for folder in ["images", "masks", "scenes"]:
        (out / folder).mkdir(parents=True, exist_ok=True)
    records = []
    for state, installed in STATES.items():
        for view, camera in VIEWS.items():
            sample_id = f"assembly_{state}_{view}"
            objects = scene(state)
            im, mask = render(objects, camera)
            image_path, mask_path, scene_path = (f"images/{sample_id}.png", f"masks/{sample_id}.png", f"scenes/{sample_id}.json")
            im.save(out / image_path)
            mask.save(out / mask_path)
            write_json(out / scene_path, dict(objects=objects, camera=camera, target=[0, 0, .35],
                                            projection="orthographic", horizontal_span=4.8,
                                            directional_light=[-3, -4, 8], resolution=[WIDTH, HEIGHT]))
            label_array = np.asarray(mask)
            assert all((label_array == 10 + i).sum() > 50 for i in installed), "Installed bolt is not visible"
            assert all(not (label_array == 10 + i).any() for i in set(range(4)) - set(installed))
            records.append(dict(sample_id=sample_id, image_path=image_path, mask_path=mask_path,
                                scene_path=scene_path, source_type="procedural_3d_render", synthetic=True,
                                state=state, view=view, installed_bolt_positions=installed,
                                missing_bolt_positions=sorted(set(range(4)) - set(installed)),
                                bolt_count=len(installed), annotation_source="scene_configuration_not_image_recognition",
                                run_id=None, order_id=None, work_order_id=None, equipment_id=None,
                                operation_id=None, sim_time_min=None,
                                image_sha256=sha(out / image_path), mask_sha256=sha(out / mask_path)))
            print(f"Rendered {sample_id}", flush=True)
    preview(out, records)
    repeat = None
    if args.verify_repeat:
        im, _ = render(scene("complete"), VIEWS["front"])
        with Image.open(out / "images/assembly_complete_front.png") as saved:
            repeat = bool(np.array_equal(np.asarray(im), np.asarray(saved)))
        assert repeat
    assert len({r["image_sha256"] for r in records}) == 6
    write_json(out / "manifest.json", dict(dataset_id="SYNTH-ASSEMBLY-V1", version=1,
                generated_at=datetime.now(timezone.utc).isoformat(), renderer="numpy_analytic_ray_casting_v1",
                script_sha256=sha(Path(__file__)), python=platform.python_version(), numpy=np.__version__,
                pillow=PIL.__version__, units="arbitrary_scene_units", samples=records,
                label_ids={"0": "background", "1": "work_surface", "2": "base", "3": "cover_plate",
                           "4": "central_housing", "5": "empty_recess", "10-13": "bolt_position_0_to_3"},
                limitation="Independent visual states; no physical tightening simulation, no synchronized business events."))
    write_json(out / "validation.json", dict(image_count=6, resolution=[WIDTH, HEIGHT],
                unique_images=6, masks_match_configured_bolt_presence=True, repeated_render_pixel_equal=repeat,
                business_binding="unbound", business_tests_run=False))
    print(f"Saved dataset: {out}", flush=True)


if __name__ == "__main__":
    main()
