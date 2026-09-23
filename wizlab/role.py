"""CSP role trust and bindings, read through the native CLIs."""
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

from . import core, wiz

_DELEGATOR_RE = re.compile(r"role/prod-[A-Za-z0-9_-]+-AssumeRoleDelegator", re.IGNORECASE)


ROLE_NAME = "WizAccess-Role"


# GCP grants Wiz nothing to assume: Wiz binds roles to its OWN managed service account, so the graded
# state is the project's IAM policy, not a trust policy. These five built-ins are what the vendor
# module binds under its defaults (fetcher_roles_standard, module build 2696 main.tf:7-14) and what
# the manual guide tells a customer to bind by hand — so asserting exactly these accepts both paths.
GCP_WIZ_ROLES = (
    "roles/viewer",
    "roles/browser",
    "roles/iam.securityReviewer",
    "roles/cloudasset.viewer",
    "roles/serviceusage.serviceUsageViewer",
)


# --- role trust-policy helpers. IAM policy docs write plurals as scalar-or-list and hand back the
# trust doc either decoded or percent-encoded depending on the CLI build, so accept every shape or
# the assertion flickers. ---
def _as_list(v):
    if v is None:
        return []
    return list(v) if isinstance(v, (list, tuple)) else [v]


def _decode_trust_policy(doc):
    if isinstance(doc, dict):
        return doc
    if isinstance(doc, (str, bytes)):
        text = doc.decode("utf-8", "replace") if isinstance(doc, bytes) else doc
        for cand in (text, urllib.parse.unquote_plus(text), urllib.parse.unquote(text)):
            try:
                p = json.loads(cand)
            except ValueError:
                continue
            if isinstance(p, dict):
                return p
    return None


def _grants_assume_role(stmt):
    if str(stmt.get("Effect", "")).strip().lower() != "allow":
        return False
    for a in _as_list(stmt.get("Action")):
        s = str(a).strip().lower()
        if s in ("*", "sts:*", "sts:assumerole") or (s.endswith("*") and "sts:assumerole".startswith(s[:-1])):
            return True
    return False


def _principals(stmt):
    pr = stmt.get("Principal")
    if isinstance(pr, str):
        return [pr]
    if isinstance(pr, dict):
        return [str(v) for vals in pr.values() for v in _as_list(vals)]
    return []


def _service_principals(stmt):
    """The Service key alone. Flattened across keys the way _principals does it, an AWS principal
    would satisfy a Service assertion while the service that has to assume the role still cannot."""
    pr = stmt.get("Principal")
    if isinstance(pr, dict):
        return [str(v) for k, vals in pr.items() if str(k).strip().lower() == "service" for v in _as_list(vals)]
    return []


def _trusts_delegator(stmt, delegator):
    principals = _principals(stmt)
    if delegator:
        return delegator in principals
    return any(_DELEGATOR_RE.search(p) for p in principals)


def _external_ids(stmt):
    # Only equality pins the value: a StringLike or Null condition would pass a role Wiz can't assume.
    cond = stmt.get("Condition") or {}
    if not isinstance(cond, dict):
        return []
    out = []
    for op, mp in cond.items():
        if "equals" not in str(op).lower() or not isinstance(mp, dict):
            continue
        for k, v in mp.items():
            if str(k).strip().lower() == "sts:externalid":
                out.extend(str(x) for x in _as_list(v))
    return out


def _wiz_delegator():
    params, tid = wiz._managed_identity()
    return params["aws"].get("roleArn") or "", tid


def _wiz_gcp_sa():
    params, _tid = wiz._managed_identity()
    return params["gcp"].get("serviceAccountEmail") or ""


def _wiz_azure_object_id():
    """Wiz's service-principal OBJECT id in the CUSTOMER's directory. Unknowable to Wiz (its API
    exposes only the Application id) and unreadable from the lease (the lookup is Microsoft Graph,
    which Instruqt's Entra policy denies), so it is an operator secret — tenant-keyed exactly like
    the client id/secret in token_and_dc()."""
    return core._tenant_env("AZURE_APP_OBJECT_ID") or core.die(
        3, f"WIZ_{core._tenant()}_AZURE_APP_OBJECT_ID not in environment (operator secret; not wired?)")


