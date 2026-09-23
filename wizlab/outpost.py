"""The AWS Outpost lifecycle: create, grade, uninstall, delete."""
import datetime
import sys
import time

from . import core

# --- Wiz Outpost (Automated deploy on the customer's own account). Scope by name via `search`
# (OutpostFilters has no account/region field), exactly like the sensor plane. `status` is the
# OutpostStatus lifecycle enum: CONNECTED = a region scan cluster with a heartbeat (healthy
# terminal), INITIALIZED = the object is registered and NOTHING MORE — an Outpost whose roleARN names
# an account that is not ours still reaches INITIALIZED with errorCode null, so it is not evidence any
# AWS resource exists. The serviceType filter is a LIST. useWizServiceAccount:true means createOutpost
# returns a null serviceAccount. ---
OUTPOSTS_Q = """query Outposts($s: String, $after: String) {
  outposts(first: 50, after: $after, filterBy: { search: $s, serviceType: [AWS] }) {
    nodes {
      id name status inAccountOutpostType serviceType allowedRegions
      config { ... on OutpostAWSConfig { roleARN } }
      clusters { id region }
    }
    totalCount pageInfo { hasNextPage endCursor }
  }
}"""


OUTPOST_Q = """query Outpost($id: ID!) {
  outpost(id: $id) { id name status }
}"""


CREATE_OUTPOST = """mutation CreateOutpost($input: CreateOutpostInput!) {
  createOutpost(input: $input) { outpost { id name status } }
}"""


UNINSTALL_OUTPOST = """mutation UninstallOutpost($input: UninstallOutpostInput!) {
  uninstallOutpost(input: $input) { outpost { id status } }
}"""


DELETE_OUTPOST = """mutation DeleteOutpost($input: DeleteOutpostInput!) {
  deleteOutpost(input: $input) { _stub }
}"""


# Takes a CLUSTER id, not the Outpost's: an Outpost carrying no cluster has no provision pass to re-run.
INVOKE_CLUSTER_UPDATE = """mutation InvokeOutpostClusterUpdate($input: InvokeOutpostClusterUpdateInput!) {
  invokeOutpostClusterUpdate(input: $input) { requestID }
}"""


# The only OUTPOST-SCOPED proof that a disk was actually scanned, and the reason this lab needs no
# cloud connector: cloudResources/vulnerabilityFindings answer "is the account inventoried", which is
# the connector's job, whereas this counts scans performed BY one outpostId. Buckets are one point per
# UTC day, so sum the window. outpostId is String! (not ID!). CONNECTED-with-zero is common: CONNECTED
# means the cluster has a heartbeat, not that it has scanned anything.
SCAN_METRICS = """query ScanMetrics($s: DateTime!, $e: DateTime!, $o: String!) {
  resourceScanMetricsTrend(startDate: $s, endDate: $e, outpostId: $o) {
    dataPoints { timestamp aggregatedMetrics { totalScansCount successfulScansCount failedScansCount } }
  }
}"""


def _outpost_name(args):
    # Pinned to the session stem so the reaper's `search` finds it and concurrent leases never collide.
    return core._named(args)


def _resolve_outpost(name):
    """The AWS Outpost whose name exactly matches (search is substring, so filter to ==),
    CONNECTED-first so a healthy one outranks a stale UNINSTALLED record on a recycled name."""
    nodes = core._all_nodes(OUTPOSTS_Q, {"s": name}, "outposts")
    return core._prefer([n for n in nodes if n.get("name") == name], "CONNECTED")


