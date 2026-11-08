"""Paste corrected captions onto the recorded frames. Usage: python patch.py <take> <out_take>

Finds the exact frame where each wrong caption starts and the next one begins (pixel change in the
caption area), then pastes the corrected caption PNG on every frame in between."""

import datetime as dt
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

here = Path(__file__).parent
take, out = Path(sys.argv[1]), Path(sys.argv[2])
(out / "frames").mkdir(parents=True, exist_ok=True)
tl = json.loads((take / "timeline.json").read_text())
spec = json.loads((here / "patches.json").read_text(encoding="utf-8"))
meta = json.loads((here / "out" / "patches" / "meta.json").read_text())
frames = tl["frames"]
day = dt.datetime.fromtimestamp(frames[0]["t"], dt.UTC).date()


def wall(hms: str) -> float:
    return dt.datetime.combine(day, dt.time.fromisoformat(hms), dt.UTC).timestamp()


def img(i: int) -> np.ndarray:
    return np.asarray(Image.open(take / "frames" / frames[i]["file"]).convert("L"), dtype=np.int16)


def first_change(t0: float, t1: float, box: tuple[int, int, int, int], thr: float) -> int:
    """Index of the first frame in [t0, t1] whose box differs from the frame before it by > thr."""
    x0, y0, x1, y1 = box
    idx = [i for i, f in enumerate(frames) if t0 <= f["t"] <= t1 and i > 0]
    for i in idx:
        if np.abs(img(i)[y0:y1, x0:x1] - img(i - 1)[y0:y1, x0:x1]).mean() > thr:
            return i
    raise SystemExit(f"no change found between {t0} and {t1}")


edits: dict[int, list[tuple[Image.Image, tuple[int, int]]]] = {}
for p in spec["captions"]:
    box = (16, int(meta[p["id"]]["top"]) - 8, 704, 893)
    a = first_change(wall(p["at"]) - 0.3, wall(p["at"]) + 1.5, box, 2.0)
    b = first_change(wall(p["until"]) - 0.3, wall(p["until"]) + 1.5, box, 2.0)
    png = Image.open(here / "out" / "patches" / f"{p['id']}.png").convert("RGBA")
    for i in range(a, b):
        edits.setdefault(i, []).append((png, (0, 0)))
    print(f"{p['id']}: frames {a}..{b - 1} ({frames[b]['t'] - frames[a]['t']:.1f}s real)")

def first_departure(t0: float, t1: float, thr: float) -> int:
    """First frame in (t0, t1] that differs from the frame at t0 by > thr (catches slow fades)."""
    ref_i = max(i for i, f in enumerate(frames) if f["t"] <= t0)
    ref = img(ref_i)
    for i in range(ref_i + 1, len(frames)):
        if frames[i]["t"] > t1:
            break
        if np.abs(img(i) - ref).mean() > thr:
            return i
    raise SystemExit(f"no departure between {t0} and {t1}")


card_start = first_departure(wall(spec["card"]["at"]) - 0.5, wall(spec["card"]["at"]) + 4, 8.0)
outro = first_departure(frames[card_start]["t"] + 3.0, frames[card_start]["t"] + 9, 0.8)
stat = Image.open(here / "out" / "patches" / "card.png").convert("RGBA")
for i, f in enumerate(frames):
    # The stats finish animating ~1.3 s in; the card is then static, so its last frame is held on screen.
    if (frames[card_start]["t"] + 1.0 <= f["t"] < frames[outro]["t"]) or i == outro - 1:
        edits.setdefault(i, []).append((stat, (meta["card"]["x"], meta["card"]["y"])))
print(f"card: frames {card_start}..{outro - 1}")

new_frames = []
for i, f in enumerate(frames):
    if i in edits:
        im = Image.open(take / "frames" / f["file"]).convert("RGBA")
        for png, pos in edits[i]:
            im.alpha_composite(png, pos)
        name = "p" + f["file"]
        im.convert("RGB").save(out / "frames" / name, quality=92)
        new_frames.append({"t": f["t"], "file": str((out / "frames" / name).resolve())})
    else:
        new_frames.append({"t": f["t"], "file": str((take / "frames" / f["file"]).resolve())})
(out / "timeline.json").write_text(json.dumps({**tl, "frames": new_frames}))
print(f"patched {len(edits)} frames")
