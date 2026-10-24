"""Code labs: the Dockerfile→image correlation edge, the learner-repo pool slots, and the Patch PR.

GitHub is reached as the `wiz-labs-code-grader` App through one JSON secret, `WIZ_LABS_GH_APP`
(`{"app_id", "install_id", "key"}`, key a base64 PEM): the nine-secret cap leaves no room for three.
Pool claims are refs `refs/claims/<lab>/slot-NN` in te-code-labs pointing at an empty commit whose
message is the session stem; ref creation is atomic, so two graders cannot take one slot
(te-code-labs `scripts/pool.py`, the seed/reset side of the same refs).
"""
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

from . import core

GH_API = "https://api.github.com"
ORG = "wiz-training"
CONTROL_REPO = "te-code-labs"
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
DEFAULT_RULE = "Docker Correlation"
_LAB_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")
_CLAIM_REF_RE = re.compile(r"^refs/claims/([^/]+)/slot-(.+)$")
_TOKEN = {}

CORRELATIONS_Q = """query Correlations($f: GraphEntityCorrelationFilters, $after: String) {
  graphEntityCorrelations(first: 100, after: $after, filterBy: $f) {
    nodes { id ignored correlationRule { name } from { type name properties } to { type name } }
    pageInfo { hasNextPage endCursor }
  }
}"""


def _repo(args):
    repo = args.repo or core.die(2, "--repo <org/name> is required")
    if not _REPO_RE.match(repo):
        core.die(2, f"--repo {repo!r}: <org>/<name>")
    return repo


def cmd_correlation_inspect(args):
    """Assert the code-to-cloud edge: a correlation under --rule (default Docker Correlation) from a
    Dockerfile whose externalId starts `github.com##<org/name>##` to a CONTAINER_IMAGE. Read on the image
    side, filtered by `toType`, so a sibling slot's edge never grades this repo."""
    repo = _repo(args)
    prefix = f"github.com##{repo}##"
    filters = {"f": {"toType": {"equals": ["CONTAINER_IMAGE"]}}}
    nodes = core._all_nodes(CORRELATIONS_Q, filters, "graphEntityCorrelations")
    edges = [n for n in nodes if not n.get("ignored")
             and (n.get("correlationRule") or {}).get("name") == args.rule
             and str(((n.get("from") or {}).get("properties") or {}).get("externalId", "")).startswith(prefix)]
    if not edges:
        print(f"no {args.rule} edge from {repo} to a container image")
        sys.exit(1)
    for e in edges:
        print(f"edge {e['id']}: {e['from']['properties']['externalId']} -> {(e.get('to') or {}).get('name')}")


PR_SCANS_Q = """query PullRequestScans($f: CloudEventFilters, $after: String) {
  cloudEvents(first: 50, after: $after, filterBy: $f) {
    nodes { ... on CloudEvent { timestamp externalId extraDetails { ... on CloudEventCICDScanDetails {
      status { state verdict } totalMatches codeAnalyzerDetails { pullRequest { id infoURL } } } } } }
    pageInfo { hasNextPage endCursor }
  }
}"""
PASSING_VERDICTS = ("PASSED_BY_POLICY", "WARN_BY_POLICY")


def _pr_id(node):
    details = (node.get("extraDetails") or {}).get("codeAnalyzerDetails") or {}
    return str((details.get("pullRequest") or {}).get("id"))


def cmd_pullrequest_inspect(args):
    """Assert the Patch PR's scan: a CI_CD_SCAN cloud event with trigger PULL_REQUEST on the repo, matched
    client-side on pullRequest.id (no server-side PR filter exists), newest first since each covering VCS
    connector emits its own row. Without --number the PR is the open one on head patch/<stem>, read from
    GitHub. scanned = state SUCCESS; passed = SUCCESS and a verdict in
    PASSING_VERDICTS — WARN_BY_POLICY counts because the default vulnerabilities policy warns on every
    Juice Shop slot's npm tree and the PR plane carries no Dockerfile finding. PENDING or no row is 1; a
    scan that did not run (FAILURE, SKIPPED) is 3."""
    repo = _repo(args)
    number = args.number
    if number is None:
        # A check must find the learner's PR without opening one: the head is deterministic.
        number = _open_pr(repo, f"patch/{core._stem(args)}")
        if number is None:
            print(f"no open PR on {repo} head patch/{core._stem(args)}")
            sys.exit(1)
    # cloudEvents answers inside a default window when none is sent; a session is hours, not days.
    filters = {"f": {"kind": {"equals": ["CI_CD_SCAN"]},
                     "cicdScan": {"trigger": {"equals": ["PULL_REQUEST"]}},
                     "timestamp": {"inLast": {"amount": 7, "unit": "DurationFilterValueUnitDays"}},
                     "resource": {"subjectResourceExternalId": {"equals": [f"github.com##{repo}"]}}}}
    nodes = core._all_nodes(PR_SCANS_Q, filters, "cloudEvents")
    rows = [n for n in nodes if _pr_id(n) == str(number)]
    if not rows:
        print(f"no PR-triggered scan for {repo}#{number}")
        sys.exit(1)
    row = max(rows, key=lambda n: n.get("timestamp") or "")
    details = row.get("extraDetails") or {}
    state, verdict = (details.get("status") or {}).get("state"), (details.get("status") or {}).get("verdict")
    findings = details.get("totalMatches")
    core._emit(f"PR_SCAN_STATE={state}\nPR_SCAN_VERDICT={verdict}\nPR_SCAN_FINDINGS={findings}\n")
    if state in ("FAILURE", "SKIPPED"):
        core.die(3, f"{repo}#{number}: the PR scan did not run ({state})")
    if state != "SUCCESS":
        sys.exit(1)
    if args.require == "passed" and verdict not in PASSING_VERDICTS:
        sys.exit(1)


