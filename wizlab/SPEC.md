# wizlab spec

wizlab is the lab runtime. It owns every Wiz and CSP **API** fact, check, and mutation; labs are thin
wrappers (one `wizlab` call + an exit-code remap). Change it only within this contract.

## Grammar
`wizlab <noun> <verb>`.

| verb | means |
|---|---|
| inspect | read + assert, exit 0/1 (learner checks) |
| ensure | idempotent converge — create OR correct to the desired state (solves, setup) |
| delete | remove one resource (reapers) |
| verify | env/session health (check 1) |
| reap | audit + prefix cleanup of a session's footprint |
| tenant | (`wiz`) emit live tenant connector facts as `KEY=value` (+ `$EXEC_OUTPUT`) for a script/terraform; grow by adding keys, never removing |

Exit codes: 0 satisfied · 1 not · 2 invocation error · 3 environment error. Learner checks remap 2/3→1:
`wizlab --check <noun> <verb> …` does the remap and prints the real code on stderr. A flag the verb does
not read is invocation error 2 (`FLAGS`), never ignored.

### What `user reap` exit 0 promises
One outcome per resource: `removed`, `absent`, `deferred`, `unknown`, `failed`. Exit 3 is `failed` or
`deferred` — the signal for the reaper to keep `lab-<sid>@`, its only handle back to leftovers, and
retry the sid. Exit 0 promises that every guaranteed-type resource (`_SWEEP_TYPES`) named for the
session, and every audit-attributed create carrying a handler and a name, is removed or absent. It does
NOT promise the session created nothing else; that residue is counted as `unknown`. Blanket blocking on
`unknown` is rejected — handler coverage is partial by construction, so it would fail every reap and
retain every user. Operator decision, 2026-09-05.

### What `ensure` promises
Exit 0 means the object matches every flag the call named, and what changed is printed. Where the
verb cannot make that true it exits 3 naming the difference and mutates nothing. Operator decision,
2026-09-10, forced by a solve re-run handing the sensor install an empty secret.

| noun | object exists | rule |
|---|---|---|
| sensor, serviceaccount | any | delete, re-mint, emit credentials: the secret is shown once and not re-fetchable |
| connector (aws), workflow, user | drifted, or connector `--reauth` | patch / reset / rotate to the requested state; `--reauth` patches `authParams` unchanged, which re-inits the connector |
| connector (gcp, azure) | any | left as found, exit 0: nothing in it can drift |
| policy | flags differ from the live params | exit 3, no mutation: a shared tenant fixture other labs grade against changes deliberately |
| outpost | `--role-arn`/`--region` differ | exit 3, no mutation: a role change is a knowing delete-and-recreate |

### Runner identity, and the floor `session verify` enforces
The image carries its own `TE_RUNNER_TAG` + `TE_RUNNER_REV`, baked from the git tag and sha CI built
(`Dockerfile` ARG→ENV, plus OCI `image.version`/`image.revision` labels). A play pins HCL at import, so
a tag string in a lab repo names what `main` held then, not what the container runs; these two are the
only answer a running grader can give, and `session verify` prints them on check 1.

`session verify --min-runner vX.Y.Z` asserts this image is at or above the lab's declared verb floor.
Exit **2** below the floor — a pin disagreeing with its own floor is a broken declaration, never learner
state, so a validator reads FAIL and not INCONCLUSIVE. Exit **3** when the identity is absent: a runner
cannot be graded against a floor it cannot name. The floor is checked before any token is spent.
Versions compare as int tuples, because string order puts `v0.1.9` above `v0.1.36`.

The identity is read from the process env, falling back to PID 1. The fallback is convenience, not
a fix: sshd scrubs its sessions' environment of `WIZ_*` too, so a shell that cannot name the
runner cannot authenticate either — importing PID 1's env is required over ssh regardless
(`README.md` §Three env contexts).

