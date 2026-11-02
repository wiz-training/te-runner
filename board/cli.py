"""Action board: `arm` validates and resolves a manifest; `serve` runs the HTTP board; `run` triggers an action."""
import datetime
import html
import http.server
import json
import os
import pathlib
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

_SCHEMA_PATH = pathlib.Path(__file__).resolve().parent / "manifest.schema.json"
# Credential nouns (wizlab/SPEC.md §Credential nouns) and `user` emit secrets on stdout.
FORBIDDEN_NOUNS = frozenset({"sensor", "apiaccount", "serviceaccount", "mcp", "user"})
_VAR_RE = re.compile(r"\{([^{}]*)\}")
_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_OUTPUT_RE = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")
_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
_MAX_LOG_BYTES = 64 * 1024


class BoardError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def _run_lab_dir(env=None):
    src = os.environ if env is None else env
    return pathlib.Path(src.get("RUN_LAB_DIR") or "/run/lab")


def _utc_now():
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_arg(arg, env):
    def _sub(match):
        name = match.group(1)
        if not _NAME_RE.match(name):
            raise BoardError(2, f"invalid variable placeholder {{{name}}}")
        if name == "STEM":
            sid = env.get("INSTRUQT_SESSION_ID") or ""
            if not sid:
                raise BoardError(2, "unresolved {STEM}: INSTRUQT_SESSION_ID is not set")
            prefix = env.get("WIZLAB_SESSION_PREFIX") or "lab"
            val = f"{prefix}-{sid}"
        else:
            val = env.get(f"BOARD_VAR_{name}") or ""
            if not val:
                raise BoardError(2, f"unresolved {{{name}}}: BOARD_VAR_{name} is not set")
        if "\n" in val:
            raise BoardError(2, f"resolved {{{name}}} contains a newline")
        return val

    resolved = _VAR_RE.sub(_sub, arg)
    if "{" in resolved or "}" in resolved:
        raise BoardError(2, f"unmatched brace in argument {arg!r}")
    return resolved


def _validate_and_resolve(manifest, env):
    seen = set()
    for action in manifest["actions"]:
        aid = action["id"]
        if aid in seen:
            raise BoardError(2, f"duplicate action id {aid!r}")
        seen.add(aid)
        for step in action["steps"]:
            noun = step["argv"][0]
            if noun in FORBIDDEN_NOUNS:
                raise BoardError(2, f"step noun {noun!r} prints secrets and cannot run on the board")
            step["argv"] = [_resolve_arg(a, env) for a in step["argv"]]


def _check_granted_verbs(manifest, run_lab):
    verbs_file = run_lab / "learner-verbs"
    if not verbs_file.is_file():
        raise BoardError(3, f"learner grant {verbs_file} is missing (run wizlab-grant first)")
    granted = {ln.strip() for ln in verbs_file.read_text().splitlines() if ln.strip()}
    for action in manifest["actions"]:
        for step in action["steps"]:
            pair = f"{step['argv'][0]} {step['argv'][1]}"
            if pair not in granted:
                raise BoardError(3, f"step '{pair}' is not listed in {verbs_file}")


def _import_jsonschema():
    try:
        import jsonschema

        return jsonschema
    except ImportError:
        for parent in pathlib.Path(__file__).resolve().parents:
            for sp in parent.glob(".venv/lib/python*/site-packages"):
                sys.path.append(str(sp))
        import jsonschema

        return jsonschema


def arm(env=None):
    """Validate `$BOARD_MANIFEST`, resolve `{NAME}` and `{STEM}`, check `learner-verbs`, write `board.json`."""
    jsonschema = _import_jsonschema()

    env = os.environ if env is None else env
    raw = env.get("BOARD_MANIFEST") or ""
    if not raw:
        raise BoardError(2, "BOARD_MANIFEST is not set")
    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError as e:
        raise BoardError(2, f"BOARD_MANIFEST is not valid JSON: {e}") from e
    schema = json.loads(_SCHEMA_PATH.read_text())
    try:
        jsonschema.validate(instance=manifest, schema=schema)
    except jsonschema.ValidationError as e:
        raise BoardError(2, f"BOARD_MANIFEST schema violation: {e.message}") from e

    _validate_and_resolve(manifest, env)
    run_lab = _run_lab_dir(env)
    _check_granted_verbs(manifest, run_lab)
    run_lab.mkdir(parents=True, exist_ok=True)
    run_lab.chmod(0o755)
    out = run_lab / "board.json"
    out.write_text(json.dumps(manifest, indent=2) + "\n")
    out.chmod(0o644)
    return 0


