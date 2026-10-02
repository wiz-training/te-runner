"""A THIRD_PARTY service account: the plain API credential an agent or a script in a lab reads the tenant with."""
import sys

from . import core, sensor

# --- createServiceAccount(type: THIRD_PARTY) is the one service-account type a lab both mints and
# deletes directly: deleteServiceAccount accepts it, unlike a CLI deployment's or an integration's
# account, so the reaper's ServiceAccount prefix sweep is its whole backstop. clientSecret is readable
# only in the create payload, so `ensure` is delete-then-mint. Lifecycle and scope rules: SPEC.md. ---
SA_TYPE = "THIRD_PARTY"


# Tool visibility on the remote MCP server and every data query are filtered by the account's scopes;
# a lab that reads the tenant wants the one string that covers them all.
_DEFAULT_SCOPES = "read:all"


def _api_name(args):
    return core._named(args, "-api")


def cmd_apiaccount_ensure(args):
    """Mint this session's THIRD_PARTY service account and emit WIZ_CLIENT_ID / WIZ_CLIENT_SECRET to
    stdout and $EXEC_OUTPUT. The secret is returned once, so this converges to ONE fresh account: any
    existing one on this name is deleted first. Setup only — never a learner check (it prints a secret)."""
    name, scopes = _api_name(args), core._scopes(args, _DEFAULT_SCOPES)
    # assignedProjectIds [] is tenant-wide; expiresAt null leaves the reap as the only end of life.
    extra = {"assignedProjectIds": [], "expiresAt": args.expires_at}
    sensor._ensure_sa(name, SA_TYPE, scopes, "WIZ_CLIENT_ID", "WIZ_CLIENT_SECRET", "third-party", extra)
    print(f"scopes {','.join(scopes)}", file=sys.stderr)


def cmd_apiaccount_inspect(args):
    """Assert this session's THIRD_PARTY service account exists (--require exists)."""
    name = _api_name(args)
    node = sensor._find_sa(name)
    if not node:
        print(f"no service account named {name}")
        sys.exit(1)
    print(f"service account {node['name']} ({node['id']})")


def cmd_apiaccount_delete(args):
    """Delete this session's THIRD_PARTY service account, by --id or (default) the session-stem name."""
    return sensor._delete_sa(args, _api_name(args))
