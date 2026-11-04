"""Wiz Code: CI/CD scans and the IaC scan policy fixture."""
import contextlib
import io
import os
import pathlib
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
# Published state SUCCESS lands <= ~20s after `wizcli scan dir` (measurements.yaml `planes.code_scan.publish`). ---
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
        "{ nodes { id status { state verdict } result { ... on CICDDiskScanResult { "
        "sast { id rule { id } } } } } totalCount } }"
    )
    data, _ = core.api(qy, {})
    nodes = (data.get("cicdScans") or {}).get("nodes") or []
    return nodes[0] if nodes else None


def _inspect_verdict(node, require, tag_key, tag_value, rule_ids_str):
    st = node.get("status") or {}
    state, verdict = st.get("state"), st.get("verdict")

    if require.startswith("sast-findings="):
        try:
            expected_count = int(require.split("=")[1])
        except ValueError:
            core.die(2, "--require sast-findings=<n> must specify an integer")
        if state in ("IN_PROGRESS", "PENDING"):
            return False
        # Wait until the scan completes before validating findings.
        if not (verdict or state in ("FAILURE", "SKIPPED", "SUCCESS")):
            return False

        res = node.get("result") or {}
        sast_findings = res.get("sast") or []
        if rule_ids_str:
            rids = set(r.strip() for r in rule_ids_str.split(","))
            sast_findings = [f for f in sast_findings if (f.get("rule") or {}).get("id") in rids]
        if len(sast_findings) == expected_count:
            core._emit(f"SAST_FINDINGS={len(sast_findings)}\n")
            print(f"code-scan: {node['id']} sast-findings={len(sast_findings)}")
            return True
        print(f"code-scan: {node['id']} sast-findings={len(sast_findings)} (expected {expected_count})")
        sys.exit(1)

    if require == "published":
        print(f"code-scan: {node['id']} present ({tag_key}={tag_value}) state={state} verdict={verdict}")
        return True
    if require == "pass":
        if verdict == "PASSED_BY_POLICY":
            print(f"code-scan: {node['id']} PASSED_BY_POLICY")
            return True
        if verdict in ("FAILED_BY_POLICY", "WARN_BY_POLICY") or state in ("FAILURE", "SKIPPED"):
            print(f"code-scan: {node['id']} {verdict or state}")
            sys.exit(1)
        return False

    core.die(2, f"unrecognized --require {require}")


