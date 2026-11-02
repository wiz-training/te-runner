"""The board page: server-rendered HTML for `GET /`, refreshed in place by a small script that polls
the same route while a run is active, so the learner's scroll position and open logs survive.
No external asset: the page is served inside the lab's `service` proxy."""
import datetime
import html

_STATUS_WORD = {
    "pending": "Waiting",
    "running": "Running",
    "succeeded": "Done",
    "failed": "Failed",
    "skipped": "Skipped",
}


def _parse_ts(value):
    if not value:
        return None
    try:
        return datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.UTC)
    except ValueError:
        return None


def _duration(started, finished):
    a, b = _parse_ts(started), _parse_ts(finished)
    if not a or not b:
        return ""
    secs = int((b - a).total_seconds())
    return f"{secs // 60} min {secs % 60} s" if secs >= 60 else f"{secs} s"


def _esc(value):
    return html.escape(str(value), quote=True)


def _render_results(action, latest):
    outputs = (latest or {}).get("outputs") or {}
    rows = []
    for item in action.get("show") or []:
        val = outputs.get(item["key"])
        if not val:
            continue
        if item["as"] == "link":
            rows.append(
                f'<a class="result result-link" href="{_esc(val)}" target="_blank" rel="noopener noreferrer">'
                f'<span class="result-label">{_esc(item["label"])}</span>'
                f'<span class="result-value">{_esc(val)}</span>'
                f'<span class="result-arrow" aria-hidden="true">↗</span></a>'
            )
        else:
            rows.append(
                f'<div class="result"><span class="result-label">{_esc(item["label"])}</span>'
                f'<span class="result-value">{_esc(val)}</span></div>'
            )
    return f'<div class="results">{"".join(rows)}</div>' if rows else ""


def _render_step(idx, step):
    status = step["status"]
    rc = step.get("exit_code")
    meta = []
    dur = _duration(step.get("started_at"), step.get("finished_at"))
    if dur:
        meta.append(dur)
    if rc not in (None, 0):
        meta.append(f"exit {rc}")
    cmd = _esc("wizlab " + " ".join(step.get("argv") or []))
    log = _esc(step.get("log") or "")
    open_attr = " open" if status in ("running", "failed") else ""
    log_block = (
        f'<details class="step-log"{open_attr}><summary>Log</summary><pre>{log}</pre></details>' if log else ""
    )
    return (
        f'<li class="step step-{_esc(status)}" data-step="{idx}">'
        f'<span class="step-mark" aria-hidden="true"><span class="step-dot"></span></span>'
        f'<div class="step-body"><div class="step-head"><span class="step-title">{_esc(step["title"])}</span>'
        f'<span class="step-meta">{_esc(_STATUS_WORD.get(status, status))}'
        f'{"".join(f" · {_esc(m)}" for m in meta)}</span></div>'
        f'<code class="step-cmd">{cmd}</code>{log_block}</div></li>'
    )


def _render_run(latest):
    if not latest:
        return '<p class="empty">Nothing has run yet. The steps appear here with their logs.</p>'
    steps = latest.get("steps") or []
    done = sum(1 for s in steps if s["status"] in ("succeeded", "failed", "skipped"))
    status = latest["status"]
    if status == "running":
        summary = f"Running step {min(done + 1, len(steps))} of {len(steps)}"
    else:
        total = _duration(latest.get("started_at"), latest.get("finished_at"))
        summary = f"{'Completed' if status == 'succeeded' else 'Failed'}{f' in {total}' if total else ''}"
    return (
        f'<div class="run run-{_esc(status)}"><div class="run-head"><span class="run-state">{_esc(summary)}</span>'
        f'<span class="run-id">{_esc(latest["run_id"])}</span></div>'
        f'<ol class="steps">{"".join(_render_step(i, s) for i, s in enumerate(steps))}</ol></div>'
    )


def _render_button(action, running, latest):
    aid = _esc(action["id"])
    verb = "Running…" if running else ("Run again" if latest else action["label"])
    return (
        f'<button class="action" id="action-{aid}" data-action="{aid}" '
        f'data-confirm="{_esc(action.get("confirm") or "")}"'
        f'{" disabled" if running else ""} onclick="triggerAction(this)">'
        f'<span class="spinner" aria-hidden="true"></span><span class="action-text">{_esc(verb)}</span></button>'
    )


