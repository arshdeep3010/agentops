# Demo video tools

These scripts recorded `results/video/agentops-linkedin.mp4`: a real run of the ShopLite coupon task, as a portrait (4:5) MP4 for LinkedIn with captions burned in.

## How it works

1. **`linkedin.js`** drives the AgentOps page in headless Edge and follows the run's event log. It adds animated title cards, captions, a step bar, a tap effect, and a panel for the 9 checks (`overlay.js`). It saves a JPEG frame whenever the screen changes, plus a timeline of the periods when the model is working.
2. **`build.py`** turns the frames into an H.264 MP4 (1080×1350, 30 fps). Each period of model work is sped up to 3–7 s, and the overlay labels it "sped up". Everything else plays in real time.
3. **`patch.py`** + **`render_patches.js`** (only if needed) paste corrected captions onto frames that were already recorded. `patches.json` holds the corrections made to the 7 Nov 2026 take.
4. **`sheet.py`** makes a contact sheet of chosen frames, for checking a take.

## Run it

AgentOps must be running (live or offline) and the sandbox image must be built.

```bash
cd tools/video
npm install
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt   # Windows; .venv/bin on macOS/Linux
npx playwright install ffmpeg                 # Playwright's ffmpeg, used for screen capture
node linkedin.js http://localhost:8820 out/take
.venv/Scripts/python build.py out/take out/agentops.mp4
```

If the AI reviewer's comments get escalated to the merge approval, the script waits for you to approve or reject the merge in the AgentOps page. It never approves escalated comments on its own unless `AUTO_APPROVE_ESCALATED=1` is set.

## Keep it honest

- **Watch the whole take before posting.** The captions are written from the run's events, so check each one against what the run actually did.
- **The 7 Nov take needed corrections.** Its script read the crossed-out total instead of the discounted one, skipped one checks panel, and summarised the review wrongly. Those captions were corrected with `patch.py`, and `linkedin.js` is fixed for those cases. Expect a new model to find new cases.
- **Frames are 720×900.** The screen capture ignores the device pixel ratio, so `build.py` scales them up 1.5×. Text is slightly soft.
