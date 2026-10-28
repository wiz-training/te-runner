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
| tenant | (`wiz`) emit live tenant connector + endpoint facts as `KEY=value` (+ `$EXEC_OUTPUT`) for a script/terraform; grow by adding keys, never removing |

Exit codes: 0 satisfied · 1 not · 2 invocation error · 3 environment error · 4 `user reap` only, cleanup
still in flight · 5 `user reap` only, residue only an operator can clear; the user record is not a handle
to it and is deleted (`_reap_exit`). Learner checks remap every nonzero code to 1 — a code outside the task's
declared list puts the session in a terminal `validating_error` — with `wizlab --check <noun> <verb> …`,
which prints the real code on stderr; CI consumes the codes raw. A flag the verb does not read is
invocation error 2 (`FLAGS`), never ignored.

Deliberate exceptions (never renamed; shipped labs call them):

| pair | why `ensure` / `inspect` / `delete` does not fit |
|---|---|
| `slot claim` | a pool slot is claimed from the free set (`refs/heads/claims/<lab>/slot-NN`), not converged by name |
| `slot release` | drops the session's claimed pool slot refs; inverse of `slot claim` |
| `patch open` | branches `patch/<stem>` and opens a Dockerfile PR on GitHub, not a converged resource |
| `user login-url` | read-only author tool that prints a tenant's SAML sign-in URL without grading |

### What `user reap` exit 0 promises
One outcome per resource; `failed` outranks `operator`, which outranks `deferred` (`_reap_exit`).

| outcome | exit | `lab-<sid>@` |
|---|---|---|
| `failed` | 3 | kept, sid retried: no later pass finishes it alone |
| `operator` | 5 | deleted, run red naming the sid: only a human clears it, by its `lab-<sid>` name, never through the user |
| `deferred` | 4 | kept, sid retried: teardown in flight; the next daily pass finishes it |
| `removed`, `absent`, `unknown` | 0 | deleted |

Exit 0 promises that every guaranteed-type resource (`_SWEEP_TYPES`) named for the session, every
code-lab slot claim whose commit message is the stem or whose commit is older than `CLAIM_TTL_S` (7200s),
along with its open `patch/<stem>` PR and branch (needs `WIZ_LABS_GH_APP`; absent = not swept, said
once; a GitHub transport fault is `deferred`), and every audit-attributed create carrying a handler and a
name, is removed or absent; other residue is `unknown`.
Blocking on `unknown` would fail every reap and retain every user, since handler coverage is partial by
construction. Operator decision, 2026-09-05.
Audit-attributed creates whose name is not stem-owned (`lab-<sid>` or `lab-<sid>[-@]*`) are reported as `unknown` (`REVIEW audit-only …: not stem-owned, left in place`, counted under `audit-only`) and not deleted (measured 2026-10-27: audit `actionParameters` = `{input, selection}`; no created id; 128 session reaps across `reap.yml` runs `40322957385`, `40186901829`, `40086319544` had 0 `audit-only` deletes, 110× `# 0 removed (0 audit-only)` and 18× `# 1 removed (0 audit-only)`).

### What `ensure` promises
Exit 0 means the object matches every flag the call named, and what changed is printed. Where the
verb cannot make that true it exits 3 naming the difference and mutates nothing. Operator decision,
2026-09-10, forced by a solve re-run handing the sensor install an empty secret.

| noun | object exists | rule |
|---|---|---|
| sensor, apiaccount, serviceaccount, mcp | any | delete, re-mint, emit credentials: the secret is shown once and not re-fetchable |
| connector (aws), workflow | drifted, foreign session, or connector `--reauth` | delete any foreign `lab-<other>` connector on the account first; patch / reset / rotate to the requested state, or create when none remains; `--reauth` patches `authParams` unchanged, which re-inits the connector |
| user | any | one `action: create` call to the Okta router with a freshly generated password, every run; wizlab reads back only `login` and emits `WIZ_USER`/`WIZ_PWD`. `user inspect` reads Okta; `--wiz` reads the tenant's `users` record `user delete` removes, with the tenant pair alone |
| connector (gcp, azure) | any | delete any foreign `lab-<other>` connector on the account first; an existing non-foreign one is left as found, exit 0: nothing in it can drift |
| policy | flags differ from the live params | exit 3, no mutation: a shared tenant fixture other labs grade against changes deliberately |
| outpost | `--role-arn`/`--region` differ | exit 3, no mutation: a role change is a knowing delete-and-recreate |