def render_card(action, latest):
    running = bool(latest and latest.get("status") == "running")
    failed = bool(latest and latest.get("status") == "failed")
    desc = f'<p class="card-desc">{_esc(action["description"])}</p>' if action.get("description") else ""
    hint = f'<p class="hint">{_esc(action["failure_hint"])}</p>' if failed and action.get("failure_hint") else ""
    return (
        f'<section class="card" data-action-id="{_esc(action["id"])}" aria-busy="{"true" if running else "false"}">'
        f'<div class="card-top"><div><h2 class="card-title">{_esc(action["label"])}</h2>{desc}</div>'
        f"{_render_button(action, running, latest)}</div>"
        f"{_render_results(action, latest)}{hint}{_render_run(latest)}</section>"
    )


def _shell(title, armed, running, body):
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{_esc(title)}</title>"
        "<meta name='description' content='Runs the granted actions of this lab and shows the log of each step.'>"
        f"<style>{PAGE_STYLE}</style></head><body>"
        f"<main id='board-root' data-armed='{'true' if armed else 'false'}' "
        f"data-running='{'true' if running else 'false'}'>"
        f"{body}</main><script>{PAGE_SCRIPT}</script></body></html>"
    )


def render_page(board, latest_for):
    """`board` is the armed manifest or None; `latest_for(action_id)` returns the newest envelope or None."""
    if board is None:
        body = (
            "<header class='head'><h1>Getting ready</h1>"
            "<p class='lede'>The lab is still arming the action board. This page refreshes on its own.</p></header>"
        )
        return _shell("Getting ready", False, False, body)
    pairs = [(a, latest_for(a["id"])) for a in board["actions"]]
    any_running = any(lat and lat.get("status") == "running" for _, lat in pairs)
    state = "Running" if any_running else "Ready"
    body = (
        f"<header class='head'><h1>{_esc(board['title'])}</h1>"
        f"<span class='chip chip-{state.lower()}'><span class='chip-dot'></span>{state}</span></header>"
        + "".join(render_card(a, lat) for a, lat in pairs)
    )
    return _shell(board["title"], True, any_running, body)


