"""The Wiz MCP integration: the credential an agent in a lab reaches the remote MCP server with."""
from . import core, credential

# --- A WIZ_MCP integration mints its own read-only service account; deleteIntegration cascades to it,
# and is the only path to it (reap._reap_integration_or_uniform). The type takes no `params`.
# Lifecycle and scope rules: SPEC.md. ---
MCP_TYPE = "WIZ_MCP"


MCP_ACTIVE = "ACTIVE"


INTEGRATIONS_Q = """query Integrations($f: IntegrationFilters, $after: String) {
  integrations(first: 50, after: $after, filterBy: $f) {
    nodes { id name status }
    pageInfo { hasNextPage endCursor }
  }
}"""


CREATE_INTEGRATION = """mutation CreateIntegration($input: CreateIntegrationInput!) {
  createIntegration(input: $input) {
    integration { id name status serviceAccount { clientId clientSecret scopes type } }
  }
}"""


def _mcp_input(name, args):
    # The WIZ_MCP type default is 11 read scopes that omit read:vulnerabilities and
    # read:ai_security_findings, and remote-MCP tool visibility is filtered by the account's permissions:
    # an agent asked to read a CVE with the default list sees no tool at all rather than an error. Any
    # list other than the type default is refused unless overrideScopes says so.
    inp = {"name": name, "type": MCP_TYPE, "serviceAccountScopes": core._scopes(args, credential._DEFAULT_SCOPES),
           "overrideScopes": True, "isAccessibleToAllProjects": True}
    if args.expires_at:
        inp["serviceAccountExpiresAt"] = args.expires_at
    return inp


def _mcp_creds(data):
    integ = (data.get("createIntegration") or {}).get("integration") or {}
    sa = integ.get("serviceAccount") or {}
    return integ.get("id"), sa.get("clientId"), sa.get("clientSecret")


# `--require exists` is what a setup check asserts: createIntegration returns INITIALIZING and ACTIVE
# follows first use, so a freshly built lab fails `active` while being healthy.
MCP_INTEGRATION = credential.Credential(
    label="wiz mcp integration", suffix="-mcp",
    find_doc=INTEGRATIONS_Q, find_field="integrations",
    find_vars=lambda name: {"f": {"type": [MCP_TYPE], "search": name}},
    create_doc=CREATE_INTEGRATION, create_input=_mcp_input,
    creds=_mcp_creds, delete_mutation="deleteIntegration",
    emit_keys=("WIZ_CLIENT_ID", "WIZ_CLIENT_SECRET"), sweep_type="Integration",
    status_field="status",
    require={**credential.EXISTS, "active": lambda node: node.get("status") == MCP_ACTIVE})


def cmd_mcp_ensure(args):
    return credential.ensure(MCP_INTEGRATION, args)


def cmd_mcp_inspect(args):
    return credential.inspect(MCP_INTEGRATION, args)


def cmd_mcp_delete(args):
    return credential.delete(MCP_INTEGRATION, args)
