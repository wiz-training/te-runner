"""Kubernetes connectors the lab owns, and the container-plane check that grades enumeration."""
import os
import sys

from . import connector, core

# A Kubernetes connector is keyed on the cluster's external id, which for EKS is the cluster ARN.
# The ARN comes from `aws eks describe-cluster`, never from a KUBERNETES_CLUSTER graph entity: in the
# state these verbs grade (no connector) that entity does not exist yet.
BY_CLUSTER = """query K8sConnectors($ids: [String!]) {
  connectors(filterBy: { kubernetesClusterExternalIds: $ids }, first: 20) {
    nodes { id name enabled status type { id } }
    totalCount
  }
}"""


# Create, update and delete are the cloud connector's own documents (connector.CREATE / UPDATE / DELETE).


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
    if r.returncode != 0 and "ResourceNotFoundException" in (r.stderr or ""):
        return None
    return (core._csp_json(r, f"aws eks describe-cluster {name}") or {}).get("cluster") or None


def _cluster_arn(args, synthesize=False):
    """The connector's external id. `--cluster-arn` is authoritative; `--cluster` (default: the session
    stem) resolves through describe-cluster. When `synthesize` is True (`delete`), a gone cluster falls
    back to the ARN built from AWS_DEFAULT_REGION and AWS_ACCOUNT_ID (te-aws-scan-mode-dependency-201
    sandbox.hcl:33-34); otherwise a missing cluster is exit 1 — the fixture is absent, which is
    learner-visible state, not an environment fault."""
    if args.cluster_arn:
        return args.cluster_arn, None
    name = core._named(args, flag="cluster")
    cluster = _describe_cluster(name)
    if cluster is None:
        region, account = os.getenv("AWS_DEFAULT_REGION"), os.getenv("AWS_ACCOUNT_ID")
        if synthesize and region and account:
            return f"arn:aws:eks:{region}:{account}:cluster/{name}", None
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
    name = core._named(args, "-k8s")
    data, _ = core.api(connector.CREATE, {"input": {"name": name, "type": "eks", "enabled": args.enabled == "true",
                                          "authParams": auth, "extraConfig": {"installationType": "Script"}}})
    c = core._created(data, "createConnector", "connector")
    print(f"created k8s connector {c['name']} ({c['id']}) status={c['status']}")


def cmd_k8sconnector_ensure(args):
    """Converge to one Kubernetes connector for the cluster in the requested enabled state: absent ->
    createConnector; present with `enabled` differing -> updateConnector; present and matching -> 0.
    An auto-onboarded child answers the patch with `Child connectors cannot be individually
    disabled/enabled`; that surfaces as 3, since only a connector the lab created can be toggled."""
    want = args.enabled == "true"
    stem = core._stem_opt(args)
    arn, cluster = _cluster_arn(args)
    nodes = _find(arn)
    if not nodes:
        return _create(args, arn, cluster)
    n = min(nodes, key=lambda n: (not connector._stem_owned(n, stem), connector._foreign(n, stem),
                                  n.get("status") != "CONNECTED"))
    if connector._foreign(n, stem):
        core.die(3, f"k8s connector {n['name']} ({n['id']}) belongs to another session; not mutated")
    if bool(n.get("enabled")) == want:
        print(f"k8s connector {n['name']} ({n['id']}) already enabled={n['enabled']} status={n['status']}")
        return
    data, _ = core.api(connector.UPDATE, {"input": {"id": n["id"], "patch": {"enabled": want}}})
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
    n = core._prefer(nodes, "CONNECTED")
    print(f"k8s connector {n['name']} ({n['id']}): enabled={n.get('enabled')} status={n.get('status')}")
    sys.exit(0 if _REQUIRE[args.require](n) else 1)


def cmd_k8sconnector_delete(args):
    arn, _ = _cluster_arn(args, synthesize=True)
    nodes = _find(arn)
    if not nodes:
        print(f"no Kubernetes connector for {arn}; nothing to delete")
        return
    for n in nodes:
        core.api(connector.DELETE, {"id": n["id"]})
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
