"""The per-lease Okta user behind Wiz SSO, and the tenant's login URL."""
import json
import os
import secrets
import string
import sys
import urllib.error
import urllib.parse
import urllib.request

from . import core


# --- lab user (Okta via Workflows Router): backs Wiz SSO for a learner. ---
def _okta_env():
    vals = {}
    for k in ("OKTA_WF_INVOKE_URL", "OKTA_WF_CLIENT_TOKEN"):
        v = os.getenv(k)
        if not v:
            core.die(3, f"{k} not in environment (Okta Workflows secret; not wired?)")
        vals[k] = v
    return vals["OKTA_WF_INVOKE_URL"], vals["OKTA_WF_CLIENT_TOKEN"]


def _okta_call(invoke_url, token, body):
    """POST body to the Okta Workflows Router; returns the parsed response."""
    req = urllib.request.Request(
        invoke_url,
        data=json.dumps(body).encode(),
        headers={
            "x-api-client-token": token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        b = e.read().decode(errors="replace")[:500]
        core.die(3, f"Okta Workflows HTTP {e.code}: {b}")
    except Exception as e:
        core.die(3, f"Okta Workflows transport failure: {type(e).__name__}: {e}")


def _okta_login(args):
    """Session stem as the Okta login: lab-<session_id>. The router appends the domain."""
    return core._lab_stem(core._session_id(args))


def _gen_password():
    """20 chars; guaranteed upper/lower/digit/symbol for any Okta password policy."""
    pool = string.ascii_letters + string.digits
    return (
        secrets.choice(string.ascii_uppercase)
        + "".join(secrets.choice(pool) for _ in range(16))
        + secrets.choice(string.digits)
        + "!"
    )


# Per-tenant SSO constants that CANNOT be derived: the suffix Wiz appends to the tenant id in its
# Cognito domain, and the SSO app client id. The tenant id itself IS derived (JWT `tid`), so it is
# not stored here — one source, no drift. WIZ_<T>_COGNITO_SUFFIX / WIZ_<T>_SSO_CLIENT_ID override
# so a new tenant is an env pair, like its credential, not an image release.
_TENANT_SSO = {
    "TBCMP": {"cognito_suffix": "34dq", "client_id": "4lgopniht2g4j58sirh4kh5gtl", "idp_name": "Okta"},
    "TE": {"cognito_suffix": "o8cy", "client_id": "54snbgo7lek43ct9ph3coc5484", "idp_name": "Okta"},
}


_COGNITO_REGION = "us-east-1"  # Wiz's own auth infrastructure, not the lab's cloud region.


def _wiz_login_url():
    """The tenant's IdP-initiated Cognito authorize URL, or None if it cannot be built.

    A lab Okta user CANNOT sign in at app.wiz.io — that renders a real login page the account
    fails against, and the learner blames their creds. Only this IdP-init URL routes them straight
    to the lab's Okta. Full-URL env overrides win so a track can pin it.
    """
    override = core._tenant_env("LOGIN_URL")
    if override:
        return override
    sso = _TENANT_SSO.get(core._tenant(), {})
    suffix = core._tenant_env("COGNITO_SUFFIX") or sso.get("cognito_suffix")
    client_id = core._tenant_env("SSO_CLIENT_ID") or sso.get("client_id")
    idp_name = core._tenant_env("SSO_IDP_NAME") or sso.get("idp_name", "Okta")
    if not suffix or not client_id:
        return None
    _tok, _dc, tid = core.token_and_dc()
    if not tid:
        return None
    callback = urllib.parse.quote(f"https://auth.app.wiz.io/api/oidc/idp-init-callback/{tid}", safe="")
    return (
        f"https://{tid}-{suffix}.auth.{_COGNITO_REGION}.amazoncognito.com"
        f"/oauth2/authorize?response_type=code&identity_provider={idp_name}"
        f"&client_id={client_id}&redirect_uri={callback}"
    )


def cmd_user_ensure(args):
    """Create the per-lease Okta user backing Wiz SSO and publish WIZ_USER/WIZ_PWD/OKTA_USER_ID."""
    invoke_url, token = _okta_env()
    login = _okta_login(args)
    profile = args.profile or os.getenv("LAB_PROFILE")
    if not profile:
        core.die(2, "--profile or LAB_PROFILE required")
    pwd = _gen_password()
    participant = os.getenv("INSTRUQT_PARTICIPANT_ID", "manual")
    res = _okta_call(invoke_url, token, {
        "action": "create",
        "profile": profile,
        "login": login,
        "password": pwd,
        "participant_id": participant,
    })
    # The router appends the tenant's email domain; what it returns as `login` is what the learner
    # signs in with. A router that returns nothing leaves the stem, which is still the reap key.
    user = res.get("login") or login
    core._emit(f"WIZ_USER={user}\nWIZ_PWD={pwd}\n")
    print(f"user created: {user} (profile {profile})")


def cmd_user_inspect(args):
    """Check that the per-lease Okta user exists. Exit 0 if present, 1 if absent."""
    invoke_url, token = _okta_env()
    login = _okta_login(args)
    res = _okta_call(invoke_url, token, {"action": "inspect", "login": login})
    if not res.get("okta_user_id"):
        print(f"no Okta user for {login}")
        sys.exit(1)
    print(f"user {login} exists")


def cmd_user_delete(args):
    """Teardown the per-lease Okta user by login. Idempotent: absent is OK."""
    invoke_url, token = _okta_env()
    login = _okta_login(args)
    _okta_call(invoke_url, token, {"action": "teardown", "login": login})
    print(f"teardown sent for {login}")


def cmd_user_login_url(args):
    """Print the tenant's Wiz SSO login URL. Author tool: a lab pins this URL as a note literal (it is
    a per-tenant constant, identical for every learner), and regenerates it here when the tenant
    changes — the single source that keeps the literal from silently drifting."""
    url = _wiz_login_url()
    if not url:
        core.die(3, "cannot build login URL: no SSO entry for tenant or no tid in token")
    print(url)