def cmd_outpost_inspect(args):
    """Assert the session's Outpost is known to Wiz (--require exists), has reached INITIALIZED (the
    object is registered — NOT that the orchestrator role works; see the note above the queries), is
    CONNECTED (a region scan cluster with a heartbeat — the healthy terminal, and the only status that
    proves the deploy), or has SCANNED at least one disk (--require scanned, the workload-scan proof;
    CONNECTED alone does not imply it). The UI word 'Active' maps to CONNECTED, so grade the enum,
    not the label."""
    name = _outpost_name(args)
    require = args.require
    node = _resolve_outpost(name)
    if not node:
        print(f"no outpost named {name}")
        sys.exit(1)
    st = node["status"]
    print(f"outpost {node['name']} ({node['id']}): status={st}")
    if require == "exists":
        return
    if require == "connected":
        sys.exit(0 if st == "CONNECTED" else 1)
    if require == "scanned":
        sys.exit(_scan_counts(node["id"], args.lookback_days))
    # initialized: CONNECTED is a strict superset (a connected outpost passed through INITIALIZED),
    # so accept both — a check demanding exactly INITIALIZED would flip back to failure the moment
    # the scan cluster comes up.
    sys.exit(0 if st in ("INITIALIZED", "CONNECTED") else 1)


def _scan_counts(oid, lookback_days):
    """Exit code for '--require scanned': 0 iff this Outpost completed >=1 successful workload scan in
    the window. Failed-only counts as not satisfied but is printed — a nonzero failedScansCount is the
    signal that the cluster is up and the node pool cannot snapshot (an SCP/KMS gap), which reads very
    differently to an operator than 'nothing has happened yet'."""
    now = datetime.datetime.now(datetime.UTC)
    data, _ = core.api(SCAN_METRICS, {"s": (now - datetime.timedelta(days=lookback_days)).isoformat(),
                                 "e": now.isoformat(), "o": oid})
    points = ((data.get("resourceScanMetricsTrend") or {}).get("dataPoints") or [])
    agg = [p.get("aggregatedMetrics") or {} for p in points]
    ok = sum(m.get("successfulScansCount") or 0 for m in agg)
    failed = sum(m.get("failedScansCount") or 0 for m in agg)
    print(f"outpost scans in {lookback_days}d: {ok} successful, {failed} failed")
    return 0 if ok > 0 else 1


def cmd_outpost_ensure(args):
    """Create the AWS Automated Outpost named on the session stem, idempotent by name. Needs the
    orchestrator role ARN (--role-arn, the output of the Wiz orchestrator TF module) — Wiz assumes it
    to build the EKS scan cluster + network in the leased account. useWizServiceAccount so no
    per-outpost SA is minted. Solve/setup only."""
    name = _outpost_name(args)
    role_arn = args.role_arn or core.die(
        2, "outpost ensure needs --role-arn (the orchestrator role ARN from the TF module)")
    region = args.region or "us-east-1"
    existing = _resolve_outpost(name)
    if existing:
        # A role change is a knowing delete-and-recreate, never a silent "nothing to do".
        core._drift(f"outpost {name} ({existing['id']})", [
            ("roleARN", (existing.get("config") or {}).get("roleARN"), role_arn),
            ("allowedRegions", existing.get("allowedRegions"), [region] if args.region else None),
        ])
        if existing["status"] == "ERROR":
            return _reprovision(existing)
        print(f"outpost {name} already exists ({existing['id']}) status={existing['status']}; nothing to do")
        return
    # allowedRegions pins the scan cluster to the workload region (the EC2's). The capture used []
    # (unrestricted); [region] is the deterministic form — the validator confirms the cluster lands
    # in <region>. awsConfig keys are the capture's exact shape (roleARN is caps).
    inp = {
        "name": name,
        "serviceType": "AWS",
        "enabled": True,
        "selfManaged": False,
        "useWizServiceAccount": True,
        "forceHttp1": False,
        "awsConfig": {"disableNatGateway": True, "enablePrivateCluster": False, "roleARN": role_arn},
        "managedConfig": {"kubernetesLoggingEnabled": False, "kubernetesCloudMonitoringEnabled": False,
                          "deploySensor": False},
        "allowedRegions": [region],
    }
    data, _ = core.api(CREATE_OUTPOST, {"input": inp})
    o = (data.get("createOutpost") or {}).get("outpost") or {}
    if not o:
        core.die(3, "createOutpost returned no outpost")
    print(f"created outpost {o['name']} ({o['id']}) status={o.get('status')}")


