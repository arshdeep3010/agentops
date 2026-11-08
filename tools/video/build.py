"""Turn a recorded take (JPEG frames + timeline.json) into a LinkedIn MP4.

Wait segments (the model working) are sped up so each lasts 3-7 s on screen; everything else plays
at real speed. Usage: python build.py <takeDir> <out.mp4>
"""

import json
import subprocess
import sys
from pathlib import Path

import imageio_ffmpeg

take, out = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()  # ffmpeg reads list paths relative to the list
tl = json.loads((take / "timeline.json").read_text())
frames, segs, end = tl["frames"], tl["segments"], tl["end"]

# Piecewise time warp: real time -> video time.
warp = []  # (start, end, speed)
for s in segs:
    length = s["end"] - s["start"]
    if length < 4:
        continue
    target = min(7.0, max(3.0, length / 10))
    warp.append((s["start"], s["end"], length / target))


def vt(t: float) -> float:
    """Video time of real time t."""
    v = t - frames[0]["t"]
    for a, b, speed in warp:
        if t <= a:
            break
        covered = min(t, b) - a
        v -= covered - covered / speed
    return v


lines = []
for i, f in enumerate(frames):
    nxt = frames[i + 1]["t"] if i + 1 < len(frames) else end
    dur = max(0.0, vt(nxt) - vt(f["t"]))
    if dur <= 0:
        continue
    lines.append(f"file '{(take / 'frames' / f['file']).as_posix()}'\nduration {dur:.4f}")
lines.append(f"file '{(take / 'frames' / frames[-1]['file']).as_posix()}'")
(take / "list.txt").write_text("\n".join(lines) + "\n")

total = vt(end)
print(f"{len(frames)} frames, real {end - frames[0]['t']:.0f}s -> video {total:.1f}s; "
      f"sped up {len(warp)} waits: " + ", ".join(f"{b - a:.0f}s@{s:.0f}x" for a, b, s in warp))

ff = imageio_ffmpeg.get_ffmpeg_exe()
cmd = [ff, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(take / "list.txt"),
       "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
       "-vf", "scale=1080:1350:flags=lanczos:in_range=pc:out_range=tv,fps=30,format=yuv420p",
       "-c:v", "libx264", "-preset", "slow", "-crf", "18", "-profile:v", "high", "-r", "30",
       "-pix_fmt", "yuv420p", "-color_range", "tv", "-colorspace", "bt470bg", "-color_primaries", "bt470bg",
       "-color_trc", "bt709",
       "-c:a", "aac", "-b:a", "96k", "-shortest", "-movflags", "+faststart", str(out)]
subprocess.run(cmd, check=True)
print("wrote", out, f"{out.stat().st_size / 1e6:.1f} MB")
