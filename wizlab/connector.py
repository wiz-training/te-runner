"""Cloud Connectors and the scanned-instance check."""
import os
import sys

from . import core, outpost, role

FIND = f"""query FindConnector($ids: [String!]) {{
  connectors(filterBy: {{ cloudAccountExternalId: $ids }}, first: 10) {{
    nodes {{{core._NODE}
    }}
    totalCount
  }}
}}"""


BY_TYPE = f"""query ConnectorsByType($t: [String!], $after: String) {{
  connectors(filterBy: {{ connectorType: $t }}, first: 100, after: $after) {{
    nodes {{{core._NODE}
    }}
    pageInfo {{ hasNextPage endCursor }}
  }}
}}"""


# Server-side substring search on name (the Deployments page's own filter; `search` also backs the
# reaper's prefix sweep). Bounded by the lab's own naming stem, so it has no page-size cliff — unlike
# BY_TYPE. Verified live on TS_PROD: a full name and a half-name both matched, and it returns the
# connector's CHILD deployments too ("GAR in <name>"), which is why _parents still filters by type.
SEARCH = f"""query SearchConnectors($s: String!) {{
  connectors(filterBy: {{ search: $s }}, first: 50) {{
    nodes {{{core._NODE}
    }}
    totalCount
  }}
}}"""


CREATE = """mutation CreateConnector($input: CreateConnectorInput!) {
  createConnector(input: $input) { connector { id name status } }
}"""


DELETE = core._delete_doc("deleteConnector")


# Selects `enabled` too: the Kubernetes connector's toggle patch reads it back from this same document.
UPDATE = """mutation UpdateConnector($input: UpdateConnectorInput!) {
  updateConnector(input: $input) { connector { id name enabled status } }
}"""


# Inventory presence check: a scanned cloud resource of a type, filtered to the lease account.
# subscriptionExternalId is the CSP account number; type is the Wiz graph type.
INSTANCES = """query Instances($f: CloudResourceFilters) {
  cloudResources(first: 1, filterBy: $f) { totalCount }
}"""


def _targets(node, account_id):
    if not account_id:  # empty id would make ":{}:" == "::" match any ARN — never match nothing
        return False
    cfg = node.get("config") or {}
    if cfg.get("projectId") == account_id or cfg.get("subscriptionId") == account_id:
        return True
    arn = cfg.get("customerRoleARN") or ""
    return f":{account_id}:" in arn


def _parents(nodes, cloud):
    """Cloud connectors, not the child deployments Wiz auto-spawns (e.g. `ECR in <acct>`,
    type.id=ecr) nor the builtin self-hosted pseudo-connector. Confirmed against TBCMP via
    the GUI deployments query: our connector is type.id=aws, its child ECR is type.id=ecr."""
    return [n for n in nodes if (n.get("type") or {}).get("id") == cloud]


def _foreign(node, stem):
    """True when `node` carries another session's `lab-<sid>` name or Outpost binding. A non-`lab-`
    connector (learner-named in the console) is never foreign."""
    if not stem:
        return False
    prefix = f"{core._session_prefix()}-"
    name = node.get("name") or ""
    oname = (node.get("outpost") or {}).get("name") or ""
    return (name.startswith(prefix) and not core._owned(name, stem)) or (
        oname.startswith(prefix) and not core._owned(oname, stem)
    )


def _stem_owned(node, stem):
    """True when `node` is named on `stem` and not bound to another session's Outpost."""
    return bool(stem and core._owned(node.get("name") or "", stem) and not _foreign(node, stem))


def _merge_parents(existing, nodes, cloud, account_id=None):
    seen = {n.get("id") for n in existing}
    extra = [n for n in _parents(nodes, cloud) if not account_id or _targets(n, account_id)]
    return existing + [n for n in extra if n.get("id") not in seen]


