"""Kubernetes connectors the lab owns, and the container-plane check that grades enumeration."""
import json
import sys

from . import core

# A Kubernetes connector is keyed on the cluster's external id, which for EKS is the cluster ARN.
# The ARN comes from `aws eks describe-cluster`, never from a KUBERNETES_CLUSTER graph entity: in the
# state these verbs grade (no connector) that entity does not exist yet.
BY_CLUSTER = """query K8sConnectors($ids: [String!]) {
  connectors(filterBy: { kubernetesClusterExternalIds: $ids }, first: 20) {
    nodes { id name enabled status type { id } }
    totalCount
  }
}"""


CREATE = """mutation CreateConnector($input: CreateConnectorInput!) {
  createConnector(input: $input) { connector { id name status } }
}"""


UPDATE = """mutation UpdateConnector($input: UpdateConnectorInput!) {
  updateConnector(input: $input) { connector { id name enabled status } }
}"""


DELETE = """mutation DeleteConnector($input: DeleteConnectorInput!) {
  deleteConnector(input: $input) { _stub }
}"""


# graphSearch takes only query/first/after. The CONTAINER -> INSTANCE_OF -> CONTAINER_IMAGE traversal is
# the one observable of deployed-image scanning: registry counters never move in deployed-only mode and
# the image's own subscriptionExternalId is the repository owner's account, so the account filter goes
# on the CONTAINER. Never add cloudPlatform (matches 0 on CONTAINER) or sourceProvider (null on most
# scanned private images).
GRAPH = """query Containers($q: GraphEntityQueryInput!) {
  graphSearch(query: $q, first: 1) { totalCount }
}"""


# The token's ServiceAccount: a lab script applies it (with a cluster-admin ClusterRoleBinding) before
# `ensure` runs. kubectl is the fourth CSP-side CLI, present in the image from v0.1.54.
SERVICE_ACCOUNT = "wiz-connector"
SA_NAMESPACE = "kube-system"
TOKEN_DURATION = "24h"  # a play lasts hours; a bound token that outlives it is a leaked credential


def _kubectl(*a): return core._cli("kubectl", *a)


def _describe_cluster(name):
    r = core._aws("eks", "describe-cluster", "--name", name, "--output", "json")
    if r.returncode != 0:
        if "ResourceNotFoundException" in (r.stderr or ""):
            return None
        core.die(3, f"aws eks describe-cluster {name} failed: {r.stderr.strip()[:200]}")
    try:
        return json.loads(r.stdout or "{}").get("cluster") or None
    except json.JSONDecodeError:
        core.die(3, f"aws eks describe-cluster returned no JSON: {r.stdout.strip()[:200]!r}")


def _cluster_arn(args):
    """The connector's external id. `--cluster-arn` is authoritative; `--cluster` (default: the session
    stem) resolves through describe-cluster, and a cluster that does not exist is exit 1 — the fixture
    is absent, which is learner-visible state, not an environment fault."""
    if args.cluster_arn:
        return args.cluster_arn, None
    name = args.cluster or core._lab_stem(core._session_id(args))
    cluster = _describe_cluster(name)
    if cluster is None:
        print(f"no EKS cluster named {name}")
        sys.exit(1)
    return cluster["arn"], cluster


def _find(arn):
    data, _ = core.api(BY_CLUSTER, {"ids": [arn]})
    return ((data.get("connectors") or {}).get("nodes")) or []


def _token(cluster_name):
    """A bound token for the connector's ServiceAccount. update-kubeconfig first: the grader has no
    kubeconfig until then, and the aws CLI writes the exec-auth entry the token request needs."""
    r = core._aws("eks", "update-kubeconfig", "--name", cluster_name)
    if r.returncode != 0:
        core.die(3, f"aws eks update-kubeconfig failed: {r.stderr.strip()[:200]}")
    r = _kubectl("-n", SA_NAMESPACE, "create", "token", SERVICE_ACCOUNT, f"--duration={TOKEN_DURATION}")
    if r.returncode != 0 or not (r.stdout or "").strip():
        core.die(3, f"kubectl create token {SA_NAMESPACE}/{SERVICE_ACCOUNT} failed: {r.stderr.strip()[:200]}")
    return r.stdout.strip()


