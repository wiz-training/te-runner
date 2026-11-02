"""Wiz Code: CI/CD scans and the IaC scan policy fixture."""
import contextlib
import io
import os
import re
import subprocess
import sys
import tempfile
import time
import types

from . import codelab, core, credential, serviceaccount

# --- Wiz Code CI/CD scan, published by `wizcli scan dir --tags session=<stem>`. A learner check must
# grade the tenant-side scan, not the CLI exit code: WARN_BY_POLICY exits 0, so only the published
# verdict tells a blocking finding from a pass. Scoped by the session tag; the latest scan wins.
# Publish latency is unmeasured, so --require polls to --timeout. ---
_TAG_OK = re.compile(r"^[A-Za-z0-9._-]+$")
_REF_OK = re.compile(r"^[A-Za-z0-9._/-]+$")
_ENSURE_TIMEOUT_S = 180
_ENSURE_INTERVAL_S = 10
_ENSURE_POLLS = _ENSURE_TIMEOUT_S // _ENSURE_INTERVAL_S


def _cicd_latest(tag_key, tag_value):
    """The most recent CICDScan carrying tag key=value. Inlined (not a typed variable) so a wrong
    input-type name can't error the query; the tag is validated to a safe charset first."""
    if not (_TAG_OK.match(tag_key) and _TAG_OK.match(tag_value)):
        core.die(2, "code-scan tag key/value must match [A-Za-z0-9._-]+")
    qy = (
        'query { cicdScans(first: 1, filterBy: { tag: { key: "' + tag_key + '", value: "' + tag_value + '" } }, '
        "orderBy: { field: CREATED_AT, direction: DESC }) "
        "{ nodes { id status { state verdict } } totalCount } }"
    )
    data, _ = core.api(qy, {})
    nodes = (data.get("cicdScans") or {}).get("nodes") or []
    return nodes[0] if nodes else None


def _inspect_verdict(node, require, tag_key, tag_value):
    st = node.get("status") or {}
    state, verdict = st.get("state"), st.get("verdict")
    if require == "published":
        print(f"code-scan: {node['id']} present ({tag_key}={tag_value}) state={state} verdict={verdict}")
        return True
    if verdict == "PASSED_BY_POLICY":
        print(f"code-scan: {node['id']} PASSED_BY_POLICY")
        return True
    if verdict in ("FAILED_BY_POLICY", "WARN_BY_POLICY") or state in ("FAILURE", "SKIPPED"):
        print(f"code-scan: {node['id']} {verdict or state}")
        sys.exit(1)
    return False


def cmd_codescan_inspect(args):
    """Assert a Wiz CLI scan for this session. --require published (>=1 scan tagged session=<stem>) or
    pass (latest scan verdict PASSED_BY_POLICY). Polls every --interval up to --timeout (default 180s)
    because publish latency is unmeasured; a definitive FAILED_BY_POLICY exits 1 without waiting.
    WARN_BY_POLICY and terminal non-running states (FAILURE, SKIPPED) likewise exit 1 on the first poll."""
    require = args.require
    tag_key = args.tag_key
    tag_value = core._named(args, flag="tag_value")
    timeout = args.timeout
    interval = args.interval
    deadline = time.monotonic() + timeout
    while True:
        node = _cicd_latest(tag_key, tag_value)
        if node and _inspect_verdict(node, require, tag_key, tag_value):
            return
        if time.monotonic() >= deadline:
            waited = "passing " if require == "pass" else ""
            print(f"code-scan: no {waited}scan for {tag_key}={tag_value} within {timeout}s")
            sys.exit(1)
        time.sleep(interval)


def _codescan_published(scan_name, node):
    st = (node or {}).get("status") or {}
    state, verdict = st.get("state"), st.get("verdict")
    if not node or state in ("IN_PROGRESS", "FAILURE", "SKIPPED") or not (verdict or state):
        return False
    core._emit(f"SCAN_NAME={scan_name}\nSCAN_VERDICT={verdict or ''}\n")
    print(f"code-scan: {node.get('id')} present (session={scan_name}) state={state} verdict={verdict}")
    return True


def _codescan_cli_env(args, stem):
    """Return the child env for wizcli, reusing `<stem>-cli` when present or minting via
    `credential.ensure` with stdout/stderr/$EXEC_OUTPUT suppressed so the secret stays in memory."""
    env = dict(os.environ)
    if credential.find(serviceaccount.CLI_DEPLOYMENT, f"{stem}-cli"):
        return env
    ns = types.SimpleNamespace(session=args.session, name=f"{stem}-cli", out_file=None)
    buf = io.StringIO()
    saved = os.environ.pop("EXEC_OUTPUT", None)
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            credential.ensure(serviceaccount.CLI_DEPLOYMENT, ns)
    finally:
        if saved is not None:
            os.environ["EXEC_OUTPUT"] = saved
    for line in buf.getvalue().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            env[k] = v
    return env


