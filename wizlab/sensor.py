"""The Runtime Sensor's service account, the sensor itself, and its detections."""
import sys

from . import core

# --- Runtime Sensor (connectorless). A type:SENSOR service account is the sensor's credential; the
# installed sensor registers under the HOST name (a lab sets the host name to the session stem, so the
# stem scopes both planes). Neither SensorFilters nor DetectionFilters carries a cloud-account/instance
# field, so `search` by name is the only session key; detections then scope by the resolved sensorId. ---
SA_FIND = """query FindSA($n: String!, $after: String) {
  serviceAccounts(first: 50, after: $after, filterBy: { name: $n, deleted: false }) {
    nodes { id name } pageInfo { hasNextPage endCursor }
  }
}"""


CREATE_SA = """mutation CreateSA($input: CreateServiceAccountInput!) {
  createServiceAccount(input: $input) { serviceAccount { id name clientId clientSecret } }
}"""


SENSORS_Q = """query Sensors($s: String!, $after: String) {
  sensors(first: 50, after: $after, filterBy: { search: $s }) {
    nodes { id name status type } totalCount pageInfo { hasNextPage endCursor }
  }
}"""


# Default DetectionType is GENERATED_THREAT: the MATCH_ONLY sibling is invisible without the explicit
# filter, which IS the track's Threats-vs-Detections lesson. sensorId scopes to one lab's sensor.
DETECTIONS_Q = """query Dets($f: DetectionFilters) {
  detections(first: 1, filterBy: $f) { totalCount }
}"""


def _sensor_name(args):
    # The installed sensor's name == the host name, which a lab pins to the session stem.
    return core._named(args)


def _sa_name(args):
    return core._named(args, "-sensor")


def _resolve_sensor(name):
    """The sensor whose name exactly matches, ACTIVE-first (search is substring, so filter to ==)."""
    nodes = core._all_nodes(SENSORS_Q, {"s": name}, "sensors")
    return core._prefer([n for n in nodes if n.get("name") == name], "ACTIVE")


def _find_sa(name):
    """The service account whose name matches exactly (the filter is substring, so filter to ==)."""
    return core._exact(core._all_nodes(SA_FIND, {"n": name}, "serviceAccounts"), name)


def _ensure_sa(name, sa_type, scopes, id_var, secret_var, label, extra=None):
    """Converge to ONE fresh service account named on the session stem, parameterized over type +
    scopes (SENSOR takes none; a THIRD_PARTY account takes read:all etc.) plus any further
    CreateServiceAccountInput fields in `extra`. The client secret is shown once and not re-fetchable,
    so an existing account is deleted and re-minted: every run emits <id_var>/<secret_var> to stdout
    and $EXEC_OUTPUT (postconditions: SPEC.md §What `ensure` promises). Solve/setup only — never a
    learner check (it prints a secret)."""
    existing = _find_sa(name)
    if existing:
        core.api(core._delete_doc("deleteServiceAccount"), {"id": existing["id"]})
        print(f"deleted {label} service account {name} ({existing['id']}); re-minting", file=sys.stderr)
    inp = {"name": name, "type": sa_type, **(extra or {})}
    if scopes:
        inp["scopes"] = scopes
    data, _ = core.api(CREATE_SA, {"input": inp})
    sa = (data.get("createServiceAccount") or {}).get("serviceAccount") or {}
    cid, sec = sa.get("clientId"), sa.get("clientSecret")
    if not cid or not sec:
        core.die(3, "createServiceAccount returned no client credentials")
    core._emit(f"{id_var}={cid}\n{secret_var}={sec}\n")
    print(f"created {label} service account {name} ({sa['id']})", file=sys.stderr)


def _delete_sa(args, name):
    """Delete a service account by --id or (default) the given session-stem name. The reaper's prefix
    sweep already covers ServiceAccount, so this is for a solve/explicit teardown."""
    sid = args.id
    if not sid:
        node = _find_sa(name)
        if not node:
            print(f"no service account named {name}; nothing to delete")
            return
        sid = node["id"]
    core.api(core._delete_doc("deleteServiceAccount"), {"id": sid})
    print(f"deleted service account {sid}")


def cmd_sensor_ensure(args):
    """Mint the type:SENSOR service account the Runtime Sensor authenticates with, emitting
    WIZ_API_CLIENT_ID / WIZ_API_CLIENT_SECRET (the install one-liner's own variable names). See
    _ensure_sa for the shared idempotency + emit contract."""
    return _ensure_sa(_sa_name(args), "SENSOR", None, "WIZ_API_CLIENT_ID", "WIZ_API_CLIENT_SECRET", "sensor")


def cmd_sensor_delete(args):
    """Delete the sensor service account (see _delete_sa)."""
    return _delete_sa(args, _sa_name(args))


def cmd_sensor_inspect(args):
    """Assert the session's sensor is known to Wiz (--require exists) or reports Active (--require
    active). SensorStatus is ACTIVE/INACTIVE only; a pre-Active sensor reads INACTIVE or is absent."""
    name = _sensor_name(args)
    require = args.require
    node = _resolve_sensor(name)
    if not node:
        print(f"no sensor named {name}")
        sys.exit(1)
    print(f"sensor {node['name']} ({node['id']}): status={node['status']} type={node['type']}")
    if require == "exists":
        return
    sys.exit(0 if node["status"] == "ACTIVE" else 1)


def cmd_detection_inspect(args):
    """Assert >=1 detection for --rule-name on the session's sensor. Default type GENERATED_THREAT;
    --match-only queries the MATCH_ONLY side (the rule that matched and raised no threat). Keyed on
    matchedRuleName + type + the resolved sensorId — never a rule id (tenant-specific) and never an
    Issue/Threat object (tenant-wide anti-burst cap)."""
    name = _sensor_name(args)
    rule = args.rule_name or core.die(2, "detection inspect needs --rule-name")
    dtype = "MATCH_ONLY" if args.match_only else "GENERATED_THREAT"
    minutes = args.since_minutes
    node = _resolve_sensor(name)
    if not node:
        print(f"no sensor named {name}; cannot scope detections")
        sys.exit(1)
    f = {
        "sensorId": {"equals": [node["id"]]},
        "matchedRuleName": {"equals": [rule]},
        "type": {"equals": [dtype]},
        "startedAt": {"inLast": {"amount": minutes, "unit": "DurationFilterValueUnitMinutes"}},
    }
    data, _ = core.api(DETECTIONS_Q, {"f": f})
    n = (data.get("detections") or {}).get("totalCount") or 0
    print(f"detection inspect: {n} {dtype} for rule {rule!r} on sensor {name} ({node['id']})")
    sys.exit(0 if n > 0 else 1)