def _create(args, arn, cluster):
    """createConnector accepts the ARN as clusterExternalID with no cluster entity in the graph; the
    connector appears in the cluster-keyed list within ~15 s and reaches CONNECTED at ~7 min."""
    if cluster is None:
        cluster = _describe_cluster(args.cluster or arn.rsplit("/", 1)[-1])
        if cluster is None:
            core.die(3, f"cannot describe the cluster behind {arn}; the connector needs its endpoint and CA")
    auth = {
        "apiServerEndpoint": cluster["endpoint"],
        "authProvider": "service-account",
        "clusterExternalID": arn,
        "tlsConfig": {"serverCA": cluster["certificateAuthority"]["data"]},
        "authProviderConfig": {"serviceAccountToken": _token(cluster["name"])},
        "isOnPrem": False,
    }
    name = args.name or f"{core._lab_stem(core._session_id(args))}-k8s"
    data, _ = core.api(CREATE, {"input": {"name": name, "type": "eks", "enabled": args.enabled == "true",
                                          "authParams": auth, "extraConfig": {"installationType": "Script"}}})
    c = (data.get("createConnector") or {}).get("connector") or {}
    if not c:
        core.die(3, "createConnector returned no connector")
    print(f"created k8s connector {c['name']} ({c['id']}) status={c['status']}")


def cmd_k8sconnector_ensure(args):
    """Converge to one Kubernetes connector for the cluster in the requested enabled state: absent ->
    createConnector; present with `enabled` differing -> updateConnector; present and matching -> 0.
    An auto-onboarded child answers the patch with `Child connectors cannot be individually
    disabled/enabled`; that surfaces as 3, since only a connector the lab created can be toggled."""
    want = args.enabled == "true"
    arn, cluster = _cluster_arn(args)
    nodes = _find(arn)
    if not nodes:
        return _create(args, arn, cluster)
    n = nodes[0]
    if bool(n.get("enabled")) == want:
        print(f"k8s connector {n['name']} ({n['id']}) already enabled={n['enabled']} status={n['status']}")
        return
    data, _ = core.api(UPDATE, {"input": {"id": n["id"], "patch": {"enabled": want}}})
    c = (data.get("updateConnector") or {}).get("connector") or {}
    print(f"k8s connector {n['name']}: enabled {n.get('enabled')} -> {c.get('enabled', want)} status={c.get('status')}")


# --require reads lifecycle state: `exists` any record, `connected` the terminal healthy status,
# `disabled` the status updateConnector(enabled:false) leaves. INITIAL_SCANNING is the window after
# create and before the first scan, so `exists` is what a repair check asserts.
_REQUIRE = {"exists": lambda n: True,
            "connected": lambda n: n.get("status") == "CONNECTED",
            "disabled": lambda n: n.get("status") == "DISABLED"}


def cmd_k8sconnector_inspect(args):
    arn, _ = _cluster_arn(args)
    nodes = _find(arn)
    if not nodes:
        print(f"no Kubernetes connector for {arn}")
        sys.exit(1)
    n = nodes[0]
    print(f"k8s connector {n['name']} ({n['id']}): enabled={n.get('enabled')} status={n.get('status')}")
    sys.exit(0 if _REQUIRE[args.require](n) else 1)


def cmd_k8sconnector_delete(args):
    arn, _ = _cluster_arn(args)
    nodes = _find(arn)
    if not nodes:
        print(f"no Kubernetes connector for {arn}; nothing to delete")
        return
    for n in nodes:
        core.api(DELETE, {"input": {"id": n["id"]}})
        print(f"deleted k8s connector {n['name']} ({n['id']})")


def cmd_container_inspect(args):
    """Assert Wiz enumerates >=1 CONTAINER in the account whose image contains --image-contains, and
    with --require-image that the container reaches a scanned CONTAINER_IMAGE over INSTANCE_OF."""
    account = core._account_id(args, "container inspect")
    where = {"subscriptionExternalId": {"EQUALS": [account]}}
    if args.image_contains:
        where["image"] = {"CONTAINS": [args.image_contains]}
    rel = {"type": [{"type": "INSTANCE_OF"}], "with": {"type": ["CONTAINER_IMAGE"], "select": True}}
    if not args.require_image:
        rel["optional"] = True
    q = {"type": ["CONTAINER"], "select": True, "where": where, "relationships": [rel]}
    data, _ = core.api(GRAPH, {"q": q})
    n = (data.get("graphSearch") or {}).get("totalCount") or 0
    what = "container+image pairs" if args.require_image else "containers"
    print(f"container inspect: {n} {what} in account {account}"
          + (f" with image containing {args.image_contains!r}" if args.image_contains else ""))
    sys.exit(0 if n > 0 else 1)
