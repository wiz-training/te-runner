# Review: findings ranked by return on effort

Every finding from the code-level and design-level reviews, one row each, ranked by payoff per unit
of work. Effort: S = under a day, M = days, L = a week or cross-repo. Baseline: `ruff` clean, radon
average A with 13 functions at C, 202 tests in `test_wizlab.py` green (plus 5 reaper, 1 entrypoint).
Symbols name the `wizlab` package unless a path is given. The last column is the blast radius: what the fix
touches, what depends on it (tests, labs, other repos), and where the fixing agent looks next.

## Tier 4 — measure before deciding

| # | Finding | Do | Effort | Blast radius → guidance |
|---|---|---|---|---|
| 30 | The reaper's audit layer, its five-outcome model and `UNKNOWN` handling exist for free-named GUI creates. Every lab now instructs `lab-<sid>-*` names; nothing measures how often audit removes what the sweep missed | read the `audit-only` count `cmd_reap` prints on its summary line across several weeks of `reap.yml` logs; near zero → keep audit as report, sweep is the delete path | L | `_reap_enumerate`, `_reap_one`, `_input_name`, `_REAP_OVERRIDES`. `SPEC.md` records the current exit-0 promise as an operator decision, so a change there goes through the change process. |

## Holds up — keep

Exit codes 0/1/2/3 (platform-forced). The `lab-<sid>` stem (replaced an account stem after a live
collision). `_submissions` owning the resend budget by document type. Grading enums, never UI labels.
Every measurement carrying its reproducer. One pinned image per lab.

## Next actions

#30 is tracked in `te-labkit-v2/TODO.md` (reaper audit window), with the measurement that decides it.
