"""Wraps DeepSeek Harness's minimal.py (inside WSL2) into the same TaskRun.

Written before the harness was actually installed (§15's WSL2/Ubuntu-24.04
setup was still in progress) -- so, same honesty rule as
qwen_code_adapter.py: this is a black-box subprocess wrapper, not a structured
tool-call trace. `verified`/`claimed_success` are approximated from stdout the
same way, not observed. docs/s18_assignment.md §10c says the harness keeps
its own JSONL session logs automatically; once it's actually running, check
`session_root/<session_id>.jsonl` for a real step trace and replace the
stdout-regex approximation below with that if the schema supports it -- don't
leave both methods coexisting silently disagreeing.
"""
from __future__ import annotations

import re
import shlex
import subprocess
import time
from pathlib import PureWindowsPath

from S18Code.harnesses.base import InfraError, Step, TaskRun

name = "deepseek_harness"

_FAIL_LANGUAGE = re.compile(
    r"\b(cannot|can't|unable to|could not|couldn't|failed to fix|did not fix|not able to)\b",
    re.IGNORECASE,
)
_TEST_EVIDENCE = re.compile(r"\bpytest\b|\bpassed\b|\bfailed\b|\berror\b", re.IGNORECASE)
_INFRA_ERROR = re.compile(r"\b(429|rate.?limit|quota|5\d\d\b|timed? ?out|ECONNREFUSED)\b", re.IGNORECASE)


def _to_wsl_path(win_path) -> str:
    """C:\\a\\b -> /mnt/c/a/b, so a WSL-side script can see a Windows workspace."""
    p = PureWindowsPath(str(win_path))
    drive = p.drive.rstrip(":").lower()
    rest = "/".join(p.parts[1:])
    return f"/mnt/{drive}/{rest}"


async def run(
    task: dict,
    ws,
    *,
    model: str,
    env: dict,
    session_root: str,
    max_tokens: int,
    distro: str = "Ubuntu-24.04",
    harness_dir: str = "~/WSL-Ubuntu-24-Wksp/deepseek-harness",
    shim_port: int = 8877,
    timeout: int = 300,
) -> TaskRun:
    run_ = TaskRun(task_id=task["id"], harness=name, model=model)
    t0 = time.time()
    original_tests = {rel: body for rel, body in task["tests"].items()}
    session_id = f"{task['id']}-{int(t0)}"
    wsl_ws = _to_wsl_path(ws)

    # WSL2's gateway IP is only known once inside the WSL2 network namespace,
    # and is not stable across host reboots/WSL restarts -- computed at call
    # time (confirmed working: `ip route show default`'s 3rd field). `env`
    # supplies only the dummy key; the shim injects real routing (gemini_shim.py).
    #
    # Written to a real .sh file and run as `bash <path>`, not passed inline as
    # `bash -lc "<one string with | and $(...) in it>"` -- confirmed live that
    # the latter is unreliable through `wsl.exe`'s own argv handling from a
    # Windows-side subprocess.run(list): the exact same pipe+substitution
    # string produced three different (all wrong) results across three
    # invocations. A plain script-file path has no shell metacharacters left
    # in the argv for anything to mis-parse.
    script = (
        # harness_dir is our own trusted default (starts with `~`), not quoted
        # -- shlex.quote would single-quote it and disable tilde expansion,
        # confirmed live as `cd '~/...': No such file or directory`.
        f"cd {harness_dir}\n"
        f". .venv/bin/activate\n"
        f"WSL_HOST_IP=$(ip route show default | awk '{{print $3}}')\n"
        f"export DEEPSEEK_API_KEY={shlex.quote(env['DEEPSEEK_API_KEY'])}\n"
        f'export DEEPSEEK_BASE_URL="http://$WSL_HOST_IP:{shim_port}/v1"\n'
        f"export DSH_MODEL={shlex.quote(model)}\n"
        f"python examples/jsonrpc-agent/minimal.py "
        f"--workspace {shlex.quote(wsl_ws)} --session-root {shlex.quote(session_root)} "
        f"--session-id {shlex.quote(session_id)} --max-tokens {max_tokens} "
        f"{shlex.quote(task['prompt'])}\n"
    )
    script_path = ws / "_dsh_run.sh"
    script_path.write_text(script, newline="\n")
    cmd = ["wsl", "-d", distro, "--", "bash", f"{wsl_ws}/_dsh_run.sh"]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        run_.error = f"deepseek harness timed out after {timeout}s"
        run_.ended = "max_steps"
        run_.calls = 1
        run_.seconds = time.time() - t0
        run_.steps.append(Step("command", "deepseek minimal.py", False, (e.stdout or "")[:300]))
        return run_

    run_.calls = 1
    stdout, stderr = proc.stdout or "", proc.stderr or ""
    combined = stdout + "\n" + stderr

    if proc.returncode != 0 and _INFRA_ERROR.search(combined):
        raise InfraError(f"deepseek harness exit {proc.returncode}: {combined[-300:]}", status=None)

    if not stdout.strip() and not stderr.strip():
        run_.unusable_replies = 1

    for rel, original in original_tests.items():
        p = ws / rel
        if p.is_file() and p.read_text() != original:
            run_.steps.append(Step("edit", rel, True, "post-hoc: protected test file changed on disk"))

    if _TEST_EVIDENCE.search(combined):
        run_.steps.append(Step("command", "(inferred from deepseek stdout)", True))

    run_.steps.append(Step("answer", detail=stdout.strip()[:200]))
    run_.ended = "done"
    run_.claimed_success = proc.returncode == 0 and not _FAIL_LANGUAGE.search(stdout)
    run_.seconds = time.time() - t0
    run_.tokens = len(combined) // 4
    return run_
