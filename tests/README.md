# Regression tests

```bash
python -m pytest              # ~15s, no server needed, no LLM, no email sent
python -m pytest --llm        # also run the tests that call a real model
python -m pytest -k papers    # one area
```

**Run this before and after any AI tool touches this repo.** That is what it is
for: several features here fail *silently* rather than loudly, and this suite is
the only thing that notices.

## What it needs

Just the database (`python -m core.init_db` once). The API is driven in-process
through `TestClient` — do **not** start uvicorn first.

## Safety properties baked in

- **No email ever leaves the machine.** `core.mailer.send_email` is patched out
  for the whole session (autouse), and asserts any recipient is `@ats.local`.
  Nearly every write path here sends mail; an opt-in guard would be one
  forgotten fixture away from mailing a real candidate.
- **No leftover data.** Every job a test creates is cascade-deleted on teardown,
  which doubles as a live check that the cascade still covers every child table.
- **No LLM by default.** Model-dependent tests are marked `llm` and skipped
  unless you pass `--llm`, so the suite stays fast, free and deterministic.

## The tests that matter most

| File | Guards |
|---|---|
| `test_papers.py` | **The scoring spine.** A test is a POOL; each candidate draws their own paper. Every scoring path must read `questions_for(assignment)`, never `test.questions`. Getting this wrong does not raise — it silently marks candidates wrong for questions they never saw (acing your own 5-of-10 paper scores 50, not 100). |
| `test_attempts.py` | Reset keeps history and mints a *different* link. Guards the `_check_duplicate` landmine: a kept attempt-1 submission must not make the replacement link 410 "already completed". |
| `test_job_delete.py` | The job cascade covers **every** child table. This broke twice from two hand-maintained copies drifting; there is now one (`core/job_delete.py`) and a test that forbids re-forking it. |
| `test_gate_and_auto_invite.py` | Auto-invite stays off unless opted in, fires once, and **never breaks the submission it rides on**. |
| `test_predictability.py` | The overlap/exposure numbers HR is shown are correct. |

If a change makes these fail, fix the change — not the test. Each failure
message says what would break in production.

## Known noise

`datetime.utcnow()` deprecation warnings are pre-existing and filtered in
`pytest.ini`. Fixing them means moving every column to timezone-aware
datetimes — its own change, not something to smuggle into a test run.
