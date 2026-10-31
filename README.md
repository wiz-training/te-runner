# te-runner

The one Docker image TE 2.0 labs reference.
Carries `wizlab` (`/usr/local/bin/wizlab`) on python 3.12 and the CLIs the `build.yml` smoke step
asserts by name; `Dockerfile` holds each pin.

Build: CI only — push a `v*` tag to `origin` (`wiz-training/te-runner`), whose
Actions publish the **public** `ghcr.io/wiz-training/te-runner:<tag>` that labs
pin (Instruqt pulls anonymously; there is no registry-credential path for GHCR).
The build lints the tagged sha, smoke-tests and pushes the versioned image,
and then advances `ghcr.io/wiz-training/te-runner:reap` (which `reap.yml` pins). Never `latest`.
The package must stay **Public** and list this repo under "Manage Actions
access" with Write — both live in the package settings, not the workflow, and
losing either shows up as `manifest unknown`/401 on pull or
`denied: permission_denied: write_package` on push. Tags through v0.1.50 live
only at `ghcr.io/eh24905-wiz/te-runner`; v0.1.51+ publish here.

A tag reaches every lab that moves its pin, so it has the blast radius a module
publish does: `wiz-tenant` 1.0.0 took out 23 labs at once because nothing booted
it before labs pinned it. Canary a new tag on `te-dev-aws` — bump that lab's pin
first, boot it, reach "Enter lab" — before any other lab moves. `runner-floor.py`
checks a pin meets a lab's verb floor, not that the image boots.

Read tags with `git tag --sort=-v:refname` — the default sort is lexicographic
and puts `v0.1.9` above `v0.1.36`. There are no GitHub release objects, so the
newest tag IS "the latest released image", and `main` may run ahead of it: what
labs pin is the tag, never `HEAD`.

Each image names itself — `TE_RUNNER_TAG`/`TE_RUNNER_REV` in its env and OCI
`image.version`/`image.revision` labels — so a running grader can be asked what
it is (`wizlab session verify` prints it), and a lab can assert its verb floor
with `session verify --min-runner vX.Y.Z` (`wizlab/SPEC.md`). Both are empty on
images before v0.1.37, which is why that floor gate binds only at v0.1.37+.

## Env contexts on a grader
A lab passes the Wiz pair and `OKTA_WF_*` through exec and task `environment` maps, never
`container.environment` (te-labkit-v2 `authoring/lab-patterns.md` §One container), so a `wizlab` call
that works in one context exits 3 in another:

| Context | Carries | Reach it by |
|---|---|---|
| platform check/solve/exec executor | `container.environment` (lease creds, `WIZ_TENANT`, `INSTRUQT_SESSION_ID`, `TE_RUNNER_*`) plus the script's own map: Wiz pair, `OKTA_WF_*` | the platform only |
| PID 1, `tmux attach -t dev` | `container.environment` only: no Wiz pair, no `OKTA_WF_*` | dev tracks |
| plain `ssh root@<grader>` | nothing: sshd starts each session clean | dev tracks |
| `/run/lab/wiz.env` (root 0600) | what `wizlab-grant` snapshotted from the setup exec (`wizlab/SPEC.md` §The learner shell) | `set -a; . /run/lab/wiz.env; set +a` |

Over ssh, source the snapshot first. The `te-dev-<csp>` profiles put the pair on
`container.environment`; there PID 1 holds the whole set:

```sh
while IFS= read -r -d '' kv; do export "$kv"; done < /proc/1/environ
```

`wizlab` exit codes and the `--check` remap learner checks use: `wizlab/SPEC.md` §Grammar.

## Nightly canary

`.github/workflows/canary.yml` runs daily at 05:00 UTC (an hour before `reap.yml` at 06:00 UTC) and on manual dispatch (`gh workflow run canary.yml -f lab=te-dev-aws`). It checks out `wiz-training/te-labkit-v2`, stops any leftover session cached in `WIZLAB_PLAY_DIR`, boots `--lab` via `wizops play start --grep "^CANARY"` (retrying once on `"Lab failed to start"`), writes `$GITHUB_STEP_SUMMARY`, and opens or comments on a `canary`-labelled issue on non-`ok` outcomes.

| Outcome | Condition | Exit / Issue |
|---|---|---|
| `ok` | `GREP=CANARY=ok` | exit 0, no issue |
| `degraded` | `GREP=CANARY=degraded` | exit 1, issue / comment |
| `no-verdict` | booted (`rc=0`), `GREP=none` | exit 1, issue / comment |
| `no-sandbox` | both attempts failed with `Lab failed to start` | exit 1, issue / comment |
| `error` | any other non-zero exit | exit 1, no issue |

Secrets: `INSTRUQT_TOKEN` (mapped to `INSTRUQT_API`) and `WIZ_LABS_GH_APP` (the `wiz-labs-code-grader` App, installed on every org repo): `.github/actions/app-token` mints a one-hour installation token from it for the te-labkit-v2 checkout, so no personal PAT is read.

A second job, `gamekit`, canaries the GameKit service on the same schedule with no lab and no standing event: it checks out `wiz-training/te-gamekit-service` and runs `service/roundtrip.py <live url> … canary --every` (create an event from the `canary` pack → register → answer one question of each type → board → delete). `GAMEKIT_CANARY=ok:…` is `ok`; `degraded:<step>` or no line opens or comments on the `canary`-labelled issue titled `… on GameKit`. Secrets: `WIZ_LABS_GH_APP` again for the te-gamekit-service checkout, `GAMEKIT_ADMIN_TOKEN`, `GAMEKIT_LAB_TOKEN` (SSM `/gamekit/admin_token`, `/gamekit/lab_token`). It lives here because the service repo is private and Actions minutes run only on public repos.

## Workflows

| Workflow | Purpose | Trigger / Secrets |
|---|---|---|
| `.github/workflows/secrets-sync.yml` (`scripts/secrets_sync.py`) | Syncs the 16 Instruqt `wiz` team secrets from AWS SSM (source of truth: `/te-labkit/*` plus `/gamekit/{admin,lab,super}_token`, base64-encoded for `upsertTeamSecret`) and reports unmanaged `teamSecrets` names without deleting them | Manual only: `gh workflow run secrets-sync.yml -f dry_run=false` (`-f only=NAME[,NAME]` restricts; default `dry_run=true`). Reads `INSTRUQT_TOKEN`, `AWS_SSM_SYNC_ACCESS_KEY_ID`, `AWS_SSM_SYNC_SECRET_ACCESS_KEY` (`us-east-1`; static IAM keys until the AWS account has a GitHub OIDC provider) |

Open items for the repo family live in `te-labkit-v2/TODO.md`; review backlogs behind them are
`research/review.md` and `research/health-plan.md`.