def find_connector(account_id, cloud="aws", stem=None):
    """Connectors whose OWN config targets the account, stem-owned and active-first.

    Three lookups, cheapest-and-most-correct first. Gate each on finding no PARENT (or no stem-owned /
    non-foreign parent when `stem` is known), never on no nodes: Wiz attaches a builtin pseudo-connector
    to every account, so "no nodes" never occurs and would silently retire the fallbacks — the exact bug
    that made delete miss a 3s-old connector, and a leftover `lab-<old>` connector on a recycled lease
    account must not hide this session's `lab-<sid>-connector` in the 1-2 min pre-link window.

    1. cloudAccountExternalId — the account LINK, which Wiz creates 1-2 min AFTER the connector, so a
       check run right after create/onboarding won't match yet.
    2. name search, when the caller knows this session's stem and step 1 returned no stem-owned parent —
       server-side, bounded by a session-unique name, so it covers the pre-link window with no page-size
       cliff.
    3. BY_TYPE — matches the connector's own config for anything NOT named on the stem (a learner who
       typed a different name, a pre-existing connector) when steps 1-2 found no non-foreign parent. It
       walks every page: a tenant holds far more connectors than one page, and stopping at the first
       would make "no match" stop meaning "absent", which reports learner state that was never read and
       lets `ensure` create a duplicate of a connector it merely couldn't see. A walk that cannot be
       trusted to be complete exits 3 (core._all_nodes) rather than concluding."""
    data, _ = core.api(FIND, {"ids": [account_id]})
    parents = _merge_parents([], ((data.get("connectors") or {}).get("nodes")) or [], cloud)
    if stem and not any(_stem_owned(n, stem) for n in parents):
        data, _ = core.api(SEARCH, {"s": stem})
        parents = _merge_parents(parents, ((data.get("connectors") or {}).get("nodes")) or [], cloud, account_id)
    if not [n for n in parents if not _foreign(n, stem)]:
        nodes = core._all_nodes(BY_TYPE, {"t": [cloud]}, "connectors")
        parents = _merge_parents(parents, nodes, cloud, account_id)
    # This session's stem outweighs a learner name, which outweighs a foreign leftover; within each tier,
    # an active connector outweighs a stale ERROR one.
    return sorted(
        parents,
        key=lambda n: (not _stem_owned(n, stem), _foreign(n, stem),
                       not (n.get("enabled") and n.get("status") != "ERROR")),
    )


def _reap_foreign(account, cloud, stem):
    """Delete any foreign `lab-<other>` connector on `account` and return the remaining matches."""
    matches = []
    for n in find_connector(account, cloud, stem):
        if _foreign(n, stem):
            core.api(DELETE, {"id": n["id"]})
            print(f"deleted stale connector {n['name']} ({n['id']})")
        else:
            matches.append(n)
    return matches


def cmd_connector_inspect(args):
    cloud = args.cloud
    account = core._account_id(args, "connector inspect")
    require = args.require
    if require != "outpost-bound":
        core._unused(args, f"--require {require}", "--outpost-id", "--outpost-name")
    stem = core._stem_opt(args)
    matches = [n for n in find_connector(account, cloud, stem) if not _foreign(n, stem)]
    if not matches:
        print(f"no connector targets {account}")
        sys.exit(1)
    node = matches[0]
    print(f"connector {node['name']} ({node['id']}): enabled={node['enabled']} status={node['status']}")
    if require == "exists":
        return
    if require == "outpost-bound":
        return _assert_outpost_bound(args, node)
    # healthy: role-first, a connector sits in INITIAL_SCANNING for ~2-3 min, then CONNECTED (the
    # terminal state), never ERROR. Require CONNECTED, not merely not-ERROR: INITIAL_SCANNING is the
    # window before Wiz has actually assumed the role, so not-ERROR there is a false pass.
    sys.exit(0 if node["enabled"] and node["status"] == "CONNECTED" else 1)


def _connector_name(args):
    # No --name on the connector verbs: the account is the key, so the name is always the stem's.
    return f"{core._stem(args)}-connector"


def _create_connector(name, cloud, auth, extra):
    data, _ = core.api(
        CREATE,
        {"input": {"name": name, "type": cloud, "authParams": auth, "extraConfig": extra}},
    )
    c = core._created(data, "createConnector", "connector")
    print(f"created connector {c['name']} ({c['id']}) status={c['status']}")


def _enable_if_disabled(n):
    """Re-enable a disabled connector via UPDATE `{enabled: True}` alone (never inside authParams)."""
    if n.get("enabled", True):
        return False
    data, _ = core.api(UPDATE, {"input": {"id": n["id"], "patch": {"enabled": True}}})
    c = (data.get("updateConnector") or {}).get("connector") or {}
    print(f"connector {n['name']}: enabled {n.get('enabled')} -> {c.get('enabled', True)} status={c.get('status')}")
    return True


def _ensure_gcp(args, account):
    """GCP has nothing to converge: the connector carries a project id and Wiz's own managed identity,
    neither of which can drift the way an AWS customerRoleARN does (there is no customer-side role).
    So this is create-if-absent (plus re-enabling a disabled connector) — the ARN repair path AWS needs
    has no GCP equivalent."""
    matches = _reap_foreign(account, "gcp", core._stem_opt(args))
    if matches:
        n = matches[0]
        if _enable_if_disabled(n):
            return
        print(f"connector {n['name']} already targets project {account}; nothing to do")
        return
    # Project scope means every scope list is empty, NOT absent.
    extra = {
        "projects": [],
        "excludedProjects": [],
        "includedFolders": [],
        "excludedFolders": [],
        "includedRegions": [],
        "excludedRegions": [],
    }
    return _create_connector(_connector_name(args), "gcp", {"isManagedIdentity": True, "project_id": account}, extra)


