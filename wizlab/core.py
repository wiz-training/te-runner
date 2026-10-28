"""The shared floor: errors, flags, the Wiz transport and pager, tenant keying, naming, CSP CLIs."""
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# The token audience. A lab that hands an agent or an SDK its own Wiz credential needs it and the
# endpoints below spelled the way wizlab spells them (`wiz tenant`), so they live here, never as
# literals at a call site.
AUDIENCE = "wiz-api"


def _domain():
    """WIZ_<TENANT>_DOMAIN, default app.wiz.io: a demo-environment tenant lives under demo.wiz.io with the
    same auth, api and mcp host shapes, so one variable moves every call. The portal is the domain
    itself; every other Wiz host is composed from it through _host. A literal `app.wiz.io` elsewhere is
    the bug this prevents."""
    return _tenant_env("DOMAIN") or "app.wiz.io"


def _host(prefix):
    """`<prefix>.<domain>`: the auth and api.<dc> hosts share one domain and differ only here."""
    return f"{prefix}.{_domain()}"


def auth_url():
    return f"https://{_host('auth')}/oauth/token"


def api_url(dc):
    return f"https://{_host(f'api.{dc}')}/graphql"


CLOUDS = ("aws", "gcp", "azure")


# Azure subscription ids are GUIDs, and Wiz stores them lowercase: an UPPERCASED id returns
# totalCount 0 silently rather than erroring, which grades a healthy subscription as "nothing found".
# AWS account ids are digits and GCP project ids are already lowercase, so normalising only things
# shaped like a GUID is safe on every cloud.
_GUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


def _norm_account(account):
    return account.lower() if _GUID_RE.match(account or "") else account


_NODE = """
      id name enabled status
      type { id }
      outpost { id name }
      config {
        ... on ConnectorConfigAWS { customerRoleARN externalIdNonce }
        ... on ConnectorConfigGCP { projectId: project_id }
        ... on ConnectorConfigAzure { subscriptionId tenantId isManagedIdentity }
      }"""


class WizlabError(Exception):
    """A verb's failure with its exit code: 2 the caller's, 3 the environment's. Raised anywhere and
    reported once by main(); a handler that returns has succeeded."""
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def die(code, msg):
    raise WizlabError(code, msg)


def _fail(e):
    print(f"wizlab: {e}", file=sys.stderr)
    return e.code


def _exact(nodes, name):
    """The node named exactly `name`: every list filter is a substring match, so == is the caller's."""
    return next((n for n in nodes if n.get("name") == name), None)


def _prefer(nodes, *statuses):
    """The node in the earliest tier of `statuses` if there is one, else the first: a healthy record
    outranks a stale one left on a recycled name."""
    rank = {s: i for i, s in enumerate(statuses)}
    return min(nodes, key=lambda n: rank.get(n.get("status"), len(statuses)), default=None)


def _emit(lines, secret=False):
    """KEY=value lines to stdout and, under an Instruqt `exec` resource, to $EXEC_OUTPUT, where a note
    reads them back as resource.exec.<name>.output.<KEY>.

    `secret=True` keeps the values off stdout when $EXEC_OUTPUT is set: the platform captures an
    exec's stdout into `instruqt lab logs`, readable by anyone with access to the lab, while the note
    reads $EXEC_OUTPUT. The keys still print, so an operator reading the log sees the verb published.
    Without $EXEC_OUTPUT the values print as usual — a hand run has no log and needs them. A verb
    whose caller parses our stdout for the value cannot pass this."""
    out = os.getenv("EXEC_OUTPUT")
    if secret and out:
        sys.stdout.write("".join(f"{ln.split('=', 1)[0]}=<redacted>\n" for ln in lines.splitlines() if ln))
    else:
        sys.stdout.write(lines)
    if out:
        with open(out, "a") as f:
            f.write(lines)


