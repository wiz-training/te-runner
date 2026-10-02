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
                                   # The user is the only handle back to the footprint, so a delete that
                                   # failed is not cleanup done.
                                   ((0, 1), rp.FAILED, [reap, delete])]:
            with self.subTest(codes=codes), mock.patch.object(rp, "_wizlab", side_effect=codes) as wizlab:
                self.assertEqual(rp._reap_session("T", "s1", True), want)
                self.assertEqual([c.args[1:3] for c in wizlab.call_args_list], calls)

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
                 mock.patch.dict(rp.os.environ, {"REAP_SESSIONS": "s1,s2"}, clear=True), \
                 mock.patch.object(rp.sys, "argv", ["reap_orphans.py", "--commit"]), \
                 mock.patch.object(rp.sys, "stderr", io.StringIO()) as err:
                code = None
                try:
                    rp.main()
                except SystemExit as e:
                    code = e.code
                self.assertEqual(code, want)
                self.assertEqual('REAP_SESSIONS="s1,s2"' in err.getvalue(), want is not None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
