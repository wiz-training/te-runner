"""The Runtime Sensor's service account, the sensor itself, and its detections."""
import sys

from . import core, credential

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


def _resolve_sensor(name):
    """The sensor whose name exactly matches, ACTIVE-first (search is substring, so filter to ==)."""
    nodes = core._all_nodes(SENSORS_Q, {"s": name}, "sensors")
    return core._prefer([n for n in nodes if n.get("name") == name], "ACTIVE")


def _sa_creds(data):
    sa = (data.get("createServiceAccount") or {}).get("serviceAccount") or {}
    return sa.get("id"), sa.get("clientId"), sa.get("clientSecret")


# WIZ_API_CLIENT_ID / _SECRET are the install one-liner's own variable names. Both `type:SENSOR` and the
# THIRD_PARTY account share these documents: deleteServiceAccount accepts what createServiceAccount
# minted, unlike a CLI deployment's or an integration's account (apiaccount, serviceaccount, mcp).
SENSOR_SA = credential.Credential(
    label="sensor service account", suffix="-sensor",
    find_doc=SA_FIND, find_field="serviceAccounts", find_vars=lambda name: {"n": name},
    create_doc=CREATE_SA, create_input=lambda name, _args: {"name": name, "type": "SENSOR"},
    creds=_sa_creds, delete_mutation="deleteServiceAccount",
    emit_keys=("WIZ_API_CLIENT_ID", "WIZ_API_CLIENT_SECRET"), sweep_type="ServiceAccount",
    require=credential.EXISTS)


def _find_sa(name):
    return credential.find(SENSOR_SA, name)


def cmd_sensor_ensure(args):
    return credential.ensure(SENSOR_SA, args)


def cmd_sensor_delete(args):
    return credential.delete(SENSOR_SA, args)


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