Binds only at v0.1.37+: earlier images carry no identity and silently ignore the flag (unparsed flags
are not rejected), so the repin is what activates the gate. Operator decision, 2026-09-07: this is env
health under `verify`, not a new noun — a floor is not an API fact and would fail the API-level bar
below as `runner inspect`.

### The learner shell (v0.1.55+)
The image ships a non-root `learner` user, `sudo`, and one sudoers rule: `learner` may run
`/usr/local/bin/wizlab-learner` without a password, nothing else. A lab keeps every operator secret
out of `container.environment` (a `terminal` opened as `learner` inherits that env whole) and passes
them through exec and task `environment` maps instead. The setup exec calls
`wizlab-grant "<noun> <verb>" …`, which snapshots its own `WIZ_*`, `LAB_KEYCLOAK_*`, `GIT_CONFIG_*` and
the lease credentials (`AWS_*`, `GOOGLE_*`, `ARM_*`; sudo's `env_reset` strips the caller's) to
`/run/lab/wiz.env` (0600) and lists the admitted pairs in `/run/lab/learner-verbs`. `bin/wizlab`
re-issues a non-root call as `sudo -n wizlab-learner`, which admits a listed pair, sources the
snapshot, and execs wizlab; an unlisted pair or no grant is exit 2 with the permitted list on stderr,
never a traceback and never a value. Checks and solves run as root with their task's map and call
wizlab directly. Operator decision, 2026-09-25, forced by the shared tenant's service account being
readable from every learner terminal.

## Nouns
`session`, `connector`, `role`, `instance`, `user`, `wiz`, `outpost`, for Kubernetes labs `k8sconnector`
and `container`, for connectorless Runtime-Sensor labs `sensor` and `detection`, for Wiz Code labs
`serviceaccount` and `code-scan`, and for Workflows labs `workflow` and `workflow-run`:
- `k8sconnector ensure|inspect|delete --cluster <eks name> | --cluster-arn <arn>` — a Kubernetes
  connector the lab owns. Keyed on `connectors(filterBy:{kubernetesClusterExternalIds})`, where the EKS
  external id is the cluster ARN taken from `aws eks describe-cluster`, **never from a
  `KUBERNETES_CLUSTER` graph entity**: in the state these verbs grade (no connector) that entity does not
  exist. `ensure --enabled true|false` is create-or-correct: `createConnector(type:"eks",
  authParams:{apiServerEndpoint, authProvider:"service-account", clusterExternalID, tlsConfig:{serverCA},
  authProviderConfig:{serviceAccountToken}, isOnPrem:false}, extraConfig:{installationType:"Script"})`
  with a 24 h bound token from `kubectl create token kube-system/wiz-connector` (the lab applies that
  ServiceAccount and its cluster-admin binding), else `updateConnector(patch:{enabled})`. An
  auto-onboarded child refuses the patch (`Child connectors cannot be individually disabled/enabled`) and
  is deleted, not toggled. `inspect --require exists|connected|disabled` reads `status`; `exists` is what
  a repair check asserts, since INITIAL_SCANNING precedes CONNECTED by ~7 min. Ordering fact the verbs
  rely on: a cluster created after the cloud connector reaches CONNECTED is never auto-onboarded.
  kubectl is the fourth CSP-side CLI (operator decision, 2026-09-24, forced by the token mint).
- `container inspect --account-id <id> [--image-contains S] [--require-image]` — asserts Wiz enumerates
  ≥1 `CONTAINER` in the account (image substring optional), traversing `INSTANCE_OF` to a
  `CONTAINER_IMAGE`; `--require-image` makes the traversal required, which is the deployed-image scan
  observable. Three filters are never emitted: `cloudPlatform` on CONTAINER matches 0 against entities
  carrying the value; a CONTAINER_IMAGE's `subscriptionExternalId` is the repository owner's account; and
  `sourceProvider` is null on most scanned private images. Registry counters are not a signal in
  deployed-only mode, so nothing here reads `containerRegistries`.