# --- GitHub as the App ---
def _app():
    raw = os.getenv("WIZ_LABS_GH_APP") or core.die(3, "WIZ_LABS_GH_APP not in environment")
    try:
        app = json.loads(raw)
        return app["app_id"], app["install_id"], base64.b64decode(app["key"])
    except (ValueError, KeyError, TypeError) as e:
        core.die(3, f"WIZ_LABS_GH_APP is not {{app_id, install_id, key}} JSON: {e}")


def _b64url(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _jwt(app_id, pem):
    """RS256 App JWT, signed with cryptography: the image carries it for azure-cli and no pyjwt."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    now = int(time.time())
    head = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    body = _b64url(json.dumps({"iat": now - 60, "exp": now + 540, "iss": str(app_id)}).encode())
    key = serialization.load_pem_private_key(pem, password=None)
    sig = key.sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{head}.{body}.{_b64url(sig)}"


def _gh_call(method, path, body=None, token=None):
    """One REST request; the process boundary tests replace. Returns (status, json); a transport or
    5xx fault is environment error 3, every 4xx returns to the caller to tolerate or die on."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(GH_API + path, data=data, method=method, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, (json.load(r) if r.status != 204 else {})
    except urllib.error.HTTPError as e:
        if e.code >= 500:
            core.die(3, f"GitHub {e.code} on {method} {path}")
        return e.code, json.loads(e.read() or b"{}")
    except OSError as e:
        core.die(3, f"GitHub transport failure on {method} {path}: {e}")


def _token():
    if "t" not in _TOKEN:
        app_id, install_id, pem = _app()
        status, body = _gh_call("POST", f"/app/installations/{install_id}/access_tokens", {}, token=_jwt(app_id, pem))
        if status != 201:
            core.die(3, f"GitHub installation token: {status} {body.get('message', '')}")
        _TOKEN["t"] = body["token"]
    return _TOKEN["t"]


def _gh(method, path, body=None, tolerate=()):
    status, out = _gh_call(method, path, body, token=_token())
    if status >= 400 and status not in tolerate:
        core.die(3, f"GitHub {status} on {method} {path}: {str(out.get('message', ''))[:200]}")
    return status, out


# --- pool slots ---
def _lab(args):
    lab = args.lab or core.die(2, "--lab <name> is required")
    if not _LAB_RE.match(lab):
        core.die(2, f"--lab {lab!r}: kebab-case")
    return lab


def _slot_repo(lab, slot):
    return f"{ORG}/te-code-{lab}-{slot}"


def _claims(lab):
    """{slot: session stem} from the claim refs."""
    status, refs = _gh("GET", f"/repos/{ORG}/{CONTROL_REPO}/git/matching-refs/claims/{lab}/", tolerate=(404,))
    out = {}
    for ref in refs if status == 200 else []:
        _, commit = _gh("GET", f"/repos/{ORG}/{CONTROL_REPO}/git/commits/{ref['object']['sha']}")
        out[ref["ref"].rsplit("slot-", 1)[-1]] = commit["message"].strip()
    return out


def _emit_slot(lab, slot):
    core._emit(f"SLOT={slot}\nREPO={_slot_repo(lab, slot)}\n")


def cmd_slot_claim(args):
    """Take the first free pool slot for this session and print SLOT / REPO. A slot this session already
    holds is reprinted, never re-claimed; no free slot is 1."""
    lab, stem = _lab(args), core._stem(args)
    _, repos = _gh("GET", f"/orgs/{ORG}/repos?per_page=100&type=public")
    prefix = f"te-code-{lab}-"
    slots = sorted(r["name"][len(prefix):] for r in repos if r["name"].startswith(prefix))
    claims = _claims(lab)
    held = [s for s, who in claims.items() if who == stem]
    if held:
        return _emit_slot(lab, held[0])
    for slot in slots:
        if slot in claims:
            continue
        _, commit = _gh("POST", f"/repos/{ORG}/{CONTROL_REPO}/git/commits",
                        {"message": stem, "tree": EMPTY_TREE, "parents": []})
        status, _ = _gh("POST", f"/repos/{ORG}/{CONTROL_REPO}/git/refs",
                        {"ref": f"refs/claims/{lab}/slot-{slot}", "sha": commit["sha"]}, tolerate=(422,))
        if status == 201:
            return _emit_slot(lab, slot)
    print(f"no free slot for {lab}: {len(slots)} slot(s), {len(claims)} claimed")
    sys.exit(1)


def cmd_slot_release(args):
    """Drop every claim this session holds; nothing held is a no-op."""
    lab, stem = _lab(args), core._stem(args)
    for slot, who in _claims(lab).items():
        if who == stem:
            _gh("DELETE", f"/repos/{ORG}/{CONTROL_REPO}/git/refs/claims/{lab}/slot-{slot}", tolerate=(422, 404))
            print(f"released slot {slot}")


def release_claims_for(stem, commit):
    """Release every pool claim across all labs whose commit message is `stem`. Returns
    [(lab, slot, outcome)] with outcome 'removed' or 'absent' (`reap.REMOVED`/`reap.ABSENT`, passed as
    strings so the outcome constants stay defined in `reap` alone)."""
    status, refs = _gh("GET", f"/repos/{ORG}/{CONTROL_REPO}/git/matching-refs/claims/", tolerate=(404,))
    out = []
    for ref in refs if status == 200 else []:
        m = _CLAIM_REF_RE.match(ref.get("ref") or "")
        if not m:
            continue
        lab, slot = m.groups()
        _, commit_obj = _gh("GET", f"/repos/{ORG}/{CONTROL_REPO}/git/commits/{ref['object']['sha']}")
        if commit_obj["message"].strip() != stem:
            continue
        if not commit:
            print(f"DRY-RUN would release slot {lab}/{slot}")
            out.append((lab, slot, "removed"))
            continue
        del_status, _ = _gh("DELETE", f"/repos/{ORG}/{CONTROL_REPO}/git/refs/claims/{lab}/slot-{slot}",
                            tolerate=(422, 404))
        if del_status == 404:
            print(f"absent slot {lab}/{slot}")
            out.append((lab, slot, "absent"))
        else:
            print(f"released slot {lab}/{slot}")
            out.append((lab, slot, "removed"))
    return out


# --- the Patch PR ---
def _open_pr(repo, head):
    _, prs = _gh("GET", f"/repos/{repo}/pulls?state=open&head={ORG}:{head}")
    return prs[0]["number"] if prs else None


def cmd_patch_open(args):
    """Open the learner's fix on `patch/<stem>`: Dockerfile `USER root` → `USER node`, one PR against main;
    prints PR_NUMBER. Rerun finds the open PR; a Dockerfile with no `USER root` is 1."""
    repo, stem = _repo(args), core._stem(args)
    head = f"patch/{stem}"
    number = _open_pr(repo, head)
    if number is None:
        _, f = _gh("GET", f"/repos/{repo}/contents/Dockerfile?ref=main")
        text = base64.b64decode(f["content"]).decode()
        if "USER root" not in text:
            print(f"{repo} Dockerfile has no USER root to patch")
            sys.exit(1)
        _, main = _gh("GET", f"/repos/{repo}/git/ref/heads/main")
        _gh("POST", f"/repos/{repo}/git/refs", {"ref": f"refs/heads/{head}", "sha": main["object"]["sha"]},
            tolerate=(422,))
        _gh("PUT", f"/repos/{repo}/contents/Dockerfile", {
            "message": f"{stem}: run Juice Shop as node, not root", "branch": head, "sha": f["sha"],
            "content": base64.b64encode(text.replace("USER root", "USER node").encode()).decode()})
        _, pr = _gh("POST", f"/repos/{repo}/pulls", {
            "title": "Run Juice Shop as a non-root user", "head": head, "base": "main",
            "body": f"Opened by the {stem} lab session: `USER root` → `USER node`."})
        number = pr["number"]
    core._emit(f"PR_NUMBER={number}\n")
