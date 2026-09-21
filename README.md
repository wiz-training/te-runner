# te-runner

The one Docker image TE 2.0 labs reference.
Carries `wizlab` (`/usr/local/bin/wizlab`), AWS CLI v2, Terraform 1.16.2, python 3.12, jq.

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

## Three env contexts on a grader
`entrypoint.sh` creates them, and they carry different environments — a `wizlab`
call that works in one exits 3 in another:

| Context | Carries `WIZ_*`, lease creds, `TE_RUNNER_*` | Reach it by |
|---|---|---|
| platform check/solve/exec executor | yes — a container exec gets image `ENV` + the `environment` block | the platform only |
| `tmux attach -t dev` | yes — started by the entrypoint, inherits its env | dev tracks |
| plain `ssh root@<grader>` | **no** — sshd builds a clean env per session | dev tracks |

So over ssh, import PID 1's env before any `wizlab` call, or every one of them
fails on auth:

```sh
while IFS= read -r -d '' kv; do export "$kv"; done < /proc/1/environ
```

`wizlab` exit codes: 0 satisfied · 1 not satisfied · 2 invocation · 3
environment. In learner checks, remap 2/3 to 1 (an out-of-list code puts the
session in a terminal `validating_error`) with `wizlab --check <noun> <verb> …`,
which prints the real code on stderr; consume them raw in CI.

Review backlog, ranked by return on effort: `research/review.md`.

Next action: measure the post-role connector `healthy` enum on a live lease
(TODO in `wizlab/connector.py`).