PAGE_STYLE = """
:root{--bg:#f4f5f7;--surface:#fff;--ink:#0f172a;--muted:#5b6472;--line:#e3e6ea;--accent:#1d4ed8;--accent-ink:#fff;
--ok:#15803d;--warn:#b45309;--bad:#b91c1c;--log-bg:#0b1220;--log-ink:#d7dde7;--radius:12px;
--shadow:0 1px 2px rgba(15,23,42,.06),0 8px 24px -16px rgba(15,23,42,.25)}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
-webkit-font-smoothing:antialiased}
main{max-width:760px;margin:0 auto;padding:28px 20px 48px}
.head{display:flex;align-items:center;justify-content:space-between;gap:16px;margin:0 0 20px}
h1{font-size:22px;line-height:1.25;letter-spacing:-.01em;margin:0;font-weight:650}
.lede{color:var(--muted);margin:6px 0 0}
.chip{display:inline-flex;align-items:center;gap:8px;padding:4px 10px;border-radius:999px;background:var(--surface);
border:1px solid var(--line);font-size:13px;color:var(--muted);white-space:nowrap}
.chip-dot{width:8px;height:8px;border-radius:50%;background:var(--ok)}
.chip-running .chip-dot{background:var(--warn);animation:pulse 1.4s ease-in-out infinite}
.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow);
padding:20px 22px;margin-bottom:16px}
.card-top{display:flex;align-items:flex-start;justify-content:space-between;gap:20px}
.card-title{font-size:17px;margin:0;font-weight:600;letter-spacing:-.005em}
.card-desc{color:var(--muted);margin:4px 0 0;max-width:46ch}
.action{flex:none;display:inline-flex;align-items:center;gap:8px;background:var(--accent);color:var(--accent-ink);
border:0;border-radius:8px;padding:10px 16px;font:inherit;font-weight:600;cursor:pointer;
transition:transform .12s ease,filter .12s ease,opacity .12s ease}
.action:hover:not(:disabled){filter:brightness(1.08);transform:translateY(-1px)}
.action:active:not(:disabled){transform:translateY(0)}
.action:focus-visible{outline:3px solid rgba(29,78,216,.35);outline-offset:2px}
.action:disabled{opacity:.7;cursor:progress}
.spinner{display:none;width:14px;height:14px;border-radius:50%;border:2px solid rgba(255,255,255,.4);
border-top-color:#fff;animation:spin .8s linear infinite}
[aria-busy="true"] .spinner{display:inline-block}
.results{display:grid;gap:8px;margin:16px 0 4px}
.result{display:flex;align-items:baseline;gap:10px;padding:10px 14px;border-radius:8px;background:#f0f6ff;
border:1px solid #d6e4ff;text-decoration:none;color:inherit;min-width:0}
.result-link:hover{background:#e6f0ff}
.result-label{font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:var(--accent);flex:none}
.result-value{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px;overflow:hidden;
text-overflow:ellipsis;white-space:nowrap;flex:1}
.result-arrow{color:var(--accent);flex:none}
.hint{margin:12px 0 0;padding:10px 14px;border-radius:8px;background:#fff4f4;border:1px solid #fecaca;color:var(--bad)}
.empty{color:var(--muted);margin:16px 0 0;font-size:14px}
.run{margin-top:18px;border-top:1px solid var(--line);padding-top:14px}
.run-head{display:flex;justify-content:space-between;align-items:baseline;gap:12px;margin-bottom:10px}
.run-state{font-weight:600;font-size:14px}
.run-failed .run-state{color:var(--bad)}.run-succeeded .run-state{color:var(--ok)}
.run-running .run-state{color:var(--warn)}
.run-id{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:var(--muted)}
.steps{list-style:none;margin:0;padding:0}
.step{display:grid;grid-template-columns:22px 1fr;gap:12px;position:relative;padding:0 0 14px}
.step:not(:last-child)::before{content:"";position:absolute;left:10px;top:20px;bottom:0;width:2px;background:var(--line)}
.step-mark{display:flex;align-items:center;justify-content:center;width:22px;height:22px}
.step-dot{width:10px;height:10px;border-radius:50%;background:#cbd2dc;box-shadow:0 0 0 3px var(--surface)}
.step-running .step-dot{background:var(--warn);animation:pulse 1.4s ease-in-out infinite}
.step-succeeded .step-dot{background:var(--ok)}.step-failed .step-dot{background:var(--bad)}
.step-skipped .step-dot{background:transparent;border:2px dashed #cbd2dc}
.step-body{min-width:0}
.step-head{display:flex;justify-content:space-between;gap:12px;align-items:baseline}
.step-title{font-weight:600;font-size:14px}
.step-meta{font-size:12px;color:var(--muted);white-space:nowrap}
.step-failed .step-meta{color:var(--bad)}
.step-cmd{display:block;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:var(--muted);
margin-top:2px;overflow-wrap:anywhere}
.step-log{margin-top:8px}
.step-log summary{cursor:pointer;font-size:12px;color:var(--muted);user-select:none}
.step-log pre{margin:6px 0 0;background:var(--log-bg);color:var(--log-ink);padding:10px 12px;border-radius:8px;
font-size:12px;line-height:1.5;max-height:260px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.35}}
@keyframes spin{to{transform:rotate(360deg)}}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
@media (max-width:560px){main{padding:18px 14px 36px}.card{padding:16px}.card-top{flex-direction:column}
.action{width:100%;justify-content:center}.step-head{flex-direction:column;gap:2px}.step-meta{white-space:normal}}
"""

PAGE_SCRIPT = """
const POLL_MS = 2000;
function openLogs() {
  const out = {};
  document.querySelectorAll('.card').forEach(card => {
    const id = card.dataset.actionId;
    out[id] = [...card.querySelectorAll('details')].map(d => d.open);
  });
  return out;
}
function restoreLogs(state) {
  document.querySelectorAll('.card').forEach(card => {
    const saved = state[card.dataset.actionId];
    if (!saved) return;
    card.querySelectorAll('details').forEach((d, i) => { if (saved[i] !== undefined) d.open = saved[i]; });
  });
}
async function refresh() {
  const res = await fetch('/', {cache: 'no-store'});
  if (!res.ok) return;
  const doc = new DOMParser().parseFromString(await res.text(), 'text/html');
  const next = doc.getElementById('board-root');
  const root = document.getElementById('board-root');
  if (!next || !root) return;
  const logs = openLogs();
  root.innerHTML = next.innerHTML;
  root.dataset.armed = next.dataset.armed;
  root.dataset.running = next.dataset.running;
  restoreLogs(logs);
  document.title = doc.title;
}
async function triggerAction(btn) {
  if (btn.dataset.confirm && !window.confirm(btn.dataset.confirm)) return;
  btn.disabled = true;
  btn.closest('.card').setAttribute('aria-busy', 'true');
  await fetch('/api/actions/' + encodeURIComponent(btn.dataset.action) + '/runs', {method: 'POST'});
  await refresh();
}
setInterval(() => {
  const root = document.getElementById('board-root');
  if (root && (root.dataset.armed === 'false' || root.dataset.running === 'true')) refresh();
}, POLL_MS);
"""
