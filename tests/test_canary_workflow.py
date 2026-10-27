"""Structural and shell-step tests for `.github/workflows/canary.yml`.

Follows the stdlib-only parser style of `tests/test_build_workflow.py`: parses job steps from indentation and
executes the extracted `run:` shell/python blocks against stubbed `wizops` and `gh` binaries with no live
Instruqt or GitHub calls.
"""
import os
import pathlib
import re
import stat
import subprocess
import sys
import tempfile
import typing
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_build_workflow import REAP_YML, ROOT, _image_steps, _live, _run_block

CANARY_YML = ROOT / ".github" / "workflows" / "canary.yml"


def _cron_hour(text):
    m = re.search(r'cron:\s*"0\s+(\d+)\s+\*\s+\*\s+\*"', _live(text))
    return int(m.group(1)) if m else None


class CanaryWorkflowTests(unittest.TestCase):
    SAMPLE_OK = (
        "PLAY_URL=https://play.instruqt.com/embed/wiz/labs/te-dev-aws?token=pt_1\n"
        "STARTED_AT=2026-10-24T05:04:12Z\n"
        "RESUMED=no\n"
        "GREP=CANARY=ok\n"
        "GREP=CANARY_RUNNER=v0.1.82\n"
        "GREP=CANARY_SESSION=ok\n"
        "GREP=CANARY_USER=ok\n"
        "GREP=CANARY_VERBS=46\n"
        "STOPPED=yes\n"
    )
    SAMPLE_DEGRADED = SAMPLE_OK.replace("GREP=CANARY=ok\n", "GREP=CANARY=degraded\n").replace(
        "GREP=CANARY_USER=ok\n", "GREP=CANARY_USER=ok;inspect:absent\n"
    )
    SAMPLE_NO_VERDICT = (
        "PLAY_URL=https://play.instruqt.com/embed/wiz/labs/te-dev-aws?token=pt_1\n"
        "STARTED_AT=2026-10-24T05:04:12Z\n"
        "RESUMED=no\n"
        "GREP=none\n"
        "STOPPED=yes\n"
    )
    ERR_NO_SANDBOX = "wizops: play: start failed (Enter lab wait (175s): Lab failed to start)\n"
    ERR_OTHER = "wizops: play: start failed (Start/Resume lab wait (30s): TimeoutError)\n"

    # (name, rc, attempts, stdout, err1, err2, expected_outcome, expected_exit)
    OUTCOME_ROWS: typing.ClassVar = (
        ("ok", 0, 1, SAMPLE_OK, "", "", "ok", 0),
        ("degraded", 0, 1, SAMPLE_DEGRADED, "", "", "degraded", 1),
        ("no-verdict", 0, 1, SAMPLE_NO_VERDICT, "", "", "no-verdict", 1),
        ("no-sandbox", 1, 2, "", ERR_NO_SANDBOX, ERR_NO_SANDBOX, "no-sandbox", 1),
        ("error on single non-sandbox failure", 1, 1, "", ERR_OTHER, "", "error", 1),
    )

    def test_workflow_parses_and_runs_on_host_before_reap_cron(self):
        text = CANARY_YML.read_text()
        live = _live(text)
        self.assertIn("runs-on: ubuntu-24.04", live)
        self.assertNotIn("container:", live)
        self.assertNotIn("save-always:", live)
        # `runner.*` is not a context GitHub evaluates in a job-level `env:`; a dispatch of such a file
        # is refused with HTTP 422 "Unrecognized named-value: 'runner'" (first live dispatch, 2026-10-24).
        job_env = live.split("    steps:")[0]
        self.assertNotIn("runner.", job_env)
        self.assertIn('WIZLAB_PLAY_DIR=$RUNNER_TEMP', live)
        self.assertIn('CANARY_STATE_DIR=$RUNNER_TEMP', live)
        canary_hr = _cron_hour(text)
        reap_hr = _cron_hour(REAP_YML.read_text())
        self.assertIsNotNone(canary_hr)
        self.assertIsNotNone(reap_hr)
        self.assertLess(canary_hr, reap_hr)
        steps = [label for label, _ in _image_steps(text)]
        self.assertEqual(
            steps,
            [
                "paths",
                "check required secrets",
                "checkout te-runner",
                "checkout te-labkit-v2",
                "setup python",
                "install playwright",
                "cache browser profile",
                "clean",
                "boot",
                "verdict",
                "report issue",
            ],
        )

    def test_concurrency_group_is_per_lab_and_does_not_cancel_in_progress(self):
        live = _live(CANARY_YML.read_text())
        self.assertRegex(
            live,
            r"concurrency:\s*\n\s*group:\s*canary-\$\{\{\s*inputs\.lab\s*\|\|\s*'te-dev-aws'\s*\}\}\s*\n\s*cancel-in-progress:\s*false",
        )

    def test_step_1_refuses_when_labkit_read_token_is_missing(self):
        steps = dict(_image_steps(CANARY_YML.read_text()))
        script = _run_block(steps["check required secrets"])
        bad = subprocess.run(
            ["sh", "-e", "-c", script],
            env={**os.environ, "LABKIT_READ_TOKEN": "", "INSTRUQT_API": "tok"},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(bad.returncode, 1)
        self.assertIn("LABKIT_READ_TOKEN", bad.stdout + bad.stderr)

    def test_verdict_warns_and_notes_when_canary_runner_lags_newest_tag(self):
        steps = dict(_image_steps(CANARY_YML.read_text()))
        self.assertIn("checkout te-runner", steps)
        verdict_script = _run_block(steps["verdict"])
        ls_tags = (
            "7d1e9d4f583f866140cb574e2d0fcb986b49edd4\trefs/tags/v0.1.85\n"
            "aa16c400f1709cdd8196dde93eb299c31700ae91\trefs/tags/v0.1.88\n"
            "b8d2ae7dc0a05eb2e2964e95c7ec11aa7ef539ad\trefs/tags/v0.1.9\n"
        )
        ok_current = self.SAMPLE_OK.replace("CANARY_RUNNER=v0.1.82", "CANARY_RUNNER=v0.1.88")
        cases = (
            ("lags newest tag", self.SAMPLE_OK, 0, ls_tags, True, "ok", 0),
            ("matches newest tag", ok_current, 0, ls_tags, False, "ok", 0),
            ("no CANARY_RUNNER line", self.SAMPLE_NO_VERDICT, 0, ls_tags, False, "no-verdict", 1),
            ("git ls-remote fails", self.SAMPLE_OK, 128, "", False, "ok", 0),
        )
        for name, boot_out, git_rc, git_out, want_lag, want_outcome, want_exit in cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as d:
                tmp = pathlib.Path(d)
                state = tmp / "state"
                state.mkdir()
                summary = tmp / "step_summary.md"
                (state / "boot.rc").write_text("0\n")
                (state / "boot.attempts").write_text("1\n")
                (state / "boot.out").write_text(boot_out)
                (state / "boot.err").write_text("")
                git_bin = tmp / "bin" / "git"
                git_bin.parent.mkdir()
                git_args = tmp / "git.args"
                git_stdout = tmp / "git.out"
                git_stdout.write_text(git_out)
                git_bin.write_text(
                    "#!/bin/sh\n"
                    f"printf '%s\\n' \"$*\" >> {str(git_args)!r}\n"
                    f"cat {str(git_stdout)!r}\n"
                    f"exit {git_rc}\n"
                )
                git_bin.chmod(git_bin.stat().st_mode | stat.S_IXUSR)
                res = subprocess.run(
                    ["sh", "-e", "-c", verdict_script],
                    cwd=tmp,
                    env={
                        **os.environ,
                        "PATH": f"{git_bin.parent}:{os.environ['PATH']}",
                        "LAB": "te-dev-aws",
                        "CANARY_STATE_DIR": str(state),
                        "GITHUB_STEP_SUMMARY": str(summary),
                    },
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(res.returncode, want_exit, res.stdout + res.stderr)
                self.assertEqual((state / "outcome").read_text(), want_outcome)
                if want_lag:
                    self.assertIn("ls-remote --tags --refs origin refs/tags/v*", git_args.read_text())
                    self.assertRegex(res.stdout, r"(?m)^::warning::.*v0\.1\.82.*v0\.1\.88")
                    self.assertIn("v0.1.82", summary.read_text())
                    self.assertIn("v0.1.88", summary.read_text())
                    self.assertNotIn("v0.1.9", summary.read_text())
                else:
                    self.assertNotIn("::warning::", res.stdout)
                    self.assertNotIn("newest `te-runner` tag", summary.read_text())

    def test_boot_retries_once_only_on_lab_failed_to_start(self):
        steps = dict(_image_steps(CANARY_YML.read_text()))
        boot_script = _run_block(steps["boot"])
        cases = (
            ("no-sandbox retries once", [1, 1], [self.ERR_NO_SANDBOX, self.ERR_NO_SANDBOX], 2, "1"),
            ("no-sandbox recovers on retry", [1, 0], [self.ERR_NO_SANDBOX, ""], 2, "0"),
            ("other failure does not retry", [1, 0], [self.ERR_OTHER, ""], 1, "1"),
            ("first-try success does not retry", [0, 0], ["", ""], 1, "0"),
        )
        for name, codes, errs, want_calls, want_rc in cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as d:
                tmp = pathlib.Path(d)
                wizops = tmp / "labkit" / "scripts" / "wizops"
                wizops.parent.mkdir(parents=True)
                calls_file = tmp / "calls"
                wizops.write_text(
                    "#!/usr/bin/env python3\n"
                    "import pathlib, sys\n"
                    f"p = pathlib.Path({str(calls_file)!r})\n"
                    "n = int(p.read_text()) if p.exists() else 0\n"
                    "p.write_text(str(n + 1))\n"
                    f"codes = {codes!r}\n"
                    f"errs = {errs!r}\n"
                    "if errs[n]: sys.stderr.write(errs[n])\n"
                    f"if codes[n] == 0: sys.stdout.write({self.SAMPLE_OK!r})\n"
                    "sys.exit(codes[n])\n"
                )
                wizops.chmod(wizops.stat().st_mode | stat.S_IXUSR)
                state = tmp / "state"
                res = subprocess.run(
                    ["sh", "-e", "-c", boot_script],
                    cwd=tmp,
                    env={**os.environ, "LAB": "te-dev-aws", "CANARY_STATE_DIR": str(state)},
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(res.returncode, 0, res.stderr)
                self.assertEqual(int(calls_file.read_text()), want_calls)
                self.assertEqual((state / "boot.rc").read_text().strip(), want_rc)
                self.assertEqual(int((state / "boot.attempts").read_text().strip()), want_calls)

    def test_verdict_maps_five_outcomes_and_writes_step_summary(self):
        steps = dict(_image_steps(CANARY_YML.read_text()))
        verdict_script = _run_block(steps["verdict"])
        for name, rc, attempts, out, err1, err2, want_outcome, want_exit in self.OUTCOME_ROWS:
            with self.subTest(outcome=name), tempfile.TemporaryDirectory() as d:
                state = pathlib.Path(d) / "state"
                state.mkdir()
                summary = pathlib.Path(d) / "step_summary.md"
                (state / "boot.rc").write_text(f"{rc}\n")
                (state / "boot.attempts").write_text(f"{attempts}\n")
                (state / "boot.out").write_text(out)
                (state / "attempt1.err").write_text(err1)
                (state / "attempt2.err").write_text(err2)
                (state / "boot.err").write_text(err2 or err1)
                res = subprocess.run(
                    ["sh", "-e", "-c", verdict_script],
                    cwd=d,
                    env={
                        **os.environ,
                        "LAB": "te-dev-aws",
                        "CANARY_STATE_DIR": str(state),
                        "GITHUB_STEP_SUMMARY": str(summary),
                    },
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(res.returncode, want_exit, res.stdout + res.stderr)
                self.assertEqual((state / "outcome").read_text(), want_outcome)
                self.assertIn(f"`{want_outcome}`", summary.read_text())

    def test_issue_step_never_issues_on_ok_and_creates_or_comments_on_failures(self):
        steps = dict(_image_steps(CANARY_YML.read_text()))
        issue_script = _run_block(steps["report issue"])
        cases = (
            ("ok makes no gh calls", "ok", "", []),
            ("error makes no gh calls", "error", "", []),
            ("degraded creates issue when none open", "degraded", "",
             ["label create", "issue list", "issue create"]),
            ("no-verdict creates issue when none open", "no-verdict", "",
             ["label create", "issue list", "issue create"]),
            ("no-sandbox comments when open issue exists", "no-sandbox", "42",
             ["label create", "issue list", "issue comment 42"]),
        )
        for name, outcome, existing_num, want_cmds in cases:
            with self.subTest(case=name), tempfile.TemporaryDirectory() as d:
                tmp = pathlib.Path(d)
                state = tmp / "state"
                state.mkdir()
                (state / "outcome").write_text(outcome)
                (state / "summary.md").write_text(f"### Canary: `te-dev-aws` — `{outcome}`\n")
                log_file = tmp / "gh.log"
                gh = tmp / "bin" / "gh"
                gh.parent.mkdir()
                gh.write_text(
                    "#!/bin/sh\n"
                    f"printf '%s\\n' \"$*\" >> {str(log_file)!r}\n"
                    'if [ "$1" = "issue" ] && [ "$2" = "list" ]; then\n'
                    f"  printf '%s\\n' {existing_num!r}\n"
                    "fi\n"
                )
                gh.chmod(gh.stat().st_mode | stat.S_IXUSR)
                res = subprocess.run(
                    ["sh", "-e", "-c", issue_script],
                    env={
                        **os.environ,
                        "PATH": f"{gh.parent}:{os.environ['PATH']}",
                        "LAB": "te-dev-aws",
                        "CANARY_STATE_DIR": str(state),
                    },
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(res.returncode, 0, res.stderr)
                logged = log_file.read_text().splitlines() if log_file.exists() else []
                self.assertEqual(len(logged), len(want_cmds), logged)
                for got, want_sub in zip(logged, want_cmds, strict=True):
                    self.assertIn(want_sub, got)

    def test_all_workflows_pin_ubuntu_24_04_and_current_node24_actions(self):
        wf_dir = ROOT / ".github" / "workflows"
        files = sorted(wf_dir.glob("*.yml"))
        self.assertTrue(files)
        for p in files:
            live = _live(p.read_text())
            for banned in ("ubuntu-latest", "checkout@v4", "setup-python@v5"):
                self.assertNotIn(banned, live, f"{p.name} still contains {banned!r}")


if __name__ == "__main__":
    unittest.main()