def _reprovision(outpost):
    """ERROR is terminal on its own: Wiz never retries a failed provision pass, so an Outpost whose
    cause has since been fixed stays ERROR and its health issue's lastSeenAt stops advancing. The
    cluster update is the only trigger that re-runs the pass — no uninstall, no redeploy."""
    clusters = outpost.get("clusters") or []
    if not clusters:
        print(f"outpost {outpost['name']} ({outpost['id']}) is ERROR with no cluster to update")
        sys.exit(1)
    for c in clusters:
        data, _ = core.api(INVOKE_CLUSTER_UPDATE, {"input": {"id": c["id"]}})
        rid = (data.get("invokeOutpostClusterUpdate") or {}).get("requestID")
        print(f"outpost {outpost['name']}: cluster {c['id']} ({c.get('region')}) re-provisioning, requestID={rid}")


_OUTPOST_DELETABLE = ("UNINSTALLED", "UNINSTALLATION_FAILED")

# No proven transition reaches UNINSTALLED from here, and deleteOutpost refuses the record, so the
# terminal behaviour is undecided: report it as state (1), never as an environment fault (3).
_OUTPOST_STUCK = ("PARTIALLY_UNINSTALLED",)


def _uninstall_in_flight(status):
    """A status already carrying UNINSTALL is in or past the uninstall flow. Re-firing
    UNINSTALL_OUTPOST on one is the call that errors."""
    return "UNINSTALL" in (status or "")


def cmd_outpost_delete(args):
    """Best-effort reap of the session's Outpost. deleteOutpost on a live Outpost fails with a
    server-side internal error, and still fails while UNINSTALLING — the only working order is
    uninstall -> wait for UNINSTALLED -> delete (uninstall takes ~100s with no real infra; real EKS
    teardown is longer, hence --timeout). Exits 0 even when the object outlives the wait: the EKS
    infra dies with the lease, so a lingering UNINSTALLED record is cosmetic and the out-of-band
    daily reaper sweeps it. Resolve by --id or the session-stem name."""
    oid, name = args.id, None
    if not oid:
        name = _outpost_name(args)
        node = _resolve_outpost(name)
        if not node:
            print(f"no outpost named {name}; nothing to delete")
            return
        oid, status = node["id"], node["status"]
    else:
        status = (_outpost_by_id(oid) or {}).get("status")
    if status not in _OUTPOST_DELETABLE and status not in _OUTPOST_STUCK:
        if not _uninstall_in_flight(status):
            core.api(UNINSTALL_OUTPOST, {"input": {"id": oid}})
            print(f"uninstalling outpost {oid} (was {status})")
        status = _await_uninstalled(oid, args.timeout)
    if status == "GONE":
        print(f"outpost {oid} no longer exists; nothing left to delete")
        return
    if status in _OUTPOST_STUCK:
        print(f"outpost {oid} is {status}: the uninstall cannot be re-fired and deleteOutpost refuses "
              f"the record; it needs an operator")
        sys.exit(1)
    if status not in _OUTPOST_DELETABLE:
        print(f"outpost {oid} still {status} after the wait; leaving the record for the daily reaper")
        return
    core.api(DELETE_OUTPOST, {"input": {"id": oid}})
    print(f"deleted outpost {oid}")


def _outpost_by_id(oid):
    data, _ = core.api(OUTPOST_Q, {"id": oid})
    return data.get("outpost")


def _await_uninstalled(oid, timeout):
    """Poll until the uninstall reaches a terminal state; returns the last status seen. A stuck status
    ends the wait too — polling it out only spends the timeout."""
    deadline, status = time.monotonic() + timeout, "UNINSTALLING"
    while time.monotonic() < deadline:
        time.sleep(20)
        status = (_outpost_by_id(oid) or {}).get("status") or "GONE"
        if status in _OUTPOST_DELETABLE or status in _OUTPOST_STUCK or status == "GONE":
            break
    return status