### Credential nouns
`sensor` (its service account), `apiaccount`, `serviceaccount` and `mcp` are each a `credential.Credential`
record driven by `credential.ensure`, `credential.inspect` and `credential.delete`. A record holds the
print `label`, the stem `suffix`, the find document, field and variable builder, the create document and
`input` builder, the path to `(id, clientId, clientSecret)` in the payload, the delete mutation and its
selection, the two `emit_keys`, an optional `status_field`, the `--require` predicates and the
`_SWEEP_TYPES` member the reaper reaches it through. A new credential noun is a record plus its documents
in its own module, and a row in `CredentialRecords`. Not records: `connector`, `k8sconnector`, `outpost`
and `workflow` patch in place or exit 3 on drift, never re-mint; `role` is lease-account IAM (`role inspect [--cloud aws|gcp|azure] [--role-name N] [--account-id <id>] [--trusts-service <principal>]`; `role ensure [--cloud aws] [--role-name N] [--external-id <id>]`); `user` is Okta.

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

### Verb floors
The tag that first reads each contract; a lab pins at or above the highest row it calls.

| floor | first read |
|---|---|
| v0.1.37 | `--min-runner`, `TE_RUNNER_TAG` |
| v0.1.46 | `workflow ensure` patches in place (`updateAutomationWorkflow`) |
| v0.1.47 | `wizlab --check`; an undeclared flag is exit 2 |
| v0.1.48 | package split (`wizlab/*.py`) |
| v0.1.49 | `FLAGS` table: typed values, choices, defaults |
| v0.1.55 | learner shell (`wizlab-grant`, `wizlab-learner`) |
| v0.1.65 | Okta `OKTA_WF_*` (`user ensure`, `user inspect`) |
| v0.1.70 | `wiz tenant` emits every key |
| v0.1.71 | `workflow ensure` exact-name; `user inspect --wiz`; a flag another `--cloud` reads is exit 2 |
| v0.1.72 | `user reap` exit 5: `operator` residue, the user record deleted |
| v0.1.73 | reaper attempts `deleteOutpost` on a PARTIALLY_UNINSTALLED record |
| v0.1.74 | credential nouns share one record-driven ensure/inspect/delete; unified success line |
| v0.1.77 | `correlation inspect`, `pullrequest inspect`, `slot claim\|release`, `patch open`; `WIZ_LABS_GH_APP` |
| v0.1.78 | `user login-url` derived from the tenant: `samlIdentityProviderConfig.idpLoginUrl` of the lab domain's IdP |
| v0.1.79 | `user login-url` emits the Cognito authorize route by default; `WIZ_<T>_LOGIN_ROUTE=idp-login` |
| v0.1.80 | `user ensure` redacts `WIZ_PWD` on stdout under an exec; `serviceaccount\|apiaccount ensure --out-file <path>` takes the pair without the play log seeing it |
| v0.1.81 | `--out-file` redacts stdout whatever `$EXEC_OUTPUT` holds: on v0.1.80 a caller blanking it got the secret printed anyway |
| v0.1.82 | `user reap` releases code-lab slot claims whose commit message is the stem (`WIZ_LABS_GH_APP`) |
| v0.1.84 | `connector ensure` deletes any foreign `lab-<other>` connector on the account before converging (preserving learner-named connectors); `connector inspect` filters foreign `lab-<other>` connectors out and ranks this session's first |
| v0.1.85 | `slot claim` evicts claims older than `CLAIM_TTL_S` (7200s); `slot release` and `user reap` close the open `patch/<stem>` PR, delete `refs/heads/patch/<stem>`, and sweep expired claims regardless of session |

<!-- verb-floors:begin -->
| noun | verb | first tag |
|---|---|---|
| session | verify | v0.1.0 |
| connector | inspect | v0.1.0 |
| connector | ensure | v0.1.0 |
| connector | delete | v0.1.0 |
| instance | inspect | v0.1.13 |
| k8sconnector | ensure | v0.1.54 |
| k8sconnector | inspect | v0.1.54 |
| k8sconnector | delete | v0.1.54 |
| container | inspect | v0.1.54 |
| sensor | ensure | v0.1.29 |
| sensor | delete | v0.1.29 |
| sensor | inspect | v0.1.29 |
| serviceaccount | ensure | v0.1.34 |
| serviceaccount | inspect | v0.1.34 |
| serviceaccount | delete | v0.1.34 |
| apiaccount | ensure | v0.1.63 |
| apiaccount | inspect | v0.1.63 |
| apiaccount | delete | v0.1.63 |
| mcp | ensure | v0.1.61 |
| mcp | inspect | v0.1.61 |
| mcp | delete | v0.1.61 |
| code-scan | inspect | v0.1.34 |
| correlation | inspect | v0.1.77 |
| pullrequest | inspect | v0.1.77 |
| slot | claim | v0.1.77 |
| slot | release | v0.1.77 |
| patch | open | v0.1.77 |
| policy | ensure | v0.1.34 |
| policy | inspect | v0.1.34 |
| policy | delete | v0.1.34 |
| detection | inspect | v0.1.29 |
| workflow | inspect | v0.1.40 |
| workflow | ensure | v0.1.40 |
| workflow-run | inspect | v0.1.40 |
| workflow-run | ensure | v0.1.40 |
| outpost | inspect | v0.1.31 |
| outpost | ensure | v0.1.31 |
| outpost | delete | v0.1.31 |
| role | inspect | v0.1.3 |
| role | ensure | v0.1.3 |
| user | ensure | v0.1.5 |
| user | inspect | v0.1.5 |
| user | delete | v0.1.5 |
| user | login-url | v0.1.8 |
| wiz | tenant | v0.1.16 |
| user | reap | v0.1.10 |
<!-- verb-floors:end -->

