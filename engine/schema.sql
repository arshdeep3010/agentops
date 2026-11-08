-- AgentOps schema. run_events is the append-only log; runs / step_runs are the projection
-- of that log, always updated in the same transaction as the event that changes them.

CREATE TABLE IF NOT EXISTS workflows (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name        text NOT NULL,
    version     int  NOT NULL,
    graph       jsonb NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (name, version)
);

CREATE TABLE IF NOT EXISTS runs (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workflow_id       uuid NOT NULL REFERENCES workflows(id),
    status            text NOT NULL,          -- running | waiting | done | failed | cancelled
    input             jsonb NOT NULL DEFAULT '{}',
    overrides         jsonb NOT NULL DEFAULT '{}',  -- per-step config patches (replay)
    replay_of         uuid REFERENCES runs(id),
    tokens_used       int NOT NULL DEFAULT 0,
    cost_inr          numeric(12, 4) NOT NULL DEFAULT 0,
    budget_inr        numeric(12, 4),
    cancel_requested  boolean NOT NULL DEFAULT false,
    error             text,
    started_at        timestamptz NOT NULL DEFAULT now(),
    finished_at       timestamptz
);
CREATE INDEX IF NOT EXISTS runs_started_idx ON runs (started_at DESC);

CREATE TABLE IF NOT EXISTS step_runs (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id        uuid NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    step_key      text NOT NULL,
    -- pending | queued | running | waiting | done | skipped | failed | cancelled
    status        text NOT NULL,
    attempt       int  NOT NULL DEFAULT 0,    -- fencing token: +1 on every claim
    max_attempts  int  NOT NULL DEFAULT 3,
    worker_id     text,
    lease_until   timestamptz,
    queued_at     timestamptz,
    available_at  timestamptz NOT NULL DEFAULT now(),  -- retry backoff
    started_at    timestamptz,
    finished_at   timestamptz,
    output        jsonb,
    error         text,
    reused        boolean NOT NULL DEFAULT false,      -- output copied by a replay
    UNIQUE (run_id, step_key)
);
CREATE INDEX IF NOT EXISTS step_runs_claim_idx
    ON step_runs (available_at) WHERE status IN ('queued', 'running');

CREATE TABLE IF NOT EXISTS run_events (
    seq       bigserial PRIMARY KEY,
    run_id    uuid NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    step_key  text,
    type      text NOT NULL,
    data      jsonb NOT NULL DEFAULT '{}',
    at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS run_events_run_idx ON run_events (run_id, seq);

CREATE TABLE IF NOT EXISTS approvals (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id         uuid NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    step_key       text NOT NULL,
    request        jsonb NOT NULL DEFAULT '{}',   -- what the approver is shown
    decision       text,                          -- approved | rejected
    decided_by     text,
    note           text,
    requested_at   timestamptz NOT NULL DEFAULT now(),
    decided_at     timestamptz,
    UNIQUE (run_id, step_key)
);

-- Results of side-effecting tool calls, keyed so a retried step never repeats the effect.
CREATE TABLE IF NOT EXISTS tool_calls (
    idempotency_key  text PRIMARY KEY,
    run_id           uuid NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    tool             text NOT NULL,
    result           jsonb NOT NULL,
    at               timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS workers (
    id            text PRIMARY KEY,
    pid           int,
    started_at    timestamptz NOT NULL DEFAULT now(),
    last_seen     timestamptz NOT NULL DEFAULT now(),
    current_step  uuid,
    steps_done    int NOT NULL DEFAULT 0
);

-- A stand-in for an external accounting API that honours idempotency keys (like Stripe).
-- demo_ledger_requests records every request it receives; demo_ledger the effects applied.
CREATE TABLE IF NOT EXISTS demo_ledger_requests (
    id               bigserial PRIMARY KEY,
    idempotency_key  text NOT NULL,
    at               timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS demo_ledger (
    idempotency_key  text PRIMARY KEY,
    run_id           uuid NOT NULL,
    vendor           text,
    amount_inr       numeric(14, 2),
    invoice_no       text,
    at               timestamptz NOT NULL DEFAULT now()
);

-- Loops (added with the SDLC pipeline): a failed gate or "request changes" rewinds a step and
-- everything after it. `iteration` counts the loops; `attempt_floor` keeps the retry budget
-- per iteration while `attempt` keeps growing as the fencing token; `feedback` is what the
-- rewound step should fix this time.
ALTER TABLE step_runs ADD COLUMN IF NOT EXISTS iteration int NOT NULL DEFAULT 0;
ALTER TABLE step_runs ADD COLUMN IF NOT EXISTS attempt_floor int NOT NULL DEFAULT 0;
ALTER TABLE step_runs ADD COLUMN IF NOT EXISTS feedback jsonb;

-- Documents each phase produces (requirements.md, design.md, tests, diff, PR body…).
CREATE TABLE IF NOT EXISTS artifacts (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id      uuid NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    step_key    text NOT NULL,
    iteration   int NOT NULL DEFAULT 0,
    name        text NOT NULL,
    media_type  text NOT NULL DEFAULT 'text/markdown',
    sha256      text NOT NULL,
    content     text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, step_key, iteration, name)
);