def _codescan_run(argv, *, cwd=None, env=None, ok_codes=(0,)):
    try:
        proc = subprocess.run(
            argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=core._CLI_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        core.die(3, f"{argv[0]}: {e}")
    if proc.returncode not in ok_codes:
        detail = (proc.stderr or proc.stdout or "").strip() or f"exit {proc.returncode}"
        core.die(3, f"{' '.join(argv[:2])}: {detail}")
    return proc


def _codescan_poll(scan_name):
    deadline = time.monotonic() + _ENSURE_TIMEOUT_S
    for attempt in range(_ENSURE_POLLS):
        node = _cicd_latest("session", scan_name)
        if _codescan_published(scan_name, node):
            return
        if ((node or {}).get("status") or {}).get("state") in ("FAILURE", "SKIPPED"):
            break
        if attempt + 1 >= _ENSURE_POLLS or time.monotonic() >= deadline:
            break
        time.sleep(_ENSURE_INTERVAL_S)
    print(f"code-scan: no scan for session={scan_name} within {_ENSURE_TIMEOUT_S}s")
    sys.exit(1)


def cmd_codescan_ensure(args):
    """Converge a session-tagged Wiz CLI directory scan (`session=<stem>-<name-suffix>`). Short-circuits
    when a published scan already exists; otherwise reuses or mints `<stem>-cli` (passing credentials
    only in `wizcli`'s child environment), shallow-clones `--ref` of `https://github.com/<repo>.git`,
    runs `wizcli auth` + `wizcli scan dir` (tolerating policy-failure exit), and polls to a published
    verdict."""
    repo = codelab._repo(args)
    ref, suffix, policy, types_arg = args.ref, args.name_suffix, args.policy, args.types
    if not ref or not suffix or not policy:
        core.die(2, "code-scan ensure requires --ref, --name-suffix, --policy")
    if not _REF_OK.match(ref) or ref.startswith("-"):
        core.die(2, f"--ref must match [A-Za-z0-9._/-]+, got {ref!r}")
    stem = core._stem(args)
    scan_name = f"{stem}-{suffix}"
    if not _TAG_OK.match(scan_name):
        core.die(2, f"--name-suffix must match [A-Za-z0-9._-]+, got {suffix!r}")
    if _codescan_published(scan_name, _cicd_latest("session", scan_name)):
        return
    child_env = _codescan_cli_env(args, stem)
    with tempfile.TemporaryDirectory() as tmpdir:
        _codescan_run(
            ["git", "clone", "--depth", "1", "--branch", ref, f"https://github.com/{repo}.git", tmpdir],
            env=child_env,
        )
        _codescan_run(["wizcli", "auth"], cwd=tmpdir, env=child_env)
        scan_cmd = ["wizcli", "scan", "dir", "."]
        if types_arg:
            scan_cmd += ["--types", types_arg]
        scan_cmd += ["--policies", policy, "--tags", f"session={scan_name}", "--name", scan_name]
        _codescan_run(scan_cmd, cwd=tmpdir, env=child_env, ok_codes=(0, 1, 4))
    _codescan_poll(scan_name)


# --- Wiz Code CI/CD scan policy: the BLOCK gate a code-scan lab's `wizcli scan dir --policies <name>`
# trips. A SHARED, PERSISTENT fixture (not session-scoped) so the reaper never deletes it. Scoped to
# one builtin Dockerfile control so it never blocks unrelated scans, and default:false so only a scan
# that names it is gated. ---
CICD_POLICIES_Q = """query CicdPolicies($f: CICDScanPolicyFilters, $after: String) {
  cicdScanPolicies(first: 50, after: $after, filterBy: $f) {
    nodes {
      id name
      params { ... on CICDScanPolicyParamsIAC { severityThreshold countThreshold cloudConfigurationRules { id } } }
    }
    pageInfo { hasNextPage endCursor }
  }
}"""


CREATE_CICD_POLICY = """mutation CreateCicdPolicy($input: CreateCICDScanPolicyInput!) {
  createCICDScanPolicy(input: $input) { scanPolicy { id name } }
}"""


CONFIG_RULE_Q = """query ConfigRule($f: CloudConfigurationRuleFilters) {
  cloudConfigurationRules(first: 5, filterBy: $f) { nodes { id name severity } }
}"""


ROOT_DOCKERFILE_CONTROL = "Last User Is 'root'"  # builtin, HIGH, matcher DOCKER_FILE


def _policy_name(args):
    return args.name or core.die(2, "policy needs --name <policy-name>")


def _find_policy(name):
    """The IaC CI/CD scan policy whose name matches exactly (names filter is a contains, so ==)."""
    nodes = core._all_nodes(CICD_POLICIES_Q, {"f": {"type": ["IAC"], "names": [name]}}, "cicdScanPolicies")
    return core._exact(nodes, name)


def _resolve_dockerfile_control(search):
    """Resolve the builtin Dockerfile control live — its id is tenant-specific, never hard-coded. The
    name must match EXACTLY and once: `search` is a server-side contains, so its first hit can be a
    different control, scoping the policy to a condition the lab never asks the learner to fix.
    `--rule-id` is the way to scope to anything else."""
    data, _ = core.api(CONFIG_RULE_Q, {"f": {"search": search, "matcherType": ["DOCKER_FILE"]}})
    nodes = (data.get("cloudConfigurationRules") or {}).get("nodes") or []
    exact = [n for n in nodes if n.get("name") == ROOT_DOCKERFILE_CONTROL]
    if len(exact) > 1:
        core.die(3, f"tenant holds {len(exact)} DOCKER_FILE controls named {ROOT_DOCKERFILE_CONTROL!r}; "
               f"pass --rule-id to name the one to scope to")
    return exact[0] if exact else None


def cmd_policy_ensure(args):
    """Ensure the BLOCK CI/CD IaC scan policy named --name exists (idempotent by name). Scoped to the
    builtin Dockerfile control 'Last User Is root' so `wizcli scan dir --policies <name>` FAILS
    (exit 4) on a root Dockerfile and passes once fixed. Shared, persistent fixture — the reaper never
    deletes it. --severity / --count-threshold override the defaults; --rule-id scopes to a different
    control outright, while --control-search only narrows the lookup for the documented one."""
    name = _policy_name(args)
    existing = _find_policy(name)
    if existing:
        # Only a flag the caller named can differ: the defaults never argue with a live fixture.
        live = existing.get("params") or {}
        rule_id = args.rule_id
        core._drift(f"cicd scan policy {name} ({existing['id']})", [
            ("severityThreshold", live.get("severityThreshold"), args.severity),
            ("countThreshold", live.get("countThreshold"), args.count_threshold),
            ("cloudConfigurationRules", sorted(r["id"] for r in live.get("cloudConfigurationRules") or []),
             [rule_id] if rule_id else None),
        ])
        print(f"cicd scan policy {name} exists ({existing['id']})")
        return
    rule_id = args.rule_id
    if not rule_id:
        ctl = _resolve_dockerfile_control(args.control_search)
        if not ctl:
            core.die(3, f"no DOCKER_FILE control matching {ROOT_DOCKERFILE_CONTROL!r} in this tenant")
        rule_id = ctl["id"]
    inp = {
        "name": name,
        "default": False,
        "lifecycleTargets": ["DEPLOY", "BUILD", "CODE"],
        "policyLifecycleEnforcements": [{"enforcementMethod": "BLOCK", "deploymentLifecycle": "CLI"}],
        # countThreshold must be positive (0 is rejected live). 1 fails on the first HIGH hit from the
        # scoped control; scoping to the one control means no unrelated IaC finding trips it.
        "iacParams": {
            "severityThreshold": args.severity or "HIGH",
            "countThreshold": args.count_threshold or 1,
            "cloudConfigurationRules": [rule_id],
        },
    }
    data, _ = core.api(CREATE_CICD_POLICY, {"input": inp})
    pol = core._created(data, "createCICDScanPolicy", "scanPolicy")
    print(f"created cicd scan policy {name} ({pol['id']}) scoped to control {rule_id}")


def cmd_policy_inspect(args):
    """Assert the CI/CD scan policy named --name exists (--require exists) — setup verification."""
    name = _policy_name(args)
    node = _find_policy(name)
    if not node:
        print(f"no cicd scan policy named {name}")
        sys.exit(1)
    print(f"cicd scan policy {node['name']} ({node['id']})")


def cmd_policy_delete(args):
    """Delete the CI/CD scan policy named --name. CRUD symmetry only — the reaper never calls this: the
    policy is a shared, persistent fixture, not a session's footprint."""
    name = _policy_name(args)
    node = _find_policy(name)
    if not node:
        print(f"no cicd scan policy named {name}; nothing to delete")
        return
    core.api(core._delete_doc("deleteCICDScanPolicy", "id"), {"id": node["id"]})
    print(f"deleted cicd scan policy {node['id']}")