### The learner shell
The image ships a non-root `learner` user, `sudo`, and one sudoers rule: `learner` may run
`/usr/local/bin/wizlab-learner` without a password, nothing else. A lab keeps every operator secret
out of `container.environment` (a `terminal` opened as `learner` inherits that env whole) and passes
them through exec and task `environment` maps instead. The setup exec calls
`wizlab-grant "<noun> <verb>" …`, which snapshots its own `WIZ_*`, `INSTRUQT_SESSION_ID`,
`WIZLAB_SESSION_PREFIX`, `OKTA_WF_INVOKE_URL`, `OKTA_WF_CLIENT_TOKEN`, `LAB_PROFILE`,
`INSTRUQT_PARTICIPANT_ID`, `GIT_CONFIG_*`, and the lease credentials (`AWS_ACCESS_KEY_ID`,
`AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`, `AWS_DEFAULT_REGION`, `AWS_ACCOUNT_ID`,
`GOOGLE_CREDENTIALS`, `GOOGLE_PROJECT`, `ARM_*`; sudo's `env_reset` strips the caller's, and
`GOOGLE_REGION` is deliberately not admitted because nothing in te-runner reads it — Terraform
does) to
`/run/lab/wiz.env` (0600) and lists the admitted pairs in `/run/lab/learner-verbs`. `bin/wizlab`
re-issues a non-root call as `sudo -n wizlab-learner`, which admits a listed pair, sources the
snapshot, and execs wizlab; an unlisted pair or no grant is exit 2 with the permitted list on stderr,
never a traceback and never a value. Checks and solves run as root with their task's map and call
wizlab directly. Operator decision, 2026-09-25, forced by the shared tenant's service account being
readable from every learner terminal.

## Nouns
`session`, `connector`, `role`, `instance`, `user`, `wiz`, `outpost`, for Kubernetes labs `k8sconnector`
and `container`, for connectorless Runtime-Sensor labs `sensor` and `detection`, for Wiz Code labs
`serviceaccount`, `code-scan`, `policy`, `correlation`, `pullrequest`, `slot` and `patch`, for agent labs
`apiaccount` and `mcp`, and for Workflows labs `workflow` and `workflow-run`:
- `session verify [--min-runner vX.Y.Z] [--cloud aws|gcp|azure] [--account-id <id>]` — env/session health, optional CSP identity check, and runner floor check; see §Runner identity, and the floor `session verify` enforces.
- `role inspect|ensure` — lease-account IAM role trust grading (`inspect`) and AWS trust creation/repair (`ensure`); see §What `ensure` promises and §Credential nouns.
- `instance inspect --account-id <id> [--type VIRTUAL_MACHINE]` — asserts Wiz enumerates ≥1 cloud resource of `--type` in the account (`cloudResources`); see §Grammar.
- `user ensure|inspect|delete|reap` — Okta Workflows user provisioning/grading (`ensure`, `inspect`), Wiz user removal (`inspect --wiz`, `delete`), and session footprint cleanup (`reap`); see §What `ensure` promises and §What `user reap` exit 0 promises. `user reap --commit` requires a 12-char `[a-z0-9]` Instruqt play id (exit 2 otherwise; dry-run accepts any session id).
  Flags (`--session <sid>` overrides `INSTRUQT_SESSION_ID` on all four): `ensure [--profile P]`; `inspect [--wiz] [--domain D]`; `delete [--domain D]`; `reap [--commit] [--domain D] [--email E] [--last-min 1440]`.