def _load_board(run_lab):
    path = run_lab / "board.json"
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"wizlab-board: {path}: {e}", file=sys.stderr)
        return None
    if not isinstance(data, dict) or data.get("schema") != 1 or not isinstance(data.get("actions"), list):
        return None
    return data


def _runs_dir(run_lab):
    return run_lab / "board" / "runs"


def _write_envelope(runs_dir, envelope):
    runs_dir.mkdir(parents=True, exist_ok=True)
    run_id = envelope["run_id"]
    tmp = runs_dir / f".{run_id}.tmp"
    target = runs_dir / f"{run_id}.json"
    tmp.write_text(json.dumps(envelope, indent=2) + "\n")
    os.replace(tmp, target)


def _read_envelope(runs_dir, run_id):
    if not _ID_RE.match(run_id):
        return None
    path = runs_dir / f"{run_id}.json"
    try:
        if not path.is_file():
            return None
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"wizlab-board: {path}: {e}", file=sys.stderr)
        return None


def _all_envelopes(runs_dir):
    try:
        if not runs_dir.is_dir():
            return []
        paths = sorted(runs_dir.iterdir())
    except OSError as e:
        print(f"wizlab-board: {runs_dir}: {e}", file=sys.stderr)
        return []
    items = []
    for p in paths:
        if p.name.startswith(".") or not p.name.endswith(".json"):
            continue
        try:
            items.append(json.loads(p.read_text()))
        except (OSError, json.JSONDecodeError) as e:
            print(f"wizlab-board: {p}: {e}", file=sys.stderr)
            continue
    return items


def _latest_for_action(runs_dir, action_id):
    matches = [e for e in _all_envelopes(runs_dir) if e.get("action") == action_id]
    return max(matches, key=lambda e: e.get("run_id", "")) if matches else None


def _active_run(runs_dir):
    for env in _all_envelopes(runs_dir):
        if env.get("status") == "running":
            return env.get("run_id")
    return None


def _decode(val):
    if val is None:
        return ""
    return val.decode("utf-8", errors="replace") if isinstance(val, bytes) else val


def _tail_64k(text):
    raw = text.encode("utf-8", errors="replace")
    if len(raw) > _MAX_LOG_BYTES:
        raw = raw[-_MAX_LOG_BYTES:]
    return raw.decode("utf-8", errors="ignore")


def _extract_outputs(stdout, outputs):
    for line in stdout.splitlines():
        if m := _OUTPUT_RE.match(line):
            outputs[m.group(1)] = m.group(2)


