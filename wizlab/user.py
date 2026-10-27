"""The per-lease Okta user behind Wiz SSO, and the tenant's login URL."""
import os
import secrets
import string
import sys
import urllib.parse

from . import core


# --- lab user (Okta via Workflows Router): backs Wiz SSO for a learner. ---
def _okta_env():
    def need(k):
        return os.getenv(k) or core.die(3, f"{k} not in environment (Okta Workflows secret; not wired?)")
    return need("OKTA_WF_INVOKE_URL"), need("OKTA_WF_CLIENT_TOKEN")


def _okta_call(invoke_url, token, body):
    """POST body to the Okta Workflows Router; returns the parsed response. One submission: the router
    creates the user before it answers, so a resend on a timeout mints a second login."""
    headers = {"x-api-client-token": token, "Content-Type": "application/json", "Accept": "application/json"}
    return core._post(invoke_url, body, headers, attempts=1)


def _okta_login(args):
    """Session stem as the Okta login: lab-<session_id>. The router appends the domain."""
    return core._stem(args)


def _gen_password():
    """20 chars; guaranteed upper/lower/digit/symbol for any Okta password policy."""
    pool = string.ascii_letters + string.digits
    return (
        secrets.choice(string.ascii_uppercase)
        + "".join(secrets.choice(pool) for _ in range(16))
        + secrets.choice(string.digits)
        + "!"
    )


# Per-tenant SSO constants that CANNOT be derived — the suffix Wiz appends to the tenant id in its
# Cognito domain, and the SSO app client id — come from WIZ_<T>_COGNITO_SUFFIX / WIZ_<T>_SSO_CLIENT_ID,
# never a literal: the image is public, and a tenant is an env pair, like its credential, not an image
# release. The tenant id itself IS derived (JWT `tid`).
_COGNITO_REGION = "us-east-1"  # Wiz's own auth infrastructure, not the lab's cloud region.


LAB_IDP_Q = """query LabIdp($search: String) {
  users(first: 20, filterBy: { search: $search }) { nodes { identityProviderV2 { name } } }
}"""
IDPS_Q = """query Idps { samlIdentityProviders(first: 20) { nodes { name relayState } } }"""
IDP_CONFIG_Q = """query IdpConfig($name: String!) { samlIdentityProviderConfig(name: $name) { idpLoginUrl ssoUrl } }"""


def _lab_idp(domain):
    """The SAML identity provider the lab domain signs in through: WIZ_<T>_SSO_IDP_NAME, else the
    provider of an existing lab-domain user, else the tenant's only provider. Several providers and no
    lab user yet is invocation error 2 naming them: a guess would route learners to a stranger's IdP."""
    name = core._tenant_env("SSO_IDP_NAME")
    if name:
        return name
    data, _ = core.api(LAB_IDP_Q, {"search": f"@{domain}"})
    for n in (data.get("users") or {}).get("nodes") or []:
        idp = (n.get("identityProviderV2") or {}).get("name")
        if idp:
            return idp
    data, _ = core.api(IDPS_Q, {})
    names = [n["name"] for n in (data.get("samlIdentityProviders") or {}).get("nodes") or [] if n.get("name")]
    if len(names) == 1:
        return names[0]
    if names:
        core.die(2, f"{len(names)} identity providers and no @{domain} user yet: "
                    f"set WIZ_<T>_SSO_IDP_NAME to one of {', '.join(names)}")
    return None


def _derived_login_url(domain):
    """The sign-in URL as the tenant itself states it, no secret: by default the Cognito authorize route,
    `https://<samlIdentityProviderConfig.ssoUrl host>/oauth2/authorize?<samlIdentityProviders.relayState>`;
    with WIZ_<T>_LOGIN_ROUTE=idp-login, the portal entrypoint `samlIdentityProviderConfig.idpLoginUrl`. The
    authorize route is the default because the portal entrypoint ends at Cognito's hosted login with 403
    for a lab user on a tenant whose app client has that UI off; a tenant whose app client refuses the
    SAML hand-back on the authorize route ("Login option is not available") sets the env. None when the
    tenant has no SAML provider."""
    idp = _lab_idp(domain)
    if not idp:
        return None
    data, _ = core.api(IDP_CONFIG_Q, {"name": idp})
    cfg = data.get("samlIdentityProviderConfig") or {}
    if core._tenant_env("LOGIN_ROUTE") == "idp-login":
        return cfg.get("idpLoginUrl") or None
    data, _ = core.api(IDPS_Q, {})
    relay = next((n.get("relayState") for n in (data.get("samlIdentityProviders") or {}).get("nodes") or []
                  if n.get("name") == idp), None)
    host = urllib.parse.urlsplit(cfg.get("ssoUrl") or "").netloc
    if host and relay:
        return f"https://{host}/oauth2/authorize?{relay}"
    return cfg.get("idpLoginUrl") or None


