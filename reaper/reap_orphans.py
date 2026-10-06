#!/usr/bin/env python3
"""Out-of-band reaper. Per tenant: ask Instruqt labPlayReports for STOPPED lab sessions (tagged
tid:<tenant>), then reap each session's Wiz footprint + Okta user via wizlab, keyed on the
session id (objects are named lab-<session_id>). DRY-RUN by default; --commit deletes.

Stopped is Instruqt's own done-signal (immune to long/paused labs). The session id is the join:
it's the labPlayReports id AND the naming stem. No account, no Okta attributes, no age heuristic.

Env: INSTRUQT_TOKEN (API key); REAP_TENANTS, the comma-separated tenant keys to sweep (default TBCMP);
for --commit also WIZ_<TENANT>_CLIENT_ID/SECRET per listed tenant + OKTA_WF_INVOKE_URL/CLIENT_TOKEN.
REAP_DOMAIN is the Wiz audit-log performer domain when labs do not use wizlab's default: unset, a
performer on another domain is "absent" and the Wiz footprint reap reads as done.
"""
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta

TEAM = os.getenv("INSTRUQT_TEAM", "wiz")
# Rolling window, so a session is seen by several consecutive runs. That is safe only because an
# already-reaped resource is a no-op, not a failure. It must span enough daily runs to finish a
# multi-pass teardown: an Outpost uninstalls before it can be deleted, so a pass that finds it
# `uninstalling` defers the delete and only a LATER pass completes it. A session that ages out
# mid-teardown leaves its footprint orphaned, and connectors accumulating past what a lookup can page
# break every lab's staging (wizlab find_connector).
WINDOW_H = int(os.getenv("REAP_WINDOW_HOURS", "48"))


def _tenants(spec):
    """tenant key (the WIZ_TENANT value wizlab keys creds on) -> the lab's Instruqt tag, tid:<key lower-cased>.
    Env-driven so a tenant onboards with a workflow line and its secret pair, not a rebuild."""
    keys = [k.strip().upper() for k in spec.split(",") if k.strip()]
    return {k: f"tid:{k.lower()}" for k in keys}


TENANTS = _tenants(os.getenv("REAP_TENANTS", "TBCMP"))
PAGE_SIZE = 500
MAX_PAGES = 20  # a server that ignores `skip` would otherwise page forever inside the cron container


def _die(msg):
    print(f"reap_orphans: {msg}", file=sys.stderr)
    sys.exit(1)


