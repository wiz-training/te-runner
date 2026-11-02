#!/usr/bin/env python3
"""Contract tables for `board` (`wizlab-board arm | serve | run`)."""
import contextlib
import io
import json
import pathlib
import stat
import subprocess
import sys
import tempfile
import threading
import types
import typing
import unittest
import urllib.error
import urllib.request
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import board


def _proc(returncode=0, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _manifest(actions=None, schema=1, title="Ship the fix"):
    if actions is None:
        actions = [{
            "id": "open-fix",
            "label": "Open the fix PR",
            "description": "Opens a pull request and runs both scans.",
            "confirm": "Opens a PR on your repository.",
            "steps": [
                {"title": "Open the PR", "argv": ["patch", "open", "--repo", "{REPO}"], "timeout_s": 120},
                {"title": "Scan patch", "argv": ["code-scan", "ensure", "--ref", "patch/{STEM}"], "timeout_s": 300},
            ],
            "show": [{"key": "PR_URL", "label": "Pull request", "as": "link"}],
        }]
    return {"schema": schema, "title": title, "actions": actions}


def _http(port, method, path):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        method=method,
        data=b"{}" if method == "POST" else None,
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            raw = resp.read().decode("utf-8")
            ctype = resp.headers.get("Content-Type", "")
            return resp.status, (json.loads(raw) if "json" in ctype else raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        return e.code, (json.loads(raw) if raw.startswith("{") else raw)


@contextlib.contextmanager
def _live_server(run_lab_dir, synchronous=False):
    srv = board.BoardServer(("127.0.0.1", 0), run_lab_dir=run_lab_dir, synchronous=synchronous)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv.server_address[1]
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=2)


_VALID_ENV = {
    "BOARD_VAR_REPO": "wiz-training/te-code-juice-shop-01",
    "INSTRUQT_SESSION_ID": "abc123def456",
}
_GRANTED = "patch open\ncode-scan ensure\n"


class ArmContract(unittest.TestCase):
    """`wizlab-board arm` exit-code table (`board/SPEC.md` §Manifest):
    0 armed (writes 0644 board.json with {NAME} and {STEM} resolved);
    2 schema violation, unresolved {NAME}/{STEM}, or credential/secret noun;
    3 missing learner-verbs grant or ungranted `<noun> <verb>` step."""

    # (case, manifest_obj_or_raw, extra_env, learner_verbs_content, want_exit)
    ROWS: typing.ClassVar = (
        ("valid manifest resolves {REPO} and {STEM}", _manifest(), _VALID_ENV, _GRANTED, 0),
        ("missing BOARD_MANIFEST", "", _VALID_ENV, _GRANTED, 2),
        ("invalid JSON", "{not-json", _VALID_ENV, _GRANTED, 2),
        ("wrong schema version", _manifest(schema=2), _VALID_ENV, _GRANTED, 2),
        ("empty actions array", _manifest(actions=[]), _VALID_ENV, _GRANTED, 2),
        ("invalid action id pattern", _manifest(actions=[{
            "id": "BAD_ID", "label": "Run",
            "steps": [{"title": "Step", "argv": ["patch", "open"], "timeout_s": 60}],
        }]), _VALID_ENV, _GRANTED, 2),
        ("newline in argv rejected by schema", _manifest(actions=[{
            "id": "open-fix", "label": "Run",
            "steps": [{"title": "Step", "argv": ["patch", "open", "--repo\nbad"], "timeout_s": 60}],
        }]), _VALID_ENV, _GRANTED, 2),
        ("unresolved {REPO}", _manifest(), {"INSTRUQT_SESSION_ID": "abc123def456"}, _GRANTED, 2),
        ("unresolved {STEM}", _manifest(), {"BOARD_VAR_REPO": "wiz-training/slot-01"}, _GRANTED, 2),
        ("resolved var containing newline", _manifest(), {
            "BOARD_VAR_REPO": "wiz-training/slot-01\ninjected",
            "INSTRUQT_SESSION_ID": "abc123def456",
        }, _GRANTED, 2),
        *(
            (f"forbidden secret noun {noun}", _manifest(actions=[{
                "id": "mint-secret", "label": "Mint",
                "steps": [{"title": "Mint", "argv": [noun, "ensure"], "timeout_s": 60}],
            }]), _VALID_ENV, f"{noun} ensure\n", 2)
            for noun in ("sensor", "apiaccount", "serviceaccount", "mcp", "user")
        ),
        ("no learner-verbs grant file", _manifest(), _VALID_ENV, None, 3),
        ("step verb pair absent from learner-verbs", _manifest(), _VALID_ENV, "patch open\n", 3),
    )

    def test_arm_contract_rows(self):
        for label, mf, extra_env, verbs_text, want_rc in self.ROWS:
            with self.subTest(case=label), tempfile.TemporaryDirectory() as td:
                run_lab = pathlib.Path(td) / "run" / "lab"
                if verbs_text is not None:
                    run_lab.mkdir(parents=True, exist_ok=True)
                    (run_lab / "learner-verbs").write_text(verbs_text)
                raw = mf if isinstance(mf, str) else json.dumps(mf)
                env = {"RUN_LAB_DIR": str(run_lab), "BOARD_MANIFEST": raw, **extra_env}
                with contextlib.redirect_stderr(io.StringIO()):
                    rc = board.main(["arm"], env=env)
                self.assertEqual(rc, want_rc)
                board_json = run_lab / "board.json"
                if want_rc == 0:
                    self.assertTrue(board_json.is_file())
                    self.assertEqual(stat.S_IMODE(board_json.stat().st_mode), 0o644)
                    written = json.loads(board_json.read_text())
                    steps = written["actions"][0]["steps"]
                    want_s0 = ["patch", "open", "--repo", "wiz-training/te-code-juice-shop-01"]
                    self.assertEqual(steps[0]["argv"], want_s0)
                    self.assertEqual(steps[1]["argv"], ["code-scan", "ensure", "--ref", "patch/lab-abc123def456"])
                else:
                    self.assertFalse(board_json.exists())


class RouteContract(unittest.TestCase):
    """HTTP route by board state -> status (`board/SPEC.md` §HTTP API) on a live `BoardServer`."""

    # (board_state, method, path, want_status, body_check)
    ROWS: typing.ClassVar = (
        ("unarmed", "GET", "/", 200, "Getting ready"),
        ("unarmed", "GET", "/api/board", 503, None),
        ("unarmed", "POST", "/api/actions/open-fix/runs", 503, None),
        ("unarmed", "GET", "/api/runs/20261102T000000Z-0001", 503, None),
        ("unarmed", "GET", "/api/actions/open-fix/runs/latest", 503, None),
        ("unreadable", "GET", "/", 200, "Getting ready"),
        ("unreadable", "GET", "/api/board", 503, None),
        ("unreadable", "POST", "/api/actions/open-fix/runs", 503, None),
        ("unreadable", "GET", "/api/runs/20261102T000000Z-0001", 503, None),
        ("unreadable", "GET", "/api/actions/open-fix/runs/latest", 503, None),
        ("armed_idle", "GET", "/", 200, "Open the fix PR"),
        ("armed_idle", "GET", "/api/board", 200, "Ship the fix"),
        ("armed_idle", "POST", "/api/actions/unknown/runs", 404, None),
        ("armed_idle", "GET", "/api/runs/20261102T000000Z-9999", 404, None),
        ("armed_idle", "GET", "/api/actions/unknown/runs/latest", 404, None),
        ("armed_idle", "GET", "/api/actions/open-fix/runs/latest", 404, None),
        ("armed_idle", "GET", "/api/unknown", 404, None),
        ("armed_running", "POST", "/api/actions/open-fix/runs", 409, "active_run_id"),
        ("armed_running", "GET", "/api/runs/{active}", 200, "running"),
        ("armed_running", "GET", "/api/actions/open-fix/runs/latest", 200, "running"),
        ("armed_finished", "GET", "/api/runs/{active}", 200, "succeeded"),
        ("armed_finished", "GET", "/api/actions/open-fix/runs/latest", 200, "succeeded"),
        ("armed_finished", "GET", "/", 200, 'target="_blank"'),
        ("armed_finished", "POST", "/api/actions/open-fix/runs", 202, "new_run_id"),
    )

    def test_http_route_by_board_state_rows(self):
        with tempfile.TemporaryDirectory() as td:
            run_lab = pathlib.Path(td) / "run" / "lab"
            started_evt = threading.Event()
            release_evt = threading.Event()

            def _fake_run(cmd, **kwargs):
                if not release_evt.is_set():
                    started_evt.set()
                    release_evt.wait(timeout=5)
                return _proc(0, stdout="PR_URL=https://github.com/org/repo/pull/7\n", stderr="USER root -> USER node\n")

            err_buf = io.StringIO()
            with (
                mock.patch("board.cli.subprocess.run", side_effect=_fake_run),
                contextlib.redirect_stderr(err_buf),
                _live_server(run_lab) as port,
            ):
                state = "unarmed"
                active_id = None
                for row_state, method, path_tmpl, want_status, check in self.ROWS:
                    if row_state != state:
                        if state == "unreadable":
                            run_lab.chmod(0o755)
                        if row_state == "unreadable":
                            run_lab.mkdir(parents=True, exist_ok=True)
                            (run_lab / "board.json").write_text(json.dumps(_manifest()))
                            run_lab.chmod(0o000)
                        elif row_state == "armed_idle":
                            run_lab.mkdir(parents=True, exist_ok=True)
                            (run_lab / "learner-verbs").write_text(_GRANTED)
                            env = {
                                "RUN_LAB_DIR": str(run_lab),
                                "BOARD_MANIFEST": json.dumps(_manifest()),
                                **_VALID_ENV,
                            }
                            self.assertEqual(board.main(["arm"], env=env), 0)
                        elif row_state == "armed_running":
                            st, body = _http(port, "POST", "/api/actions/open-fix/runs")
                            self.assertEqual(st, 202)
                            active_id = body["run_id"]
                            self.assertTrue(started_evt.wait(timeout=2))
                        elif row_state == "armed_finished":
                            release_evt.set()
                            client_env = {"TE_BOARD_PORT": str(port)}
                            with contextlib.redirect_stdout(io.StringIO()):
                                rc = board.run_action("open-fix", wait=True, env=client_env, poll_s=0.01)
                            self.assertEqual(rc, 0)
                        state = row_state

                    path = path_tmpl.replace("{active}", active_id or "")
                    with self.subTest(state=row_state, method=method, path=path_tmpl):
                        status, payload = _http(port, method, path)
                        self.assertEqual(status, want_status)
                        if row_state == "unreadable" and want_status == 503:
                            self.assertIn("Permission denied", err_buf.getvalue())
                        if check == "active_run_id":
                            self.assertEqual(payload, {"run_id": active_id})
                        elif check == "new_run_id":
                            self.assertGreater(payload["run_id"], active_id)
                        elif check and isinstance(payload, dict):
                            self.assertIn(check, json.dumps(payload))
                        elif check and isinstance(payload, str):
                            self.assertIn(check, payload)


class RunExecutionContract(unittest.TestCase):
    """Step exit sequence -> run `status`, step statuses, `outputs` override, 64 KiB log cap, and `run --wait` exit."""

    THREE_STEPS: typing.ClassVar = [
        {"title": "Open PR", "argv": ["patch", "open", "--repo", "{REPO}"], "timeout_s": 120},
        {"title": "Scan main", "argv": ["code-scan", "ensure", "--ref", "main"], "timeout_s": 300},
        {"title": "Scan patch", "argv": ["code-scan", "ensure", "--ref", "patch/{STEM}"], "timeout_s": 300},
    ]

    # (case, step_results, want_run_status, want_wait_rc, want_step_statuses, want_exit_codes, want_outputs, want_calls)
    ROWS: typing.ClassVar = (
        (
            "all steps succeed and later KEY=value overrides earlier",
            [
                _proc(0, stdout="PR_URL=https://github.com/org/repo/pull/3\nSCAN_VERDICT=INITIAL\n", stderr="diff\n"),
                _proc(0, stdout="SCAN_NAME=lab-s-main\nSCAN_VERDICT=FAILED_BY_POLICY\n", stderr=""),
                _proc(0, stdout="SCAN_NAME=lab-s-patch\nSCAN_VERDICT=PASSED_BY_POLICY\n", stderr=""),
            ],
            "succeeded",
            0,
            ["succeeded", "succeeded", "succeeded"],
            [0, 0, 0],
            {
                "PR_URL": "https://github.com/org/repo/pull/3",
                "SCAN_NAME": "lab-s-patch",
                "SCAN_VERDICT": "PASSED_BY_POLICY",
            },
            3,
        ),
        (
            "first nonzero exit fails run and skips remaining steps",
            [
                _proc(1, stdout="", stderr="no USER root in Dockerfile\n"),
                _proc(0, stdout="SCAN_NAME=should-not-run\n", stderr=""),
                _proc(0, stdout="SCAN_NAME=should-not-run\n", stderr=""),
            ],
            "failed",
            1,
            ["failed", "skipped", "skipped"],
            [1, None, None],
            {},
            1,
        ),
        (
            "middle step nonzero exit skips final step",
            [
                _proc(0, stdout="PR_URL=https://github.com/org/repo/pull/4\n", stderr=""),
                _proc(3, stdout="", stderr="transport error\n"),
                _proc(0, stdout="SCAN_NAME=should-not-run\n", stderr=""),
            ],
            "failed",
            1,
            ["succeeded", "failed", "skipped"],
            [0, 3, None],
            {"PR_URL": "https://github.com/org/repo/pull/4"},
            2,
        ),
        (
            "step timeout kills process with exit_code 124 and skips remaining",
            [
                _proc(0, stdout="PR_URL=https://github.com/org/repo/pull/5\n", stderr=""),
                subprocess.TimeoutExpired(
                    cmd=["wizlab", "code-scan", "ensure"], timeout=300, output=b"partial\n", stderr=b"timed out\n"
                ),
                _proc(0, stdout="", stderr=""),
            ],
            "failed",
            1,
            ["succeeded", "failed", "skipped"],
            [0, 124, None],
            {"PR_URL": "https://github.com/org/repo/pull/5"},
            2,
        ),
        (
            "step log retains last 64 KiB of stdout+stderr",
            [
                _proc(
                    0,
                    stdout="HEAD-" + ("x" * 70000) + "\nPR_URL=https://github.com/org/repo/pull/6\n",
                    stderr="TAIL",
                ),
                _proc(0, stdout="", stderr=""),
                _proc(0, stdout="", stderr=""),
            ],
            "succeeded",
            0,
            ["succeeded", "succeeded", "succeeded"],
            [0, 0, 0],
            {"PR_URL": "https://github.com/org/repo/pull/6"},
            3,
        ),
        (
            "409 conflict attaches to active run without starting a second run",
            [
                _proc(0, stdout="PR_URL=https://github.com/org/repo/pull/8\n", stderr=""),
                _proc(0, stdout="", stderr=""),
                _proc(0, stdout="", stderr=""),
            ],
            "succeeded",
            0,
            ["succeeded", "succeeded", "succeeded"],
            [0, 0, 0],
            {"PR_URL": "https://github.com/org/repo/pull/8"},
            3,
        ),
        (
            "run exits 3 when action id is unknown",
            [],
            None,
            3,
            [],
            [],
            {},
            0,
        ),
    )

    def test_step_exit_sequence_rows(self):
        for (
            label,
            step_results,
            want_status,
            want_rc,
            want_step_statuses,
            want_codes,
            want_outputs,
            want_calls,
        ) in self.ROWS:
            with self.subTest(case=label), tempfile.TemporaryDirectory() as td:
                run_lab = pathlib.Path(td) / "run" / "lab"
                run_lab.mkdir(parents=True)
                (run_lab / "learner-verbs").write_text(_GRANTED)
                mf = _manifest(actions=[{
                    "id": "open-fix",
                    "label": "Open the fix PR",
                    "steps": self.THREE_STEPS,
                    "show": [{"key": "PR_URL", "label": "Pull request", "as": "link"}],
                }])
                arm_env = {"RUN_LAB_DIR": str(run_lab), "BOARD_MANIFEST": json.dumps(mf), **_VALID_ENV}
                self.assertEqual(board.main(["arm"], env=arm_env), 0)

                calls = []
                it = iter(step_results)
                started = threading.Event()
                release = threading.Event()
                is_409 = "409" in label

                def _fake_subproc(
                    cmd, _calls=calls, _it=it, _started=started, _release=release, _hold=is_409, **kwargs
                ):
                    _calls.append(cmd)
                    if _hold and len(_calls) == 1:
                        _started.set()
                        _release.wait(timeout=5)
                    item = next(_it)
                    if isinstance(item, Exception):
                        raise item
                    return item

                with (
                    mock.patch("board.cli.subprocess.run", side_effect=_fake_subproc),
                    _live_server(run_lab, synchronous=not is_409) as port,
                ):
                    run_env = {"TE_BOARD_PORT": str(port), "RUN_LAB_DIR": str(run_lab)}
                    action_id = "unknown" if want_rc == 3 else "open-fix"
                    if is_409:
                        st, first = _http(port, "POST", "/api/actions/open-fix/runs")
                        self.assertEqual(st, 202)
                        self.assertTrue(started.wait(timeout=2))
                        threading.Timer(0.05, release.set).start()
                    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                        rc = board.main(["run", action_id, "--wait"], env=run_env)
                    self.assertEqual(rc, want_rc)
                    self.assertEqual(len(calls), want_calls)
                    self.assertTrue(all(c[0] == "wizlab" for c in calls))
                    if want_status is None:
                        continue

                    st, env_data = _http(port, "GET", "/api/actions/open-fix/runs/latest")
                    self.assertEqual(st, 200)
                    if is_409:
                        self.assertEqual(env_data["run_id"], first["run_id"])
                    self.assertEqual(env_data["status"], want_status)
                    self.assertIsNotNone(env_data["finished_at"])
                    self.assertEqual([s["status"] for s in env_data["steps"]], want_step_statuses)
                    self.assertEqual([s["exit_code"] for s in env_data["steps"]], want_codes)
                    for s in env_data["steps"]:
                        if s["status"] == "skipped":
                            self.assertIsNone(s["started_at"])
                            self.assertIsNone(s["finished_at"])
                        else:
                            self.assertRegex(s["started_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
                            self.assertRegex(s["finished_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
                    self.assertEqual(env_data["outputs"], want_outputs)
                    if "64 KiB" in label:
                        log_bytes = env_data["steps"][0]["log"].encode("utf-8")
                        self.assertEqual(len(log_bytes), 65536)
                        self.assertTrue(env_data["steps"][0]["log"].endswith("TAIL"))
                        self.assertNotIn("HEAD-", env_data["steps"][0]["log"])


if __name__ == "__main__":
    unittest.main()
