"""Chaos-test workflow: fan-out, a side effect, and slow steps so random kills land mid-step.

    prepare → post (idempotent tool, pauses after the call) ┐
            → side (slow code)                              ┴→ finish
"""

import random

from engine.context import StepContext
from engine.workflow import StepSpec, Workflow, code, register_workflow


@code("chaos_work")
def chaos_work(ctx: StepContext) -> dict:
    lo, hi = ctx.config.get("min_s", 0.1), ctx.config.get("max_s", 0.6)
    took = random.uniform(lo, hi)
    ctx.sleep(took)
    return {"took_s": round(took, 3), "attempt": ctx.attempt}


CHAOS = register_workflow(Workflow(
    name="chaos",
    version=2,  # v2: pause inside the tool call too, so kills also land after the API applied it
    description="Used by the chaos test: parallel branches, one side effect, slow steps.",
    steps=(
        StepSpec("prepare", "code", "Prepare", config={"fn": "chaos_work"}, max_attempts=6),
        StepSpec("post", "tool", "Post (side effect)", needs=("prepare",), max_attempts=6, config={
            "tool": "post_to_ledger", "pause_inside_tool_s": 0.3, "pause_after_s": 0.2,
            "args": {"vendor": "chaos", "amount_inr": 1, "invoice_no": "$input.n"}}),
        StepSpec("side", "code", "Side branch", needs=("prepare",), config={"fn": "chaos_work"},
                 max_attempts=6),
        StepSpec("finish", "code", "Finish", needs=("post", "side"), config={"fn": "chaos_work"},
                 max_attempts=6),
    ),
))
