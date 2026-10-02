# Health plan: confirmed defects and durability gaps, in fix order

One row per finding from the 2026-10-02 read-through. Order is by blast radius: a fix whose
ripple is contained lands before one that changes a contract. Each row names the symbol, the
reproducer that proves it present, and what else the change touches. Status: `todo`, `fixed`,
`deferred (decision)`.

## Fix queue

| # | Symbol | Defect | Reproducer | Ripple | Status |
|---|---|---|---|---|---|
| 1 | `build.yml` smoke step, learner line | `A; test $? -eq 2 && B; test $? -ne 0 && C` passes for any exit of `A`: a failed `test` sets `$?` to 1, which the next `test -ne 0` accepts | `sh -c 'true && (exit 3) >/dev/null; test $? -eq 2 && false; test $? -ne 0 && true'; echo $?` → 0 | CI only; no code path | fixed |
| 2 | `connector._patch_aws` / `_reauth_guard` | The guard runs only when `same`; a drifted ARN on an Outpost-bound connector patches `authParams` without `outpostId`, dropping the binding | bound node, ARN differs, `connector ensure --role-arn X` with no outpost flags → `updateConnector` patch `{authParams:{customerRoleARN}}`, exit 0 | `cmd_connector_ensure` only caller; a repair solve on an Outpost lab now exits 2 until it passes the outpost flags | fixed |
| 3 | `outpost.cmd_outpost_delete`, `--id` branch | Absent record reads `status None`, not `GONE`; `uninstallOutpost` is sent and the poll runs to `--timeout` | `outpost delete --id <gone>` → `uninstallOutpost` sent, "still UNINSTALLING after the wait" | `--id` callers only; `_reap_outpost` has its own GONE path | fixed |
| 4 | `reap_orphans._wizlab` | `subprocess.run(timeout=300)` with no `TimeoutExpired` handler: one slow session tracebacks out of `main`, skipping every later session and tenant and the `REAP_SESSIONS` hint | mock `subprocess.run` to raise `TimeoutExpired` → traceback, exit 1, no summary | `_reap_session` reads the code: a timeout becomes 3 → FAILED for that sid, run continues | fixed |
| 5 | `user._kc_group_id` | Takes `groups[0]` without an exact-name check; `exact` on the groups endpoint is version-dependent | realm answering `[{name:"global-contributor-old"}]` for `search=global-contributor` → joined to the wrong group | `cmd_user_ensure` group-join; `FakeKeycloak` already filters exact | fixed |
| 6 | `core._claims` / `token_and_dc` | A non-JWT token or a missing `dc` claim raises IndexError/KeyError, reported as `internal error` exit 2, not environment 3 | auth answering `{"access_token":"opaque"}` → exit 2 | `token_and_dc` only; `--check` already remaps | fixed |
| 7 | `bin/wizlab-grant` snapshot loop | `env \| grep` is line-based (a value with a newline keeps its first line) and the allowlist omits `WIZ_<T>_AZURE_APP_OBJECT_ID`, `WIZ_<T>_LOGIN_URL`, tenant-less `WIZ_CLIENT_ID/SECRET`, `WIZLAB_SESSION_PREFIX` | `printf 'X=a\nb\n' \| grep '^X='` → `X=a`; `role inspect --cloud azure` from the learner shell → exit 3 | snapshot format unchanged (`%s=%q` lines), so `wizlab-learner` and the smoke `grep AWS_DEFAULT_REGION=` still hold | fixed |
| 8 | `workflow.cmd_workflow_ensure --dry-run` | Issues exit 1; `SPEC.md` says issues exit 2 (the caller's bug) | `--dry-run` on a definition with one issue → 1 | labkit `wiz-workflows-debug-201/research.md` records only "dry-run exit 0" cases; test row changes 1 → 2 | fixed |
| 9 | `reap.cmd_reap --last-min` vs `reap_orphans.WINDOW_H` | Audit window 1440 min, reaper window 48 h and never passes `--last-min`: creates older than 24 h at pass time are never enumerated; `audit-only` is undercounted | second-pass reap of a session whose creates are >24 h old → no audit rows | Widening doubles audit pages; past `_PAGE_CAP` (30×100 entries) the enumeration alert is FAILED and the run goes red. Needs the 48 h mutation count on each swept tenant first | deferred (decision) |

## Deferred, judgment calls

| Symbol | Why not now | Owner decides |
|---|---|---|
| `user._publish_user` vs `core._emit` | Two writers of `$EXEC_OUTPUT`; unifying echoes the password on a solve's stdout | add a quiet mode to `_emit` |
| `serviceaccount` and `mcp` ensure | Delete-then-mint through a deployment and an integration; `_ensure_sa` already serves the two plain service-account nouns, and a helper over these two takes four callables | refactor only with the next credential noun |
| `outpost` region, `--group` defaults | Literals with no env override; no lab has needed another value | add env defaults when one does |

Landed from the earlier table: `core.api` names an error it tolerates beside data on stderr;
`k8s` reuses `connector.CREATE`/`DELETE`; `WIZ_<T>_COGNITO_SUFFIX`/`WIZ_<T>_SSO_CLIENT_ID` override
`_TENANT_SSO`; `REAP_DOMAIN` rides on the reaper's `user` calls; `FOOTPRINT` ties every `ensure`
verb to `_SWEEP_TYPES`; ruff pinned 0.16.9.

## Next actions

Rows 1–8 shipped in `v0.1.63`. Row 9 and the deferred table are tracked in `te-labkit-v2/TODO.md`.