def _run_step(step_def):
    try:
        proc = subprocess.run(
            ["wizlab", *step_def["argv"]],
            capture_output=True,
            text=True,
            timeout=step_def["timeout_s"],
            check=False,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired as e:
        return 124, _decode(e.stdout), _decode(e.stderr)
    except OSError as e:
        return 3, "", f"wizlab exec failed: {e}\n"


def _execute_run(server, runs_dir, envelope, steps_def):
    for idx, step_def in enumerate(steps_def):
        with server.lock:
            envelope["steps"][idx]["status"] = "running"
            envelope["steps"][idx]["started_at"] = _utc_now()
            _write_envelope(runs_dir, envelope)
        rc, out, err = _run_step(step_def)
        now = _utc_now()
        with server.lock:
            _extract_outputs(out, envelope["outputs"])
            envelope["steps"][idx]["finished_at"] = now
            envelope["steps"][idx]["exit_code"] = rc
            envelope["steps"][idx]["log"] = _tail_64k(out + err)
            if rc != 0:
                envelope["steps"][idx]["status"] = "failed"
                for rest in envelope["steps"][idx + 1 :]:
                    rest["status"] = "skipped"
                    rest["exit_code"] = None
                envelope["status"] = "failed"
                envelope["finished_at"] = now
                _write_envelope(runs_dir, envelope)
                return
            envelope["steps"][idx]["status"] = "succeeded"
            _write_envelope(runs_dir, envelope)
    with server.lock:
        envelope["status"] = "succeeded"
        envelope["finished_at"] = _utc_now()
        _write_envelope(runs_dir, envelope)


def _public_board(board):
    actions = []
    for a in board["actions"]:
        row = {
            "id": a["id"],
            "label": a["label"],
            "description": a.get("description", ""),
            "confirm": a.get("confirm", ""),
        }
        if "show" in a:
            row["show"] = a["show"]
        if "failure_hint" in a:
            row["failure_hint"] = a["failure_hint"]
        actions.append(row)
    return {"schema": board["schema"], "title": board["title"], "actions": actions}


def _render_show_rows(action, latest):
    outputs = (latest or {}).get("outputs") or {}
    parts = []
    for item in action.get("show") or []:
        key, label, kind = item["key"], html.escape(item["label"]), item["as"]
        val = outputs.get(key)
        if not val:
            continue
        esc_val = html.escape(val)
        if kind == "link":
            parts.append(f'<div class="show-row"><strong>{label}:</strong> '
                         f'<a href="{esc_val}" target="_blank" rel="noopener noreferrer">{esc_val}</a></div>')
        else:
            parts.append(f'<div class="show-row"><strong>{label}:</strong> <span>{esc_val}</span></div>')
    return "".join(parts)


def _render_steps(latest):
    if not latest:
        return '<div class="empty">No runs yet.</div>'
    parts = [f'<div class="run-status status-{html.escape(latest["status"])}">'
             f'Run {html.escape(latest["run_id"])}: {html.escape(latest["status"])}</div>']
    for s in latest.get("steps") or []:
        cmd = html.escape("wizlab " + " ".join(s.get("argv") or []))
        rc = "" if s.get("exit_code") is None else f" (exit {s['exit_code']})"
        log = html.escape(s.get("log") or "")
        parts.append(
            f'<div class="step step-{html.escape(s["status"])}">'
            f'<div class="step-hdr"><strong>{html.escape(s["title"])}</strong> '
            f'<code>{cmd}</code> — <span class="badge">{html.escape(s["status"])}{rc}</span></div>'
            f'<pre class="step-log">{log}</pre></div>'
        )
    return "".join(parts)


_PAGE_SCRIPT = """
async function triggerAction(btn) {
  const id = btn.dataset.action;
  const confirmMsg = btn.dataset.confirm;
  if (confirmMsg && !window.confirm(confirmMsg)) return;
  btn.disabled = true;
  await fetch('/api/actions/' + encodeURIComponent(id) + '/runs', {method: 'POST'});
  window.location.reload();
}
setInterval(async () => {
  const root = document.getElementById('board-root');
  if (root && root.dataset.armed === 'false') {
    const r = await fetch('/api/board');
    if (r.ok) window.location.reload();
    return;
  }
  if (root && root.dataset.running === 'true') {
    window.location.reload();
  }
}, 2000);
"""

_PAGE_STYLE = """
body { font-family: system-ui, sans-serif; margin: 2rem; color: #111827; background: #f9fafb; }
.card { background: #fff; border: 1px solid #e5e7eb; border-radius: 8px; padding: 1.25rem; margin-bottom: 1rem; }
button { background: #2563eb; color: #fff; border: none; border-radius: 6px; padding: 0.5rem 1rem; cursor: pointer; }
button:disabled { opacity: 0.6; cursor: not-allowed; }
pre.step-log { background: #111827; color: #f3f4f6; padding: 0.75rem; border-radius: 6px; overflow-x: auto; }
.show-row { margin: 0.5rem 0; }
"""


def _render_card(action, latest):
    aid = action["id"]
    desc = f"<p>{html.escape(action['description'])}</p>" if action.get("description") else ""
    confirm_attr = html.escape(action.get("confirm") or "", quote=True)
    hint = ""
    if latest and latest.get("status") == "failed" and action.get("failure_hint"):
        hint = f'<div class="failure-hint">{html.escape(action["failure_hint"])}</div>'
    return (
        f'<div class="card" data-action-id="{html.escape(aid)}">'
        f"{desc}"
        f'<button data-action="{html.escape(aid)}" data-confirm="{confirm_attr}" '
        f'onclick="triggerAction(this)">{html.escape(action["label"])}</button>'
        f"{_render_show_rows(action, latest)}{hint}{_render_steps(latest)}</div>"
    )


def _render_page(board, runs_dir):
    if board is None:
        return (
            "<!doctype html><html><head><meta charset='utf-8'><title>Getting ready</title>"
            f"<style>{_PAGE_STYLE}</style></head><body>"
            "<div id='board-root' data-armed='false' data-running='false'>"
            "<h1>Getting ready</h1><p>Waiting for the lab environment to finish arming the action board…</p>"
            f"</div><script>{_PAGE_SCRIPT}</script></body></html>"
        )
    pairs = [(a, _latest_for_action(runs_dir, a["id"])) for a in board["actions"]]
    any_running = any(lat and lat.get("status") == "running" for _, lat in pairs)
    cards = [_render_card(a, lat) for a, lat in pairs]
    title = html.escape(board["title"])
    running_flag = "true" if any_running else "false"
    return (
        f"<!doctype html><html><head><meta charset='utf-8'><title>{title}</title>"
        f"<style>{_PAGE_STYLE}</style></head><body>"
        f"<div id='board-root' data-armed='true' data-running='{running_flag}'>"
        f"<h1>{title}</h1>{''.join(cards)}</div>"
        f"<script>{_PAGE_SCRIPT}</script></body></html>"
    )


class BoardServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, run_lab_dir=None, synchronous=False):
        super().__init__(server_address, _Handler)
        self.run_lab_dir = pathlib.Path(run_lab_dir) if run_lab_dir else None
        self.synchronous = synchronous
        self.lock = threading.Lock()
        self.seq = 0

    def current_run_lab(self):
        return self.run_lab_dir or _run_lab_dir()

    def next_run_id(self, runs_dir):
        self.seq = max(self.seq, len(_all_envelopes(runs_dir))) + 1
        stamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
        return f"{stamp}-{self.seq:04d}"