def _claims(tok):
    """The JWT's payload. An opaque or truncated token is the auth server's state (3), never a bug (2)."""
    try:
        pad = tok.split(".")[1]
        pad += "=" * (-len(pad) % 4)
        obj = json.loads(base64.urlsafe_b64decode(pad))
    except (AttributeError, IndexError, ValueError) as e:
        die(3, f"access token is not a JWT: {type(e).__name__}")
    if not isinstance(obj, dict):
        die(3, f"access token payload is not a JSON object: {type(obj).__name__}")
    return obj


def _err_body(e):
    try:
        return e.read().decode(errors="replace")[:500]
    except OSError as err:
        return f"<unreadable body: {type(err).__name__}: {err}>"


def _post(url, data, headers, attempts=3):
    # `attempts` is a SUBMISSION budget the caller owns, not a transport nicety: every failure counted
    # here (transport, 5xx/429, an undecodable body) leaves the outcome UNKNOWN, so resubmitting is only
    # safe when the request is. Retrying transient failures with backoff keeps a momentary Wiz blip from
    # exiting 3 mid-setup. 4xx (auth/bad request) is not transient — fail fast. The default suits the
    # token mints, which allocate no resource; GraphQL callers pass _submissions(query).
    payload = data.encode() if isinstance(data, str) else json.dumps(data).encode()
    last = ""
    for i in range(attempts):
        req = urllib.request.Request(url, data=payload, headers=headers)  # fresh per attempt
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                obj = json.loads(r.read())
            if not isinstance(obj, dict):
                raise ValueError(f"expected JSON object, got {type(obj).__name__}")
            return obj
        except urllib.error.HTTPError as e:
            body = _err_body(e)
            if e.code < 500 and e.code != 429:
                die(3, f"HTTP {e.code} from {url}: {body}")
            last = f"HTTP {e.code} from {url}: {body}"
        except Exception as e:
            last = f"transport failure to {url}: {type(e).__name__}: {e}"
        if i < attempts - 1:
            time.sleep(2**i)  # 1s, 2s
    if attempts == 1:
        die(3, f"{last}; not resubmitted — the server may have applied it, so inspect or reconcile "
               f"state by name rather than assuming nothing happened")
    die(3, f"after {attempts} attempts: {last}")


_DEFAULT_TENANT = "TBCMP"


def _tenant():
    return os.getenv("WIZ_TENANT") or _DEFAULT_TENANT


def _tenant_env(name):
    """WIZ_<TENANT>_<name>, else the tenant-less WIZ_<name> a single-tenant env still sets."""
    return os.getenv(f"WIZ_{_tenant()}_{name}") or os.getenv(f"WIZ_{name}")


_TOKENS = {}


def token_and_dc():
    """(token, dc, tid) for the tenant, minted once and reused until a minute before its `exp`: one
    verdict used to cost up to six mints, each a round trip to the auth server."""
    tenant = _tenant()
    hit = _TOKENS.get(tenant)
    if hit and hit[3] - time.time() > 60:
        return hit[:3]
    cid, sec = _tenant_env("CLIENT_ID"), _tenant_env("CLIENT_SECRET")
    if not cid or not sec:
        die(3, f"WIZ_{tenant}_CLIENT_ID / _CLIENT_SECRET not in environment")
    payload = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "audience": AUDIENCE,
            "client_id": cid,
            "client_secret": sec,
        }
    )
    res = _post(auth_url(), payload, {"Content-Type": "application/x-www-form-urlencoded"})
    tok = res.get("access_token")
    if not tok:
        die(3, f"no token returned: {res.get('message') or res}")
    claims = _claims(tok)
    dc = claims.get("dc") or die(3, "access token carries no dc claim; cannot address the API")
    exp = claims.get("exp") or 0
    _TOKENS[tenant] = (tok, dc, claims.get("tid"), exp if isinstance(exp, (int, float)) else 0)
    return _TOKENS[tenant][:3]


# Wiz answers a transient fault as a GraphQL error with HTTP 200, so _post's 5xx retry never sees it.
# Left unretried it became a learner-facing lie: a 500 on a `connector inspect` read exits 3, the
# check wrapper remaps 3->1, and the learner is told "no connector targets this subscription" seconds
# after creating one (observed once during azure-connector-101 validation).
_TRANSIENT_GQL = ("internal server error", "temporarily unavailable", "service unavailable", "timeout")


