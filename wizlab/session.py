"""`session verify`: env, credential and runner-floor health, the lab's check 1."""
import os
import pathlib
import re

from . import core

# The delegator ARN is per-tenant and its `prod-<dc>` segment varies by data center, so it is never
# a constant — resolved live from managedIdentityParameters.aws.roleArn.
IDENTITY = """query ManagedIdentityParameters {
  managedIdentityParameters {
    aws { roleArn defaultEnvironment }
    gcp { serviceAccountEmail }
    azure { commercial { appId } }
  }
}"""


def _runner_id():
    """This image's own identity — `(tag, rev)`, baked at build from the git tag and sha it was built
    from. Read per call, not once at import, so a test can set it. Empty on an image built before
    v0.1.37 and on a run from a git checkout: absence is the answer, never a default.

    Falls back to PID 1, which is the only context in the container that always carries the image's
    own ENV: sshd hands its sessions a scrubbed environment (measured — a validator driving over the
    tailnet sees no TE_RUNNER_*), so without this a floor assertion would exit 3 anywhere but the
    platform's own executor."""
    tag, rev = os.getenv("TE_RUNNER_TAG", ""), os.getenv("TE_RUNNER_REV", "")
    if tag:
        return tag, rev
    try:
        env = pathlib.Path("/proc/1/environ").read_bytes().decode("utf-8", "replace")
    except OSError:
        return "", ""
    kv = dict(p.split("=", 1) for p in env.split("\0") if "=" in p)
    return kv.get("TE_RUNNER_TAG", ""), kv.get("TE_RUNNER_REV", "")


def _version(s):
    """`v0.1.36` -> `(0, 1, 36)`, None if not a release version. Ints, because the string compare that
    reads naturally puts v0.1.9 ABOVE v0.1.36 and a floor would silently pass."""
    m = re.fullmatch(r"v?(\d+(?:\.\d+)*)", s.strip())
    return tuple(int(p) for p in m.group(1).split(".")) if m else None


def _require_runner(floor):
    """Assert this image meets a lab's declared verb floor.

    A lab's `sandbox.hcl` names its floor in a comment nothing reads, so a pin below it surfaces
    mid-play as a check the learner cannot pass. Exit 2, not 1: a pin disagreeing with its own floor is
    a broken declaration, never learner state. Exit 3 when the identity is absent — a runner cannot be
    graded against a floor it cannot name. Images before v0.1.37 carry no identity AND ignore this flag
    (unknown flags are never parsed), so the gate binds only once a lab is repinned to v0.1.37+."""
    want = _version(floor)
    if want is None:
        core.die(2, f"--min-runner: {floor!r} is not a version like v0.1.36")
    tag, _rev = _runner_id()
    if not tag:
        core.die(3, "--min-runner: TE_RUNNER_TAG unset — not an image build (a git checkout, or an image "
               "built before v0.1.37), so this runner's version is unknowable")
    have = _version(tag)
    if have is None:
        core.die(3, f"--min-runner: runner tag {tag!r} is not a release version")
    if have < want:
        core.die(2, f"runner {tag} is below this lab's floor {floor}: repin the image in sandbox.hcl "
               "(a play pins HCL at import, so the repin lands before the start click)")


_CSP_REQUIRED_VARS = {
    "aws":   ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"],
    "gcp":   ["GOOGLE_CREDENTIALS", "GOOGLE_PROJECT"],
    "azure": ["ARM_CLIENT_ID", "ARM_CLIENT_SECRET", "ARM_TENANT_ID", "ARM_SUBSCRIPTION_ID"],
}


def _verify_aws(account_id):
    ident = core._csp_json(core._aws("sts", "get-caller-identity"), "aws sts get-caller-identity") or {}
    account = str(ident.get("Account") or "")
    if not account:
        core.die(3, "aws sts get-caller-identity named no Account")
    if account_id and account != account_id:
        core.die(3, f"AWS credentials are for account {account}, not the {account_id} this lease grades")


def _verify_gcp(account_id):
    accounts = core._csp_json(core._gcp("auth", "list", "--format=json"), "gcloud auth list") or []
    if not [a for a in accounts if a.get("status") == "ACTIVE"]:
        core.die(3, f"gcloud holds no ACTIVE account ({len(accounts)} listed); GOOGLE_CREDENTIALS did not activate")
    project = os.getenv("GOOGLE_PROJECT")
    if account_id and project != account_id:
        core.die(3, f"GCP credentials are for project {project}, not the {account_id} this lease grades")


def _verify_azure(account_id):
    sub = (core._csp_json(core._az("account", "show", "-o", "json"), "az account show") or {}).get("id") or ""
    want = os.getenv("ARM_SUBSCRIPTION_ID")
    if sub.lower() != want.lower():
        core.die(3, f"az is logged in to subscription {sub or '(none)'}, not ARM_SUBSCRIPTION_ID {want}")
    if account_id and sub.lower() != account_id.lower():
        core.die(3, f"az is logged in to subscription {sub}, not the {account_id} this lease grades")


_VERIFY = {"aws": _verify_aws, "gcp": _verify_gcp, "azure": _verify_azure}


def _verify_csp(cloud, account_id=None):
    missing = [v for v in _CSP_REQUIRED_VARS[cloud] if not os.getenv(v)]
    if missing:
        core.die(3, f"CSP credentials missing from environment: {', '.join(missing)}")
    _VERIFY[cloud](account_id)


def cmd_session_verify(args):
    # The floor first: it needs no credential, and a mispinned image is the cheaper fault to name.
    floor = args.min_runner
    if floor:
        _require_runner(floor)
    if args.account_id and not args.cloud:
        core.die(2, "--account-id is compared to a cloud credential's own account and needs --cloud")
    _tok, dc, tid = core.token_and_dc()
    # `viewer` is not a Wiz root field (returns "Resource not found"); `connectors` is. This confirms
    # the token is accepted by the API, not just minted by auth.
    core.api("query { connectors(first: 1) { totalCount } }", {})
    if args.cloud:
        _verify_csp(args.cloud, args.account_id)
    # Name the runner on every check-1 line: it is the only record of what a play actually ran, since
    # the tag in a repo is what main held at import, not what this container is.
    tag, rev = _runner_id()
    print(f"ok: authenticated to dc={dc} tenant={tid} runner={tag or 'unknown'}@{rev[:7] or 'unknown'}")
