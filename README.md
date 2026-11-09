# AgentOps

**Describe a feature in one sentence, and AgentOps builds it like a small software team:** it writes the requirements, the design and the tests first, then writes the code, checks it in a Docker sandbox, and asks you before anything is merged.

> **Status:** built and tested offline, with AI-written code running only in a sandbox (Docker locally, a sealed Lambda function on AWS). Deployed to AWS Lambda with Terraform and GitHub Actions ([infra/README.md](infra/README.md)); **[try the live demo](https://ncf7tzviny6x7qkw7aaxk7xszq0lvnbk.lambda-url.ap-south-1.on.aws/)** (the AI's replies are pre-written; the sandbox, checks and shop are real). Not built yet: the SDLC-vs-one-shot benchmark and a real GitHub PR (the PR is written locally for now). See [PROJECT.md](PROJECT.md) section 0.

## The demo: one sentence in, a working feature out

**ShopLite** is a tiny online shop (FastAPI backend + one web page) in `sdlc/examples/shoplite`. You give AgentOps one sentence, and it adds the feature to the shop, backend and frontend:

1. Open AgentOps and press **See the shop as it is now**. There's no coupon box in the cart.
2. Press **Build it** with the sample task: *"Add discount coupons. The code SAVE10 takes 10% off the cart at checkout, in any letter case. Show a coupon box in the cart, and reject codes the shop doesn't know."*
3. Approve the requirements, then the design. Tests are written first and fail, then get locked. The first build refuses `save10` in lowercase; the locked tests catch it and send it back. The second build passes all 9 checks, and a PR is written with a traceability matrix.
4. Approve the merge, then press **Open the shop**. The cart now has a coupon box: two Desk Lamps (₹3,798.00) with `save10` cost ₹3,418.20.

In offline mode (`LLM_MODE=fake`) the model's replies for this task are pre-written (`sdlc/fakes_shoplite.py`), so the demo runs the same way every time without an API key. The sandbox, checks, tests and merged shop are real. Because only this task has replies, the task box is locked in offline mode (the API refuses any other task too). With `LLM_MODE=live`, you can type your own task.

**Live run, 7 Nov 2026** (NVIDIA `nemotron-3-ultra-550b-a55b`): the coupon task merged in 5.5 min after 3 builds. One locked test caught the first build, and the AI reviewer sent the second one back. In the merged shop, `save10` on two Desk Lamps charges ₹3,418.20. Two earlier live attempts that day stopped at the tests-first check, which led to two fixes. The details are in [PROJECT.md](PROJECT.md) under "Live ShopLite runs".

## Run it

```bash
docker compose up -d db                      # Postgres on localhost:54329
docker build -t agentops-sandbox:2 sandbox   # the sandbox the AI's code runs in
python -m venv .venv && .venv/Scripts/pip install -r requirements-dev.txt   # Windows; use .venv/bin on macOS/Linux
cp .env.example .env                         # offline replay, 3 workers (see live mode below)
.venv/Scripts/python -m uvicorn api.main:app --port 8820
```

Open http://localhost:8820 and follow the four steps above. This starts in offline mode (`LLM_MODE=fake`), the same as the live demo: the task box is locked to the coupon task.

### Build your own feature (live mode)

To type your own task, switch to live mode so a real model writes the code:

1. Get a free API key from NVIDIA at https://build.nvidia.com (or use any OpenAI-compatible API).
2. In your `.env`, set:
   ```bash
   LLM_MODE=live
   LLM_API_KEY=your-key-here
   ```
   For another provider, also change `LLM_BASE_URL` and `LLM_MODEL`.
3. Restart the server (stop uvicorn and run the last command again). The header now shows the model's name instead of "offline replay", and the task box is unlocked.
4. Type a feature for the ShopLite shop, for example: *"Don't let shoppers add more of a product than is in stock."* Then press **Build it** and approve each step as before.

Things to know:
- Your key stays on your machine: `.env` is git-ignored.
- The AI's code only ever runs in the sandbox container, never on your machine directly.
- A real model is slower and less predictable than the replay. The live run on 7 Nov took 5.5 min and 3 builds on NVIDIA's free tier. If the checks keep failing, the run stops and asks you instead of merging.
- Tasks are for the ShopLite example (`sdlc/examples/shoplite`). The 30-runs-a-day limit only applies to the hosted demo.

## Deploy it to AWS

Live demo: https://ncf7tzviny6x7qkw7aaxk7xszq0lvnbk.lambda-url.ap-south-1.on.aws/ (offline replay, 30 runs a day).

`infra/` is Terraform for AWS Lambda; `.github/workflows/deploy.yml` deploys every push to `main` that passes CI. Four functions: the API (the same FastAPI app, with a streaming function URL), a worker that is woken when there is work, a sandbox in a VPC with no internet route for the gates, and a preview that serves the merged shop. No NAT gateway, load balancer or RDS, so nothing is billed by the hour; bring any Postgres. The defaults are the offline replay and 30 runs a day. Steps: [infra/README.md](infra/README.md).

## Under the hood: an engine that survives crashes

The pipeline runs on a workflow engine built on Postgres alone. Every step is saved as it finishes, so the pipeline can stop for your approval, survive a crashed worker, and never repeat a side effect. The engine is tested with two small workflows of its own (`workflows/invoice.py`, `workflows/chaos.py`); they are test fixtures and are not shown in the UI.

- **Kill a worker mid-step, and another one takes over.** Each claimed step has a lease that the worker renews with heartbeats. When a worker dies, its lease runs out and another worker reclaims the step. Finished steps are never run again.
- **Side effects happen once.** Tool calls carry an idempotency key that stays the same across retries and is passed to the external API. A retried step reuses the recorded result, and the API dedupes a repeated request.
- **Late results are thrown away.** Every claim increments an attempt number. A worker that wakes up after losing its lease can't overwrite the newer result (fencing).
- **Steps that need a person hold nothing while they wait.** An approval step parks the run as a database row. Approving it queues the next step.
- **Replay from any step.** This starts a new run on the same workflow version. Earlier outputs are reused, so their side effects are not repeated.
- **Everything is an event.** Each state change is written to an append-only log in the same transaction. A test rebuilds every run's status from the log alone and compares it with the tables.

### Measured

Chaos test, 4 Nov 2026: `CHAOS_RUNS=500 pytest -m chaos -s`. Three worker processes were hard-killed at random (TerminateProcess, no cleanup) about once a second and restarted 1 s later. Lease: 2 s. Raw output is in `results/chaos.json`.

| Metric | Result |
|---|---|
| Runs | 500 (2,000 steps) |
| Workers killed | 435 |
| Steps taken over from a dead worker | 361 |
| **Runs lost** | **0** (500 / 500 done) |
| **Duplicate side effects** | **0**. 59 repeated requests reached the ledger after kills; the idempotency key deduped every one. |
| Missing side effects | 0 |
| Takeover time after a kill | median 1.91 s, p95 2.36 s (≈ the 2 s lease), max 10.3 s |
| Extra attempts per step | 0.18 |

Caveats: it used the offline fake model, and the test steps sleep 0.1–0.6 s on purpose so kills land mid-step, so this is a durability test, not a throughput benchmark. The 10.3 s maximum is probably a kill that landed while the other workers were also down or restarting; I haven't confirmed that.

## Tests

```bash
.venv/Scripts/python -m pytest -q                     # 51 engine, API, SDLC and AWS-mode tests against real Postgres (sandbox tests skip without the image)
.venv/Scripts/python -m pytest -m chaos -s -q         # chaos test (CHAOS_RUNS=500 for the full run)
```

`tests/test_lambda_images.py` runs the real gate scripts and the preview through the Lambda images, using the Lambda emulator in the AWS base image. Its docstring has the commands; CI runs it on every push.

## How it works

```
Browser ──REST + SSE──► FastAPI ──► Postgres ◄── worker 1, 2, 3 …
                          ▲          │  runs · step_runs · run_events · approvals · tool_calls
                          └─ LISTEN ─┘  NOTIFY on every event
```

On AWS the workers are Lambda functions started on demand instead of long-running processes; the engine is the same (see [infra/README.md](infra/README.md)).

Workers claim steps with `SELECT … FOR UPDATE SKIP LOCKED`. The queue lives in the same database and transactions as the run state, so a step can never be "done" in the queue but missing from the state. More detail, including the low-level design, is in [PROJECT.md](PROJECT.md).

## Stack

Python 3.13 · FastAPI · Postgres 16 (state, queue, LISTEN/NOTIFY) · psycopg 3 · pydantic · Server-Sent Events · Docker · pytest · AWS Lambda · Terraform · GitHub Actions
