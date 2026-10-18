# te-docs — Tech Experiences team hub

Decision 2026-10-18, owner: the operator. Forcing constraint: the knowledge a new engineer or
their agent needs to keep the lights on spans repos (te-runner, te-labkit-v2, Okta, the sandbox
program, gamekit, events, the team's own AWS org) and no repo today is the one place to look for it.
A new private repo `wiz-training/te-docs` holds what spans repos; what changes with a repo's code
stays in that repo. Test for a page: *which PR would update this?* If a lab or te-runner PR would,
the page lives there and te-docs links to it.

## Layout

Checked out as `~/Work/te-docs`, a sibling like every te repo. The root `README.md` routes by job in
one line each: new here, something broke, building a lab, looking something up.

| Folder | Holds |
|---|---|
| `team/` | onboarding path, team processes, the inventory table (secret, tenant, account, registry → owning system page), `open-questions.md` |
| `infra/` | accounts the team owns, one page per provider: AWS org (DNS, S3, BYOS pool), GCP, Azure, GitHub org and GHCR |
| `instruqt/` | platform facts no product owns: org setup, nine-secret budget, embed tokens and play minting, CLI/API, lab validate, cloud lease behaviour, billing |
| `labs/` | the lab family: toolchain map, generated index, cross-repo runbooks; pointers down to te-labkit-v2 and te-runner |
| `sandboxes/` | blank admin CSP accounts vended for clean-room work, BYO or vended tenant, lifecycle, cleanup |
| `okta/` | tenants, groups, workflows, learner identity mint and reap |
| `gamekit/` | admin, facilitator, service, questions, quizkit, feedback |
| `events/` | partner summit, CTF request app, run-sheets |

Inside a system folder: `README.md` naming its repos and purpose, then `how-to/`, `reference/`,
`explanation/`. A folder exists only once it holds a page. No tutorials folder: the onboarding path
is the one tutorial. A system gets a folder when someone can be paged for it or build with it;
otherwise it is a page inside the folder that depends on it.

## Content rules — te-docs `CLAUDE.md`, under 30 lines, `AGENTS.md` a symlink

te-labkit-v2 `CLAUDE.md` stays canonical for lab repos; te-docs is the scope above it and writes its
own rules. Carried unchanged: one fact in one place; reproducer or bare URL for external state;
timeless present; replace a wrong record, never annotate; banned filler; no intro/outro; decisions
keep owner, date and forcing constraint; commit-body and code-example caps. Dropped: the 100-line
and 120-word caps. Budget follows page type, declared in one front-matter line:

| Type | Shape | Ends when |
|---|---|---|
| how-to | one task; prerequisites first; numbered, copy-pasteable steps | the task is done |
| runbook | a how-to triggered by a symptom: symptom, confirm (one command), fix, verify | verified |
| reference | tables, one subject per page, grows by rows | the enumerable facts are listed |
| explanation | causality and decisions, links to reference | the reader can predict behaviour |

Added: a page opens with what the reader has when finished; a how-to links to explanation rather
than inlining it; a generated page carries a header naming its generator and is never hand-edited.

## Generated lab index — `scripts/lab-index.py`, stdlib

Walks `../te-*`; a repo is a lab when it holds `main.hcl` plus `tasks.hcl` or a `track-spec.json`.
Reads `track-spec.json` (slug, title, csp, tenant, persona, skill_level, scope, reusable_bricks,
runner_needs), `sandbox.hcl` (te-runner pin), `modules.hcl` (module pins), git (last change). Writes
`labs/reference/index.md`: one row per lab sorted by slug, flags for a missing spec; a second table
maps each reusable brick to the labs declaring it. `--check` exits 1 when the committed page differs
from a fresh render. No Instruqt or tenant calls: published state is a wizops verb, linked not
rendered. Retiring te-labkit-v2 `tracks/` is a separate PR there.

## Agents and the `/doc` skill

| Role | Model | Tools | Deliverable |
|---|---|---|---|
| `doc-curator` | opus | Read, Bash across siblings; Write/Edit on te-docs only | filing plan: source file → system folder, page type, new/edit, backing reproducer or URL; sibling-repo facts and open questions flagged, not filed |
| `doc-writer` | opus | Read, Bash, Write, Edit on te-docs only | one page of one declared type from its plan rows; refuses mixed types or missing sources |
| `doc-checker` | sonnet | Read, Bash; no Write | PASS/FAIL from `doc-drift.py` and `lab-index.py --check`, failing references listed |

`/doc <folder>` (default `inbox/`) is the orchestrator and writes nothing itself: triage → curator →
**plan gate** (rows struck, merged, redirected) → one writer per destination page → checker → file
(inbox files fully landed are deleted in the same change; leftovers keep a `.notes.md`; open questions
go to `team/open-questions.md`) → **review gate** with pages touched, leftovers, branch. It refuses
folders outside te-docs and rows without a source, and never touches sibling repos.

## Gate — `scripts/doc-drift.py`, stdlib, exit 0/1 with page:line:reference per failure

| Reference | Checked against |
|---|---|
| sibling path (`te-runner/wizlab/SPEC.md`) | file exists in the sibling checkout |
| lab slug | a row in the generated index |
| secret name `WIZ_*`, `OKTA_*`, `TE_*` | the `team/` inventory table |
| internal link | target page and anchor exist |
| generated page | header present, body matches a fresh render |

Pre-PR, by hand: `doc-drift.py`, `lab-index.py --check`, labkit `secret-scan.py`. `lint.yml` mirrors
them for when Actions run. `main` takes PRs only; the no-push-main hook is copied in.

## Next actions

1. Create the repo: CLAUDE.md, root README, eight system READMEs; land both scripts, three agents, the skill.
2. Generate and commit the lab index.
3. First inbox: te-labkit-v2 `TODO.md`, te-runner `research/`, auto-memory entries, Okta migration notes, Google Docs exports. Run `/doc inbox/`.
4. Interview-written pages no inbox yields: onboarding path, inventory table, Instruqt platform page.

Out of scope: a rendered site (private repo, Free plan), moving anything out of lab repos, AGENTS.md beyond the symlink.
