"""Author tool against the tenant: `wiz tenant`, the live connector facts a lab's provisioning eval()s."""
from . import core, session


def cmd_wiz_tenant(args):
    """Emit this tenant's connector and endpoint facts as KEY=value, live from the Wiz API — no
    hardcoded per-tenant values. A script `eval`s stdout to feed a terraform apply (the Wiz connector-role
    module needs remote-arn + external-id). One call, one auth; grow it by adding keys (readers take
    only what they know). To $EXEC_OUTPUT too, for a note/HCL ref."""
    params, tid = _managed_identity()
    aws, gcp, azure = params["aws"], params["gcp"], params["azure"]
    facts = {
        "WIZ_REMOTE_ARN": aws.get("roleArn") or "",     # the delegator Wiz assumes (per-tenant/dc)
        "WIZ_EXTERNAL_ID": tid or "",                    # tenant id == the role's sts:ExternalId
        "WIZ_AWS_ENV": aws.get("defaultEnvironment") or "",
        # GCP's whole Wiz-side identity: the SA the vendor TF module takes as
        # wiz_managed_identity_external_id. Its prod-<dc> segment is per-tenant AND per-data-center,
        # so a lab must feed this live value to terraform, never commit a literal.
        "WIZ_GCP_SERVICE_ACCOUNT": gcp.get("serviceAccountEmail") or "",
        # Azure's Application (client) id. NOT the service-principal OBJECT id, which lives in the
        # customer's own directory and is unknowable to Wiz — that one is an operator secret.
        "WIZ_AZURE_APP_ID": ((azure.get("commercial") or {}).get("appId")) or "",
    }
    # Fatal only when the tenant yields NOTHING: a GCP-only lab must not die because this tenant has
    # no AWS managed identity, and vice versa. Callers assert the one key they need
    # (`: "${WIZ_GCP_SERVICE_ACCOUNT:?...}"`), which is also what "readers take only what they know"
    # requires — emitting a key is this verb's job, needing it is the script's. The endpoint keys below
    # are outside the guard: they come from the token this call already minted, so they are as live as
    # the token and absent only if nothing authenticated at all.
    if not any(facts.values()):
        core.die(3, "managedIdentityParameters returned no usable tenant facts (no aws roleArn, no gcp SA, no tid)")
    _tok, dc, _tid = core.token_and_dc()
    facts.update({
        # The token's `dc` claim. A lab hands these to an agent, an SDK or an MCP client that
        # authenticates on its own and cannot be told the data center any other way: the MCP server
        # reads it as the `Wiz-DataCenter` header, and a wrong or missing one falls back to browser
        # OAuth silently rather than erroring.
        "WIZ_DATA_CENTER": dc,
        "WIZ_API_URL": core.api_url(dc),
        "WIZ_AUTH_URL": core.AUTH_URL,
        "WIZ_AUDIENCE": core.AUDIENCE,
    })
    core._emit("".join(f"{k}={v}\n" for k, v in facts.items() if v))


def _managed_identity():
    data, tid = core.api(session.IDENTITY, {})
    params = data.get("managedIdentityParameters") or {}
    return {"aws": params.get("aws") or {}, "gcp": params.get("gcp") or {}, "azure": params.get("azure") or {}}, tid
