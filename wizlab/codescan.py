"""Wiz Code: CI/CD scans and the IaC scan policy fixture."""
import re
import sys
import time

from . import core

# --- Wiz Code CI/CD scan, published by `wizcli scan dir --tags session=<stem>`. A learner check must
# grade the tenant-side scan, not the CLI exit code: WARN_BY_POLICY exits 0, so only the published
# verdict tells a blocking finding from a pass. Scoped by the session tag; the latest scan wins.
# Publish latency is unmeasured, so --require polls to --timeout. ---
_TAG_OK = re.compile(r"^[A-Za-z0-9._-]+$")


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
        if node:
            st = node.get("status") or {}
            state, verdict = st.get("state"), st.get("verdict")
            if require == "published":
                print(f"code-scan: {node['id']} present ({tag_key}={tag_value}) state={state} verdict={verdict}")
                return
            if verdict == "PASSED_BY_POLICY":
                print(f"code-scan: {node['id']} PASSED_BY_POLICY")
                return
            if verdict in ("FAILED_BY_POLICY", "WARN_BY_POLICY") or state in ("FAILURE", "SKIPPED"):
                print(f"code-scan: {node['id']} {verdict or state}")
                sys.exit(1)
            # verdict not yet set (scan still running) — keep polling
        if time.monotonic() >= deadline:
            waited = "passing " if require == "pass" else ""
            print(f"code-scan: no {waited}scan for {tag_key}={tag_value} within {timeout}s")
            sys.exit(1)
        time.sleep(interval)


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
