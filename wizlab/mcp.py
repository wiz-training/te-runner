"""The Wiz MCP integration: the credential an agent in a lab reaches the remote MCP server with."""
import sys

from . import core

# --- A WIZ_MCP integration mints its own read-only service account, and `clientSecret` is readable
# only in the create payload -- so `ensure` is delete-then-mint, like `serviceaccount`. The type takes
# no `params`. Lifecycle and scope rules: SPEC.md. ---
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


# The WIZ_MCP type default is 11 read scopes that omit read:vulnerabilities and
# read:ai_security_findings, and remote-MCP tool visibility is filtered by the account's permissions:
# an agent asked to read a CVE with the default list sees no tool at all rather than an error.
_DEFAULT_SCOPES = "read:all"


def _mcp_name(args):
    return core._named(args, "-mcp")


def _find_integration(name):
    """The WIZ_MCP integration named exactly `name`. `IntegrationFilters.search` is a substring match
    and carries no exact-name field, so == is the caller's."""
    nodes = core._all_nodes(INTEGRATIONS_Q, {"f": {"type": [MCP_TYPE], "search": name}}, "integrations")
    return core._exact(nodes, name)


def _delete_integration(iid):
    core.api(core._delete_doc("deleteIntegration"), {"id": iid})


def cmd_mcp_ensure(args):
    """Mint this session's Wiz MCP integration and emit its service account as WIZ_CLIENT_ID /
    WIZ_CLIENT_SECRET to stdout and $EXEC_OUTPUT. The secret is returned once, so this converges to
    ONE fresh integration: any existing one on this name is deleted first. Setup only — never a
    learner check (it prints a secret)."""
    name, scopes = _mcp_name(args), core._scopes(args, _DEFAULT_SCOPES)
    existing = _find_integration(name)
    if existing:
        _delete_integration(existing["id"])
    inp = {"name": name, "type": MCP_TYPE, "serviceAccountScopes": scopes,
           # Any list other than the type default is refused unless this says so.
           "overrideScopes": True, "isAccessibleToAllProjects": True}
    if args.expires_at:
        inp["serviceAccountExpiresAt"] = args.expires_at
    data, _ = core.api(CREATE_INTEGRATION, {"input": inp})
    integ = (data.get("createIntegration") or {}).get("integration") or {}
    sa = integ.get("serviceAccount") or {}
    cid, sec = sa.get("clientId"), sa.get("clientSecret")
    if not cid or not sec:
        core.die(3, "createIntegration returned no service-account credentials")
    core._emit(f"WIZ_CLIENT_ID={cid}\nWIZ_CLIENT_SECRET={sec}\n")
    print(f"created wiz mcp integration {name} ({integ.get('id')}) scopes {','.join(scopes)}", file=sys.stderr)


def cmd_mcp_inspect(args):
    """Assert this session's Wiz MCP integration. `--require exists` is what a setup check asserts:
    createIntegration returns INITIALIZING and ACTIVE follows first use, so a freshly built lab fails
    `active` while being healthy."""
    name = _mcp_name(args)
    node = _find_integration(name)
    if not node:
        print(f"no wiz mcp integration named {name}")
        sys.exit(1)
    if args.require == "active" and node.get("status") != MCP_ACTIVE:
        print(f"wiz mcp integration {name} is {node.get('status')}, not {MCP_ACTIVE}")
        sys.exit(1)
    print(f"wiz mcp integration {node['name']} ({node['id']}) {node.get('status')}")


def cmd_mcp_delete(args):
    """Delete this session's Wiz MCP integration, by --id or (default) the session-stem name. Deleting
    the integration is also the only path to the service account it minted — see
    _reap_service_account."""
    iid = args.id
    if not iid:
        name = _mcp_name(args)
        node = _find_integration(name)
        if not node:
            print(f"no wiz mcp integration named {name}; nothing to delete")
            return
        iid = node["id"]
    _delete_integration(iid)
    print(f"deleted wiz mcp integration {iid}")
