"""Contact sheet of chosen frames. Usage: python sheet.py <take> <out.jpg> idx idx ..."""
import json
import sys
from pathlib import Path

from PIL import Image

take, out = Path(sys.argv[1]), sys.argv[2]
fr = json.loads((take / "timeline.json").read_text())["frames"]
picks = [int(x) for x in sys.argv[3:]]
ims = [Image.open(take / "frames" / fr[i]["file"]).resize((360, 450)) for i in picks]
cols = 4
sheet = Image.new("RGB", (360 * cols, 450 * ((len(ims) + cols - 1) // cols)), "white")
for k, im in enumerate(ims):
    sheet.paste(im, ((k % cols) * 360, (k // cols) * 450))
sheet.save(out, quality=90)
print("ok")