def _wiz_login_url(domain):
    """The tenant's SSO login URL, or None if it cannot be built: WIZ_<T>_LOGIN_URL verbatim, else the
    portal's idp-login entrypoint from WIZ_<T>_IDP_LOGIN_CLIENT_ID, else the same entrypoint read from
    the tenant (`_derived_login_url`, no secret), else the IdP-initiated Cognito authorize URL from
    WIZ_<T>_COGNITO_SUFFIX + WIZ_<T>_SSO_CLIENT_ID.

    A lab Okta user CANNOT sign in at the portal's front door — that renders a real login page the
    account fails against, and the learner blames their creds — nor at the IdP's own SAML login URL,
    which Cognito rejects ("Invalid samlResponse or relayState"). Only the idp-login entrypoint or the
    Cognito authorize URL routes them straight to the lab's Okta. Full-URL env overrides win so a track
    can pin it.
    """
    override = core._tenant_env("LOGIN_URL")
    if override:
        return override
    idp_name = core._tenant_env("SSO_IDP_NAME") or "Okta"
    # The portal's own SSO entrypoint. A tenant whose Cognito app client refuses the lab user's SAML
    # hand-back ("Login option is not available") still completes through it, so it wins when set.
    idp_login_client = core._tenant_env("IDP_LOGIN_CLIENT_ID")
    if idp_login_client:
        return f"https://{core._domain()}/idp-login?clientId={idp_login_client}&idp={idp_name}"
    derived = _derived_login_url(domain)
    if derived:
        return derived
    suffix = core._tenant_env("COGNITO_SUFFIX")
    client_id = core._tenant_env("SSO_CLIENT_ID")
    if not suffix or not client_id:
        return None
    _tok, _dc, tid = core.token_and_dc()
    if not tid:
        return None
    callback = urllib.parse.quote(f"https://{core._host('auth')}/api/oidc/idp-init-callback/{tid}", safe="")
    return (
        f"https://{tid}-{suffix}.auth.{_COGNITO_REGION}.amazoncognito.com"
        f"/oauth2/authorize?response_type=code&identity_provider={idp_name}"
        f"&client_id={client_id}&redirect_uri={callback}"
    )


def cmd_user_ensure(args):
    """Create the per-lease Okta user backing Wiz SSO and publish WIZ_USER/WIZ_PWD."""
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
    # secret: nothing parses this verb's stdout — the sign-in note reads $EXEC_OUTPUT — and the
    # password was readable in `instruqt lab logs` for the whole life of every play.
    core._emit(f"WIZ_USER={user}\nWIZ_PWD={pwd}\n", secret=True)
    print(f"user created: {user} (profile {profile})")


def cmd_user_inspect(args):
    """Two reads of one login, exit 0 present and 1 absent. Default: Okta, through the router — the login
    `user ensure` created, which Okta expires on its own schedule. `--wiz`: the tenant's own `users` record
    the first SSO sign-in minted and `user delete` removes; it outlives the Okta login, so only this read
    confirms the delete. `--wiz` needs the tenant pair alone, never OKTA_WF_*."""
    if args.wiz:
        return _inspect_wiz_user(args)
    core._unused(args, "Okta (pass --wiz)", "--domain")
    invoke_url, token = _okta_env()
    login = _okta_login(args)
    res = _okta_call(invoke_url, token, {"action": "inspect", "login": login})
    if not res.get("okta_user_id"):
        print(f"no Okta user for {login}")
        sys.exit(1)
    print(f"user {login} exists")


# Wiz's own record of the login, minted by the first SSO sign-in as `<idp>_<email>` and kept after the
# Okta login expires. `search` is a substring filter, so the exact email is the caller's. deleteUser is
# a soft delete: the record stays listed as status DELETED, and a second deleteUser on it is an opaque
# INTERNAL error, so a DELETED record is absent here.
USERS_Q = """query LabUsers($search: String, $after: String) {
  users(first: 100, after: $after, filterBy: { search: $search }) {
    nodes { id email status identityProviderTypeV2 }
    pageInfo { hasNextPage endCursor }
  }
}"""


def _wiz_users(email):
    return [n for n in core._all_nodes(USERS_Q, {"search": email}, "users")
            if (n.get("email") or "").lower() == email.lower() and n.get("status") != "DELETED"]


def _inspect_wiz_user(args):
    args.domain = args.domain or "wizlabs.cloudseclabs.io"
    email = core._lab_user_email(args)
    users = _wiz_users(email)
    if not users:
        print(f"no Wiz user for {email}")
        sys.exit(1)
    for u in users:
        print(f"wiz user {u['id']} ({u.get('identityProviderTypeV2')}) for {email}")


def cmd_user_delete(args):
    """Delete the Wiz user record(s) for the session's login, lab-<sid>@--domain. Okta expires the login
    itself (its Expire One / Expired Sweep flows), so this reaches Wiz, never the router: the record its
    first SSO sign-in minted outlives the login until something deletes it. Idempotent: absent is OK."""
    email = core._lab_user_email(args)
    users = _wiz_users(email)
    if not users:
        print(f"no Wiz user for {email}; nothing to delete")
        return
    for u in users:
        core.api(core._delete_doc("deleteUser"), {"id": u["id"]})
        print(f"deleted Wiz user {u['id']} ({u.get('identityProviderTypeV2')})")


def cmd_user_login_url(args):
    """Print the tenant's Wiz SSO login URL. Author tool: a lab pins this URL as a note literal (it is
    a per-tenant constant, identical for every learner), and regenerates it here when the tenant
    changes — the single source that keeps the literal from silently drifting."""
    url = _wiz_login_url(args.domain)
    if not url:
        core.die(3, "cannot build login URL: the tenant has no SAML identity provider and none of WIZ_<T>_LOGIN_URL, "
                    "WIZ_<T>_IDP_LOGIN_CLIENT_ID, or WIZ_<T>_COGNITO_SUFFIX + WIZ_<T>_SSO_CLIENT_ID is set")
    print(url)
