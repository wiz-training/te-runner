"""`user reap`: audit-attributed and prefix-swept teardown of a session's Wiz footprint."""
import json
import re
import sys
from collections import Counter

from . import core, outpost, serviceaccount

# reap: delete a lab user's Wiz footprint from the audit log. Delete is uniform delete<Type>(input:{id})
# across ~34 of 36 create types; correlation (find by name) is per-type — the generic handler
# (plural+search, hard verify) covers the norm, _REAP_OVERRIDES holds the exceptions.

# One outcome per resource. The exit code answers one question — must a human come back? — because the
# reaper keeps the session's Keycloak user, its only handle back to leftovers, on anything but 0. So
# FAILED and DEFERRED block and UNKNOWN does not: handler coverage is partial by construction (the
# generic handler misses most of the ~34 create types), so blocking a miss would fail every reap and
# retain every user the reaper exists to delete. Guaranteed types are _SWEEP_TYPES; outside that set
# the audit layer is opportunistic, and a miss is a record, not a verdict.
REMOVED, ABSENT, DEFERRED, UNKNOWN, FAILED = "removed", "absent", "deferred", "unknown", "failed"


_REAP_BLOCKING = (FAILED, DEFERRED)


_REAP_NEEDS_REVIEW = (FAILED, DEFERRED, UNKNOWN)


def _reap_outpost(tok, dc, _h, oid, name):
    """Outpost delete is a lifecycle, not a mutation: deleteOutpost fails on a live record and on one
    still UNINSTALLING, so the order is uninstall -> UNINSTALLED -> delete. The reaper never waits that
    out inside a cron container, so a record mid-uninstall is DEFERRED — blocking keeps the user, and
    the audit entry plus the rolling window bring the next daily pass back to it minutes later. A
    status already carrying UNINSTALL takes no second uninstall: that call is what refuses."""
    data, errs = core._gql(tok, dc, outpost.OUTPOST_Q, {"id": oid})
    if errs:
        return FAILED, f"status unreadable ({errs[0].get('message', '?')})"
    status = ((data.get("outpost") or {}).get("status")) or "GONE"
    if status == "GONE":
        return ABSENT, None
    if status in outpost._OUTPOST_DELETABLE:
        _d, derr = core._gql(tok, dc, outpost.DELETE_OUTPOST, {"input": {"id": oid}})
        return (FAILED, derr[0].get("message", "?")) if derr else (REMOVED, None)
    if not outpost._uninstall_in_flight(status):
        _d, derr = core._gql(tok, dc, outpost.UNINSTALL_OUTPOST, {"input": {"id": oid}})
        if derr:
            return FAILED, f"uninstall refused ({derr[0].get('message', '?')})"
    return DEFERRED, f"{status.lower()}; delete deferred to the next pass"


# A CLI deployment's service account carries the deployment's name plus this suffix, and that name is
# the only handle back to the owner: the API exposes no reverse lookup from a service account to its
# deployment.
_CLI_SA_SUFFIX = re.compile(r"^(?P<dep>.+)-deployment-[0-9a-fA-F-]{36}$")


def _reap_service_account(tok, dc, h, rid, name):
    """`deleteServiceAccount` rejects a CLI deployment's account, so the owning deployment is the only
    handle; on the uniform path it is a permanent FAILED that retains the session's user forever."""
    m = _CLI_SA_SUFFIX.match(name)
    if not m:
        return _reap_delete_uniform(tok, dc, h, rid, name)
    dep = m.group("dep")
    data, errs = core._gql(tok, dc, serviceaccount.CLI_DEPLOYMENTS_Q, {"f": {"type": ["WIZ_CLI"], "search": dep}})
    if errs:
        return FAILED, f"deployment lookup failed ({errs[0].get('message', '?')})"
    nodes = ((data.get("deployments") or {}).get("nodes") or [])
    owner = core._exact(nodes, dep)
    if not owner:
        # Nothing left that can delete this record, so blocking would retain the user forever with no
        # pass able to clear it. UNKNOWN reports the residue without holding the handle.
        return UNKNOWN, f"orphaned CLI service account; owning deployment {dep!r} is absent"
    _d, derr = core._gql(tok, dc, core._delete_doc("deleteCliDeployment", "id"), {"id": owner["id"]})
    if derr:
        return FAILED, derr[0].get("message", "?")
    if _reap_find(tok, dc, h, name)[1] == 0:
        return REMOVED, None
    return FAILED, "still present after deleting its CLI deployment"


_REAP_OVERRIDES = {
    "ServiceAccount": {"list": "serviceAccounts", "filter": "name", "soft": True,
                       "deleter": _reap_service_account},
    "Outpost": {"deleter": _reap_outpost},
}