_READ_SUBMISSIONS = 3


def _submissions(query):
    """How many times this GraphQL document may be SENT, which is a property of the document, not of
    the failure that interrupted it. Wiz applies a mutation before it answers, so a 503, a timeout or a
    body that will not decode leaves "did it happen?" unanswerable — and a resend creates the second
    connector. Reads carry no such risk and keep the bounded retry. Anything not recognisably a read
    counts as a mutation. Idempotency: https://www.rfc-editor.org/rfc/rfc9110.html#section-9.2.2"""
    return _READ_SUBMISSIONS if query.lstrip().startswith(("query", "{")) else 1


# Pages a lookup follows before it stops calling the remainder "absent". A list on the session stem or
# an exact name past this is an anomaly to name, never a page to guess about.
_PAGE_CAP = 30


def _next_cursor(conn, seen):
    """(cursor, None) for the page after `conn`; (None, None) when it was the last; (None, reason) when
    the walk cannot be trusted: no cursor, a cursor already fetched (a server ignoring `after`), or
    the cap. A short walk is never "absent" — callers refuse to conclude on it."""
    pi = conn.get("pageInfo") or {}
    if not pi.get("hasNextPage"):
        return None, None
    cur = pi.get("endCursor")
    if not cur:
        return None, "hasNextPage without an endCursor"
    if cur in seen:
        return None, "the server repeated a cursor"
    if len(seen) + 1 >= _PAGE_CAP:
        return None, f"more than {_PAGE_CAP} pages"
    seen.add(cur)
    return cur, None


def _err_text(e):
    return str((e.get("message") if isinstance(e, dict) else e) or "?")


def _gql_msg(errs):
    """The first GraphQL error's message: the one line a tally or an alert carries."""
    return _err_text(errs[0])


def _paged(send, query, variables, field):
    """Every node of connection `field`, following pageInfo through `send(query, variables) -> (data,
    errors)`. Returns (nodes, alert); the alert names why the walk stopped short."""
    nodes, cursor, seen = [], None, set()
    while True:
        data, errs = send(query, {**variables, "after": cursor})
        if errs:
            return nodes, _gql_msg(errs)
        conn = data.get(field) or {}
        nodes += conn.get("nodes") or []
        cursor, alert = _next_cursor(conn, seen)
        if cursor is None:
            return nodes, alert


def _api_send(query, variables):
    # A page Wiz truncated arrives as data plus errors in one body; the pager must see both.
    data, _tid, errors = _api_full(query, variables)
    return data, errors


def _all_nodes(query, variables, field):
    """Every node the document matches, or exit 3: a lookup that feeds a delete or an ==1 guard must see
    the whole set, and a check that cannot see it must not report learner state."""
    nodes, alert = _paged(_api_send, query, variables, field)
    if alert:
        die(3, f"{field}: {alert}; cannot prove what is absent")
    return nodes


def _delete_doc(mutation, select="_stub"):
    # The id is a variable, never spliced: `--id` on `credential.delete` is user input. The mutation
    # name stays in the document text — callers and tests route on it.
    return f"mutation DeleteById($id: ID!) {{ {mutation}(input: {{ id: $id }}) {{ {select} }} }}"


def _graphql(tok, dc, query, variables=None):
    """(data, errors) for one document, sent within its own submission budget."""
    body = {"query": query} if variables is None else {"query": query, "variables": variables}
    res = _post(api_url(dc), body,
                {"Content-Type": "application/json", "Authorization": f"Bearer {tok}"},
                attempts=_submissions(query))
    data, errors = res.get("data"), res.get("errors")
    if data is not None and not isinstance(data, dict):
        die(3, f"GraphQL data is not a JSON object: {type(data).__name__}")
    if errors is not None and not isinstance(errors, list):
        die(3, f"GraphQL errors is not a list: {type(errors).__name__}")
    return data or {}, errors or []