def _start_or_conflict(server, board, runs_dir, action_id):
    action = next((a for a in board["actions"] if a["id"] == action_id), None)
    if action is None:
        return 404, {"error": f"unknown action {action_id!r}"}
    with server.lock:
        active = _active_run(runs_dir)
        if active:
            return 409, {"run_id": active}
        run_id = server.next_run_id(runs_dir)
        steps_def = [dict(s, argv=list(s["argv"])) for s in action["steps"]]
        envelope = {
            "schema": 1,
            "run_id": run_id,
            "action": action_id,
            "started_at": _utc_now(),
            "finished_at": None,
            "status": "running",
            "steps": [
                {
                    "title": s["title"],
                    "argv": list(s["argv"]),
                    "status": "pending",
                    "started_at": None,
                    "finished_at": None,
                    "exit_code": None,
                    "log": "",
                }
                for s in steps_def
            ],
            "outputs": {},
        }
        _write_envelope(runs_dir, envelope)
    if server.synchronous:
        _execute_run(server, runs_dir, envelope, steps_def)
    else:
        t = threading.Thread(target=_execute_run, args=(server, runs_dir, envelope, steps_def), daemon=True)
        t.start()
    return 202, {"run_id": run_id}


def _route_api_get(path, board, runs_dir):
    if path == "/api/board":
        return 200, _public_board(board)
    if m := re.fullmatch(r"/api/runs/([a-zA-Z0-9_-]+)", path):
        env = _read_envelope(runs_dir, m.group(1))
        return (200, env) if env else (404, {"error": "unknown run"})
    if m := re.fullmatch(r"/api/actions/([a-zA-Z0-9_-]+)/runs/latest", path):
        aid = m.group(1)
        if not any(a["id"] == aid for a in board["actions"]):
            return 404, {"error": "unknown action"}
        env = _latest_for_action(runs_dir, aid)
        return (200, env) if env else (404, {"error": "no runs for action"})
    return 404, {"error": "not found"}


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def _send(self, code, body, content_type="application/json"):
        payload = body.encode("utf-8") if isinstance(body, str) else json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _drain_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 0:
            self.rfile.read(length)

    def do_GET(self):
        run_lab = self.server.current_run_lab()
        runs_dir = _runs_dir(run_lab)
        board = _load_board(run_lab)
        if self.path == "/":
            self._send(200, _render_page(board, runs_dir), "text/html; charset=utf-8")
            return
        if board is None:
            self._send(503, {"error": "board is not armed"})
            return
        code, payload = _route_api_get(self.path, board, runs_dir)
        self._send(code, payload)

    def do_POST(self):
        self._drain_body()
        run_lab = self.server.current_run_lab()
        board = _load_board(run_lab)
        if board is None:
            self._send(503, {"error": "board is not armed"})
            return
        if m := re.fullmatch(r"/api/actions/([a-zA-Z0-9_-]+)/runs", self.path):
            code, payload = _start_or_conflict(self.server, board, _runs_dir(run_lab), m.group(1))
            self._send(code, payload)
            return
        self._send(404, {"error": "not found"})