# Types the prefix sweep checks by name. Verified delete<X>({id}) + search/name filter, or an override
# with its own deleter; the lease-unique lab-<account> stem is a safe prefix match. This list is the
# guarantee: a lookup or delete failure here is FAILED, not UNKNOWN. Extend as labs create new types.
_SWEEP_TYPES = ["Connector", "Project", "Report", "Control", "SavedGraphQuery",
                "AutomationWorkflow", "ServiceAccount", "Outpost"]


def _plural(x):
    lc = x[0].lower() + x[1:]
    if lc.endswith("y") and lc[-2].lower() not in "aeiou":
        return lc[:-1] + "ies"
    return lc + "s"


def _reap_handler(x):
    o = _REAP_OVERRIDES.get(x, {})
    return {"list": o.get("list", _plural(x)), "filter": o.get("filter", "search"),
            "delete": "delete" + x, "soft": o.get("soft", False), "deleter": o.get("deleter")}


def _input_name(d):
    if isinstance(d, dict):
        for k in ("name", "displayName", "title"):
            if isinstance(d.get(k), str):
                return d[k]
        for v in d.values():
            r = _input_name(v)
            if r:
                return r
    return None


def _audit_entries(send, minutes, mutations_only=True):
    """Every audit entry in the window, newest page first, via `send` (api or _gql). Returns (entries,
    alert). The filter is a literal: `minutes` is an int and MUTATION an enum, neither user text."""
    scope = "actionType: MUTATION, " if mutations_only else ""
    qy = ("query Audit($after: String) { auditLogEntries(first: 100, after: $after, filterBy: { " + scope
          + f"timestamp: {{ inLast: {{ amount: {int(minutes)}, unit: DurationFilterValueUnitMinutes }} }} }}) "
          "{ nodes { action actionType status timestamp performer { id name } actionParameters } "
          "pageInfo { hasNextPage endCursor } } }")
    entries, alert = core._paged(send, qy, {}, "auditLogEntries")
    return entries, (f"audit enumeration {alert}" if alert else None)


def _reap_enumerate(tok, dc, email, minutes):
    """(SUCCESS Create* actions by `email` as (action, input name), alert). A partial list with an alert
    is what the caller turns into FAILED."""
    entries, alert = _audit_entries(lambda q, v: core._gql(tok, dc, q, v), minutes)
    out = [(n["action"], _input_name((n.get("actionParameters") or {}).get("input") or {}))
           for n in entries
           if (n.get("performer") or {}).get("name") == email and n.get("status") == "SUCCESS"
           and n["action"].startswith("Create")]
    return out, alert


def _reap_list(tok, dc, h, name):
    """Every node of a sweep type whose list filter matches `name`, all pages. Returns (nodes, error).
    The filter value is a literal: each type declares its filter as String or [String], and a literal
    coerces into either where a typed variable would not."""
    extra = ", deleted: false" if h["soft"] else ""
    qy = ("query ReapList($after: String) { " + h["list"] + "(first: 100, after: $after, filterBy: { "
          + h["filter"] + ": " + json.dumps(name) + extra
          + " }) { nodes { id name } pageInfo { hasNextPage endCursor } } }")
    return core._paged(lambda q, v: core._gql(tok, dc, q, v), qy, {}, h["list"])


def _reap_find(tok, dc, h, name):
    # Returns (id_if_exactly_one, exact_count, error). Exact-name guard is load-bearing: list filters
    # are substring, so a shared tenant demands ==1 before delete — over the WHOLE list, never a page.
    nodes, err = _reap_list(tok, dc, h, name)
    if err:
        return None, None, err
    exact = [n for n in nodes if n.get("name") == name]
    return (exact[0]["id"] if len(exact) == 1 else None), len(exact), None


def _reap_delete_uniform(tok, dc, h, rid, name):
    # Verified by re-lookup because the mutation can report success without removing the record.
    _d, derr = core._gql(tok, dc, core._delete_doc(h["delete"]), {"id": rid})
    if _reap_find(tok, dc, h, name)[1] == 0:
        return REMOVED, None
    return FAILED, derr[0].get("message", "?") if derr else "still present after delete"


def _reap_delete(tok, dc, h, rid, name):
    """One resource, one outcome. A type whose delete is a lifecycle rather than a single mutation, or
    whose record is only reachable through an owner, supplies its own deleter and may fall back to the
    uniform path per resource; everything else is the uniform delete<X>(input:{id})."""
    if h["deleter"]:
        return h["deleter"](tok, dc, h, rid, name)
    return _reap_delete_uniform(tok, dc, h, rid, name)


