"""The per-lease Keycloak user behind Wiz SSO, and the tenant's login URL."""
import json
import os
import secrets
import string
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import namedtuple

from . import core


# --- lab user (Keycloak): backs Wiz SSO for a learner. Realm/endpoint come from grader-only env,
# never hardcoded. ---
def _kc_env():
    vals = {}
    for k in ("LAB_KEYCLOAK_ENDPOINT", "LAB_KEYCLOAK_REALM", "LAB_KEYCLOAK_ADMIN_USER", "LAB_KEYCLOAK_ADMIN_PWD"):
        v = os.getenv(k)
        if not v:
            core.die(3, f"{k} not in environment (grader-only Keycloak secret; not wired?)")
        vals[k] = v
    return (
        vals["LAB_KEYCLOAK_ENDPOINT"].rstrip("/"),
        vals["LAB_KEYCLOAK_REALM"],
        vals["LAB_KEYCLOAK_ADMIN_USER"],
        vals["LAB_KEYCLOAK_ADMIN_PWD"],
    )


def _kc_token(endpoint, admin_user, admin_pwd):
    res = core._post(
        f"{endpoint}/realms/master/protocol/openid-connect/token",
        urllib.parse.urlencode(
            {"grant_type": "password", "client_id": "admin-cli", "username": admin_user, "password": admin_pwd}
        ),
        {"Content-Type": "application/x-www-form-urlencoded"},
    )
    tok = res.get("access_token")
    if not tok:
        core.die(3, "Keycloak admin token: no access_token returned")
    return tok


# The setup every `user` verb shares: grader-only env, the per-lease email, an admin token. `name`
# is the firstName only `ensure` needs; inspect/delete unpack it to `_`.
_KcSession = namedtuple("_KcSession", "endpoint realm token email name")


def _kc_session(args):
    endpoint, realm, admin_user, admin_pwd = _kc_env()
    email, name = core._lab_user_email(args)
    token = _kc_token(endpoint, admin_user, admin_pwd)
    return _KcSession(endpoint, realm, token, email, name)


def _kc_call(method, url, token, body=None):
    headers = {"Authorization": f"Bearer {token}"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        core.die(3, f"Keycloak transport failure to {url}: {type(e).__name__}: {e}")


def _kc(method, url, token, payload=None, ok=(200,), what="Keycloak"):
    """_kc_call whose status outside `ok` is an environment failure named by `what`; returns the body."""
    status, body = _kc_call(method, url, token, payload)
    if status not in ok:
        core.die(3, f"{what} HTTP {status}: {body[:300]!r}")
    return body


def _kc_user_id(endpoint, realm, token, email):
    # `search` is a substring match: a prefix collision (bob@x vs bob@x.io) would return the wrong
    # learner and a group-join/DELETE would hit them, so filter to an exact username/email and refuse
    # to guess on >1.
    q = urllib.parse.urlencode({"exact": "true", "briefRepresentation": "true", "search": email})
    body = _kc("GET", f"{endpoint}/admin/realms/{realm}/users?{q}", token, what="Keycloak user lookup")
    exact = [u for u in json.loads(body or b"[]") if email in (u.get("username"), u.get("email"))]
    if len(exact) > 1:
        core.die(3, f"{len(exact)} Keycloak users exactly match {email}; refusing to guess")
    return exact[0]["id"] if exact else None


def _kc_group_id(endpoint, realm, token, group):
    q = urllib.parse.urlencode({"search": group, "exact": "true"})
    body = _kc("GET", f"{endpoint}/admin/realms/{realm}/groups?{q}", token, what="Keycloak group lookup")
    groups = json.loads(body or b"[]")
    if not groups:
        core.die(3, f"Keycloak group '{group}' not found in realm '{realm}'")
    return groups[0]["id"]


def _gen_password(length=14):
    # upper+lower+digit guaranteed so it clears a typical Keycloak policy.
    pool = string.ascii_uppercase + string.ascii_lowercase + string.digits
    chars = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
    ] + [secrets.choice(pool) for _ in range(length - 3)]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


# Per-tenant SSO constants that CANNOT be derived: the suffix Wiz appends to the tenant id in its
# Cognito domain, and the SSO app client id. The tenant id itself IS derived (JWT `tid`), so it is
# not stored here — one source, no drift.
_TENANT_SSO = {
    "TBCMP": {"cognito_suffix": "34dq", "client_id": "4lgopniht2g4j58sirh4kh5gtl"},
    "TE": {"cognito_suffix": "o8cy", "client_id": "54snbgo7lek43ct9ph3coc5484"},
}


_COGNITO_REGION = "us-east-1"  # Wiz's own auth infrastructure, not the lab's cloud region.


