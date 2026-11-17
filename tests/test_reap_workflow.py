"""Structural and shell-step tests for `.github/workflows/reap.yml`.

Parses job steps from workflow indentation (following `test_build_workflow.py` / `test_canary_workflow.py`)
and executes the extracted shell blocks against shimmed `python` and `gh` binaries with no live API calls.
"""
import os
import pathlib
import re
import stat
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_build_workflow import REAP_YML, _image_steps, _live, _run_block


def _report_steps(text):
    return _image_steps(text[text.index("\n  report:\n") :])


def _parse_github_output(text):
    out = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        ln = lines[i]
        if "<<" in ln:
            key, delim = ln.split("<<", 1)
            body = []
            i += 1
            while i < len(lines) and lines[i] != delim:
                body.append(lines[i])
                i += 1
            out[key] = "\n".join(body)
        elif "=" in ln:
            key, val = ln.split("=", 1)
            out[key] = val
        i += 1
    return out


def _write_exe(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _eval_report_if(if_expr, reap_rc, reap_outputs):
    """Evaluate `failure() && needs.reap.outputs.failed == 'true'` for the `report` job."""
    assert "failure()" in if_expr
    assert "needs.reap.outputs.failed == 'true'" in if_expr
    return reap_rc != 0 and reap_outputs.get("failed") == "true"


class ReapWorkflowTests(unittest.TestCase):
    def test_workflow_structure_permissions_and_report_host_job(self):
        text = REAP_YML.read_text()
        live = _live(text)
        self.assertRegex(live, r"(?m)^permissions:\s*\n\s*contents:\s*read\s*\n\s*issues:\s*write\b")
        self.assertRegex(live, r"(?ms)^\s*confirm:\s*\n\s*description:.*\n\s*type:\s*string\s*\n\s*default:\s*\"\"")
        self.assertNotIn("runner.", live.split("    steps:")[0])

        reap_steps = _image_steps(text)
        self.assertEqual([label for label, _ in reap_steps], ["check confirm", "reap orphans"])
        reap_dict = dict(reap_steps)
        self.assertRegex(reap_dict["reap orphans"], r"(?m)^id:\s*reap\s*$")
        self.assertRegex(reap_dict["reap orphans"], r"(?m)^shell:\s*bash\s*$")

        report_section = live[live.index("\n  report:\n") :]
        self.assertNotIn("container:", report_section)
        self.assertRegex(report_section, r"(?m)^\s*needs:\s*reap\s*$")
        self.assertRegex(
            report_section,
            r"(?m)^\s*if:\s*failure\(\)\s*&&\s*needs\.reap\.outputs\.failed\s*==\s*'true'\s*$",
        )
        self.assertIn("GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}", report_section)
        report_steps = _report_steps(text)
        self.assertEqual([label for label, _ in report_steps], ["report issue"])

    def _run_reap_job(self, tmp, env_overrides, py_rc=0, py_stdout="", py_stderr=""):
        steps = dict(_image_steps(REAP_YML.read_text()))
        confirm_script = _run_block(steps["check confirm"])
        reap_script = _run_block(steps["reap orphans"])

        py_calls = tmp / "py.calls"
        py_bin = tmp / "bin" / "python"
        _write_exe(
            py_bin,
            "#!/usr/bin/env python3\n"
            "import pathlib, sys\n"
            f"with open({str(py_calls)!r}, 'a') as fh:\n"
            "    fh.write(' '.join(sys.argv[1:]) + '\\n')\n"
            f"sys.stdout.write({py_stdout!r})\n"
            f"sys.stderr.write({py_stderr!r})\n"
            f"sys.exit({py_rc})\n",
        )
        gh_out = tmp / "github_output"
        gh_out.write_text("")
        base_env = {
            **os.environ,
            "PATH": f"{py_bin.parent}:{os.environ['PATH']}",
            "REAP_TENANTS": "TBCMP,TE",
            "EVENT_NAME": "workflow_dispatch",
            "INPUT_COMMIT": "true",
            "REAP_COMMIT": "true",
            "REAP_SESSIONS": "",
            "REAP_SESSIONS_TENANT": "",
            "REAP_CONFIRM": "",
            "GITHUB_OUTPUT": str(gh_out),
            **env_overrides,
        }
        c_res = subprocess.run(
            ["sh", "-e", "-c", confirm_script], env=base_env, capture_output=True, text=True, check=False
        )
        if c_res.returncode != 0:
            return c_res, None, [], {}
        r_res = subprocess.run(
            ["bash", "-e", "-c", reap_script], env=base_env, capture_output=True, text=True, check=False
        )
        calls = py_calls.read_text().splitlines() if py_calls.exists() else []
        outputs = _parse_github_output(gh_out.read_text())
        return c_res, r_res, calls, outputs

    def _run_report_job(self, tmp, reap_rc, outputs, existing_issue=""):
        text = REAP_YML.read_text()
        if_m = re.search(r"(?m)^\s*if:\s*(.+)$", text[text.index("\n  report:\n") :])
        self.assertIsNotNone(if_m)
        gh_log = tmp / "gh.log"
        body_copy = tmp / "captured_body.md"
        if not _eval_report_if(if_m.group(1).strip(), reap_rc, outputs):
            return None, [], ""
        report_script = _run_block(dict(_report_steps(text))["report issue"])
        gh_bin = tmp / "bin" / "gh"
        _write_exe(
            gh_bin,
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$*\" >> {str(gh_log)!r}\n"
            'if [ "$1" = "issue" ] && [ "$2" = "list" ]; then\n'
            f"  printf '%s\\n' {existing_issue!r}\n"
            "fi\n"
            'prev=""\n'
            'for a in "$@"; do\n'
            '  if [ "$prev" = "--body-file" ]; then\n'
            f'    cp "$a" {str(body_copy)!r}\n'
            "  fi\n"
            '  prev="$a"\n'
            "done\n",
        )
        runner_tmp = tmp / "runner_temp"
        res = subprocess.run(
            ["bash", "-e", "-c", report_script],
            env={
                **os.environ,
                "PATH": f"{gh_bin.parent}:{os.environ['PATH']}",
                "RUNNER_TEMP": str(runner_tmp),
                "GH_TOKEN": "tok",
                "REAP_SUMMARY": outputs.get("summary", ""),
                "RUN_URL": "https://github.com/wiz-training/te-runner/actions/runs/99999",
            },
            capture_output=True,
            text=True,
            check=False,
        )
        logged = gh_log.read_text().splitlines() if gh_log.exists() else []
        body = body_copy.read_text() if body_copy.exists() else ""
        return res, logged, body

    def test_manual_sessions_confirm_gate_contract(self):
        """OP-3: manual dispatch with non-empty sessions + commit=true requires confirm == resolved tenant
        (exact match, case-sensitive to match reap_orphans.py/wizlab.core); schedule, dry runs, and empty
        sessions are unaffected; confirm refusal runs zero reaper calls and files no issue."""
        # (case, env_overrides, want_confirm_rc, want_py_args_or_none)
        rows = (
            (
                "sessions+commit without confirm refuses",
                {"EVENT_NAME": "workflow_dispatch", "INPUT_COMMIT": "true", "REAP_SESSIONS": "ghtvgsxcgphw",
                 "REAP_SESSIONS_TENANT": "", "REAP_CONFIRM": ""},
                1,
                None,
            ),
            (
                "wrong explicit tenant in confirm refuses",
                {"EVENT_NAME": "workflow_dispatch", "INPUT_COMMIT": "true", "REAP_SESSIONS": "ghtvgsxcgphw",
                 "REAP_SESSIONS_TENANT": "TE", "REAP_CONFIRM": "TBCMP"},
                1,
                None,
            ),
            (
                "wrong default tenant in confirm refuses",
                {"EVENT_NAME": "workflow_dispatch", "INPUT_COMMIT": "true", "REAP_SESSIONS": "ghtvgsxcgphw",
                 "REAP_SESSIONS_TENANT": "", "REAP_CONFIRM": "TE"},
                1,
                None,
            ),
            (
                "lowercase confirm against default TBCMP refuses (exact match)",
                {"EVENT_NAME": "workflow_dispatch", "INPUT_COMMIT": "true", "REAP_SESSIONS": "ghtvgsxcgphw",
                 "REAP_SESSIONS_TENANT": "", "REAP_CONFIRM": "tbcmp"},
                1,
                None,
            ),
            (
                "lowercase confirm against explicit TE refuses (exact match)",
                {"EVENT_NAME": "workflow_dispatch", "INPUT_COMMIT": "true", "REAP_SESSIONS": "ghtvgsxcgphw",
                 "REAP_SESSIONS_TENANT": "TE", "REAP_CONFIRM": "te"},
                1,
                None,
            ),
            (
                "uppercase confirm against lowercase tenant refuses (exact match)",
                {"EVENT_NAME": "workflow_dispatch", "INPUT_COMMIT": "true", "REAP_SESSIONS": "ghtvgsxcgphw",
                 "REAP_SESSIONS_TENANT": "te", "REAP_CONFIRM": "TE"},
                1,
                None,
            ),
            (
                "right default tenant runs with --commit",
                {"EVENT_NAME": "workflow_dispatch", "INPUT_COMMIT": "true", "REAP_SESSIONS": "ghtvgsxcgphw",
                 "REAP_SESSIONS_TENANT": "", "REAP_CONFIRM": "TBCMP"},
                0,
                "/opt/reaper/reap_orphans.py --commit",
            ),
            (
                "right explicit tenant runs with --commit",
                {"EVENT_NAME": "workflow_dispatch", "INPUT_COMMIT": "true", "REAP_SESSIONS": "ghtvgsxcgphw",
                 "REAP_SESSIONS_TENANT": "TE", "REAP_CONFIRM": "TE"},
                0,
                "/opt/reaper/reap_orphans.py --commit",
            ),
            (
                "whitespace/comma-only sessions requires no confirm",
                {"EVENT_NAME": "workflow_dispatch", "INPUT_COMMIT": "true", "REAP_SESSIONS": " , ",
                 "REAP_SESSIONS_TENANT": "", "REAP_CONFIRM": ""},
                0,
                "/opt/reaper/reap_orphans.py --commit",
            ),
            (
                "dry run with sessions and no confirm is unaffected",
                {"EVENT_NAME": "workflow_dispatch", "INPUT_COMMIT": "false", "REAP_SESSIONS": "ghtvgsxcgphw",
                 "REAP_SESSIONS_TENANT": "", "REAP_CONFIRM": ""},
                0,
                "/opt/reaper/reap_orphans.py",
            ),
            (
                "scheduled run is unaffected",
                {"EVENT_NAME": "schedule", "INPUT_COMMIT": "true", "REAP_COMMIT": "true",
                 "REAP_SESSIONS": "ghtvgsxcgphw", "REAP_SESSIONS_TENANT": "", "REAP_CONFIRM": ""},
                0,
                "/opt/reaper/reap_orphans.py --commit",
            ),
        )
        for name, overrides, want_rc, want_py in rows:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as d:
                tmp = pathlib.Path(d)
                c_res, r_res, py_calls, outputs = self._run_reap_job(tmp, overrides)
                self.assertEqual(c_res.returncode, want_rc, c_res.stdout + c_res.stderr)
                if want_rc != 0:
                    self.assertIn("::error::", c_res.stdout + c_res.stderr)
                    self.assertIn("nothing was reaped", c_res.stdout + c_res.stderr)
                    self.assertIsNone(r_res)
                    self.assertEqual(py_calls, [])
                    rep_res, gh_calls, _ = self._run_report_job(tmp, c_res.returncode, outputs)
                    self.assertIsNone(rep_res)
                    self.assertEqual(gh_calls, [])
                else:
                    self.assertEqual(r_res.returncode, 0, r_res.stdout + r_res.stderr)
                    self.assertEqual(py_calls, [want_py])

    def test_residue_reporting_contract(self):
        """OP-2: residue creates or comments on one 'reaper residue' issue carrying OPERATOR/FAILED lines
        and the run URL; clean runs make zero gh calls; masked/empty summary falls back cleanly."""
        residue_out = (
            "# tenant TBCMP (tid:tbcmp): 1 stopped session(s) in last 48h, 0 tenant-side (1 total)\n"
            "OPERATOR CreateReport 'lab-abc123def456-r': manual cleanup required\n"
            "operator sweep Outpost 'lab-abc123def456' (partially_uninstalled; deleteOutpost refused)\n"
            "FAILED CreateConnector 'lab-xyz987654321-c': denied\n"
        )
        residue_err = (
            "reap_orphans: lab-abc123def456@ deleted; residue needs an operator — see the wizlab line above\n"
            "reap_orphans: 2 session(s) left residue no pass can clear; finish with "
            'REAP_SESSIONS_TENANT=TBCMP REAP_SESSIONS="abc123def456,xyz987654321"\n'
        )
        # (case, env_overrides, py_rc, py_out, py_err, existing_issue, want_gh_cmds, want_in_body)
        rows = (
            (
                "clean scheduled run makes no gh calls",
                {"EVENT_NAME": "schedule", "REAP_COMMIT": "true"},
                0,
                "# tenant TBCMP (tid:tbcmp): 0 stopped session(s) in last 48h\n",
                "# 0/0 session(s) reaped\n",
                "",
                [],
                [],
            ),
            (
                "residue on scheduled run creates one issue when none open",
                {"EVENT_NAME": "schedule", "REAP_COMMIT": "true"},
                1,
                residue_out,
                residue_err,
                "",
                ["label create reaper", "issue list --state open --label reaper", "issue create --label reaper"],
                [
                    "https://github.com/wiz-training/te-runner/actions/runs/99999",
                    "OPERATOR CreateReport 'lab-abc123def456-r'",
                    "operator sweep Outpost 'lab-abc123def456'",
                    "FAILED CreateConnector 'lab-xyz987654321-c'",
                    'REAP_SESSIONS_TENANT=TBCMP REAP_SESSIONS="abc123def456,xyz987654321"',
                ],
            ),
            (
                "residue comments on existing open issue instead of creating a second",
                {"EVENT_NAME": "schedule", "REAP_COMMIT": "true"},
                1,
                residue_out,
                residue_err,
                "77",
                ["label create reaper", "issue list --state open --label reaper", "issue comment 77"],
                ["https://github.com/wiz-training/te-runner/actions/runs/99999", "OPERATOR CreateReport"],
            ),
            (
                "residue on confirmed manual run also files an issue",
                {"EVENT_NAME": "workflow_dispatch", "INPUT_COMMIT": "true",
                 "REAP_SESSIONS": "abc123def456", "REAP_SESSIONS_TENANT": "TE", "REAP_CONFIRM": "TE"},
                1,
                "FAILED CreateReport 'lab-abc123def456-r': denied\n",
                'reap_orphans: 1 session(s) left residue; REAP_SESSIONS_TENANT=TE REAP_SESSIONS="abc123def456"\n',
                "",
                ["label create reaper", "issue list --state open --label reaper", "issue create --label reaper"],
                ["FAILED CreateReport 'lab-abc123def456-r': denied"],
            ),
            (
                "non-zero exit with no OPERATOR/FAILED lines still reports fallback",
                {"EVENT_NAME": "schedule", "REAP_COMMIT": "true"},
                2,
                "unexpected traceback\n",
                "",
                "",
                ["label create reaper", "issue list --state open --label reaper", "issue create --label reaper"],
                ["reap_orphans exited 2"],
            ),
        )
        for name, overrides, py_rc, py_out, py_err, existing, want_gh, want_body in rows:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as d:
                tmp = pathlib.Path(d)
                c_res, r_res, _, outputs = self._run_reap_job(tmp, overrides, py_rc=py_rc, py_stdout=py_out,
                                                              py_stderr=py_err)
                self.assertEqual(c_res.returncode, 0, c_res.stdout + c_res.stderr)
                self.assertEqual(r_res.returncode, py_rc, r_res.stdout + r_res.stderr)
                if py_out:
                    self.assertIn(py_out.splitlines()[0], r_res.stdout)
                rep_res, gh_calls, body = self._run_report_job(tmp, r_res.returncode, outputs, existing_issue=existing)
                self.assertEqual(len(gh_calls), len(want_gh), gh_calls)
                if want_gh:
                    self.assertEqual(rep_res.returncode, 0, rep_res.stdout + rep_res.stderr)
                    for got, want_sub in zip(gh_calls, want_gh, strict=True):
                        self.assertIn(want_sub, got)
                    self.assertIn('"reaper residue" in:title', gh_calls[1])
                    if not existing:
                        self.assertIn("--title reaper residue", gh_calls[2])
                    for snippet in want_body:
                        self.assertIn(snippet, body)

    def test_summary_is_capped_and_empty_output_uses_fallback_body(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = pathlib.Path(d)
            many = "".join(f"FAILED line {i:03d}\n" for i in range(80))
            _, r_res, _, outputs = self._run_reap_job(tmp, {"EVENT_NAME": "schedule"}, py_rc=1, py_stdout=many)
            self.assertEqual(r_res.returncode, 1)
            self.assertEqual(outputs.get("failed"), "true")
            lines = outputs["summary"].splitlines()
            self.assertEqual(len(lines), 60)
            self.assertLessEqual(len(outputs["summary"].encode("utf-8")), 4000)

            # Simulate GitHub dropping `summary` (e.g. masked secret) while `failed == 'true'`.
            rep_res, gh_calls, body = self._run_report_job(tmp, 1, {"failed": "true", "summary": ""})
            self.assertEqual(rep_res.returncode, 0, rep_res.stdout + rep_res.stderr)
            self.assertEqual(len(gh_calls), 3)
            self.assertIn("https://github.com/wiz-training/te-runner/actions/runs/99999", body)
            self.assertIn("no summary output was passed", body)


if __name__ == "__main__":
    unittest.main()
