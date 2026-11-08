"""Demo workflow: approve and post an invoice.

    extract (LLM) → check_rules (code) → approve (human, only if > ₹50,000 or unknown vendor)
                                        → post (tool, idempotent, skipped for duplicates) → summary (LLM)
"""

import re

from pydantic import BaseModel, Field

from engine.context import StepContext
from engine.workflow import StepSpec, Workflow, code, fake_llm, register_workflow, schema, tool

APPROVAL_THRESHOLD_INR = 50_000
KNOWN_VENDORS = {"acme supplies", "bluefin logistics", "nimbus cloud"}


@schema("InvoiceFields")
class InvoiceFields(BaseModel):
    vendor: str = Field(min_length=1)
    invoice_no: str = Field(min_length=1)
    amount_inr: float = Field(ge=0)
    invoice_date: str = ""


@schema("InvoiceSummary")
class InvoiceSummary(BaseModel):
    summary: str = Field(min_length=1, max_length=400)


@fake_llm("invoice_extract")
def _fake_extract(ctx: StepContext, prompt: str, try_number: int) -> str:
    """Offline stand-in for the model: reads the labelled fields."""
    text = ctx.input.get("invoice_text", "")

    def field(label: str) -> str:
        m = re.search(label + r"\s*[:#]\s*(.+)", text, re.IGNORECASE)
        return m.group(1).strip() if m else ""

    amount = re.sub(r"[^\d.]", "", field(r"(?:total|amount)(?:\s+due)?")) or "0"
    import json
    return json.dumps({"vendor": field("vendor"), "invoice_no": field(r"invoice\s*(?:no|number)"),
                       "amount_inr": float(amount), "invoice_date": field("date")})


@fake_llm("invoice_summary")
def _fake_summary(ctx: StepContext, prompt: str, try_number: int) -> str:
    import json
    inv, rules = ctx.outputs.get("extract", {}), ctx.outputs.get("check_rules", {})
    if rules.get("duplicate"):
        line = f"Invoice {inv.get('invoice_no')} from {inv.get('vendor')} was already posted; skipped."
    else:
        approved = " after approval" if ctx.outputs.get("approve") else ""
        line = (f"Posted invoice {inv.get('invoice_no')} from {inv.get('vendor')} for "
                f"₹{inv.get('amount_inr', 0):,.0f}{approved}.")
    return json.dumps({"summary": line})


@code("check_invoice_rules")
def check_invoice_rules(ctx: StepContext) -> dict:
    inv = ctx.outputs["extract"]
    flags = []
    known = inv["vendor"].strip().lower() in KNOWN_VENDORS
    if not known:
        flags.append("vendor not on the approved list")
    if inv["amount_inr"] > APPROVAL_THRESHOLD_INR:
        flags.append(f"amount over ₹{APPROVAL_THRESHOLD_INR:,}")
    with ctx.pool.connection() as conn:
        dup = conn.execute("SELECT 1 FROM demo_ledger WHERE lower(vendor) = lower(%s) AND invoice_no = %s",
                           (inv["vendor"], inv["invoice_no"])).fetchone()
    return {"needs_approval": bool(flags), "duplicate": bool(dup), "flags": flags, "known_vendor": known}


@tool("post_to_ledger")
def post_to_ledger(ctx: StepContext, args: dict, idempotency_key: str) -> dict:
    """Stand-in for an external accounting API that honours idempotency keys."""
    with ctx.pool.connection() as conn, conn.transaction():
        conn.execute("INSERT INTO demo_ledger_requests (idempotency_key) VALUES (%s)", (idempotency_key,))
        applied = conn.execute(
            "INSERT INTO demo_ledger (idempotency_key, run_id, vendor, amount_inr, invoice_no) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (idempotency_key) DO NOTHING RETURNING 1",
            (idempotency_key, ctx.run_id, args.get("vendor"), args.get("amount_inr"),
             args.get("invoice_no"))).fetchone()
    # Chaos test only: widen the window where "the API did it" but the engine hasn't recorded it.
    pause = getattr(ctx, "config", {}).get("pause_inside_tool_s")
    if pause:
        ctx.sleep(float(pause))
    return {"ledger_entry": idempotency_key, "applied": bool(applied)}


EXTRACT_PROMPT = """Extract these fields from the invoice below: vendor, invoice_no, amount_inr (the total,
as a number in rupees), invoice_date.

<invoice>
{{input.invoice_text}}
</invoice>"""

SUMMARY_PROMPT = """Write one sentence for the finance team about what happened to this invoice.

Invoice: {{extract}}
Rule check: {{check_rules}}
Approval: {{approve}}
Ledger: {{post}}"""

INVOICE = register_workflow(Workflow(
    name="invoice_approval",
    version=1,
    description="Extract an invoice, check rules, get approval for large or unknown-vendor invoices, post it once.",
    steps=(
        StepSpec("extract", "llm", "Extract fields", config={
            "schema": "InvoiceFields", "fake": "invoice_extract", "prompt": EXTRACT_PROMPT,
            "system": "You extract structured fields from invoices."}),
        StepSpec("check_rules", "code", "Check rules", needs=("extract",),
                 config={"fn": "check_invoice_rules"}),
        StepSpec("approve", "approval", "Human approval", needs=("check_rules",),
                 when={"ref": "check_rules.needs_approval", "op": "eq", "value": True},
                 config={"title": "Approve this invoice?",
                         "show": {"vendor": "$extract.vendor", "invoice_no": "$extract.invoice_no",
                                  "amount_inr": "$extract.amount_inr", "flags": "$check_rules.flags"}}),
        StepSpec("post", "tool", "Post to ledger", needs=("approve",),
                 when={"ref": "check_rules.duplicate", "op": "eq", "value": False},
                 config={"tool": "post_to_ledger",
                         "args": {"vendor": "$extract.vendor", "amount_inr": "$extract.amount_inr",
                                  "invoice_no": "$extract.invoice_no"}}),
        StepSpec("summary", "llm", "Write summary", needs=("post",), config={
            "schema": "InvoiceSummary", "fake": "invoice_summary", "prompt": SUMMARY_PROMPT,
            "system": "You write short, factual notes for a finance team."}),
    ),
))
