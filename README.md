# te-runner

The one Docker image TE 2.0 labs reference.
Carries `wizlab` (`/usr/local/bin/wizlab`) on python 3.12 and the CLIs the `build.yml` smoke step
asserts by name; `Dockerfile` holds each pin.

Build: CI only — push a `v*` tag to `origin` (`wiz-training/te-runner`), whose
Actions publish the **public** `ghcr.io/wiz-training/te-runner:<tag>` that labs
pin (Instruqt pulls anonymously; there is no registry-credential path for GHCR).
The build lints the tagged sha, smoke-tests the image, and refuses a tag
`reap.yml` does not pin, so bump that pin in the tagged commit. Never `latest`.
The package must stay **Public** and list this repo under "Manage Actions
access" with Write — both live in the package settings, not the workflow, and
losing either shows up as `manifest unknown`/401 on pull or
`denied: permission_denied: write_package` on push. Tags through v0.1.50 live
only at `ghcr.io/eh24905-wiz/te-runner`; v0.1.51+ publish here.

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

Open items for the repo family live in `te-labkit-v2/TODO.md`; review backlogs behind them are
`research/review.md` and `research/health-plan.md`.