def _api_full(query, variables):
    """(data, tid, errors): a GraphQL error is fatal, except a transient one on a read, which is retried;
    errors are returned only when data rides beside them, for the caller to judge."""
    for i in range(_READ_SUBMISSIONS):
        tok, dc, tid = token_and_dc()
        data, errors = _graphql(tok, dc, query, variables)
        if not errors:
            return data, tid, []
        msg = "; ".join(_err_text(e) for e in errors)
        # One rule for both layers: whatever may not be resubmitted over HTTP may not be replayed here
        # either, so a transient GraphQL fault is retried for reads and fatal for everything else.
        is_read = _submissions(query) > 1
        if is_read and any(t in msg.lower() for t in _TRANSIENT_GQL):
            if i == _READ_SUBMISSIONS - 1:
                die(3, msg)
            time.sleep(2**i)  # 1s, 2s
            continue
        if any(data.get(k) is not None for k in data):
            return data, tid, errors
        die(3, msg)
    die(3, "unreachable")  # pragma: no cover


def api(query, variables):
    """(data, tid). A field-level error beside data is tolerated (a mutation's warning) and named on
    stderr; a lookup that must see a whole set goes through _all_nodes, which refuses it."""
    data, tid, errors = _api_full(query, variables)
    if errors:
        msg = "; ".join(_err_text(e) for e in errors)
        print(f"wizlab: {len(errors)} GraphQL error(s) beside data, ignored: {msg}", file=sys.stderr)
    return data, tid


def _created(data, mutation, field):
    """The `field` record a create/update mutation answered with, or exit 3: a mutation that returns
    data with no record has not created what the caller is about to print as created."""
    rec = (data.get(mutation) or {}).get(field) or {}
    if not rec:
        die(3, f"{mutation} returned no {field}")
    return rec


def _csp_json(r, cmd):
    """Exit 0 is not the assertion — each of these commands exits 0 while reporting no identity at all
    (`gcloud auth list` prints `[]` when nothing activated), so a returncode check passes verification
    and leaves a terraform apply mid-lab to hit the real failure."""
    if r.returncode != 0:
        die(3, f"{cmd} failed: {r.stderr.strip()}")
    if not (r.stdout or "").strip():
        die(3, f"{cmd}: printed no JSON")
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        die(3, f"{cmd} returned no parseable identity: {r.stdout.strip()[:200]!r}")


def _account_id(args, verb):
    return _norm_account(args.account_id or die(2, f"{verb} needs --account-id"))


def _unused(args, cloud, *flags):
    """Exit 2 for a flag this --cloud or mode path never reads: `FLAGS` refuses a flag the verb does not
    read, and a flag only another branch reads was parsed, then ignored, so the call graded as if it had
    taken effect."""
    where = f"--cloud {cloud}" if cloud in CLOUDS else cloud
    for flag in flags:
        if getattr(args, flag.lstrip("-").replace("-", "_")):
            die(2, f"{flag} is not read for {where}")


def _named(args, suffix="", flag="name"):
    """The `flag` option (default --name), else the session stem plus `suffix`: the reaper sweeps the stem,
    so a default name is always within its reach; the flag is for a manual or dev run. `getattr` is
    strict on purpose: a verb whose FLAGS lack the option is a bug here, not a stem by accident."""
    return getattr(args, flag) or f"{_stem(args)}{suffix}"


_SCOPE_RE = re.compile(r"^[a-z_]+:[a-z_]+$")


def _scopes(args, default):
    """--scopes as a list, refused as invocation error 2 when a string is not `<verb>:<object>`: the API
    takes an unknown scope without complaint and the account then holds fewer permissions than the
    call named, which surfaces a play later as a tool the agent cannot see."""
    wanted = [s.strip() for s in (args.scopes or default).split(",") if s.strip()]
    bad = [s for s in wanted if not _SCOPE_RE.match(s)]
    if bad:
        die(2, f"--scopes takes <verb>:<object> strings; not {', '.join(repr(b) for b in bad)}")
    return wanted


# measured 2026-10-27: 512/512 play ids in 30 d (labPlayReports) match ^[a-z0-9]{12}$.
PLAY_ID_RE = re.compile(r"[a-z0-9]{12}")
_STEM_DELIMS = ("-", "@")


