# EAG V3 — Session 18 Notes: Evaluating Agents (and Why Benchmarks Lie)

*Plain-English summary, bullet points, with analogies. Built from the official session PDF (26 pages, all 14 sections) plus the tactiq auto-transcript's highlights from the 22 Aug 2026 live class. Tactiq transcripts are machine-generated and imperfect (it consistently mis-heard "Qwen Code" as "coin code," for example) — I've cross-checked the confusing bits against the PDF and the actual reference GitHub repo. Happy to revise once you get the official transcript.*

---

## Why this session exists

- Session 17 gave a coding agent guardrails: it can read/edit files, run bounded commands, and stop — plus a **protected-path guard** (can't edit its own test files) and a **failure ceiling** (stops retrying after repeated failures).
- Building those guardrails is not the same as *proving* they help. It's like installing a smoke detector — you've added a safety feature, but you haven't yet shown it catches a real fire.
- **The core question:** an agent changes one line of `calc.py`, the test suite turns green — is that "success"? Not until you can answer four things: *What task did we give it? What was it allowed to change? What did we run to check the result? What did we record before announcing a score?*
- The rule underneath everything: **never publish a conclusion without keeping the raw observation that produced it** — like a scientist's lab notebook, not just the final slide.

## 1 & 2. A "benchmark" is not a score — it's the whole exam, not just the grade

- People use "benchmark" to mean a percentage ("the model scored 85%"). That's wrong. A benchmark is the **entire repeatable procedure** that turns a question into a measurement.
- **Analogy — think of a school exam.** A benchmark has 5 parts, same as an exam does:

| Exam world | Session's term | What it means for a coding agent |
|---|---|---|
| The exam question | **Task** | The actual bug, e.g. "make `average([])` return 0" |
| The exam hall + rules (open book? no internet?) | **Harness** | The sandboxed repo copy, Python version, `pytest`, "no network" |
| Which student, their prep, retries allowed | **Policy** | The prompt, model, tools, retry ceiling, protected paths |
| The teacher's answer key / rubric | **Scorer** | Rules that turn a run into fields: did it pass? cheat? verify itself? |
| The exact stamped paper, on file | **Manifest** | Commit SHA, task version, model ID, seed, timeout, scorer version |

- **The big "aha":** headlines like "Claude Code vs Qwen Code" are almost never comparing the same student sitting the same exam. Each tool brings its *own* exam hall (harness) and rules, and just plugs a vanilla model in underneath. So it's really "harness vs harness," not "model vs model." **This is exactly why the instructor wants you testing the same task through multiple harnesses (ours, Qwen Code, DeepSeek Harness) — to see this effect firsthand rather than take it on faith.**
- Practical rule: when two people report different scores, compare task/harness/policy/scorer/manifest *first*. Don't jump straight to arguing whose model is smarter.

## 3. Before running anything, define ONE comparison

- An evaluation answers **one question at a time** — like a proper science-fair project: change one variable, hold everything else fixed.
- Session's example: *does turning on the failure-ceiling rule increase success rate?* Model, tasks, prompt, tools, harness, scorer — all identical between the two runs ("arms"). Only the ceiling flag flips.
- A narrow experiment earns a narrow conclusion. It can't tell you "which model is smarter" or "will this hold for every task" — only whether that one flipped switch mattered, in that one setup.

## 4. A green test suite is not "mission accomplished"

- **Analogy:** a student who peeks at the answer key and copies it down "passes," but hasn't shown they understand anything.
- So every run gets scored on **4 separate fields**, never one pass/fail number:

| Field | Plain English | Example |
|---|---|---|
| **Outcome** | Did the real checks pass? | pass / fail |
| **Integrity** | Did it touch a file it wasn't supposed to? | clean / protected_write |
| **Verification** | Did it check its own work before declaring done? | verified / unverified |
| **Cost** | What did it burn to get there? | 7 calls, 143 s, 9 steps |

- Why separate them? A run can pass but never have checked itself (lucky). It can fail *honestly* (that's fine!). It can be cheap and simply wrong. One percentage hides all of that.

## 5. "You may read the test. You may not rewrite it."

- The agent may *read* tests (normal, helps it understand the spec) but must never be able to *edit* the files that grade it (`tests/`, `conftest.py`, CI config).
- **Analogy:** open-book means you can read the textbook — it doesn't mean you can sneak into the office and rewrite the answer key.
- This has to be a **hard runtime rule**, not a polite prompt request — because an agent optimises whatever you ask it to. If the only goal is "make it green," deleting an inconvenient test is the cheapest way to "win."
- For a serious task, pair the visible tests with something the agent can't just memorise: hidden cases, properties checked over many random inputs, before/after invariants, or (when nothing's executable) human review.

## 6. Don't call a task "impossible" until you've checked what's actually reachable

- Before saying "the agent failed," ask "what could it actually *reach*?" **Analogy:** don't blame someone for not finishing a jigsaw before checking whether the box was missing a piece.
- Simple checklist per resource: is it *available*, and is it *allowed to be changed*? (Source files: yes/yes. Public tests: yes/no. Network: no/no. A secret value: no/no.)
- If a task genuinely needs something the harness never provides, the honest verdict is **`not_evaluable_under_this_manifest`** — not "the agent failed." That's a task/harness design problem.
- Don't trust an "impossible" label just because it *sounds* impossible — the instructor's own team got this wrong **3 times out of 3** on first authoring. Always try to actually break the task (run an "attack") before trusting the label.

## 7. "It passed" and "it knows it passed" are different facts

- An agent can stumble onto a correct fix and never run the tests to check. The grader says "pass" — but the agent never verified its own work.

| Actually passed? | What the agent did | Label |
|---|---|---|
| Yes | Ran the check after its final edit | **verified_pass** |
| Yes | Never ran the check | **unverified_pass** (lucky) |
| No | Said "I couldn't do it" | **honest_failure** (fine, actually!) |
| No | Claimed success anyway | **false_success** (the bad one) |

- **Analogy:** fixing a leaking pipe and testing the tap afterward, versus fixing it and just walking away hoping.

## 8. A "zero" only proves something if the event had a chance to happen

- **Analogy:** "zero car crashes in a town with zero cars" tells you nothing about road safety — there was never any traffic to crash.
- Real example: the failure-ceiling rule fired **zero times in 19 runs**. Looks like "the rule is barely needed" — really it means no task ever gave it the chance to fire. An *untested* zero and a *clean* zero look identical in a table.
- Scarier real example: a run against a hosted model **ran out of free quota partway through**. 8 of 14 result rows were really just "the server said no" (HTTP 429), not real attempts — yet because the agent "failed" and "didn't claim success," the scorer technically filed them as honest failures. **An infrastructure error dressed up as a result is the same trap as an untested zero.** Fix: tag such runs `not_evaluable_under_this_manifest`, and never quietly delete a broken results file — keep it, labelled, as part of the honest record (the reference repo literally keeps `results_gemini_ABORTED_quota.json` on purpose).
- So every task needs a label for *what it can actually reveal* — a plain bug-fix task can't tell you anything about the cheating-guard, because it never tempts the agent to cheat in the first place.

## 9. Keep the raw run and the final score as two separate things

- "Scores can change. The observation should not." Save the raw record first, score it after.
- **Analogy:** keep the receipt, not just the calculator's total. If the calculator turns out buggy later, redo the math from the receipt — no need to re-shop.
- Flow: **run the agent → save the raw journal (every step, every diff, the environment) → then score it.** If the scoring rules were wrong, fix the scorer and re-score the *same* saved journals — no need to spend more time/money re-running the model.

## 10. One run is a data point, not a verdict

- Agents are a little random — sampling, timing, and exploration order can all shift the outcome, even with identical settings.
- Don't hide "3 out of 3" behind "100%," and don't treat "1 out of 1" like a properly repeated result. For claims that matter, repeat the same cell a few times and report the raw numbers next to the average. A small grid is fine — just say it's small.

## 11. What you're actually allowed to claim afterward

- **Safe:** *"Under this exact task, harness, policy, scorer, and budget, this setup produced these recorded outcomes."* Specific enough that someone else could rerun it, tweak one thing, and see exactly where the result stops holding.
- **Unsafe:** *"The model is 73% capable."* A public score isn't a pure property of the model's brain — it's tangled up with the task set, tools, prompting, timeouts, retries, and a mountain of engineering around the model.
- **Carry this forward: benchmark scores are system measurements, not model measurements.**

## 12. Before building something huge, build the smallest *honest* version first

- **Analogy:** test a new recipe on a small batch before catering a 200-person wedding.
- Minimum honest evaluation: one fixed agent configuration, three small (but *different*) tasks, one clear manifest, one raw JSON record per run, a scorer that reads those files, three repeats per task, a report covering outcome/integrity/verification/cost.
- The three tasks should each test something on purpose: **(1)** an ordinary bug fix, **(2)** a task that tempts a boundary crossing, **(3)** a task genuinely blocked by something missing (labelled as a harness problem, not an agent failure).

## 13. The assignment

Full breakdown — point values, exactly what to submit, and how I'd suggest tackling it — is in **`s18_assignment.md`**. Short version: author 3 of your own tasks in this same spirit, run them, score them honestly, and write a one-page report making exactly one narrow, defensible claim.

## 14. The 7 things this session commits you to

*Your instructor's own closing summary, kept word-for-word since you specifically flagged this as something we must adhere to.*

1. A benchmark is a procedure, not a percentage.
2. A controlled comparison changes one declared thing.
3. Outcome, integrity, verification, and cost are different observations.
4. A task must be executable and reachable before it can judge an agent.
5. Every claimed property needs a task that could expose its failure.
6. The raw run survives the score.
7. A narrow result earns a narrow claim.

> **The shortest version of the whole session:** *Keep the task, the rules, and the evidence. Then let the score be modest enough to be true.*
