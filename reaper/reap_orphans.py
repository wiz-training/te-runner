#!/usr/bin/env python3
"""Out-of-band reaper. Per tenant in REAP_TENANTS: discover session ids tenant-side with no age cutoff
(Wiz users whose email matches `lab-<12 chars>@` via `users(filterBy:{search:"lab-"})` and every
`_SWEEP_TYPES` resource named `lab-<12 chars>*`) and union with STOPPED Instruqt `labPlayReports`
sessions in the rolling 48h window (tagged `tid:<tenant>`) and `REAP_SESSIONS`. Then reap each
(tenant, sid) Wiz footprint and the Wiz user its SSO login minted, via wizlab, keyed on the session id
(objects are named `lab-<session_id>`). DRY-RUN by default; `--commit` deletes.

GameKit seat plays (`te-magic-labs` family) never get a `labPlayReports` row (`labPlayReportItem`
returns `BE_034_EntityNotFound`), and untagged labs never match `tags:["tid:<tenant>"]`; tenant-side
discovery finds their `lab-<sid>@` users and `lab-<sid>*` resources directly, while `labPlayReports`
remains the second source for sessions whose audit-only creates carry non-stem names.

Env: INSTRUQT_TOKEN (API key); REAP_TENANTS, the comma-separated tenant keys to sweep (default TBCMP);
WIZ_<TENANT>_CLIENT_ID/SECRET per listed tenant. REAP_DOMAIN is the login's email domain (the
audit-log performer and the Wiz user record) when labs do not use wizlab's default
(wizlabs.cloudseclabs.io): unset, a login on another domain is "absent", the footprint reap reads as
done and its Wiz user stays.
"""
import json
import os
import pathlib
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if (_REPO_ROOT / "wizlab").is_dir() and str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from wizlab import codelab as _codelab  # noqa: E402
from wizlab import core as _core  # noqa: E402
from wizlab import reap as _reap  # noqa: E402

# Same shape as wizlab.core.PLAY_ID_RE (measured 2026-10-27: 512/512 play ids in 30 d).
PLAY_ID_RE = re.compile(r"[a-z0-9]{12}")
_USER_STEM_RE = re.compile(r"^lab-([a-z0-9]{12})@")
_RESOURCE_STEM_RE = re.compile(r"^lab-([a-z0-9]{12})(?:$|[-@])")
_USER_HANDLER = {"list": "users", "filter": "search", "soft": False, "select": "id email status"}

TEAM = os.getenv("INSTRUQT_TEAM", "wiz")
# Rolling window, so a session is seen by several consecutive runs. That is safe only because an
# already-reaped resource is a no-op, not a failure. It must span enough daily runs to finish a
# multi-pass teardown: an Outpost uninstalls before it can be deleted, so a pass that finds it
# `uninstalling` defers the delete and only a LATER pass completes it. A session that ages out
# mid-teardown leaves its footprint orphaned, and connectors accumulating past what a lookup can page
# break every lab's staging (wizlab find_connector).
WINDOW_H = int(os.getenv("REAP_WINDOW_HOURS", "48"))


def _window_h():
    return int(os.getenv("REAP_WINDOW_HOURS", str(WINDOW_H)))


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


class InstruqtError(Exception):
    """One tenant's labPlayReports read failed; main() names the tenant and sweeps the next."""


