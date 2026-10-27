#!/usr/bin/env python3
import importlib.util
import io
import pathlib
import unittest
from unittest import mock

_spec = importlib.util.spec_from_file_location(
    "reap_orphans", pathlib.Path(__file__).resolve().parent / "reap_orphans.py")
rp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rp)


class SessionDiscovery(unittest.TestCase):
    def test_paginates_until_a_short_page(self):
        pages = [
            {"labPlayReports": {"items": [{"id": "a", "stoppedReason": "done"},
                                            {"id": "live", "stoppedReason": None}]}},
            {"labPlayReports": {"items": [{"id": "b", "stoppedReason": "timeout"}]}},
        ]
        with mock.patch.object(rp, "PAGE_SIZE", 2), \
             mock.patch.object(rp, "_instruqt", side_effect=pages) as api:
            self.assertEqual(rp.stopped_sessions("tid:x"), ["a", "b"])
        self.assertEqual([c.args[1]["skip"] for c in api.call_args_list], [0, 2])

    def test_refuses_to_page_forever_when_skip_is_ignored(self):
        full = {"labPlayReports": {"items": [{"id": "a", "stoppedReason": "done"}] * 2}}
        with mock.patch.object(rp, "PAGE_SIZE", 2), mock.patch.object(rp, "MAX_PAGES", 3), \
             mock.patch.object(rp, "_instruqt", return_value=full) as api, \
             self.assertRaises(SystemExit) as cm:
            rp.stopped_sessions("tid:x")
        self.assertEqual(api.call_count, 3)
        self.assertEqual(cm.exception.code, 1)


class TenantSelection(unittest.TestCase):
    def test_each_listed_tenant_selects_its_own_tag(self):
        """The dict was baked, so a second tenant was a code edit plus an image release; its tag is a
        pure function of the key wizlab keys creds on, so the list is the only input."""
        for spec, want in [("TBCMP", {"TBCMP": "tid:tbcmp"}),
                           ("TBCMP, te", {"TBCMP": "tid:tbcmp", "TE": "tid:te"}),
                           ("", {})]:
            with self.subTest(spec=spec):
                self.assertEqual(rp._tenants(spec), want)


