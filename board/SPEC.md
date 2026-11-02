# Action board spec

The board is a page in a lab `service` tab. Each button runs a fixed sequence of granted `wizlab`
verbs as the learner and shows each command, its output and its declared results. It grades nothing
and holds no secret. A new button is a manifest row; a new capability is a new `wizlab` verb under
`wizlab/SPEC.md` §What may be added. The board itself takes no per-lab code.

### Trust boundary
The server runs as `learner` under `env -i`. Every step is `wizlab <noun> <verb> …`, which
`bin/wizlab` re-issues as `sudo -n wizlab-learner`. The grant (`/run/lab/learner-verbs`) and the
per-verb flag allowlist in `wizlab-learner` are the whole permission model: the board adds none and
bypasses none. The HTTP API takes no arguments from the client — a request names an action id,
nothing else — so the page cannot run anything the lab did not declare.

### Lifecycle

| Stage | Who | Effect |
|---|---|---|
| boot | `entrypoint.sh`, when container env `TE_BOARD_PORT` is set (`7070`) | starts the server; `GET /` reads "Getting ready" until armed |
| grant | lab exec, `wizlab-grant "<noun> <verb>" …` | snapshot + learner verbs |
| arm | lab exec, `wizlab-board arm`, after grant | validates `$BOARD_MANIFEST`, resolves vars, writes `/run/lab/board.json` |
| run | learner click, or a solve's `wizlab-board run <id> --wait` | same executor |

The server reads `board.json` per request, so arming needs no restart.

| `wizlab-board run <id> --wait` exit | Cause |
|---|---|
| 0 | run `succeeded` |
| 1 | run `failed` |
| 3 | board down, not armed, or unknown id |

### Manifest
Contract: `manifest.schema.json`. The lab passes `BOARD_MANIFEST = file("board.json")` to the arm exec.

```json
{"schema": 1, "title": "Ship the fix", "actions": [{
  "id": "open-fix", "label": "Open the fix PR", "confirm": "Opens a PR on your repository.",
  "steps": [{"title": "Open the PR", "argv": ["patch", "open", "--repo", "{REPO}"], "timeout_s": 120}],
  "show": [{"key": "PR_URL", "label": "Pull request", "as": "link"}]}]}
```

`{NAME}` in `argv` resolves at arm from the exec's `BOARD_VAR_<NAME>`; `{STEM}` is built in (`lab-<sid>`).

| `wizlab-board arm` exit | Cause |
|---|---|
| 0 | armed |
| 2 | schema violation, unresolved `{NAME}`, or a step on `sensor`, `apiaccount`, `serviceaccount`, `mcp`, `user` (they print secrets) |
| 3 | no grant, or a step's `<noun> <verb>` absent from `learner-verbs` |

### HTTP API

| Route | Response |
|---|---|
| `GET /` | the page: one button per action, the latest run of each, polled every 2 s while running |
| `GET /api/board` | `{schema, title, actions: [{id, label, description, confirm}]}`; 503 until armed |
| `POST /api/actions/<id>/runs` | 202 `{run_id}`; 409 `{run_id}` of the active run; 404 unknown id. The body is ignored |
| `GET /api/runs/<run_id>` | the run envelope; 404 unknown |
| `GET /api/actions/<id>/runs/latest` | the newest envelope for the action; 404 none |

One run at a time board-wide. A re-click after a finished run starts a new run; the verbs make that
safe (`wizlab/SPEC.md` §What `ensure` promises, `patch open` rerun).

### Run envelope
One JSON file per run: a reload and a solve's poll read the same record.

| Field | Value |
|---|---|
| `schema`, `run_id`, `action`, `started_at`, `finished_at` | `1`, sortable id, action id, UTC times |
| `status` | `running` · `succeeded` · `failed` |
| `steps[]` | `title`, `argv`, `status` (`pending` `running` `succeeded` `failed` `skipped`), `exit_code`, `log` |
| `outputs` | `KEY=value` lines a step printed on stdout (`^[A-Z][A-Z0-9_]*=`), later steps overriding |

The first nonzero exit fails the run and skips later steps. `log` keeps the last 64 KiB of
stdout+stderr. A step over `timeout_s` is killed: `exit_code` 124.

### Rules a change keeps
- A check never reads the board or its run records; it grades tenant or GitHub state.
- A lab's solve for a board action calls `wizlab-board run`, never the verbs directly: the
  validator then proves the button.
- A manifest field that changes behaviour bumps `schema`; the server refuses a version it does not know.

### Open questions
- Whether the `service` URL answers without the learner's player session. If it does, anyone
  holding the URL can press a button; the actions stay the lab's, so the exposure is a re-run.