def _ensure_azure(args, account):
    """Create-if-absent, like GCP: an azure connector carries the subscription and Wiz's own managed
    identity, so there is nothing to patch in authParams (a tenantId mismatch is refused via core._drift
    and a disabled connector is re-enabled via UPDATE). isManagedIdentity is always set; subscriptionId
    only at subscription scope. The authParams/extraConfig BOUNDARY is inferred by analogy to
    aws+gcp: testConnectorConfig answers success:true even for a bogus key, so it cannot prove the
    split."""
    tenant_id = args.tenant_id or os.getenv("ARM_TENANT_ID")
    if not tenant_id:
        core.die(2, "connector ensure --cloud azure needs --tenant-id (or ARM_TENANT_ID): the connector "
               "stores the directory the subscription belongs to")
    matches = _reap_foreign(account, "azure", core._stem_opt(args))
    if matches:
        n = matches[0]
        core._drift(f"connector {n['name']} ({n['id']})", [
            ("tenantId", (n.get("config") or {}).get("tenantId"), tenant_id),
        ])
        if _enable_if_disabled(n):
            return
        print(f"connector {n['name']} already targets subscription {account}; nothing to do")
        return
    # Subscription scope: every scope list present and EMPTY, not absent (readback of the live 89).
    extra = {
        "includedSubscriptions": [],
        "excludedSubscriptions": [],
        "includedManagementGroups": [],
        "excludedManagementGroups": [],
        "includedRegions": [],
        "excludedRegions": [],
    }
    auth = {"isManagedIdentity": True, "subscriptionId": account, "tenantId": tenant_id}
    return _create_connector(_connector_name(args), "azure", auth, extra)


def _outpost_target(args):
    """(id, name) of the Outpost a connector binds to or is asserted against. `--outpost-name` (default:
    the session stem) is the name the console's Outpost dropdown shows, so it is how a learner picks one
    and how a solve mirrors them; `--outpost-id` skips the lookup. id is None when no such Outpost
    exists."""
    oid = args.outpost_id
    if oid:
        return oid, oid
    name = core._named(args, flag="outpost_name")
    return (outpost._resolve_outpost(name) or {}).get("id"), name


def _assert_outpost_bound(args, node):
    """Assert the connector is bound to the session's Outpost.

    An unbound connector is indistinguishable from a bound one by status: both reach CONNECTED, and the
    Outpost stays INITIALIZED with clusters null forever. `outpost` is the only field that separates
    them, which is why this is its own require rather than part of `healthy`."""
    bound = node.get("outpost") or {}
    want, name = _outpost_target(args)
    if not want:
        print(f"no outpost named {name}; nothing for a connector to bind to")
        sys.exit(1)
    role = (node.get("config") or {}).get("customerRoleARN") or "(none)"
    if not bound:
        print(f"connector is bound to NO outpost (customerRoleARN={role}); it will never build a cluster")
        sys.exit(1)
    if bound.get("id") != want:
        print(f"connector is bound to outpost {bound.get('name')} ({bound.get('id')}), not {want}")
        sys.exit(1)
    print(f"connector bound to outpost {bound.get('name')} ({bound['id']}), customerRoleARN={role}")


def _aws_auth_params(args, account):
    """The connector's `authParams`. Binding an Outpost is what makes Wiz build the scan cluster;
    unbound, the Outpost holds INITIALIZED with clusters null indefinitely. `outpostId` is the only
    binding field — there is no extraConfig key for it. The scanner role is what the node pool assumes
    to read a volume, so a connector bound without it reaches CONNECTED and still scans nothing, which
    is why the two flags are required together."""
    auth = {"customerRoleARN": args.role_arn or f"arn:aws:iam::{account}:role/{role.ROLE_NAME}"}
    scanner_arn = args.scanner_role_arn
    bind = args.outpost_id or args.outpost_name
    if bind and not scanner_arn:
        core.die(2, "binding an outpost needs --scanner-role-arn: an outpost-bound connector with no "
               "diskAnalyzer scanner role connects but never scans a disk")
    if scanner_arn and not bind:
        core.die(2, "--scanner-role-arn is only meaningful with --outpost-id or --outpost-name")
    if not bind:
        return auth
    oid, name = _outpost_target(args)
    if not oid:
        core.die(3, f"no outpost named {name}: nothing to bind this connector to")
    auth["outpostId"] = oid
    auth["diskAnalyzer"] = {"scanner": {"roleARN": scanner_arn}}
    return auth


def _binding_guard(args, node):
    """Every patch re-submits authParams whole, and an Outpost binding lives in those same authParams:
    a patch built without the outpost flags drops it, whether the patch is --reauth or a drift repair.
    The result has no lifecycle signal (CONNECTED, scans nothing), so refuse before sending."""
    if (node.get("outpost") or {}).get("id") and not (args.outpost_id or args.outpost_name):
        core.die(2, "connector ensure on an outpost-bound connector needs --outpost-id or "
                    "--outpost-name with --scanner-role-arn, or the patch drops the binding")