def _reap_one(tok, dc, action, name, commit):
    """Reap one caught Create*. Returns (outcome, review_msg_or_None) — see _REAP_BLOCKING for which
    outcomes keep the session's user alive."""
    x = action[len("Create"):]
    if not name:
        return UNKNOWN, f"UNKNOWN {action}: no name in input — manual review"
    h = _reap_handler(x)
    rid, count, err = _reap_find(tok, dc, h, name)
    if err:
        return UNKNOWN, f"UNKNOWN {action} {name!r}: no handler ({err})"
    if count == 0:
        # Steady state, not a failure: a previous run reaped it (the reap window overlaps by design)
        # or the learner deleted it themselves. The audit entry survives the resource either way.
        print(f"absent {x} {name!r}")
        return ABSENT, None
    if count > 1:
        return FAILED, f"FAILED {action} {name!r}: matched {count}, need exactly 1 — skipping"
    if not commit:
        print(f"DRY-RUN would delete {x} {name!r} ({rid[:8]}{', soft' if h['soft'] else ''})")
        return REMOVED, None
    outcome, detail = _reap_delete(tok, dc, h, rid, name)
    print(f"{outcome} {x} {name!r}" + (f" ({detail})" if detail else ""))
    if outcome not in _REAP_NEEDS_REVIEW:
        return outcome, None
    return outcome, f"{outcome.upper()} {action} {name!r}" + (f": {detail}" if detail else "")


def _reap_sweep_type(tok, dc, x, stem, commit):
    """Prefix-sweep one type: delete resources whose name starts with the lease-unique stem. Returns a
    Counter of outcomes. A lookup error is FAILED, not UNKNOWN: every type swept here is a guaranteed
    type, so an error means this sweep did not complete."""
    h = _reap_handler(x)
    nodes, err = _reap_list(tok, dc, h, stem)
    if err:
        # Nothing from a partial list is deleted: the sweep either saw the whole set or did not run.
        print(f"FAILED sweep {x}: {err}")
        return Counter({FAILED: 1})
    tally = Counter()
    for n in nodes:
        nm = n.get("name") or ""
        if not nm.startswith(stem):
            continue
        if not commit:
            print(f"DRY-RUN would sweep {x} {nm!r} ({n['id'][:8]})")
            tally[REMOVED] += 1
            continue
        outcome, detail = _reap_delete(tok, dc, h, n["id"], nm)
        print(f"{outcome} sweep {x} {nm!r}" + (f" ({detail})" if detail else ""))
        tally[outcome] += 1
    return tally


def cmd_reap(args):
    """Reap one lab session's Wiz footprint. DRY-RUN by default; --commit deletes. Scoped to the
    session (--session/INSTRUQT_SESSION_ID): (1) audit-by-actor — SUCCESS Create* by the ephemeral
    user lab-<session_id>@, mapped to delete<Type>({id}) by exact name, catches free-form learner GUI
    creates; (2) prefix sweep — delete any known-type resource named lab-<session_id>*, catches solve /
    service-account creates the audit layer can't attribute. Pair with `user delete`. Exit 0 promises
    only this: every _SWEEP_TYPES resource named for the session, and every audit-attributed create
    with a handler and a name, is removed or absent. It does NOT promise the session created nothing
    else — that residue is counted as unknown. Exit 3 (failed or deferred) is the reaper's signal to
    keep the user and come back."""
    sid = core._session_id(args)
    email = args.email or core._lab_user_email(args)[0]
    stem = core._lab_stem(sid)
    minutes = args.last_min
    commit = args.commit
    tok, dc, _ = core.token_and_dc()
    tally = Counter()
    actions, enumeration_alert = _reap_enumerate(tok, dc, email, minutes)
    if enumeration_alert:
        # FAILED, not UNKNOWN: a truncated audit list hides creates we never even tried to delete.
        print(f"FAILED {enumeration_alert}")
        tally[FAILED] += 1
    # Removals the sweep could not have found by name. Every lab now instructs `lab-<sid>-*` names, so
    # this count over the reap.yml logs decides whether the audit layer stays a delete path or becomes
    # a report.
    audit_only = 0
    for action, name in actions:
        outcome, review = _reap_one(tok, dc, action, name, commit)
        if review:
            print(review)
        tally[outcome] += 1
        if outcome == REMOVED and not (name or "").startswith(stem):
            audit_only += 1
    for x in _SWEEP_TYPES:
        tally += _reap_sweep_type(tok, dc, x, stem, commit)
    verb = "removed" if commit else "to remove (dry-run)"
    counts = ", ".join(f"{tally[o]} {o}" for o in (ABSENT, DEFERRED, UNKNOWN, FAILED))
    print(f"# {tally[REMOVED]} {verb} ({audit_only} audit-only), {counts} for {email} / {stem}*",
          file=sys.stderr)
    sys.exit(3 if commit and any(tally[o] for o in _REAP_BLOCKING) else 0)