- `wiz tenant` — emits live tenant connector and endpoint facts as `KEY=value` (+ `$EXEC_OUTPUT`); see §Grammar.
- `k8sconnector ensure|inspect|delete [--session <sid>] [--cluster <eks name> | --cluster-arn <arn>]` — a Kubernetes
  connector the lab owns. Keyed on `connectors(filterBy:{kubernetesClusterExternalIds})`, where the EKS
  external id is the cluster ARN taken from `aws eks describe-cluster`, **never from a
  `KUBERNETES_CLUSTER` graph entity**: in the state these verbs grade (no connector) that entity does not
  exist. `ensure [--name N] [--enabled true|false]` is create-or-correct: `createConnector(type:"eks",
  authParams:{apiServerEndpoint, authProvider:"service-account", clusterExternalID, tlsConfig:{serverCA},
  authProviderConfig:{serviceAccountToken}, isOnPrem:false}, extraConfig:{installationType:"Script"})`
  with a 24 h bound token from `kubectl create token kube-system/wiz-connector` (the lab applies that
  ServiceAccount and its cluster-admin binding), else `updateConnector(patch:{enabled})`. An
  auto-onboarded child refuses the patch (`Child connectors cannot be individually disabled/enabled`);
  that surfaces as exit 3 and deletes nothing — only a connector the lab created can be toggled. `inspect --require exists|connected|disabled` reads `status`; `exists` is what
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
- `outpost ensure|delete|inspect [--session <sid>] [--name N]` — a Wiz Outpost (Automated deploy in the customer account). `ensure`
  createsOutpost named on the session stem, given `--role-arn` (the orchestrator TF module output Wiz
  assumes) and optional `--region`; `inspect --require exists|initialized|connected` asserts the `OutpostStatus` enum and
  `--require scanned` counts workload scans performed BY this Outpost (`resourceScanMetricsTrend`,
  `--lookback-days` default 2); `delete` (`--id <id>` or `--name`) reaps as uninstall → poll to UNINSTALLED → delete
  (`--timeout`, default 600s); exit 0 when the uninstall outlives the wait (the record is left for the
  daily reaper), exit 1 on `PARTIALLY_UNINSTALLED` (`_OUTPOST_STUCK`): the uninstall cannot be re-fired
  and a solve must not wait on a delete that may be refused. `user reap` owns the same lifecycle as a
  guaranteed type, reports `deferred` rather than waiting, attempts `deleteOutpost` on a stuck record
  each pass and reports its refusal as `operator` (exit 5): the console delete issues the same mutation
  and the user is no handle to the record. Scoping key: the
  Outpost name == the session stem (`OutpostFilters` has no account/region field, same as the sensor
  plane). Three statuses that read as success are not: **INITIALIZED proves only that the object is
  registered**, **CONNECTED does not imply it scanned anything** (4 of 7 live ones had scanned nothing),
  and a direct delete fails.
  All measured — grade and reap off `measurements.yaml outpost_lifecycle.aws`, never off the status
  name's plain meaning. On `ensure`, an existing Outpost with `allowedRegions: []` (the console
  default, unrestricted) is compatible with any `--region`; a non-empty list that differs exits 3.
- `connector ensure|inspect|delete --account-id <id> [--cloud aws|gcp|azure] [--session <sid>]` — a Cloud Connector. `ensure` (`[--role-arn <arn>]` on AWS, `[--tenant-id <id>]` on Azure) converges only on AWS: it creates
  the connector if absent, else corrects a drifted `authParams.customerRoleARN` (the repair path).
  `--reauth` patches `authParams` even when nothing drifted: the patch is synchronous and drops the
  connector to `INITIAL_SCANNING` with `errorCode` and `lastActivity` null. It does NOT recover a
  connector: from `ERROR` on a corrected trust it holds `INITIAL_SCANNING` past 30 min, and a patch
  carrying all four `UpdateConnectorPatch` fields behaves the same. `delete` then `ensure` reaches
  `CONNECTED` in ~4 min on the same trust, and is the repair path. `--reauth` surfaces a rotated trust
  as a state change, nothing more — rotation alone leaves `CONNECTED`, `errorCode` null, no System
  Health Issue, `lastActivity` frozen, and `requestConnectorScan` (Rescan) changes none of that. AWS
  only; an Outpost-bound connector needs the outpost flags with it or the patch drops the binding
  (exit 2).
  GCP and Azure `ensure` are create-if-absent — the connector carries no ARN to drift, so an existing
  one is left as found and exits 0. AWS `ensure` sets
  `authParams.customerRoleARN`; `--outpost-id`, or `--outpost-name` (default: the session stem, the name
  the console dropdown shows), with `--scanner-role-arn` also sets `authParams.outpostId` and
  `authParams.diskAnalyzer.scanner.roleARN` — the second phase of an Outpost deploy, without which Wiz
  builds no scan cluster. `inspect --require exists|healthy|outpost-bound` (`--outpost-id` / `--outpost-name` narrow `outpost-bound`). On a recycled lease account,
  a connector whose name or bound Outpost name carries another session's `lab-<sid>` prefix is foreign:
  `ensure` deletes foreign connectors before converging or creating (leaving a non-`lab-` learner-named
  connector untouched), `inspect` filters foreign connectors out and ranks a stem-named connector ahead
  of a learner-named one, and `delete` removes all connectors for the account.
