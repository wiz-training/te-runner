#!/usr/bin/env python3
"""Out-of-band reaper. Per tenant: ask Instruqt labPlayReports for STOPPED lab sessions (tagged
tid:<tenant>), then reap each session's Wiz footprint + Keycloak user via wizlab, keyed on the
session id (objects are named lab-<session_id>). DRY-RUN by default; --commit deletes.

Stopped is Instruqt's own done-signal (immune to long/paused labs). The session id is the join:
it's the labPlayReports id AND the naming stem. No account, no Keycloak attributes, no age heuristic.

Env: INSTRUQT_TOKEN (API key); for --commit also WIZ_<TENANT>_CLIENT_ID/SECRET + LAB_KEYCLOAK_*.
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
# `uninstalling` or `partially_uninstalled` defers the delete and only a LATER pass completes it. A
# session that ages out mid-teardown leaves its footprint orphaned, and connectors accumulating past
# what a lookup can page break every lab's staging (wizlab find_connector).
WINDOW_H = int(os.getenv("REAP_WINDOW_HOURS", "48"))
# tenant key (the WIZ_TENANT value wizlab keys creds on) -> the lab's Instruqt tag. Extend as tenants onboard.
TENANTS = {"TBCMP": "tid:tbcmp"}
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


def _wizlab(tenant, *args):
    r = subprocess.run(["wizlab", *args], env={**os.environ, "WIZ_TENANT": tenant},
                       capture_output=True, text=True, check=False, timeout=300)
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    return r.returncode


def _reap_session(tenant, sid, commit):
    if not commit:
        print(f"DRY-RUN {tenant}: reap lab-{sid}* + delete lab-{sid}@")
        return True
    # Footprint first, user last: the user is the only handle back to the leftover objects, so keep it
    # when the footprint survives. Only a run inside WINDOW_H retries on its own — past that the sid
    # has aged out of labPlayReports and an operator must pass it via REAP_SESSIONS.
    if _wizlab(tenant, "user", "reap", "--session", sid, "--commit") != 0:
        # Exit 3 means failed OR deferred (a multi-pass teardown mid-flight); the preceding wizlab
        # output names which. Either way the handle stays until a pass proves the footprint gone.
        print(f"reap_orphans: retaining lab-{sid}@ because Wiz cleanup is incomplete", file=sys.stderr)
        return False
    return _wizlab(tenant, "user", "delete", "--session", sid) == 0


def main():
    commit = "--commit" in sys.argv
    total, failed = 0, []
    # Manual override: reap explicit sids regardless of tag/window. For orphans that predate a track's
    # tid:<tenant> tag (labPlayReports captures tags at play time, so a late tag never back-fills), or
    # any one-off. `REAP_SESSIONS="sid1,sid2"`; reaped under REAP_SESSIONS_TENANT (default TBCMP).
    manual = [s.strip() for s in os.getenv("REAP_SESSIONS", "").split(",") if s.strip()]
    if manual:
        mtenant = os.getenv("REAP_SESSIONS_TENANT", "TBCMP")
        print(f"# manual: {len(manual)} session(s) under {mtenant}")
        for sid in manual:
            total += 1
            if not _reap_session(mtenant, sid, commit):
                failed.append(sid)
    for tenant, tag in TENANTS.items():
        sids = stopped_sessions(tag)
        print(f"# tenant {tenant} ({tag}): {len(sids)} stopped session(s) in last {WINDOW_H}h")
        for sid in sids:
            total += 1
            if not _reap_session(tenant, sid, commit):
                failed.append(sid)
    completed = total - len(failed)
    print(f"# {completed}/{total} session(s) {'reaped' if commit else 'ready to reap (dry-run)'}", file=sys.stderr)
    if failed:
        _die(f'{len(failed)} session(s) left cleanup incomplete; see preceding wizlab output, then retry with '
             f'REAP_SESSIONS="{",".join(failed)}"')


if __name__ == "__main__":
    main()