def _wiz_login_url():
    """The tenant's IdP-initiated Cognito authorize URL, or None if it cannot be built.

    A `lab_*` Keycloak user CANNOT sign in at app.wiz.io — that renders a real login page the account
    fails against, and the learner blames their creds. Only this IdP-init URL routes them straight
    to the lab's Keycloak. Full-URL env overrides win so a track can pin it.
    """
    override = core._tenant_env("LOGIN_URL")
    if override:
        return override
    sso = _TENANT_SSO.get(core._tenant())
    if not sso:
        return None
    _tok, _dc, tid = core.token_and_dc()
    if not tid:
        return None
    callback = urllib.parse.quote(f"https://auth.app.wiz.io/api/oidc/idp-init-callback/{tid}", safe="")
    return (
        f"https://{tid}-{sso['cognito_suffix']}.auth.{_COGNITO_REGION}.amazoncognito.com"
        f"/oauth2/authorize?response_type=code&identity_provider=Keycloak"
        f"&client_id={sso['client_id']}&redirect_uri={callback}"
    )


def _publish_user(email, pwd):
    # Instruqt 2.0 replaces 1.0 agent vars: an `exec` resource writes KEY=value to $EXEC_OUTPUT,
    # read as resource.exec.<name>.output.<KEY> and rendered into a `note` via Handlebars. When not
    # under exec (grader manual run / acceptance), print so a human can use the creds. The login URL is
    # NOT emitted here: it is a per-tenant constant, not per-learner, so labs pin it as a note literal
    # (regenerate with `wizlab user login-url`); emitting a new key here would deadlock note decode.
    out = os.getenv("EXEC_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"WIZ_USER={email}\nWIZ_PWD={pwd}\n")
        print("published WIZ_USER/WIZ_PWD to $EXEC_OUTPUT")
    else:
        print(f"WIZ_USER={email}")
        print(f"WIZ_PWD={pwd}")


def cmd_user_ensure(args):
    """Create (or password-reset) the per-lease Keycloak user backing Wiz SSO, join it to the RBAC
    group, and publish WIZ_USER/WIZ_PWD. Reset on an existing user: Keycloak never returns the old
    password and the learner needs a working value."""
    endpoint, realm, token, email, name = _kc_session(args)
    group = args.group
    pwd = _gen_password()
    uid = _kc_user_id(endpoint, realm, token, email)
    if uid is None:
        _kc("POST", f"{endpoint}/admin/realms/{realm}/users", token, {
            "email": email,
            "username": email,
            "firstName": name,
            "lastName": "Student",
            "emailVerified": True,
            "enabled": True,
            "requiredActions": [],
            "credentials": [{"type": "password", "value": pwd, "temporary": False}],
        }, ok=(201,), what="create user")
        uid = _kc_user_id(endpoint, realm, token, email)
        if uid is None:
            core.die(3, f"created {email} but Keycloak does not return it on re-lookup")
        action = "created"
    else:
        _kc("PUT", f"{endpoint}/admin/realms/{realm}/users/{uid}/reset-password", token,
            {"type": "password", "value": pwd, "temporary": False}, ok=(200, 204), what="reset-password")
        action = "reset"
    gid = _kc_group_id(endpoint, realm, token, group)
    _kc("PUT", f"{endpoint}/admin/realms/{realm}/users/{uid}/groups/{gid}", token, ok=(200, 204), what="group-join")
    _publish_user(email, pwd)
    print(f"user {action}: {email} (group {group})")


def cmd_user_inspect(args):
    endpoint, realm, token, email, _ = _kc_session(args)
    group = args.group
    uid = _kc_user_id(endpoint, realm, token, email)
    if uid is None:
        print(f"no Keycloak user {email}")
        sys.exit(1)
    body = _kc("GET", f"{endpoint}/admin/realms/{realm}/users/{uid}/groups", token, what=f"Keycloak groups of {email}")
    groups = [g.get("name") for g in json.loads(body or b"[]")]
    if group not in groups:
        print(f"user {email} exists but is not in group {group}; groups={groups}")
        sys.exit(1)
    print(f"user {email} exists and is in group {group}")


def cmd_user_delete(args):
    endpoint, realm, token, email, _ = _kc_session(args)
    uid = _kc_user_id(endpoint, realm, token, email)
    if uid is None:
        print(f"no Keycloak user {email}; nothing to delete")
        return
    status, body = _kc_call("DELETE", f"{endpoint}/admin/realms/{realm}/users/{uid}", token)
    if status not in (204, 404):
        core.die(3, f"delete user HTTP {status}: {body[:300]!r}")
    print(f"deleted Keycloak user {email}")


def cmd_user_login_url(args):
    """Print the tenant's Wiz SSO login URL. Author tool: a lab pins this URL as a note literal (it is
    a per-tenant constant, identical for every learner), and regenerates it here when the tenant
    changes — the single source that keeps the literal from silently drifting."""
    url = _wiz_login_url()
    if not url:
        core.die(3, "cannot build login URL: no SSO entry for tenant or no tid in token")
    print(url)