- `sensor ensure|delete|inspect [--session <sid>] [--name N]` — a `type:SENSOR` service account is the sensor's credential
  (`ensure` re-mints it named on the session stem, emits `WIZ_API_CLIENT_ID/SECRET`; `delete` by `--id` or `--name` removes it;
  the reaper's ServiceAccount sweep also covers it). `inspect [--require exists|active]` asserts the sensor
  named for the session reports `ACTIVE`. Scoping key: the installed sensor's name == the host name,
  which a lab pins to the session stem (neither `SensorFilters` nor `DetectionFilters` has a
  cloud-account/instance field).
- `detection inspect --rule-name N [--name <sensor>] [--session <sid>] [--since-minutes 120] [--match-only]` — asserts ≥1 detection, keyed on
  `matchedRuleName` + `type` + the resolved `sensorId`. Never a rule id (tenant-specific) and never an
  Issue/Threat object (tenant-wide anti-burst cap). Default type `GENERATED_THREAT`; `--match-only`
  queries the sibling that matched and raised no threat.
- `user login-url [--domain D]` — the sign-in URL a note renders: `WIZ_<T>_LOGIN_URL` verbatim, else the
  portal `idp-login` entrypoint from `WIZ_<T>_IDP_LOGIN_CLIENT_ID`, else the route the tenant itself states
  for the IdP the lab domain signs in through (`WIZ_<T>_SSO_IDP_NAME`, else the `identityProviderV2` of an
  existing `@<domain>` user, else the tenant's only SAML provider; several and no lab user is exit 2 naming
  them): the Cognito authorize URL `https://<samlIdentityProviderConfig.ssoUrl host>/oauth2/authorize?
  <samlIdentityProviders.relayState>`, or with `WIZ_<T>_LOGIN_ROUTE=idp-login` the portal entrypoint
  `samlIdentityProviderConfig.idpLoginUrl` (for a tenant whose app client refuses the SAML hand-back on the
  authorize route; on TE that entrypoint ends at Cognito's hosted login with 403). Last, the Cognito URL
  from `WIZ_<T>_COGNITO_SUFFIX` + `WIZ_<T>_SSO_CLIENT_ID`. The IdP's own SAML `loginURL` is never emitted:
  Cognito answers it "Invalid samlResponse or relayState".
- `serviceaccount ensure|inspect|delete [--session <sid>] [--name N]` — the on-the-fly credential `wizcli auth` uses, minted via a
  CLI DEPLOYMENT (`createCliDeployment`), NOT `createServiceAccount`: `type:CLI` accounts are internal
  and that mutation rejects them. `ensure [--out-file <path>]` names it `<stem>-cli` (or `--name`) and converges to one fresh deployment
  (delete-then-mint, since the secret is shown once), emitting `WIZ_CLIENT_ID` (the deployment SA's
  `clientId`) + `WIZ_CLIENT_SECRET` (the payload's `clientSecret`) to stdout + `$EXEC_OUTPUT` (or writing the pair to `--out-file` and redacting stdout).
  `inspect --require exists` (author/legacy-only: 0 lab-script callers, kept in `VERBS` for verb-floor compatibility) and `delete` (by `--id` or `--name`) go through `deployments(type:WIZ_CLI)` /
  `deleteCliDeployment`. The deployment's SA is named `<stem>-cli-deployment-<uuid>`, which the
  reaper's `ServiceAccount` `lab-<id>*` sweep finds and **cannot delete directly**:
  `deleteServiceAccount` answers `Internal service account cannot be deleted`, so the sweep routes the
  record back through `deleteCliDeployment` on the deployment whose name is that SA name minus
  `-deployment-<uuid>` — the only handle back, since no reverse lookup exists. A deployment already
  gone leaves the SA `unknown`, not `failed`: blocking would retain the session's user with no later
  pass able to clear it. The `sensor` path stays on `createServiceAccount(type:SENSOR)`, deletable
  directly.
- `workflow inspect|ensure [--session <sid>] [--name N]` and `workflow-run inspect|ensure [--session <sid>] [--name N]` — an Automation Workflow, for labs whose
  artifact is the workflow itself. `inspect --require exists|published`: **`published` is `enabled`, and
  that is the only signal there is** — `activeVersion`, `draftVersion` and `versions` read null/0 on
  every workflow of a live tenant, so a version-based assertion fails every learner while looking
  correct. It matters because Create leaves a workflow saved-but-inactive, so `exists` alone passes
  someone who never published. `inspect` resolves the name by **prefix** on the session stem (`search`
  is a case-insensitive substring match), because a guide tells a learner to type `lab-<sid>-<scenario>`
  and the suffix is not the lesson; `--exact-name` pins it, and an `enabled` hit outranks a disabled
  leftover on the same stem. `ensure` matches the **exact name** only: a prefix hit is the learner's
  workflow, which the patch would rename to the stem and whose siblings the duplicate sweep would delete.

  `ensure --definition <file.json> [--project-id <id>] [--dry-run]` **validates, then patches in place, and creates only when absent.**
  `updateAutomationWorkflow(input:{id, patchStrict})` is the mutation the console's Save sends, and
  `UpdateAutomationWorkflowPatchStrict` is `AutomationWorkflowDefinition` minus `projectId`. The patch
  keeps the workflow id, so the test runs earlier activities graded survive a later activity's solve.
  `Workflow versions are currently not supported` is refused for `publishAutomationWorkflowVersion` and
  `updateAutomationWorkflowDraft` only; a create carrying `enabled: true` is already live, so nothing
  needs publishing. Only a second exact-name match is deleted, as a learner's duplicate attempt.
  The validate hop is not politeness — a definition with `enabled: true` and any issue is refused as
  `an enabled workflow cannot have validation errors`, naming neither the step nor the expression, while
  `validateAutomationWorkflow(input:{workflow:…}){issues{message target}}` names both, costs no mutation
  and runs on any tenant. So `--dry-run` validates and stops, which is **the only gate that exists for
  CEL**: nothing local parses it, so every expression a lab ships — in its definition and in the text it
  hands a learner — is checked here before a play. Issues exit 2 (the caller's bug, not the
  environment's) and nothing is submitted. `name` and `--project-id` are injected over the file so every
  lab lands on the reap stem. **The definition JSON lives in the lab**, since which steps and cases a
  scenario wants is a config-shape assertion barred from `--require` below.

  `workflow-run inspect [--exact-name] [--run-id <id>] [--run-type TEST] --require completed|branch --branch N|error-path` grades a run's own signal —
  `AutomationWorkflowRunStepResult.outboundEdge`: a `SWITCH_CASE` case's `branchName` or its
  `defaultBranchName`, a `CONDITION`'s `true`/`false`, an unbranched step's `main` — so this asserts the
  routing *decision*, not the definition. `error-path` is the `outpost-bound` carve-out: every failed step
  reads `outboundEdge` `error` whether or not it defines an edge, so the edge name proves nothing; what
  proves the edge was followed is a step at `status FAILED` inside a run that still `COMPLETED`, and no
  lifecycle state separates that from a run the failure killed. Two hops (name → id → runs) because
  `AutomationWorkflowRunFilters` carries no name or search key, and the first hop takes **every** workflow
  on the stem: a learner who attempts twice leaves two, so grading whichever the API returned first grades
  an arbitrary attempt. `TEST` runs only unless `--run-type` widens it, since an `AUTOMATIC` run from a
  real event would grade a learner on someone else's Threat. Without `--run-id`, steps are pooled across
  the first 20 completed runs (an earlier run can satisfy a later check, or >20 runs can push a fresh run
  out of the window); `--run-id <id>` fetches only that run via the existing `id` filter and grades its
  steps alone.
  `workflow-run ensure --data <file.json> --initial-step <step name> [--exact-name] [--trigger-type EVENT] [--timeout 60] [--interval 2.0]` fires a test run with a synthetic
  trigger payload and waits on **that run's id**, never "a completed run of this workflow" — the
  workflow's earlier runs already satisfy the latter, so a second call reports success while its own run
  is still in flight. `customData.initialSteps` is non-optional and step ids are minted per create, so
  the step is named and resolved at call time. On completion, emits `RUN_ID=<id>` to stdout +
  `$EXEC_OUTPUT` so a check that fires a run can pass `--run-id "$RUN_ID"` to `workflow-run inspect`;
  `--initial-step` and `--data` are validated (exit 2) before resolving the workflow.

  No `delete` verb: `AutomationWorkflow` is already a `_SWEEP_TYPES` member, so the generic prefix sweep
  reaches it and a second path would be two places holding one fact. Never `TRIGGER_BLUE_AGENT` in a lab
  flow: manual Blue Agent runs are 5/day/tenant and a cohort exhausts them on learner two.
- `apiaccount ensure|inspect|delete [--session <sid>] [--name N]` — a `THIRD_PARTY` service account, the plain API credential an agent
  or a script in a lab reads the tenant with; the remote MCP server takes it in the `Wiz-Client-Id` /
  `Wiz-Client-Secret` / `Wiz-DataCenter` headers with no integration object behind it. `ensure [--scopes S] [--expires-at <RFC3339>] [--out-file <path>]` names it
  `<stem>-api` (or `--name`) and converges to one fresh account (delete-then-mint, `clientSecret` readable only in the
  create payload), emitting `WIZ_CLIENT_ID` / `WIZ_CLIENT_SECRET` to stdout + `$EXEC_OUTPUT` (or writing the pair to `--out-file` and redacting stdout):
  `createServiceAccount(input:{name, type: THIRD_PARTY, scopes, assignedProjectIds: [], expiresAt})`.
  `--scopes` defaults to `read:all` under the same `<verb>:<object>` check as `mcp`; `--expires-at` is
  null by default, so the reap is the account's only end of life. `inspect --require exists` (author/legacy-only: 0 lab-script callers, kept in `VERBS` for verb-floor compatibility) and `delete
  --id|--name` go through `serviceAccounts(filterBy:{name, deleted:false})` / `deleteServiceAccount`,
  which **accepts this type**: no owner object, so the reaper's `ServiceAccount` prefix sweep deletes it
  on the uniform path. The caller needs `create:service_accounts` + `delete:service_accounts`.
- `mcp ensure|inspect|delete [--session <sid>] [--name N]` — *(author/legacy-only: 0 lab-script callers; agent labs use `apiaccount`, kept in `VERBS` for verb-floor compatibility)* the Wiz MCP integration an agent in a lab reads the tenant through.
  `ensure [--scopes S] [--expires-at <RFC3339>]` names it `<stem>-mcp` (or `--name`) and converges to one fresh integration (delete-then-mint: `clientSecret`
  is readable only in the create payload), emitting `WIZ_CLIENT_ID` / `WIZ_CLIENT_SECRET` to stdout +
  `$EXEC_OUTPUT`. `createIntegration(input:{name, type: WIZ_MCP, serviceAccountScopes, overrideScopes,
  isAccessibleToAllProjects, serviceAccountExpiresAt})`; the type takes no `params`. `--scopes` defaults
  to the single string `read:all` and a value that is not `<verb>:<object>` is invocation error 2 — the
  API accepts an unknown scope silently and the agent then sees a tool short, not an error, because
  remote-MCP tool visibility is filtered by the account's permissions. `overrideScopes: true` always
  rides along: the WIZ_MCP type default is 11 read scopes omitting `read:vulnerabilities` and
  `read:ai_security_findings`, and any other list is refused without it. `inspect --require exists|active`
  — **`exists` is what a setup check asserts**: `createIntegration` returns `INITIALIZING` and `ACTIVE`
  follows first use, so `active` fails every freshly built lab while the environment is healthy.
  `delete --id|--name` is `deleteIntegration`, which **cascades to the service account** — one call is the
  whole inverse. Lookup is `integrations(filterBy:{search, type:[WIZ_MCP]})`, substring, with no
  exact-name field, so the caller matches `==`. The minted account is `<integration name>_<uuid>`, inside
  the reaper's `ServiceAccount` prefix sweep; `deleteServiceAccount` refuses it (`type: INTEGRATION`) with
  an opaque internal error, so the sweep routes it back through `deleteIntegration` — resolved from
  `serviceAccount(id).integration`, the reverse lookup a CLI deployment does not have. `Integration` is a
  `_SWEEP_TYPES` member ahead of `ServiceAccount`, so the ordinary path deletes the owner first and finds
  the account already absent.
- `policy ensure|inspect|delete --name N` — the BLOCK CI/CD IaC scan policy a code-scan gate needs.
  `ensure [--control-search 'Last User Is'] [--rule-id <id>] [--severity S] [--count-threshold K]` is idempotent by name (§What `ensure` promises); absent, it creates a `type:IAC` policy with `enforcementMethod
  BLOCK` on `deploymentLifecycle CLI`, scoped (`iacParams.cloudConfigurationRules`) to the builtin
  Dockerfile control 'Last User Is root' (resolved live via `cloudConfigurationRules` matching `--control-search`, never
  hard-coded; `--rule-id`/`--severity`/`--count-threshold` override), `default:false` so only a
  `wizcli --policies <name>` scan is gated. `inspect --require exists` (author/legacy-only: 0 lab-script callers, kept in `VERBS` for verb-floor compatibility) verifies setup; `delete`
  (author/legacy-only: 0 lab-script callers, kept in `VERBS` for operator teardown and verb-floor compatibility) removes it. A SHARED, PERSISTENT fixture — the reaper never touches it (not session-scoped).
- `correlation inspect --repo <org/name> [--session <sid>] [--rule 'Docker Correlation'] [--since <RFC3339>|--since-boot] --require edge` — the code-to-cloud
  edge: a `graphEntityCorrelations(filterBy:{toType:{equals:[CONTAINER_IMAGE]}})` node under `--rule` whose
  `from.properties.externalId` starts `github.com##<org/name>##`, read on the image side so a sibling
  slot's edge never grades this repo; `ignored` nodes do not count. 0 ≥1 edge · 1 none. With `--since` (or
  `--since-boot`, which reads `/run/lab/boot_at` written by `entrypoint.sh` at container start; missing file is
  3, both flags together is 2), an edge counts only when `updatedAt >= since` (`GraphEntityCorrelationFilters`
  has no time filter, and on a reused slot repo Wiz updates the existing edge in place when the image is
  rebuilt while `createdAt` stays fixed — measured 2026-10-27 on TE: slot-01 `createdAt 2026-10-16T22:21:24Z`,
  `updatedAt 2026-10-26T21:53:26Z` after a solve play, and neither slot-01 nor slot-02 `updatedAt` moved during
  no-solve play `mwgvp4n6aj5j`).
- `pullrequest inspect --repo <org/name> [--number N] [--session <sid>] --require scanned|passed` — *(author/legacy-only: 0 lab-script callers, kept in `VERBS` for verb-floor compatibility)* the Patch PR's scan (no
  `--number`: the open PR on head `patch/<stem>`, read from GitHub, never opened): a
  `cloudEvents` row (`kind CI_CD_SCAN`, `cicdScan.trigger PULL_REQUEST`, `resource.subjectResourceExternalId
  github.com##<org>/<name>`) matched client-side on `codeAnalyzerDetails.pullRequest.id` (no server-side PR
  filter), newest by `timestamp` since each covering VCS connector emits one row. `scanned` = `status.state
  SUCCESS`; `passed` = SUCCESS and verdict `PASSED_BY_POLICY` or `WARN_BY_POLICY` — the default
  vulnerabilities policy warns on every Juice Shop slot's npm tree and the PR plane carries no Dockerfile
  finding, so the verdict grades a completed scan, never the fix. No row or PENDING is 1; FAILURE or
  SKIPPED (the scan did not run) is 3. Emits `PR_SCAN_STATE`, `PR_SCAN_VERDICT`, `PR_SCAN_FINDINGS`.
- `slot claim|release --lab <name> [--session <sid>]` — the learner-repo pool (te-code-labs `scripts/pool.py` seeds and
  resets it): `claim` takes the first free `te-code-<lab>-NN` by creating `refs/heads/claims/<lab>/slot-NN` in
  te-code-labs on an empty commit whose message is the session stem (atomic, so two graders cannot share
  a slot) and emits `SLOT`, `REPO`; a slot the session already holds is reprinted; a claim whose commit is
  older than `CLAIM_TTL_S` (7200s) is evicted and treated as free by minting the new claim commit with
  `parents: [<expired sha>]` and a non-force `PATCH /git/refs/heads/claims/<lab>/slot-NN` (`force: false`,
  200 = claimed, 422 `Update is not a fast forward` = another claimer evicted first, move on; GitHub
  enforces fast-forward only under `refs/heads/`); none free is 1.
  `release` drops the session's claims, closes the open PR on `patch/<stem>` and deletes
  `refs/heads/patch/<stem>` on the slot repo, a no-op when none; `user reap` does the same for every
  code-lab slot claim whose commit message is the stem or whose commit is older than `CLAIM_TTL_S` (needs
  `WIZ_LABS_GH_APP`; absent = not swept, said once). GitHub is reached as
  the App through the one JSON secret `WIZ_LABS_GH_APP` (`app_id`, `install_id`, base64 PEM `key`); absent
  is 3 before any request.
- `patch open --repo <org/name> [--session <sid>]` — the learner's fix as a PR: branch `patch/<stem>` from `main`,
  Dockerfile `USER root` → `USER node`, one PR against `main`; emits `PR_NUMBER`. A rerun finds the open
  PR on that head; a Dockerfile with no `USER root` is 1.
- `code-scan inspect --require published|pass [--session <sid>] [--tag-key session] [--tag-value <stem>] [--timeout 180] [--interval 10]` — asserts a Wiz Code CI/CD scan for this session,
  scoped by the `session` tag (`--tag-key`/`--tag-value`, default value the session stem), latest scan
  wins. `published` = ≥1 scan exists; `pass` = latest `status.verdict == PASSED_BY_POLICY`
  (`FAILED_BY_POLICY` exits 1 at once). Grades the TENANT verdict, not the CLI exit code, because
  `WARN_BY_POLICY` exits 0 — only a blocking policy distinguishes a finding from a pass. Polls every
  `--interval` to `--timeout` (default 180s) since publish latency is unmeasured. `WARN_BY_POLICY` and
  terminal non-running states (`FAILURE`, `SKIPPED`) also exit 1 on the first poll.

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
