"""The `wizcli` credential, minted through a CLI deployment."""
from . import credential

# --- Wiz CLI credential for `wizcli auth`, minted via a CLI DEPLOYMENT: createServiceAccount rejects
# type:CLI. Deleting the deployment is also the only path to the account it minted
# (reap._reap_service_account, which reads CLI_DEPLOYMENTS_Q). Lifecycle: SPEC.md. ---
CLI_DEPLOYMENTS_Q = """query CliDeployments($f: DeploymentFilters, $after: String) {
  deployments(first: 50, after: $after, filterBy: $f) { nodes { id name type } pageInfo { hasNextPage endCursor } }
}"""


CREATE_CLI_DEPLOYMENT = """mutation CreateCliDeployment($input: CreateCliDeploymentInput!) {
  createCliDeployment(input: $input) {
    clientSecret
    deployment { id name type object { ... on WizCLI { serviceAccount { name clientId } } } }
  }
}"""


def _cli_creds(data):
    # clientId rides on the deployment's service account, clientSecret beside it on the payload.
    payload = data.get("createCliDeployment") or {}
    dep = payload.get("deployment") or {}
    sa = (dep.get("object") or {}).get("serviceAccount") or {}
    return dep.get("id"), sa.get("clientId"), payload.get("clientSecret")


# WIZ_CLIENT_ID / _SECRET are wizcli's own env-var names. deleteCliDeployment selects `id`: the
# payload has no `_stub`.
CLI_DEPLOYMENT = credential.Credential(
    label="wiz cli deployment", suffix="-cli",
    find_doc=CLI_DEPLOYMENTS_Q, find_field="deployments",
    find_vars=lambda name: {"f": {"type": ["WIZ_CLI"], "search": name}},
    create_doc=CREATE_CLI_DEPLOYMENT,
    create_input=lambda name, _args: {"name": name, "projectIDs": [], "expiresAt": None},
    creds=_cli_creds, delete_mutation="deleteCliDeployment", delete_select="id",
    emit_keys=("WIZ_CLIENT_ID", "WIZ_CLIENT_SECRET"), sweep_type="ServiceAccount",
    require=credential.EXISTS)


def cmd_serviceaccount_ensure(args):
    return credential.ensure(CLI_DEPLOYMENT, args)


def cmd_serviceaccount_inspect(args):
    return credential.inspect(CLI_DEPLOYMENT, args)


def cmd_serviceaccount_delete(args):
    return credential.delete(CLI_DEPLOYMENT, args)