def _instruqt(query, variables):
    tok = os.getenv("INSTRUQT_TOKEN") or _die("INSTRUQT_TOKEN not set")
    req = urllib.request.Request(
        "https://play.instruqt.com/graphql",
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            res = json.loads(r.read())
    except urllib.error.HTTPError as e:
        _die(f"instruqt HTTP {e.code}: {e.read().decode(errors='replace')[:300]}")
    except OSError as e:
        _die(f"instruqt unreachable: {type(e).__name__}: {e}")
    if res.get("errors"):
        _die(f"instruqt: {res['errors']}")
    return res["data"]


def stopped_sessions(tag):
    now = datetime.now(UTC)
    frm = (now - timedelta(hours=WINDOW_H)).strftime("%Y-%m-%dT%H:%M:%SZ")
    q = ("query($team:String!, $tag:String!, $from:Time!, $to:Time!, $skip:Int!, $take:Int!) {"
         " labPlayReports(input:{teamSlug:$team, tags:[$tag],"
         " dateRangeFilter:{from:$from, to:$to}, pagination:{skip:$skip, take:$take}})"
         " { items { id stoppedReason } } }")
    variables = {"team": TEAM, "tag": tag, "from": frm, "to": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "skip": 0, "take": PAGE_SIZE}
    items = []
    for _ in range(MAX_PAGES):
        page = _instruqt(q, variables)["labPlayReports"]["items"]
        items.extend(page)
        if len(page) < PAGE_SIZE:
            break
        variables = {**variables, "skip": variables["skip"] + PAGE_SIZE}
    else:
        _die(f"{tag}: still paging after {MAX_PAGES * PAGE_SIZE} reports; refusing to loop")
    return [it["id"] for it in items if it.get("stoppedReason")]


# One session's `user reap` pages the audit log and nine sweep types; a hang here must cost that
# session, not the rest of the run. 3 is wizlab's own environment code, which _reap_session reads as FAILED.
WIZLAB_TIMEOUT_S = 300


def _wizlab(tenant, *args):
    try:
        r = subprocess.run(["wizlab", *args], env={**os.environ, "WIZ_TENANT": tenant},
                           capture_output=True, text=True, check=False, timeout=WIZLAB_TIMEOUT_S)
    except subprocess.TimeoutExpired as e:
        sys.stdout.write(e.stdout or "")
        sys.stderr.write(e.stderr or "")
        print(f"reap_orphans: wizlab {' '.join(args)} produced no result within {WIZLAB_TIMEOUT_S}s", file=sys.stderr)
        return 3
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    return r.returncode


# What a session's reap leaves behind. DEFERRED is not a failure: a teardown still in flight is the
# documented Outpost lifecycle, and the next pass inside WINDOW_H finishes it, so a run reporting only
# deferrals is green. FAILED is cleanup no later pass can finish on its own.
DONE, DEFERRED, FAILED = "done", "deferred", "failed"


def _domain_args():
    d = os.getenv("REAP_DOMAIN")
    return ["--domain", d] if d else []


def _reap_session(tenant, sid, commit):
    if not commit:
        print(f"DRY-RUN {tenant}: reap lab-{sid}* + delete lab-{sid}@")
        return DONE
    # Footprint first, user last: the user is the only handle back to the leftover objects, so keep it
    # when the footprint survives. Only a run inside WINDOW_H retries on its own — past that the sid
    # has aged out of labPlayReports and an operator must pass it via REAP_SESSIONS.
    rc = _wizlab(tenant, "user", "reap", "--session", sid, "--commit", *_domain_args())
    if rc != 0:
        # wizlab routes it: 4 finishes on its own, 3 does not (wizlab.reap._reap_exit). Either way the
        # handle stays until a pass proves the footprint gone.
        print(f"reap_orphans: retaining lab-{sid}@ because Wiz cleanup is incomplete", file=sys.stderr)
        return DEFERRED if rc == 4 else FAILED
    return DONE if _wizlab(tenant, "user", "delete", "--session", sid) == 0 else FAILED


def _tally(outcome, tenant, sid, failed, deferred):
    if outcome == FAILED:
        failed.setdefault(tenant, []).append(sid)
    elif outcome == DEFERRED:
        deferred.append(sid)


def _retry_hints(failed):
    """One line per tenant: REAP_SESSIONS reaps under REAP_SESSIONS_TENANT, so a hint naming only the
    sids retries a TE session under TBCMP, finds nothing, deletes the Okta user and loses the handle."""
    return "; ".join(f'REAP_SESSIONS_TENANT={t} REAP_SESSIONS="{",".join(sids)}"' for t, sids in failed.items())


def main():
    commit = "--commit" in sys.argv
    # The container's stdout is a pipe, so block-buffered; stderr is not. Unbuffered, the per-session
    # wizlab lines land after the summary that says "see preceding wizlab output".
    sys.stdout.reconfigure(line_buffering=True)
    total, failed, deferred = 0, {}, []
    # Manual override: reap explicit sids regardless of tag/window. For orphans that predate a track's
    # tid:<tenant> tag (labPlayReports captures tags at play time, so a late tag never back-fills), or
    # any one-off. `REAP_SESSIONS="sid1,sid2"`; reaped under REAP_SESSIONS_TENANT (default: the first
    # REAP_TENANTS key).
    manual = [s.strip() for s in os.getenv("REAP_SESSIONS", "").split(",") if s.strip()]
    if manual:
        mtenant = os.getenv("REAP_SESSIONS_TENANT") or next(iter(TENANTS), "TBCMP")
        print(f"# manual: {len(manual)} session(s) under {mtenant}")
        for sid in manual:
            total += 1
            _tally(_reap_session(mtenant, sid, commit), mtenant, sid, failed, deferred)
    for tenant, tag in TENANTS.items():
        sids = stopped_sessions(tag)
        print(f"# tenant {tenant} ({tag}): {len(sids)} stopped session(s) in last {WINDOW_H}h")
        for sid in sids:
            total += 1
            _tally(_reap_session(tenant, sid, commit), tenant, sid, failed, deferred)
    nfailed = sum(len(v) for v in failed.values())
    completed = total - nfailed - len(deferred)
    print(f"# {completed}/{total} session(s) {'reaped' if commit else 'ready to reap (dry-run)'}"
          f"{f', {len(deferred)} deferred to the next pass' if deferred else ''}", file=sys.stderr)
    if failed:
        # wizlab exit 3 is residue no pass can clear (wizlab.reap._reap_exit), so "retry" alone is wrong:
        # the same pass fails nightly until the sid ages out, then the retained user is the only way back.
        _die(f"{nfailed} session(s) left residue no pass can clear; see the wizlab lines above, remove it by "
             f"hand, then finish with {_retry_hints(failed)}")


if __name__ == "__main__":
    main()
