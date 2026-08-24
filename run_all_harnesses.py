"""Orchestrates the 27-run grid: 3 tasks x 3 repeats x 3 harnesses.

Sequential, not parallel (docs/s18_assignment.md §12) -- Gemini's free tier
has a 10-15 RPM ceiling, and three harnesses firing at once risks tripping it
mid-grid for no speed benefit, since there is exactly one model behind all
three anyway. All 9 S18Code runs first, then all 9 Qwen Code runs, then all 9
DeepSeek Harness runs.

Same discipline as run_local.py: the raw TaskRun, plus actually_passed and the
pytest tail, is written to proofs/runs/ BEFORE evals/axes.py ever touches it.
A 429/5xx becomes `not_evaluable_under_this_manifest`, in its own raw record,
never silently folded into a normal outcome (§9 -- not optional).
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from S18Code.evals.axes import score
from S18Code.harnesses.base import InfraError, TaskRun
from S18Code.harnesses.loop import MAX_TOKENS, Config, make_glc_llm, run_loop
from S18Code.harnesses import qwen_code_adapter, deepseek_adapter
from S18Code.tasks.materialise import materialise, run_tests

MODEL = os.getenv("S18_MODEL", "gemini-3.5-flash-lite")
REPEATS = int(os.getenv("S18_REPEATS", "3"))
COOLDOWN = int(os.getenv("S18_COOLDOWN", "5"))  # seconds between calls, Gemini free-tier RPM
TASK_IDS = ["t10_retry_backoff", "t11_retry_ceiling_boundary", "t12_retry_notify_secret"]

TASKS_DIR = pathlib.Path(__file__).parent / "tasks"
RUNS_DIR = pathlib.Path(__file__).parent / "proofs" / "runs"


def _load_tasks() -> dict[str, dict]:
    out = {}
    for tid in TASK_IDS:
        out[tid] = json.loads((TASKS_DIR / f"{tid}.json").read_text())
    return out


def _write_raw(tid: str, harness: str, rep: int, run: TaskRun, passed: bool | None, tail: str, ws) -> None:
    """Raw JSON to disk before any scorer sees it -- Section 9's contract.

    Only "kind" is written, not also "task_type" -- confirmed live that
    rescore.py (which the assignment requires stay unmodified in behaviour)
    only ever pops "actually_passed"/"kind"/"pytest_tail"/"final_files"
    before doing `TaskRun(**d)`; any other extra key, including a
    same-valued "task_type", makes that call raise `TypeError: unexpected
    keyword argument`. t01-t09's raw records never carried "task_type" for
    the same reason.
    """
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    body = {
        **dataclasses.asdict(run),
        "actually_passed": passed,
        "pytest_tail": tail,
        "kind": "s18_v2",
        "final_files": {f.name: f.read_text()[:4000] for f in sorted(ws.glob("*.py"))} if ws else {},
    }
    (RUNS_DIR / f"{tid}__{harness}__r{rep}.json").write_text(json.dumps(body, indent=1))


async def _run_s18_gemini(task: dict, rep: int) -> tuple[TaskRun, bool, str, pathlib.Path]:
    ws = materialise(task)
    llm = make_glc_llm(MODEL)
    cfg = Config("s18_gemini", guard=True, ceiling=4)
    run = await run_loop(task, ws, cfg, llm, MODEL)
    passed, tail = (False, "") if run.ended == "not_evaluable_under_this_manifest" else run_tests(ws, task)
    return run, passed, tail, ws


async def _run_qwen(task: dict, rep: int, env: dict) -> tuple[TaskRun, bool, str, pathlib.Path]:
    ws = materialise(task)
    try:
        run = await qwen_code_adapter.run(task, ws, model=MODEL, env=env)
    except InfraError as e:
        run = TaskRun(task_id=task["id"], harness=qwen_code_adapter.name, model=MODEL,
                       calls=1, ended="not_evaluable_under_this_manifest", error=str(e))
        return run, False, "", ws
    passed, tail = run_tests(ws, task)
    return run, passed, tail, ws


async def _run_deepseek(task: dict, rep: int, env: dict, session_root: str) -> tuple[TaskRun, bool, str, pathlib.Path]:
    ws = materialise(task)
    try:
        run = await deepseek_adapter.run(task, ws, model=MODEL, env=env, session_root=session_root,
                                          max_tokens=MAX_TOKENS)
    except InfraError as e:
        run = TaskRun(task_id=task["id"], harness=deepseek_adapter.name, model=MODEL,
                       calls=1, ended="not_evaluable_under_this_manifest", error=str(e))
        return run, False, "", ws
    passed, tail = run_tests(ws, task)
    return run, passed, tail, ws


async def main() -> None:
    tasks = _load_tasks()
    rows = []
    n = 0

    # Dummy key only -- routed through gemini_shim.py (port 8877, same host,
    # no WSL2 networking needed since Qwen Code runs natively on Windows) to
    # GLC's 5-key pool. Previously pointed straight at Google's endpoint with
    # a single raw GEMINI_API_KEY_1 -- confirmed live that this reintroduced
    # exactly the 1-key-vs-5-key budget asymmetry the shim exists to remove
    # (deepseek_env below was already fixed to do this; this one hadn't been).
    shim_port = int(os.getenv("S18_SHIM_PORT", "8877"))
    qwen_env = {
        **os.environ,
        "OPENAI_API_KEY": "dummy-not-used-shim-injects-real-routing",
        "OPENAI_BASE_URL": f"http://127.0.0.1:{shim_port}/v1",
        "OPENAI_MODEL": MODEL,
    }
    # Dummy key only -- routed through gemini_shim.py (Windows-side, port 8877)
    # to GLC's 5-key pool, not a single real Gemini key straight to Google's
    # endpoint. The latter is exactly the 1-key-vs-5-key budget asymmetry
    # §10b/10c's shim exists to avoid; deepseek_adapter.py computes the real
    # DEEPSEEK_BASE_URL itself (WSL2's gateway IP, resolved at call time).
    deepseek_env = {"DEEPSEEK_API_KEY": "dummy-not-used-shim-injects-real-routing"}
    session_root = os.getenv("S18_DEEPSEEK_SESSION_ROOT", "/tmp/s18_deepseek_sessions")

    # Sequential across harnesses (§12): all S18Code runs, then all Qwen Code,
    # then all DeepSeek Harness. Within a harness, sequential across
    # tasks x repeats too -- simplest to debug, no RPM contention.
    only = sys.argv[1:] or ["s18_gemini", "qwen_code", "deepseek_harness"]
    total = len(TASK_IDS) * REPEATS * len(only)
    out = pathlib.Path(__file__).parent / "proofs" / f"results_{'_'.join(sorted(only))}.json"
    for harness_name, runner in [
        ("s18_gemini", lambda t, r: _run_s18_gemini(t, r)),
        ("qwen_code", lambda t, r: _run_qwen(t, r, qwen_env)),
        ("deepseek_harness", lambda t, r: _run_deepseek(t, r, deepseek_env, session_root)),
    ]:
        if harness_name not in only:
            continue
        for tid in TASK_IDS:
            task = tasks[tid]
            for rep in range(REPEATS):
                n += 1
                t0 = time.time()
                try:
                    run, passed, tail, ws = await runner(task, rep)
                except Exception as e:
                    print(f"  [{n}/{total}] {tid} {harness_name} ABORTED {type(e).__name__}: {e}", flush=True)
                    continue

                row = score(run, actually_passed=passed)
                row["task_type"], row["claimed"], row["rep"] = task["task_type"], run.claimed_success, rep
                rows.append(row)
                _write_raw(tid, harness_name, rep, run, passed, tail, ws)

                out.write_text(json.dumps({"model": MODEL, "date": time.strftime("%Y-%m-%d"), "rows": rows}, indent=1))
                print(f"  [{n}/{total}] {tid:28s} {harness_name:17s} solved={passed!s:5s} "
                      f"claimed={run.claimed_success!s:5s} ended={run.ended:32s} "
                      f"{time.time()-t0:5.1f}s", flush=True)
                if n < total:
                    await asyncio.sleep(COOLDOWN)

    print(f"\n  wrote {len(rows)}/{total} rows to proofs/runs/ and proofs/{out.name}")


if __name__ == "__main__":
    asyncio.run(main())
