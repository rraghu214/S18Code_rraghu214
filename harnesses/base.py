"""The adapter that makes the comparison mean anything.

The experiment is: same model, same task, different harness. That only isolates
the harness if every harness is driven through one interface and scored from one
record. The moment a harness gets a bespoke call path, the result measures our
plumbing instead of their scaffold.

So every harness returns the same TaskRun, and the scorers never learn which
harness produced it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class Step:
    """One action. Whatever the harness calls it internally."""

    kind: str                      # read | edit | create | command | answer | refused
    target: str = ""               # path or command
    ok: bool = True
    detail: str = ""


@dataclass
class TaskRun:
    """What every harness must produce, and all the scorers ever see."""

    task_id: str
    harness: str
    model: str
    steps: list[Step] = field(default_factory=list)
    claimed_success: bool = False   # what the agent SAID
    seconds: float = 0.0
    tokens: int = 0
    error: str = ""

    # How the loop stopped: done | ceiling | max_steps | llm_error. Without this
    # a run that hit the step limit and a run that answered with nothing look
    # identical to a scorer, and the first version of `empty_billed` scored them
    # identically. They are not the same failure and must not share a column.
    ended: str = ""

    # Billing honesty. `calls` is every model call made and paid for; a reply is
    # `unusable` when it came back with no parseable action in it, which on a
    # reasoning model is the fully-billed non-answer this benchmark exists to
    # count. Rate, not flag: one bad reply in twelve is not the same defect as
    # twelve out of twelve.
    calls: int = 0
    unusable_replies: int = 0

    # Deliberately absent: whether it actually passed. That is the graders' job,
    # computed from the task's own tests, never from the agent's report. Keeping
    # the claim and the truth in separate fields is the whole point: their
    # disagreement is the Lucky Pass, and it is the number we came for.


class InfraError(Exception):
    """The call never reached the model — a 429/5xx, not a model failure.

    S18: an agent that stalls because the gateway said "try later" is not the
    same event as one that stalled because it couldn't solve the task. Folding
    both into ordinary `llm_error` is the exact "server said no" trap the
    session's own free-tier-quota story warns about. Raise this instead so the
    caller can label the run `not_evaluable_under_this_manifest` and keep it
    out of every real outcome/verification column.
    """

    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


class Harness(Protocol):
    name: str

    async def run(self, task: dict[str, Any], *, model: str, workspace: str) -> TaskRun:
        ...
