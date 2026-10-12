"""A THIRD_PARTY service account: the plain API credential an agent or a script in a lab reads the tenant with."""
from . import core, credential, sensor

# --- createServiceAccount(type: THIRD_PARTY) is the one service-account type a lab both mints and
# deletes directly: deleteServiceAccount accepts it, unlike a CLI deployment's or an integration's
# account, so the reaper's ServiceAccount prefix sweep is its whole backstop. Lifecycle and scope
# rules: SPEC.md. ---
SA_TYPE = "THIRD_PARTY"


def _api_input(name, args):
    # assignedProjectIds [] is tenant-wide; expiresAt null leaves the reap as the only end of life.
    return {"name": name, "type": SA_TYPE, "scopes": core._scopes(args, credential._DEFAULT_SCOPES),
            "assignedProjectIds": [], "expiresAt": args.expires_at}


API_ACCOUNT = credential.Credential(
    label="third-party service account", suffix="-api",
    find_doc=sensor.SA_FIND, find_field="serviceAccounts", find_vars=lambda name: {"n": name},
    create_doc=sensor.CREATE_SA, create_input=_api_input,
    creds=sensor._sa_creds, delete_mutation="deleteServiceAccount",
    emit_keys=("WIZ_CLIENT_ID", "WIZ_CLIENT_SECRET"), sweep_type="ServiceAccount",
    require=credential.EXISTS)


def cmd_apiaccount_ensure(args):
    return credential.ensure(API_ACCOUNT, args)


def cmd_apiaccount_inspect(args):
    return credential.inspect(API_ACCOUNT, args)


def cmd_apiaccount_delete(args):
    return credential.delete(API_ACCOUNT, args)