def _board_port(env, err_code):
    src = os.environ if env is None else env
    try:
        return int(src.get("TE_BOARD_PORT") or "7070")
    except ValueError as e:
        raise BoardError(err_code, f"invalid TE_BOARD_PORT: {e}") from e


def serve(env=None):
    env = os.environ if env is None else env
    port = _board_port(env, 2)
    srv = BoardServer(("0.0.0.0", port), run_lab_dir=env.get("RUN_LAB_DIR"))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


def _http_json(method, url):
    req = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8"))
        except Exception:
            body = {}
        return e.code, body
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        raise BoardError(3, f"board request to {url} failed: {e}") from e


def _wait_for_run(base, run_id, poll_s):
    while True:
        g_status, env_data = _http_json("GET", f"{base}/api/runs/{run_id}")
        if g_status != 200:
            raise BoardError(3, f"GET /api/runs/{run_id} returned HTTP {g_status}")
        state = env_data.get("status")
        if state == "succeeded":
            for k, v in (env_data.get("outputs") or {}).items():
                print(f"{k}={v}")
            return 0
        if state == "failed":
            return 1
        time.sleep(poll_s)


def run_action(action_id, wait=False, env=None, poll_s=0.05):
    """Trigger `POST /api/actions/<id>/runs` on `localhost:$TE_BOARD_PORT`; attach on 409; optionally wait."""
    base = f"http://127.0.0.1:{_board_port(env, 3)}"
    status, payload = _http_json("POST", f"{base}/api/actions/{action_id}/runs")
    if status not in (202, 409):
        raise BoardError(3, f"POST /api/actions/{action_id}/runs returned HTTP {status}")
    run_id = payload.get("run_id")
    if not run_id:
        raise BoardError(3, "board returned no run_id")
    if not wait:
        print(run_id)
        return 0
    return _wait_for_run(base, run_id, poll_s)


def _parse_run_args(argv):
    wait = False
    pos = []
    for a in argv:
        if a == "--wait":
            wait = True
        elif a.startswith("-"):
            raise BoardError(2, f"unknown flag {a!r} for `wizlab-board run`")
        else:
            pos.append(a)
    if len(pos) != 1:
        raise BoardError(2, "usage: wizlab-board run <id> [--wait]")
    return pos[0], wait


def main(argv=None, env=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    try:
        if argv == ["arm"]:
            return arm(env=env)
        if argv == ["serve"]:
            return serve(env=env)
        if argv and argv[0] == "run":
            action_id, wait = _parse_run_args(argv[1:])
            return run_action(action_id, wait=wait, env=env)
        raise BoardError(2, "usage: wizlab-board {serve | arm | run <id> [--wait]}")
    except BoardError as e:
        print(f"wizlab-board: {e}", file=sys.stderr)
        return e.code