def _sid(args):
    # The lab session id: unique per play, injected as INSTRUQT_SESSION_ID, and the id labPlayReports
    # returns — so the out-of-band reaper joins a stopped session straight to its objects by name.
    return args.session or os.getenv("INSTRUQT_SESSION_ID")


def _stem_opt(args):
    """The session stem when it is knowable, else None. Unlike _session_id this never dies: a check
    run by hand has no INSTRUQT_SESSION_ID, and the name-search layer is an optimisation, not a
    requirement."""
    sid = _sid(args)
    return _lab_stem(sid) if sid else None


def _session_id(args):
    return _sid(args) or die(2, "need --session or INSTRUQT_SESSION_ID (the lab session id = naming + reap key)")


def _stem(args):
    """The session's naming stem, lab-<session_id>; exit 2 when no session is known."""
    return _lab_stem(_session_id(args))


def _session_prefix():
    return os.getenv("WIZLAB_SESSION_PREFIX") or "lab"


def _lab_stem(session_id):
    # Everything a lab creates (or tells the learner to create) is named lab-<session_id>: an
    # actor-independent, per-session reap key the prefix sweep matches and labPlayReports keys on
    # directly. Override the leading token with WIZLAB_SESSION_PREFIX.
    return f"{_session_prefix()}-{session_id}"


def _owned(name, stem):
    """True when `name` is `stem` or starts with `stem` plus a stem delimiter (`-` or `@`)."""
    return bool(name and stem) and (name == stem or any(name.startswith(stem + d) for d in _STEM_DELIMS))


def _drift(label, pairs):
    """Exit 3 naming every (field, live, wanted) that differs; nothing when none does. The rule an
    `ensure` follows when it will not mutate an existing object (SPEC.md §What `ensure` promises)."""
    diffs = [f"{k} {live!r} -> {want!r}" for k, live, want in pairs if want is not None and live != want]
    if diffs:
        die(3, f"{label} exists and differs from the flags given: {', '.join(diffs)}; not mutated")


def _read_json(path, flag):
    if not path:
        die(2, f"needs {flag} <path to json>")
    try:
        with open(path) as fh:
            return json.load(fh)
    except OSError as e:
        die(2, f"cannot read {flag} {path}: {e}")
    except ValueError as e:
        die(2, f"{flag} {path} is not valid JSON: {e}")


# A hung CSP CLI otherwise outlives the platform's own check timeout and the learner sees a timeout,
# not the environment error it is.
_CLI_TIMEOUT_S = 120


def _cli(binary, *a):
    try:
        return subprocess.run([binary, *a], capture_output=True, text=True, check=False,
                              timeout=_CLI_TIMEOUT_S)
    except FileNotFoundError:
        die(3, f"{binary!r} not found in PATH")  # env error (3), not the main() catch-all's 2
    except OSError as e:
        die(3, f"cannot execute {binary!r}: {e}")
    except subprocess.TimeoutExpired:
        die(3, f"{binary!r} produced no result within {_CLI_TIMEOUT_S}s")


def _aws(*a): return _cli("aws", *a)


def _gcp(*a): return _cli("gcloud", *a)


def _az(*a): return _cli("az", *a)


def _lab_user_email(args):
    # Deterministic from the session id so `ensure` is idempotent and `delete`/reap need no stored
    # state — and the email carries the reaper's join key (lab-<session_id>@).
    return f"{_stem(args)}@{args.domain}"


def _gql(tok, dc, query, variables=None):
    # The reaper's view of the transport: errors come back so one unresolvable type alerts, not aborts;
    # a transient fault on a read is retried the same way _api_full retries it, without stacking.
    attempts = _submissions(query)
    for i in range(attempts):
        data, errors = _graphql(tok, dc, query, variables)
        if not errors or any(data.get(k) is not None for k in data):
            return data, errors
        msg = "; ".join(_err_text(e) for e in errors)
        if not any(t in msg.lower() for t in _TRANSIENT_GQL) or i == attempts - 1:
            return data, errors
        time.sleep(2**i)
    return {}, []  # pragma: no cover