class ReapOrdering(unittest.TestCase):
    def test_what_each_wizlab_exit_leaves_behind(self):
        """A run whose only residue was one Outpost mid-uninstall exited 1 and paged every night, for
        what is the documented multi-pass Outpost lifecycle. wizlab routes the two apart: 4 is cleanup a
        later pass finishes on its own, 3 needs a human (wizlab.reap._reap_exit)."""
        reap, delete = ("user", "reap"), ("user", "delete")
        for codes, want, calls in [((0, 0), rp.DONE, [reap, delete]),
                                   ((4,), rp.DEFERRED, [reap]),
                                   ((3,), rp.FAILED, [reap]),
                                   # 5 is residue the user record is no handle to, so the record goes
                                   # and the run stays red for the operator step.
                                   ((5, 0), rp.FAILED, [reap, delete]),
                                   ((5, 1), rp.FAILED, [reap, delete]),
                                   # The user is the only handle back to the footprint, so a delete that
                                   # failed is not cleanup done.
                                   ((0, 1), rp.FAILED, [reap, delete])]:
            with self.subTest(codes=codes), mock.patch.object(rp, "_wizlab", side_effect=codes) as wizlab:
                self.assertEqual(rp._reap_session("T", "s1", True), want)
                self.assertEqual([c.args[1:3] for c in wizlab.call_args_list], calls)
        # --domain rides on both: the audit-log performer and the Wiz user record are the same email.
        with mock.patch.dict(rp.os.environ, {"REAP_DOMAIN": "x.io"}), \
             mock.patch.object(rp, "_wizlab", side_effect=(0, 0)) as wizlab:
            rp._reap_session("T", "s1", True)
        for args in (list(c.args) for c in wizlab.call_args_list):
            self.assertIn("--domain", args)

    def test_reap_session_derives_last_min_from_window_hours(self):
        """reap_orphans queried 48h of stopped sessions (WINDOW_H) while calling `wizlab user reap`
        without `--last-min`, leaving the audit walk at cli.py's 1440m (24h) default."""
        with mock.patch.dict(rp.os.environ, {}, clear=True), \
             mock.patch.object(rp, "_wizlab", side_effect=(0, 0)) as wizlab:
            rp._reap_session("T", "s1", True)
        reap_args = list(wizlab.call_args_list[0].args)
        i = reap_args.index("--last-min")
        self.assertEqual(reap_args[i:i + 2], ["--last-min", "2880"])
        self.assertNotIn("--last-min", wizlab.call_args_list[1].args)
        with mock.patch.dict(rp.os.environ, {"REAP_WINDOW_HOURS": "72"}, clear=True), \
             mock.patch.object(rp, "_wizlab", side_effect=(0, 0)) as wizlab:
            rp._reap_session("T", "s1", True)
        reap_args = list(wizlab.call_args_list[0].args)
        i = reap_args.index("--last-min")
        self.assertEqual(reap_args[i:i + 2], ["--last-min", "4320"])

    def test_slot_claims_are_listed_and_inspected_at_most_once_per_commit_run(self):
        """Across multi-tenant and dual-tagged sessions, reap_orphans sweeps GitHub slot claims once
        in-process (and passes WIZLAB_SKIP_SLOT_SWEEP=1 to per-session wizlab subprocesses), including
        expired claims when zero sessions stopped."""
        sys_path = str(pathlib.Path(__file__).resolve().parent.parent / "tests")
        if sys_path not in rp.sys.path:
            rp.sys.path.insert(0, sys_path)
        import test_wizlab as tw

        gh = tw.FakeGitHub(
            claims={"01": "lab-ghtvgsxcgphw", "02": "lab-k8m01846391n", "03": "lab-live", "04": "lab-old"},
            claim_ages={"03": 30, "04": tw.wz.CLAIM_TTL_S + 60},
        )
        ok_proc = rp.subprocess.CompletedProcess(["wizlab"], 0, stdout="", stderr="")
        with mock.patch.object(rp, "TENANTS", {"TBCMP": "tid:tbcmp", "TE": "tid:te"}), \
             mock.patch.object(rp, "stopped_sessions",
                               side_effect=[["ghtvgsxcgphw", "bapr82q9xmm5"], ["ghtvgsxcgphw", "k8m01846391n"]]), \
             mock.patch.object(rp.subprocess, "run", return_value=ok_proc) as proc, \
             mock.patch.object(rp._codelab, "_gh_call", gh), \
             mock.patch.dict(rp.os.environ, dict(tw.GitHubContract.GH_ENV), clear=True), \
             mock.patch.object(rp.sys, "argv", ["reap_orphans.py", "--commit"]), \
             mock.patch.object(rp.sys, "stdout", io.TextIOWrapper(io.BytesIO())), \
             mock.patch.object(rp.sys, "stderr", io.StringIO()) as err:
            rp.main()

        reap_calls = [c for c in proc.call_args_list if c.args[0][1:3] == ["user", "reap"]]
        self.assertEqual(len(reap_calls), 4)
        for c in reap_calls:
            self.assertEqual(c.kwargs["env"].get("WIZLAB_SKIP_SLOT_SWEEP"), "1")
        matching_gets = [p for m, p in gh.calls if m == "GET" and "/git/matching-refs/" in p]
        commit_gets = [p for m, p in gh.calls if m == "GET" and "/git/commits/" in p]
        self.assertEqual(len(matching_gets), 1)
        self.assertEqual(len(commit_gets), 4)
        self.assertEqual(gh.claims, {"03": "lab-live"})
        self.assertIn("# slot claims: 3 removed, 0 absent", err.getvalue())

        # Zero stopped sessions in window still sweeps expired claims once.
        gh_zero = tw.FakeGitHub(claims={"01": "lab-old", "02": "lab-live"},
                                claim_ages={"01": tw.wz.CLAIM_TTL_S + 60, "02": 30})
        with mock.patch.object(rp, "TENANTS", {"TE": "tid:te"}), \
             mock.patch.object(rp, "stopped_sessions", return_value=[]), \
             mock.patch.object(rp.subprocess, "run", return_value=ok_proc) as proc_zero, \
             mock.patch.object(rp._codelab, "_gh_call", gh_zero), \
             mock.patch.dict(rp.os.environ, dict(tw.GitHubContract.GH_ENV), clear=True), \
             mock.patch.object(rp.sys, "argv", ["reap_orphans.py", "--commit"]), \
             mock.patch.object(rp.sys, "stdout", io.TextIOWrapper(io.BytesIO())), \
             mock.patch.object(rp.sys, "stderr", io.StringIO()) as err_zero:
            rp.main()
        proc_zero.assert_not_called()
        self.assertEqual(len([p for m, p in gh_zero.calls if m == "GET" and "/git/matching-refs/" in p]), 1)
        self.assertEqual(gh_zero.claims, {"02": "lab-live"})
        self.assertIn("# slot claims: 1 removed, 0 absent", err_zero.getvalue())

        # A dry run lists the same backlog through reads only and leaves every claim in place.
        gh_dry = tw.FakeGitHub(claims={"01": "lab-ghtvgsxcgphw", "02": "lab-live"},
                               claim_ages={"02": 30})
        with mock.patch.object(rp, "TENANTS", {"TE": "tid:te"}), \
             mock.patch.object(rp, "stopped_sessions", return_value=["ghtvgsxcgphw"]), \
             mock.patch.object(rp.subprocess, "run", return_value=ok_proc) as proc_dry, \
             mock.patch.object(rp._codelab, "_gh_call", gh_dry), \
             mock.patch.dict(rp.os.environ, dict(tw.GitHubContract.GH_ENV), clear=True), \
             mock.patch.object(rp.sys, "argv", ["reap_orphans.py"]), \
             mock.patch.object(rp.sys, "stdout", io.TextIOWrapper(io.BytesIO())) as out_dry, \
             mock.patch.object(rp.sys, "stderr", io.StringIO()) as err_dry:
            rp.main()
            out_dry.seek(0)
            dry_out = out_dry.read()
        proc_dry.assert_not_called()
        writes = [(m, p) for m, p in gh_dry.calls if m != "GET" and not p.endswith("/access_tokens")]
        self.assertEqual(writes, [])
        self.assertEqual(gh_dry.claims, {"01": "lab-ghtvgsxcgphw", "02": "lab-live"})
        self.assertIn("DRY-RUN would release slot", dry_out)
        self.assertIn("# slot claims: 1 to release (dry-run), 0 absent", err_dry.getvalue())

    def test_operator_residue_deletes_the_user_and_keeps_the_run_red(self):
        """An Outpost left PARTIALLY_UNINSTALLED by a lab's own cleanup (te-aws-outpost-trust-201,
        every play) made `wizlab user reap` exit 3, so the session's Wiz user was retained as "the only
        handle back to the leftovers". The Outpost is found by its name lab-<sid>, not through the user,
        so the retained user bought nothing and one accumulated per play until an operator deleted the
        Outpost in the console and re-dispatched the reaper (ghtvgsxcgphw, k8m01846391n). On 5 the
        record is deleted, the operator step is named, and the red run still names the sid."""
        def run(cmd, **kw):
            rc = 5 if cmd[1:3] == ["user", "reap"] else 0
            return rp.subprocess.CompletedProcess(cmd, rc, stdout="", stderr="")
        with mock.patch.object(rp, "TENANTS", {}), \
             mock.patch.object(rp.subprocess, "run", side_effect=run) as proc, \
             mock.patch.dict(rp.os.environ, {"REAP_SESSIONS": "ghtvgsxcgphw"}, clear=True), \
             mock.patch.object(rp.sys, "argv", ["reap_orphans.py", "--commit"]), \
             mock.patch.object(rp.sys, "stdout", io.TextIOWrapper(io.BytesIO())), \
             mock.patch.object(rp.sys, "stderr", io.StringIO()) as err, \
             self.assertRaises(SystemExit) as cm:
            rp.main()
        self.assertEqual(cm.exception.code, 1)
        self.assertEqual([c.args[0][1:3] for c in proc.call_args_list], [["user", "reap"], ["user", "delete"]])
        self.assertIn("reap_orphans: lab-ghtvgsxcgphw@ deleted; residue needs an operator", err.getvalue())
        self.assertIn('REAP_SESSIONS_TENANT=TBCMP REAP_SESSIONS="ghtvgsxcgphw"', err.getvalue())

    def test_manual_reap_sessions_rejects_non_play_id_before_calling_anything(self):
        for spec, bad in [("bapr", "bapr"), ("ghtvgsxcgphw,bapr", "bapr"), ("UPPERCASE123", "UPPERCASE123")]:
            with self.subTest(spec=spec), \
                 mock.patch.object(rp, "TENANTS", {"TBCMP": "tid:tbcmp"}), \
                 mock.patch.object(rp, "stopped_sessions") as stopped, \
                 mock.patch.object(rp, "_reap_session") as reap, \
                 mock.patch.object(rp.subprocess, "run") as proc, \
                 mock.patch.dict(rp.os.environ, {"REAP_SESSIONS": spec}, clear=True), \
                 mock.patch.object(rp.sys, "argv", ["reap_orphans.py", "--commit"]), \
                 mock.patch.object(rp.sys, "stderr", io.StringIO()) as err, \
                 self.assertRaises(SystemExit) as cm:
                rp.main()
            self.assertEqual(cm.exception.code, 1)
            self.assertIn(f"operator error: REAP_SESSIONS entry {bad} is not a play id", err.getvalue())
            stopped.assert_not_called()
            reap.assert_not_called()
            proc.assert_not_called()

    def test_a_hung_wizlab_costs_one_session_not_the_run(self):
        # Unhandled, TimeoutExpired left main() by traceback: every later session and tenant was skipped
        # and the REAP_SESSIONS retry hint never printed. 3 is what _reap_session reads as FAILED.
        boom = rp.subprocess.TimeoutExpired(["wizlab"], rp.WIZLAB_TIMEOUT_S, output="partial\n", stderr="")
        with mock.patch.object(rp.subprocess, "run", side_effect=boom), \
             mock.patch.object(rp.sys, "stdout", io.StringIO()) as out, \
             mock.patch.object(rp.sys, "stderr", io.StringIO()) as err:
            self.assertEqual(rp._wizlab("T", "user", "reap", "--session", "s1", "--commit"), 3)
        self.assertEqual(out.getvalue(), "partial\n")
        self.assertIn("no result within", err.getvalue())

    def test_main_goes_red_only_for_cleanup_a_later_pass_cannot_finish(self):
        # A retained user only self-heals inside WINDOW_H; past that the sid is the only way back in, so
        # the red run names it. A deferral is inside the window by construction and names nothing.
        for outcome, want in [(rp.DONE, None), (rp.DEFERRED, None), (rp.FAILED, 1)]:
            with self.subTest(outcome=outcome), \
                 mock.patch.object(rp, "TENANTS", {}), \
                 mock.patch.object(rp, "_reap_session", return_value=outcome), \
                 mock.patch.dict(rp.os.environ, {"REAP_SESSIONS": "ghtvgsxcgphw,k8m01846391n"}, clear=True), \
                 mock.patch.object(rp.sys, "argv", ["reap_orphans.py", "--commit"]), \
                 mock.patch.object(rp.sys, "stderr", io.StringIO()) as err:
                code = None
                try:
                    rp.main()
                except SystemExit as e:
                    code = e.code
                self.assertEqual(code, want)
                hint = 'REAP_SESSIONS_TENANT=TBCMP REAP_SESSIONS="ghtvgsxcgphw,k8m01846391n"'
                self.assertEqual(hint in err.getvalue(), want is not None)

    def test_a_body_that_is_not_a_graphql_answer_costs_one_tenant_not_the_run(self):
        """Instruqt answered a login page (HTML, 200) once; json.loads left _instruqt by traceback and
        every later tenant went unswept. A bad body is the same failure class as an HTTP error: the
        tenant is named, the rest of the run proceeds, and the run ends red."""
        def body(raw):
            cm = mock.MagicMock()
            cm.__enter__.return_value.read.return_value = raw
            return cm
        answers = [body(b"<html>sign in</html>"),
                   body(b'{"data": {"labPlayReports": {"items": [{"id": "b1", "stoppedReason": "done"}]}}}')]
        with mock.patch.object(rp, "TENANTS", {"A": "tid:a", "B": "tid:b"}), \
             mock.patch.object(rp.urllib.request, "urlopen", side_effect=answers), \
             mock.patch.object(rp, "_reap_session", return_value=rp.DONE) as reap, \
             mock.patch.dict(rp.os.environ, {"INSTRUQT_TOKEN": "t"}, clear=True), \
             mock.patch.object(rp.sys, "argv", ["reap_orphans.py", "--commit"]), \
             mock.patch.object(rp.sys, "stderr", io.StringIO()) as err, \
             self.assertRaises(SystemExit) as cm:
            rp.main()
        self.assertEqual(cm.exception.code, 1)
        self.assertEqual([c.args[:2] for c in reap.call_args_list], [("B", "b1")])
        self.assertIn("tenant A", err.getvalue())
        self.assertIn("<html>", err.getvalue())

    def test_retry_hint_names_the_tenant_each_failed_session_was_swept_under(self):
        # The hint named sids only; REAP_SESSIONS defaults to the first REAP_TENANTS key, so a TE session
        # retried from it swept TBCMP, found nothing, and deleted the one handle back to the TE residue.
        with mock.patch.object(rp, "TENANTS", {"TBCMP": "tid:tbcmp", "TE": "tid:te"}), \
             mock.patch.object(rp, "stopped_sessions", side_effect=[["a"], ["b", "c"]]), \
             mock.patch.object(rp, "_reap_session", side_effect=[rp.FAILED, rp.DONE, rp.FAILED]), \
             mock.patch.dict(rp.os.environ, {}, clear=True), \
             mock.patch.object(rp.sys, "argv", ["reap_orphans.py", "--commit"]), \
             mock.patch.object(rp.sys, "stderr", io.StringIO()) as err, \
             self.assertRaises(SystemExit):
            rp.main()
        self.assertIn('REAP_SESSIONS_TENANT=TBCMP REAP_SESSIONS="a"; REAP_SESSIONS_TENANT=TE REAP_SESSIONS="c"',
                      err.getvalue())
        self.assertIn("# 1/3 session(s) reaped", err.getvalue())

    def test_dry_run_reports_backlog_without_invoking_wizlab(self):
        buf = io.BytesIO()
        out = io.TextIOWrapper(buf, encoding="utf-8")
        with mock.patch.object(rp, "TENANTS", {"TBCMP": "tid:tbcmp", "TE": "tid:te"}), \
             mock.patch.object(rp, "stopped_sessions", side_effect=[["ghtvgsxcgphw"], ["k8m01846391n"]]), \
             mock.patch.object(rp, "_wizlab", wraps=rp._wizlab) as wizlab, \
             mock.patch.object(rp.subprocess, "run") as proc, \
             mock.patch.dict(rp.os.environ, {}, clear=True), \
             mock.patch.object(rp.sys, "argv", ["reap_orphans.py"]), \
             mock.patch.object(rp.sys, "stdout", out), \
             mock.patch.object(rp.sys, "stderr", io.StringIO()) as err:
            rp.main()
            self.assertEqual(rp._reap_session("TE", "ghtvgsxcgphw", False), rp.DONE)
        out.flush()
        stdout = buf.getvalue().decode()
        wizlab.assert_not_called()
        proc.assert_not_called()
        self.assertIn("DRY-RUN TBCMP: reap lab-ghtvgsxcgphw* + delete lab-ghtvgsxcgphw@", stdout)
        self.assertIn("DRY-RUN TE: reap lab-k8m01846391n* + delete lab-k8m01846391n@", stdout)
        self.assertIn("# 2/2 session(s) ready to reap (dry-run)", err.getvalue())

    def test_manual_reap_sessions_routes_to_reap_sessions_tenant(self):
        rows = [
            ({}, {"REAP_SESSIONS_TENANT": "TE"}, "TE", 0),
            ({"TBCMP": "tid:tbcmp", "TE": "tid:te"}, {"REAP_SESSIONS_TENANT": "TE"}, "TE", 2),
            ({"ALT": "tid:alt", "TE": "tid:te"}, {}, "ALT", 2),
            ({}, {"REAP_SESSIONS_TENANT": ""}, "TBCMP", 0),
            ({"TBCMP": "tid:tbcmp"}, {"REAP_SESSIONS_TENANT": "SANDBOX"}, "SANDBOX", 1),
        ]
        ok_proc = rp.subprocess.CompletedProcess(["wizlab"], 0, stdout="", stderr="")
        for tenants, extra_env, want_tenant, want_stopped in rows:
            with self.subTest(tenants=list(tenants), extra_env=extra_env):
                buf = io.BytesIO()
                out = io.TextIOWrapper(buf, encoding="utf-8")
                with mock.patch.object(rp, "TENANTS", tenants), \
                     mock.patch.object(rp, "stopped_sessions", return_value=[]) as stopped, \
                     mock.patch.object(rp.subprocess, "run", return_value=ok_proc) as proc, \
                     mock.patch.dict(rp.os.environ, {"REAP_SESSIONS": "ghtvgsxcgphw", **extra_env}, clear=True), \
                     mock.patch.object(rp.sys, "argv", ["reap_orphans.py", "--commit"]), \
                     mock.patch.object(rp.sys, "stdout", out), \
                     mock.patch.object(rp.sys, "stderr", io.StringIO()) as err:
                    rp.main()
                out.flush()
                self.assertEqual(stopped.call_count, want_stopped)
                self.assertEqual([c.args[0][1:3] for c in proc.call_args_list], [["user", "reap"], ["user", "delete"]])
                self.assertEqual([c.kwargs["env"]["WIZ_TENANT"] for c in proc.call_args_list],
                                 [want_tenant, want_tenant])
                self.assertIn(f"# manual: 1 session(s) under {want_tenant}", buf.getvalue().decode())
                self.assertIn("# 1/1 session(s) reaped", err.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