def cmd_codescan_inspect(args):
    """Assert a Wiz CLI scan for this session. --require published (>=1 scan tagged session=<stem>) or
    pass (latest scan verdict PASSED_BY_POLICY) or sast-findings=<n>.
    Polls every --interval up to --timeout (default 180s;
    measurements.yaml `planes.code_scan.publish`); a definitive FAILED_BY_POLICY exits 1 without waiting.
    WARN_BY_POLICY and terminal non-running states (FAILURE, SKIPPED) likewise exit 1 on the first poll."""
    require = args.require
    if not require:
        core.die(2, "--require is required")
    tag_key = args.tag_key
    tag_value = core._named(args, flag="tag_value")
    rule_ids = getattr(args, "rule_ids", None)
    timeout = args.timeout
    interval = args.interval
    deadline = time.monotonic() + timeout
    while True:
        node = _cicd_latest(tag_key, tag_value)
        if node and _inspect_verdict(node, require, tag_key, tag_value, rule_ids):
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
    """Return the child env for wizcli with WIZ_CLIENT_ID/SECRET from $RUN_LAB_DIR/cli-<stem>.env.
    Reuses the 0600 file only when `credential.find` still shows the deployment; otherwise mints via
    `credential.ensure --out-file` (0600, O_NOFOLLOW) with stdout/stderr suppressed."""
    run_dir = pathlib.Path(os.environ.get("RUN_LAB_DIR") or "/run/lab")
    cli_file = run_dir / f"cli-{stem}.env"
    name = f"{stem}-cli"
    if not (cli_file.is_file() and credential.find(serviceaccount.CLI_DEPLOYMENT, name)):
        run_dir.mkdir(parents=True, exist_ok=True)
        ns = types.SimpleNamespace(session=args.session, name=name, out_file=str(cli_file))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            credential.ensure(serviceaccount.CLI_DEPLOYMENT, ns)
    pair = dict(ln.split("=", 1) for ln in cli_file.read_text().splitlines() if "=" in ln)
    if not (pair.get("WIZ_CLIENT_ID") and pair.get("WIZ_CLIENT_SECRET")):
        core.die(3, f"{cli_file}: missing WIZ_CLIENT_ID or WIZ_CLIENT_SECRET")
    return {**os.environ, **pair}


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
    runs `wizcli scan dir` (tolerating policy-failure exit), and polls to a published verdict."""
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


def _find_policy(name, ptype="IAC"):
    """The IaC CI/CD scan policy whose name matches exactly (names filter is a contains, so ==)."""
    nodes = core._all_nodes(CICD_POLICIES_Q, {"f": {"type": [ptype.upper()], "names": [name]}}, "cicdScanPolicies")
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
    """Ensure the BLOCK CI/CD scan policy named --name exists."""
    name = _policy_name(args)
    ptype = getattr(args, "type", "iac")
    existing = _find_policy(name, ptype)

    lifecycles = getattr(args, "lifecycles", "CLI") or "CLI"
    lcs = [lc.strip() for lc in lifecycles.split(",")]

    if existing:
        live = existing.get("params") or {}
        if ptype == "iac":
            rule_id = args.rule_id
            core._drift(f"cicd scan policy {name} ({existing['id']})", [
                ("severityThreshold", live.get("severityThreshold"), args.severity),
                ("countThreshold", live.get("countThreshold"), args.count_threshold),
                ("cloudConfigurationRules", sorted(r["id"] for r in live.get("cloudConfigurationRules") or []),
                 [rule_id] if rule_id else None),
            ])
        elif ptype == "sast":
            rule_ids = [r.strip() for r in args.rule_ids.split(",")] if getattr(args, "rule_ids", None) else []
            scope = live.get("findingScope") or {}
            live_rules = scope.get("rules") or []
            core._drift(f"cicd scan policy {name} ({existing['id']})", [
                ("sastSeverityThreshold", live.get("sastSeverityThreshold"), args.severity),
                ("countThreshold", live.get("countThreshold"), args.count_threshold),
                ("findingScope.rules", sorted(live_rules), sorted(rule_ids) if rule_ids else None),
            ])
        print(f"cicd scan policy {name} exists ({existing['id']})")
        return

    inp = {
        "name": name,
        "default": False,
        "policyLifecycleEnforcements": [{"enforcementMethod": "BLOCK", "deploymentLifecycle": lc} for lc in lcs],
    }

    if ptype == "iac":
        rule_id = args.rule_id
        if not rule_id:
            ctl = _resolve_dockerfile_control(args.control_search)
            if not ctl:
                core.die(3, f"no DOCKER_FILE control matching {ROOT_DOCKERFILE_CONTROL!r} in this tenant")
            rule_id = ctl["id"]
        inp["lifecycleTargets"] = ["DEPLOY", "BUILD", "CODE"]
        inp["iacParams"] = {
            "severityThreshold": args.severity or "HIGH",
            "countThreshold": args.count_threshold or 1,
            "cloudConfigurationRules": [rule_id],
        }
        msg = f"scoped to control {rule_id}"
    elif ptype == "sast":
        inp["lifecycleTargets"] = ["BUILD", "CODE"]
        rule_ids = [r.strip() for r in args.rule_ids.split(",")] if getattr(args, "rule_ids", None) else []
        inp["sastParams"] = {
            "sastSeverityThreshold": args.severity or "HIGH",
            "countThreshold": args.count_threshold or 1,
            "findingScope": {"rules": rule_ids},
        }
        msg = f"scoped to SAST rules {','.join(rule_ids)}"

    data, _ = core.api(CREATE_CICD_POLICY, {"input": inp})
    pol = core._created(data, "createCICDScanPolicy", "scanPolicy")
    print(f"created cicd scan policy {name} ({pol['id']}) {msg}")

def cmd_policy_inspect(args):
    """Assert the CI/CD scan policy named --name exists (--require exists) — setup verification."""
    name = _policy_name(args)
    ptype = getattr(args, "type", "iac")
    node = _find_policy(name, ptype)
    if not node:
        print(f"no cicd scan policy named {name}")
        sys.exit(1)
    print(f"cicd scan policy {node['name']} ({node['id']})")


def cmd_policy_delete(args):
    """Delete the CI/CD scan policy named --name."""
    name = _policy_name(args)
    ptype = getattr(args, "type", "iac")
    node = _find_policy(name, ptype)
    if not node:
        print(f"no cicd scan policy named {name}; nothing to delete")
        return
    core.api(core._delete_doc("deleteCICDScanPolicy", "id"), {"id": node["id"]})
    print(f"deleted cicd scan policy {node['id']}")