- `outpost ensure|delete|inspect` — a Wiz Outpost (Automated deploy in the customer account). `ensure`
  createsOutpost named on the session stem, given `--role-arn` (the orchestrator TF module output Wiz
  assumes); `inspect --require exists|initialized|connected` asserts the `OutpostStatus` enum and
  `--require scanned` counts workload scans performed BY this Outpost (`resourceScanMetricsTrend`,
  `--lookback-days` default 2); `delete` reaps by name as uninstall → poll to UNINSTALLED → delete
  (`--timeout`, default 600s), exit 0 on a stuck record — `user reap` owns the same lifecycle as a
  guaranteed type and reports `deferred` rather than waiting. Scoping key: the Outpost name == the session
  stem (`OutpostFilters` has no account/region field, same as the sensor plane). Three statuses that
  read as success are not: **INITIALIZED proves only that the object is registered**, **CONNECTED does
  not imply it scanned anything** (4 of 7 live ones had scanned nothing), and a direct delete fails.
  All measured — grade and reap off `measurements.yaml outpost_lifecycle.aws`, never off the status
  name's plain meaning.
- `connector ensure|inspect|delete` — a Cloud Connector. `ensure` converges only on AWS: it creates
  the connector if absent, else corrects a drifted `authParams.customerRoleARN` (the repair path).
  `--reauth` patches `authParams` even when nothing drifted: the patch is synchronous, drops the
  connector to `INITIAL_SCANNING` and re-runs the assume-role. It is the only lever for a trust rotated
  AFTER `CONNECTED` — rotation alone leaves `CONNECTED`, `errorCode` null, no System Health Issue,
  `lastActivity` frozen, and `requestConnectorScan` (Rescan) changes none of that. On a broken trust the
  connector holds `INITIAL_SCANNING`; on a matching one it reaches `CONNECTED` in ~4 min. AWS only; an
  Outpost-bound connector needs the outpost flags with it or the patch drops the binding (exit 2).
  GCP and Azure `ensure` are create-if-absent — the connector carries no ARN to drift, so an existing
  one is left as found and exits 0. AWS `ensure` sets
  `authParams.customerRoleARN`; `--outpost-id`, or `--outpost-name` (default: the session stem, the name
  the console dropdown shows), with `--scanner-role-arn` also sets `authParams.outpostId` and
  `authParams.diskAnalyzer.scanner.roleARN` — the second phase of an Outpost deploy, without which Wiz
  builds no scan cluster. `inspect --require exists|healthy|outpost-bound`.