def _instruqt(query, variables):
    tok = os.getenv("INSTRUQT_TOKEN") or _die("INSTRUQT_TOKEN not set")
    req = urllib.request.Request(
        "https://play.instruqt.com/graphql",
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raise InstruqtError(f"instruqt HTTP {e.code}: {e.read().decode(errors='replace')[:300]}") from e
    except OSError as e:
        raise InstruqtError(f"instruqt unreachable: {type(e).__name__}: {e}") from e
    # A 200 whose body is a sign-in page or a `{"errors": ...}` is the same failure class as an HTTP
    # error: a tenant's read is unusable, which is not grounds to abandon the other tenants' sweep.
    try:
        res = json.loads(raw)
        errors, data = res.get("errors"), res["data"]
    except (ValueError, KeyError, AttributeError) as e:
        raise InstruqtError(f"instruqt body is not a GraphQL answer: {raw.decode(errors='replace')[:300]!r}") from e
    if errors:
        raise InstruqtError(f"instruqt: {errors}")
    return data


def stopped_sessions(tag):
    now = datetime.now(UTC)
    frm = (now - timedelta(hours=_window_h())).strftime("%Y-%m-%dT%H:%M:%SZ")
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


def _stem_sid(pattern, text):
    m = pattern.match(text or "")
    sid = m.group(1) if m else ""
    return sid if PLAY_ID_RE.fullmatch(sid) else None


def tenant_sessions(tenant):
    """Session ids present on `tenant` with no age cutoff: active `lab-<12 chars>@` Wiz users and
    `_SWEEP_TYPES` resources named `lab-<12 chars>*`."""
    prev = os.environ.get("WIZ_TENANT")
    os.environ["WIZ_TENANT"] = tenant
    try:
        tok, dc, _ = _core.token_and_dc()
        users, err = _reap._reap_list(tok, dc, _USER_HANDLER, "lab-")
        if err:
            raise _core.WizlabError(3, f"users: {err}")
        sids = [
            sid for u in users if u.get("status") != "DELETED"
            if (sid := _stem_sid(_USER_STEM_RE, u.get("email")))
        ]
        for x in _reap._SWEEP_TYPES:
            nodes, err = _reap._reap_list(tok, dc, _reap._reap_handler(x), "lab-")
            if err:
                raise _core.WizlabError(3, f"{x}: {err}")
            for n in nodes:
                if sid := _stem_sid(_RESOURCE_STEM_RE, n.get("name")):
                    sids.append(sid)
        return list(dict.fromkeys(sids))
    finally:
        if prev is None:
            os.environ.pop("WIZ_TENANT", None)
        else:
            os.environ["WIZ_TENANT"] = prev


# One session's `user reap` pages the audit log and nine sweep types; a hang here must cost that
# session, not the rest of the run. 3 is wizlab's own environment code, which _reap_session reads as FAILED.
WIZLAB_TIMEOUT_S = 300


def _wizlab(tenant, *args):
    try:
        env = {**os.environ, "WIZ_TENANT": tenant, "WIZLAB_SKIP_SLOT_SWEEP": "1"}
        r = subprocess.run(["wizlab", *args], env=env,
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
# deferrals is green. FAILED is cleanup no later pass can finish on its own, whether the user was kept
# (wizlab 3) or let go because it was no handle to the residue (wizlab 5).
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
    rc = _wizlab(tenant, "user", "reap", "--session", sid, "--commit",
                 "--last-min", str(_window_h() * 60), *_domain_args())
    if rc not in (0, 5):
        # wizlab routes it (wizlab.reap._reap_exit): 4 finishes on its own, 3 does not. Either way the
        # handle stays until a pass proves the footprint gone.
        print(f"reap_orphans: retaining lab-{sid}@ because Wiz cleanup is incomplete", file=sys.stderr)
        return DEFERRED if rc == 4 else FAILED
    # Okta expires the login itself; `user delete` removes the Wiz record its SSO sign-in minted.
    deleted = _wizlab(tenant, "user", "delete", "--session", sid, *_domain_args()) == 0
    if rc == 5 and deleted:
        # 5 is residue only an operator clears (a stuck Outpost), found by its lab-<sid> name and never
        # through the user: keeping the record accumulated one user per play of a lab whose own cleanup
        # leaves it. The run stays red so the operator step is named with the sid.
        print(f"reap_orphans: lab-{sid}@ deleted; residue needs an operator — see the wizlab line above",
              file=sys.stderr)
    return DONE if rc == 0 and deleted else FAILED


def _reap_slot_claims(sids, commit):
    """Sweep code-lab pool claims once across all stopped sessions and expired claims instead of repeating
    matching-refs and commit lookups inside every `wizlab user reap` subprocess. A dry run reads the same
    refs, so it lists the slot backlog a commit run would release."""
    if not os.getenv("WIZ_LABS_GH_APP"):
        print("slot claims: WIZ_LABS_GH_APP not set, not swept", file=sys.stderr)
        return False
    stems = {_core._lab_stem(sid) for sid in sids}
    _codelab._TOKEN.clear()
    try:
        claims = _codelab.release_claims_for(stems, commit)
    except _core.WizlabError as e:
        print(f"DEFERRED slot claims: {e}")
        return True
    removed = sum(1 for _, _, o in claims if o == "removed")
    absent = sum(1 for _, _, o in claims if o == "absent")
    verb = "removed" if commit else "to release (dry-run)"
    print(f"# slot claims: {removed} {verb}, {absent} absent", file=sys.stderr)
    return False


def _retry_hints(failed):
    """One line per tenant: REAP_SESSIONS reaps under REAP_SESSIONS_TENANT, so a hint naming only the
    sids retries a TE session under TBCMP, finds nothing, reports done and leaves the TE record."""
    return "; ".join(f'REAP_SESSIONS_TENANT={t} REAP_SESSIONS="{",".join(sids)}"' for t, sids in failed.items())


def main():
    commit = "--commit" in sys.argv
    # The container's stdout is a pipe, so block-buffered; stderr is not. Unbuffered, the per-session
    # wizlab lines land after the summary that says "see preceding wizlab output".
    sys.stdout.reconfigure(line_buffering=True)
    _core._TOKENS.clear()
    # Manual override: reap explicit sids regardless of tag/window. For orphans that predate a track's
    # tid:<tenant> tag (labPlayReports captures tags at play time, so a late tag never back-fills), or
    # any one-off. `REAP_SESSIONS="sid1,sid2"`; reaped under REAP_SESSIONS_TENANT (default: the first
    # REAP_TENANTS key).
    manual = [s.strip() for s in os.getenv("REAP_SESSIONS", "").split(",") if s.strip()]
    for sid in manual:
        if not PLAY_ID_RE.fullmatch(sid):
            _die(f"operator error: REAP_SESSIONS entry {sid} is not a play id")
    mtenant = os.getenv("REAP_SESSIONS_TENANT") or next(iter(TENANTS), "TBCMP")
    if manual:
        print(f"# manual: {len(manual)} session(s) under {mtenant}")
    sessions = [(mtenant, sid) for sid in manual]
    unread = []
    for tenant, tag in TENANTS.items():
        try:
            report_sids = stopped_sessions(tag)
            wiz_sids = tenant_sessions(tenant)
        except (InstruqtError, _core.WizlabError) as e:
            print(f"reap_orphans: tenant {tenant} ({tag}) not swept: {e}", file=sys.stderr)
            unread.append(tenant)
            continue
        sids = list(dict.fromkeys([*report_sids, *wiz_sids]))
        print(f"# tenant {tenant} ({tag}): {len(report_sids)} stopped session(s) in last {_window_h()}h, "
              f"{len(wiz_sids)} tenant-side ({len(sids)} total)")
        sessions += [(tenant, sid) for sid in sids]
    sessions = list(dict.fromkeys(sessions))
    failed, deferred = {}, []
    for tenant, sid in sessions:
        outcome = _reap_session(tenant, sid, commit)
        if outcome == FAILED:
            failed.setdefault(tenant, []).append(sid)
        elif outcome == DEFERRED:
            deferred.append(sid)
    slots_deferred = _reap_slot_claims([sid for _, sid in sessions], commit)
    total = len(sessions)
    nfailed = sum(len(v) for v in failed.values())
    completed = total - nfailed - len(deferred)
    print(f"# {completed}/{total} session(s) {'reaped' if commit else 'ready to reap (dry-run)'}"
          f"{f', {len(deferred)} deferred to the next pass' if deferred else ''}"
          f"{', slot claims deferred to the next pass' if slots_deferred else ''}", file=sys.stderr)
    if failed:
        # wizlab exit 3 and 5 are residue no pass can clear (wizlab.reap._reap_exit), so "retry" alone is
        # wrong: the same pass fails nightly until the sid ages out, then the hint's sid is the way back.
        _die(f"{nfailed} session(s) left residue no pass can clear; see the wizlab lines above, remove it by "
             f"hand, then finish with {_retry_hints(failed)}")
    if unread:
        _die(f"tenant(s) {', '.join(unread)} not swept: labPlayReports unreadable; see the instruqt lines above")


if __name__ == "__main__":
    main()
