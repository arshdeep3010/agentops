"""Workflow definitions: steps, dependencies, conditions, and the registries that run them.

A workflow is plain data (stored as JSON per version), so a run is always executed against
the exact version it started with. Python functions are referenced by name.
"""

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from pydantic import BaseModel

KINDS = ("code", "llm", "tool", "approval")


@dataclass(frozen=True)
class StepSpec:
    key: str
    kind: str
    title: str = ""
    needs: tuple[str, ...] = ()
    # Run only if a condition on earlier output holds, else the step is skipped.
    # {"ref": "check_rules.needs_approval", "op": "eq", "value": true}
    when: dict | None = None
    config: dict = field(default_factory=dict)
    max_attempts: int = 3
    timeout_s: float = 120.0
    # When this step's gate fails: rewind `rewind_to` (and everything after it) with the gate's
    # feedback, at most `max_loops` times; then the step fails. {"rewind_to": "build", "max_loops": 4}
    on_fail: dict | None = None


@dataclass(frozen=True)
class Workflow:
    name: str
    version: int
    steps: tuple[StepSpec, ...]
    description: str = ""

    def __post_init__(self):
        keys = [s.key for s in self.steps]
        if len(keys) != len(set(keys)):
            raise ValueError(f"{self.name}: duplicate step keys")
        for s in self.steps:
            if s.kind not in KINDS:
                raise ValueError(f"{self.name}.{s.key}: unknown kind {s.kind!r}")
            for dep in s.needs:
                if dep not in keys:
                    raise ValueError(f"{self.name}.{s.key}: needs unknown step {dep!r}")
            target = (s.on_fail or {}).get("rewind_to") or s.config.get("rewind_on_changes")
            if target and target not in keys:
                raise ValueError(f"{self.name}.{s.key}: rewinds to unknown step {target!r}")
        self.order()  # raises on cycles
        for s in self.steps:  # loops may only go backwards, to this step or one before it
            target = (s.on_fail or {}).get("rewind_to") or s.config.get("rewind_on_changes")
            if target and s.key not in self.downstream(target):
                raise ValueError(f"{self.name}.{s.key}: can only rewind to itself or an earlier step")

    def step(self, key: str) -> StepSpec:
        for s in self.steps:
            if s.key == key:
                return s
        raise KeyError(key)

    def order(self) -> list[str]:
        """Topological order; raises ValueError on a cycle."""
        done: list[str] = []
        remaining = {s.key: set(s.needs) for s in self.steps}
        while remaining:
            ready = sorted(k for k, deps in remaining.items() if deps <= set(done))
            if not ready:
                raise ValueError(f"{self.name}: dependency cycle in {sorted(remaining)}")
            done.extend(ready)
            for k in ready:
                del remaining[k]
        return done

    def downstream(self, key: str) -> set[str]:
        """The step itself and everything that depends on it, directly or not."""
        result = {key}
        changed = True
        while changed:
            changed = False
            for s in self.steps:
                if s.key not in result and result.intersection(s.needs):
                    result.add(s.key)
                    changed = True
        return result

    def graph(self) -> dict:
        def step(s: StepSpec) -> dict:
            d = {**asdict(s), "needs": list(s.needs)}
            if d["on_fail"] is None:
                del d["on_fail"]  # keeps graphs stored before loops existed byte-identical
            return d

        return {"name": self.name, "version": self.version, "description": self.description,
                "steps": [step(s) for s in self.steps]}

    @classmethod
    def from_graph(cls, graph: dict) -> "Workflow":
        steps = tuple(StepSpec(**{**s, "needs": tuple(s.get("needs", ()))}) for s in graph["steps"])
        return cls(name=graph["name"], version=graph["version"], steps=steps,
                   description=graph.get("description", ""))


# ---------------------------------------------------------------- references and conditions

_TEMPLATE = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")


def lookup(path: str, scope: dict) -> Any:
    """Resolve "extract.amount_inr" or "input.invoice_text" against {input, <step outputs>}."""
    value: Any = scope
    for part in path.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        else:
            return None
    return value


def resolve(value: Any, scope: dict) -> Any:
    """Replace "$step.field" strings (recursively) with values from earlier output."""
    if isinstance(value, str) and value.startswith("$"):
        return lookup(value[1:], scope)
    if isinstance(value, dict):
        return {k: resolve(v, scope) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, scope) for v in value]
    return value


def render(template: str, scope: dict) -> str:
    import json

    def sub(match: re.Match) -> str:
        found = lookup(match.group(1), scope)
        return found if isinstance(found, str) else json.dumps(found, ensure_ascii=False)

    return _TEMPLATE.sub(sub, template)


_OPS: dict[str, Callable[[Any, Any], bool]] = {
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "gt": lambda a, b: a is not None and a > b,
    "lt": lambda a, b: a is not None and a < b,
    "truthy": lambda a, _: bool(a),
    "falsy": lambda a, _: not a,
}


def condition_holds(when: dict | None, scope: dict) -> bool:
    if not when:
        return True
    op = _OPS[when.get("op", "eq")]
    return op(lookup(when["ref"], scope), when.get("value"))


# ---------------------------------------------------------------- registries

WORKFLOWS: dict[str, Workflow] = {}
CODE: dict[str, Callable] = {}
TOOLS: dict[str, Callable] = {}
SCHEMAS: dict[str, type[BaseModel]] = {}
FAKE_LLM: dict[str, Callable] = {}
GATES: dict[str, Callable] = {}  # fn(ctx, output) -> (passed: bool, feedback: dict)


def register_workflow(wf: Workflow) -> Workflow:
    WORKFLOWS[wf.name] = wf
    return wf


def code(name: str):
    def deco(fn):
        CODE[name] = fn
        return fn
    return deco


def tool(name: str):
    def deco(fn):
        TOOLS[name] = fn
        return fn
    return deco


def schema(name: str):
    def deco(model):
        SCHEMAS[name] = model
        return model
    return deco


def gate(name: str):
    def deco(fn):
        GATES[name] = fn
        return fn
    return deco


def fake_llm(name: str):
    def deco(fn):
        FAKE_LLM[name] = fn
        return fn
    return deco