- `sensor ensure|delete|inspect` — a `type:SENSOR` service account is the sensor's credential
  (`ensure` re-mints it named on the session stem, emits `WIZ_API_CLIENT_ID/SECRET`; `delete` removes it;
  the reaper's ServiceAccount sweep also covers it). `inspect --require active` asserts the sensor
  named for the session reports `ACTIVE`. Scoping key: the installed sensor's name == the host name,
  which a lab pins to the session stem (neither `SensorFilters` nor `DetectionFilters` has a
  cloud-account/instance field).
- `detection inspect --rule-name N [--match-only]` — asserts ≥1 detection, keyed on
  `matchedRuleName` + `type` + the resolved `sensorId`. Never a rule id (tenant-specific) and never an
  Issue/Threat object (tenant-wide anti-burst cap). Default type `GENERATED_THREAT`; `--match-only`
  queries the sibling that matched and raised no threat.
- `serviceaccount ensure|inspect|delete` — the on-the-fly credential `wizcli auth` uses, minted via a
  CLI DEPLOYMENT (`createCliDeployment`), NOT `createServiceAccount`: `type:CLI` accounts are internal
  and that mutation rejects them. `ensure` names it `<stem>-cli` and converges to one fresh deployment
  (delete-then-mint, since the secret is shown once), emitting `WIZ_CLIENT_ID` (the deployment SA's
  `clientId`) + `WIZ_CLIENT_SECRET` (the payload's `clientSecret`) to stdout + `$EXEC_OUTPUT`.
  `inspect --require exists` and `delete` (by `--id` or name) go through `deployments(type:WIZ_CLI)` /
  `deleteCliDeployment`. The deployment's SA is named `<stem>-cli-deployment-<uuid>`, which the
  reaper's `ServiceAccount` `lab-<id>*` sweep finds and **cannot delete directly**:
  `deleteServiceAccount` answers `Internal service account cannot be deleted`, so the sweep routes the
  record back through `deleteCliDeployment` on the deployment whose name is that SA name minus
  `-deployment-<uuid>` — the only handle back, since no reverse lookup exists. A deployment already
  gone leaves the SA `unknown`, not `failed`: blocking would retain the session's user with no later
  pass able to clear it. The `sensor` path stays on `createServiceAccount(type:SENSOR)`, deletable
  directly.
- `workflow inspect|ensure` and `workflow-run inspect|ensure` — an Automation Workflow, for labs whose
  artifact is the workflow itself. `inspect --require exists|published`: **`published` is `enabled`, and
  that is the only signal there is** — `activeVersion`, `draftVersion` and `versions` read null/0 on
  every workflow of a live tenant, so a version-based assertion fails every learner while looking
  correct. It matters because Create leaves a workflow saved-but-inactive, so `exists` alone passes
  someone who never published. Name resolution is **prefix** on the session stem (`search` is a
  case-insensitive substring match), because a guide tells a learner to type `lab-<sid>-<scenario>` and
  the suffix is not the lesson; `--exact-name` pins it, and an `enabled` hit outranks a disabled
  leftover on the same stem.

  `ensure --definition <file.json>` **validates, then patches in place, and creates only when absent.**
  `updateAutomationWorkflow(input:{id, patchStrict})` is the mutation the console's Save sends, and
  `UpdateAutomationWorkflowPatchStrict` is `AutomationWorkflowDefinition` minus `projectId`. The patch
  keeps the workflow id, so the test runs earlier activities graded survive a later activity's solve.
  `Workflow versions are currently not supported` is refused for `publishAutomationWorkflowVersion` and
  `updateAutomationWorkflowDraft` only; a create carrying `enabled: true` is already live, so nothing
  needs publishing. A second exact-name match is a learner's duplicate attempt and is deleted.
  The validate hop is not politeness — a definition with `enabled: true` and any issue is refused as
  `an enabled workflow cannot have validation errors`, naming neither the step nor the expression, while
  `validateAutomationWorkflow(input:{workflow:…}){issues{message target}}` names both, costs no mutation
  and runs on any tenant. So `--dry-run` validates and stops, which is **the only gate that exists for
  CEL**: nothing local parses it, so every expression a lab ships — in its definition and in the text it
  hands a learner — is checked here before a play. Issues exit 2 (the caller's bug, not the
  environment's) and nothing is submitted. `name` and `--project-id` are injected over the file so every
  lab lands on the reap stem. **The definition JSON lives in the lab**, since which steps and cases a
  scenario wants is a config-shape assertion barred from `--require` below.

  `workflow-run inspect --require completed|branch --branch N|error-path` grades a run's own signal —
  `AutomationWorkflowRunStepResult.outboundEdge`: a `SWITCH_CASE` case's `branchName` or its
  `defaultBranchName`, a `CONDITION`'s `true`/`false`, an unbranched step's `main` — so this asserts the
  routing *decision*, not the definition. `error-path` is the `outpost-bound` carve-out: every failed step
  reads `outboundEdge` `error` whether or not it defines an edge, so the edge name proves nothing; what
  proves the edge was followed is a step at `status FAILED` inside a run that still `COMPLETED`, and no
  lifecycle state separates that from a run the failure killed. Two hops (name → id → runs) because
  `AutomationWorkflowRunFilters` carries no name or search key, and the first hop takes **every** workflow
  on the stem: a learner who attempts twice leaves two, so grading whichever the API returned first grades
  an arbitrary attempt. `TEST` runs only unless `--run-type` widens it, since an `AUTOMATIC` run from a
  real event would grade a learner on someone else's Threat.
  `workflow-run ensure --data <file.json> --initial-step <step name>` fires a test run with a synthetic
  trigger payload and waits on **that run's id**, never "a completed run of this workflow" — the
  workflow's earlier runs already satisfy the latter, so a second call reports success while its own run
  is still in flight. `customData.initialSteps` is non-optional and step ids are minted per create, so
  the step is named and resolved at call time.

  No `delete` verb: `AutomationWorkflow` is already a `_SWEEP_TYPES` member, so the generic prefix sweep
  reaches it and a second path would be two places holding one fact. Never `TRIGGER_BLUE_AGENT` in a lab
  flow: manual Blue Agent runs are 5/day/tenant and a cohort exhausts them on learner two.
- `policy ensure|inspect|delete --name N` — the BLOCK CI/CD IaC scan policy a code-scan gate needs.
  `ensure` is idempotent by name (§What `ensure` promises); absent, it creates a `type:IAC` policy with `enforcementMethod
  BLOCK` on `deploymentLifecycle CLI`, scoped (`iacParams.cloudConfigurationRules`) to the builtin
  Dockerfile control 'Last User Is root' (resolved live via `cloudConfigurationRules`, never
  hard-coded; `--rule-id`/`--severity`/`--count-threshold` override), `default:false` so only a
  `wizcli --policies <name>` scan is gated. `inspect --require exists` verifies setup; `delete`
  removes it. A SHARED, PERSISTENT fixture — the reaper never touches it (not session-scoped).
- `code-scan inspect --require published|pass` — asserts a Wiz Code CI/CD scan for this session,
  scoped by the `session` tag (`--tag-key`/`--tag-value`, default value the session stem), latest scan
  wins. `published` = ≥1 scan exists; `pass` = latest `status.verdict == PASSED_BY_POLICY`
  (`FAILED_BY_POLICY` exits 1 at once). Grades the TENANT verdict, not the CLI exit code, because
  `WARN_BY_POLICY` exits 0 — only a blocking policy distinguishes a finding from a pass. Polls every
  `--interval` to `--timeout` (default 180s) since publish latency is unmeasured.

## What may be added
A change qualifies only if ALL hold:
1. **General** — reused across labs, not bespoke to one scenario.
2. **API-level** — a Wiz or CSP read/assert/mutation (`api()` / `_aws`) a lab runs. Not a third cloud,
   and not an operator credential: what needs `TAILSCALE_API_KEY` or `INSTRUQT_API` lives in
   `te-labkit-v2/wizops`.
3. **Fits the grammar** — a noun + one of the verbs above, at that verb's meaning.

## What may NOT
- A verb or flag per scenario or per assertion — compose existing verbs instead.
- Config-field assertions bolted onto `inspect --require` (which asserts lifecycle *state*); lean on
  the system's own signal (e.g. CONNECTED) or read a general field in the check. Carve-out only where no
  lifecycle state separates the cases: an Outpost-unbound connector still reaches CONNECTED while its
  Outpost holds INITIALIZED with `errorCode` null, so `--require outpost-bound` reads `outpost.id`.
- CSP **infrastructure** provisioning (VPCs, instances) — that is Terraform
  (`te-labkit-v2/template/infra/<csp>/`), not wizlab.
- Anything a lab can already do by composing existing verbs.

## Change process
Propose → **operator approves** → update this spec → then implement. Default answer is no; the bar is
general + API-level + fits the grammar. Gates: `ruff` + `xenon` + `test_wizlab.py` (CI); cite
`measurements.yaml`, never re-derive it. Tests lock the exit-code contract + IAM parsing — a refactor
that stays green needs no lab re-play; what a test may be is `te-labkit-v2/CLAUDE.md` §Tests.