def _patch_aws(args, node, auth, same):
    """A patch is a re-init whether or not a value changes: the response already reads INITIAL_SCANNING.
    It is NOT a recovery — from ERROR on a corrected trust the connector holds INITIAL_SCANNING past
    30 min, with every UpdateConnectorPatch field re-submitted as well as authParams alone. Repair is
    delete then ensure (CONNECTED in ~4 min). A trust rotated AFTER CONNECTED shows nothing at all —
    CONNECTED, errorCode null, no health issue, lastActivity frozen — and Rescan does not
    re-authenticate. A patch that ADDS outpostId to an existing connector is unexercised."""
    _binding_guard(args, node)
    _enable_if_disabled(node)
    data, _ = core.api(UPDATE, {"input": {"id": node["id"], "patch": {"authParams": auth}}})
    status = (((data.get("updateConnector") or {}).get("connector")) or {}).get("status")
    cur = (node.get("config") or {}).get("customerRoleARN") or "(none)"
    cur_outpost = (node.get("outpost") or {}).get("id") or "(none)"
    outpost_id = auth.get("outpostId")
    what = "re-authenticating" if same else f"customerRoleARN {cur} -> {auth['customerRoleARN']}"
    print(f"connector {node['name']}: {what}"
          + (f", outpost {cur_outpost} -> {outpost_id}" if outpost_id else "")
          + f", status={status}")


# What only `_aws_auth_params` and `_patch_aws` read: a gcp or azure connector carries no customerRoleARN,
# no Outpost binding and no authParams the tenant re-runs, so each of these is refused there, never ignored.
_AWS_ONLY = ("--role-arn", "--scanner-role-arn", "--outpost-id", "--outpost-name", "--reauth")


def cmd_connector_ensure(args):
    """Idempotent converge: create the connector if absent, else re-enable if disabled and on AWS
    correct its customerRoleARN if it drifted (this is also the repair path — a lab seeds a wrong
    ARN, `ensure` fixes it). --reauth patches even when nothing drifted (_patch_aws)."""
    cloud = args.cloud
    account = core._account_id(args, "connector ensure")
    if cloud != "aws":
        core._unused(args, cloud, *_AWS_ONLY)
    if cloud != "azure":
        core._unused(args, cloud, "--tenant-id")
    # gcp/azure are create-if-absent (no ARN to drift); only aws falls through to the repair path.
    if cloud == "gcp":
        return _ensure_gcp(args, account)
    if cloud == "azure":
        return _ensure_azure(args, account)
    auth = _aws_auth_params(args, account)
    role_arn, outpost_id = auth["customerRoleARN"], auth.get("outpostId")
    matches = _reap_foreign(account, cloud, core._stem_opt(args))
    if matches:
        n = matches[0]
        cur = (n.get("config") or {}).get("customerRoleARN") or ""
        cur_outpost = (n.get("outpost") or {}).get("id") or ""
        same = cur == role_arn and (not outpost_id or cur_outpost == outpost_id)
        if same and not args.reauth:
            if _enable_if_disabled(n):
                return
            print(f"connector {n['name']} already targets {account} with {role_arn}; nothing to do")
            return
        return _patch_aws(args, n, auth, same)
    # customerRoleARN goes in authParams, NOT extraConfig; extraConfig carries only
    # skipOrganizationScan. The role need not exist yet — the connector sits in ERROR until task 2
    # deploys WizAccess-Role.
    return _create_connector(_connector_name(args), "aws", auth, {"skipOrganizationScan": True})


def cmd_connector_delete(args):
    cloud = args.cloud
    account = core._account_id(args, "connector delete")
    matches = find_connector(account, cloud, core._stem_opt(args))
    if not matches:
        print(f"no connector targets {account}; nothing to delete")
        return
    for n in matches:
        core.api(DELETE, {"id": n["id"]})
        print(f"deleted connector {n['name']} ({n['id']})")


def cmd_instance_inspect(args):
    """Assert a scanned cloud resource of --type (default VIRTUAL_MACHINE) exists in Wiz for the
    account. Exit 0 if >=1 present, 1 if none — the 'is the EC2 scanned yet' check."""
    account = core._account_id(args, "instance inspect")
    rtype = args.type
    data, _ = core.api(INSTANCES, {"f": {"subscriptionExternalId": [account], "type": [rtype]}})
    n = (data.get("cloudResources") or {}).get("totalCount") or 0
    print(f"instance inspect: {n} {rtype} scanned in account {account}")
    sys.exit(0 if n > 0 else 1)