def _role_inspect_azure(args):
    """The Azure twin of the GCP IAM-policy assertion: does the subscription actually grant Wiz's
    service principal the ARM roles it needs? Wiz owns the App Registration in its own tenant, so
    there is nothing for a learner to create in Entra — only role assignments to make."""
    sub = core._norm_account(args.account_id or os.getenv("ARM_SUBSCRIPTION_ID"))
    if not sub:
        core.die(2, "role inspect --cloud azure needs --account-id <subscription id> (or ARM_SUBSCRIPTION_ID)")
    custom = args.role_name
    if not custom:
        # No safe default: every lab uses a session-scoped name (lab-<session_id>-WizCustomRole).
        # A fallback to any fixed string silently grades as 1 for every correctly-configured learner.
        core.die(
            2, "role inspect --cloud azure requires --role-name <display-name> (session-scoped; no default is safe)")
    # --fill-principal-name false is NOT optional: it defaults TRUE and resolves names through
    # Microsoft Graph, which Entra denies here — the call would fail for a reason unrelated to the
    # learner's state.
    res = core._az("role", "assignment", "list", "--scope", f"/subscriptions/{sub}",
              "--assignee-object-id", _wiz_azure_object_id(), "--fill-principal-name", "false",
              "--query", "[].roleDefinitionName", "-o", "json")
    if res.returncode != 0:
        # No "not found" here means learner state: the subscription IS the lease, so any az failure
        # (no session, denied, bad scope) is environment. Branch on the exit code, never on az's
        # message text.
        core.die(3, f"az role assignment list failed on /subscriptions/{sub}: {res.stderr.strip()}")
    assigned = set(json.loads(res.stdout or "[]"))
    missing = [r for r in ("Reader", custom) if r not in assigned]
    if missing:
        print(f"subscription {sub}: Wiz's service principal is missing {', '.join(missing)}")
        sys.exit(1)
    print(f"subscription {sub}: Wiz's service principal holds Reader and {custom}")


def _role_inspect_gcp(args):
    """The GCP twin of the AWS trust-policy assertion: does this project's IAM policy actually grant
    Wiz's managed SA the roles it needs to read the project? A project that grants nothing is exactly
    the state a learner reaches by opening the wizard and stopping."""
    project = args.account_id or os.getenv("GOOGLE_PROJECT")
    if not project:
        core.die(2, "role inspect --cloud gcp needs --account-id <project id> (or GOOGLE_PROJECT)")
    sa = _wiz_gcp_sa()
    if not sa:
        core.die(3, "managedIdentityParameters.gcp.serviceAccountEmail is empty; no Wiz identity to check")
    res = core._gcp("projects", "get-iam-policy", project, "--format=json")
    if res.returncode != 0:
        # Unlike AWS get-role, there is no "not found" that means learner state: the project is the
        # lease itself, so any failure here (no creds, denied, no such project) is environment (3).
        core.die(3, f"gcloud projects get-iam-policy {project} failed: {res.stderr.strip()}")
    member = f"serviceAccount:{sa}"
    policy = json.loads(res.stdout or "{}")
    bound = {b.get("role") for b in (policy.get("bindings") or []) if member in (b.get("members") or [])}
    missing = [r for r in GCP_WIZ_ROLES if r not in bound]
    if missing:
        print(f"project {project}: Wiz identity is missing {', '.join(missing)}")
        sys.exit(1)
    print(f"project {project}: all {len(GCP_WIZ_ROLES)} Wiz roles bound to the tenant's managed identity")


def _aws_role(role):
    res = core._aws("iam", "get-role", "--role-name", role, "--output", "json")
    if res.returncode != 0:
        # NoSuchEntity is the ONLY failure meaning "not found" (learner state, 1). Everything else
        # — missing/expired creds, network, AccessDenied — is environment (3): an allowlist of auth
        # strings here once let "no credentials" grade as "learner wrong".
        if "nosuchentity" in res.stderr.lower():
            print(f"role {role} not found")
            sys.exit(1)
        core.die(3, f"aws iam get-role failed probing {role}: {res.stderr.strip()}")
    return json.loads(res.stdout or "{}").get("Role") or {}


def _inspect_aws_trust(role, role_obj):
    policy = _decode_trust_policy(role_obj.get("AssumeRolePolicyDocument"))
    if policy is None:
        print(f"role {role} exists but its trust policy could not be decoded")
        sys.exit(1)
    delegator, tid = _wiz_delegator()
    grants = [s for s in _as_list(policy.get("Statement")) if isinstance(s, dict) and _grants_assume_role(s)]
    if not grants:
        print(f"role {role}: trust policy grants no sts:AssumeRole (trusts nobody)")
        sys.exit(1)
    # AWS evaluates a statement as a unit, so the principal and the externalId condition have to ride
    # in the SAME statement. Flattened across statements, the delegator under a wrong external id plus
    # another principal under the right one grades valid while Wiz's assume-role is still denied.
    # Scope: Allow statements only — a Deny that narrows them is not modelled.
    trusting = [s for s in grants if _trusts_delegator(s, delegator)]
    if not trusting:
        principals = [p for s in grants for p in _principals(s)]
        if delegator:
            print(f"role {role}: does not trust this tenant's Wiz delegator {delegator}; trusts {principals}")
        else:
            print(f"role {role}: trusts no Wiz AssumeRoleDelegator; trusts {principals}")
        sys.exit(1)
    ext = [v for s in trusting for v in _external_ids(s)]
    if not ext:
        print(f"role {role}: no StringEquals sts:ExternalId condition on the statement trusting "
              f"{delegator or 'the Wiz delegator'}; Wiz's assume-role is rejected")
        sys.exit(1)
    if tid and tid not in ext:
        print(f"role {role}: sts:ExternalId {ext} != tenant id {tid}")
        sys.exit(1)
    print(f"role {role}: trust valid (assume-role to {delegator or 'Wiz delegator'}, externalId={tid})")


def _inspect_aws_service_trust(role, role_obj, service):
    """A role Wiz's own backend passes to an AWS service (the Outpost node pool role to EKS) is not
    trusted by the Wiz delegator at all, so the delegator+externalId assertion cannot grade it: the
    graded fact is which service principal may assume it. EKS validates this at CreateNodegroup, so a
    wrong principal creates nothing and leaves no failed resource to read."""
    policy = _decode_trust_policy(role_obj.get("AssumeRolePolicyDocument"))
    if policy is None:
        print(f"role {role} exists but its trust policy could not be decoded")
        sys.exit(1)
    want = service.strip().lower()
    stmts = [s for s in _as_list(policy.get("Statement")) if isinstance(s, dict)]
    for s in stmts:
        if _grants_assume_role(s) and want in [p.strip().lower() for p in _service_principals(s)]:
            print(f"role {role}: trusts service {service} for sts:AssumeRole")
            return
    found = sorted({p for s in stmts for p in _service_principals(s)})
    print(f"role {role}: no Allow statement lets {service} assume it; "
          f"trusts service {found or 'no service principal'}")
    sys.exit(1)


def cmd_role_inspect(args):
    """Existence is NOT the assertion: a role that trusts nobody is exactly the state a learner
    reaches by creating the role and stopping, and it reports the same connector ERROR forever. So
    verify the trust policy too. Delegator+tid resolved live for an EXACT match — another data
    center's delegator looks almost identical and does not work."""
    handlers = {"gcp": _role_inspect_gcp, "azure": _role_inspect_azure}
    cloud = args.cloud
    service = args.trusts_service
    if service and cloud != "aws":
        core.die(2, "--trusts-service is aws-only; a service principal is an IAM trust-policy concept")
    if cloud in handlers:
        return handlers[cloud](args)
    role = args.role_name or ROLE_NAME
    role_obj = _aws_role(role)
    if service:
        return _inspect_aws_service_trust(role, role_obj, service)
    return _inspect_aws_trust(role, role_obj)


def cmd_role_ensure(args):
    """Deploy WizAccess-Role's trust so Wiz can assume it. Trust-only by design: task 2's check
    verifies only the trust policy. Scan permissions (SecurityAudit / ViewOnlyAccess / inline) are
    task 3's need and unverified against TBCMP health — added when there's a live health signal."""
    if args.cloud != "aws":
        # No GCP twin by design: GCP's grant is custom-role creation + setIamPolicy, i.e. CSP
        # provisioning, which stays in Terraform. Fail loudly rather than no-op.
        core.die(2, "role ensure is aws-only; on GCP the vendor Terraform module binds the roles")
    role = args.role_name or ROLE_NAME
    delegator, tid = _wiz_delegator()
    if not delegator:
        core.die(3, "managedIdentityParameters.aws.roleArn is empty; cannot build a trust policy")
    # --external-id overrides the correct tid: setup uses it to SEED a broken trust (repair labs);
    # the learner's fix / solve runs `role ensure` with no override and writes the correct tid.
    ext = args.external_id or tid
    if not ext:
        core.die(3, "no tenant id (tid) in token and no --external-id; cannot set sts:ExternalId")
    trust = json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"AWS": delegator},
                    "Action": "sts:AssumeRole",
                    "Condition": {"StringEquals": {"sts:ExternalId": ext}},
                }
            ],
        }
    )
    res = core._aws("iam", "create-role", "--role-name", role,
                    "--assume-role-policy-document", trust, "--output", "json")
    if res.returncode == 0:
        print(f"role {role}: created, trusts {delegator} (externalId={ext})")
    elif "entityalreadyexists" in res.stderr.lower():
        upd = core._aws("iam", "update-assume-role-policy", "--role-name", role, "--policy-document", trust)
        if upd.returncode != 0:
            core.die(3, f"update-assume-role-policy failed: {upd.stderr.strip()}")
        print(f"role {role}: trust updated, trusts {delegator} (externalId={ext})")
    else:
        core.die(3, f"create-role failed: {res.stderr.strip()}")
