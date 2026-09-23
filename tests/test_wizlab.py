#!/usr/bin/env python3
# Stdlib-only harness (no external deps, matching wizlab). Locks the load-bearing invariants so
# refactors are safe without re-playing a lab: the 0/1/2/3 exit-code contract, IAM-trust parsing
# breadth, flag edges, and the main() dispatch guard. Run: python tests/test_wizlab.py
# What a new test may be, and where it goes: te-labkit-v2/CLAUDE.md §Tests. Contracts are tables
# (`InspectContract.ROWS`, the grading tables), driven through `exit_code` and `FakeWiz`.
import base64
import contextlib
import io
import json
import os
import pathlib
import re
import shutil
import sys
import tempfile
import types
import typing
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import wizlab as wz


def _owner(name):
    """The module that defines `name`. Code inside the package addresses another module's name as
    `module.name`, so patching the owner reaches every caller; patching the package would reach none.
    Each name is defined in exactly one module and never imported by name into another."""
    return next(m for m in wz.MODULES if name in vars(m))

def _proc(returncode=0, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _trust(delegator, external_id, op="StringEquals", key="sts:ExternalId", action="sts:AssumeRole"):
    return {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"AWS": delegator},
            "Action": action,
            "Condition": {op: {key: external_id}},
        }],
    }


class _Ended:
    code = None


@contextlib.contextmanager
def exits():
    """The suite's one statement of how a handler ends: `.code` is what main() would exit with. A
    WizlabError is reported the way main() reports it, a learner-state sys.exit passes its code through,
    and a plain return is 0."""
    ended = _Ended()
    try:
        yield ended
    except wz.WizlabError as e:
        ended.code = wz._fail(e)
    except SystemExit as e:
        ended.code = e.code
    else:
        ended.code = 0


def _jwt(**claims):
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"h.{body}.s"


class FakeWiz:
    """A tenant behind _post: mints a token for AUTH_URL and answers GraphQL by the top-level field of
    the document, so a test names the server object that answers, not a substring of our own document.
    `fields` values: a dict is that field's payload, a callable takes the variables and returns it, a
    `FakeWiz.error(msg)` is a GraphQL error. Unlisted fields answer {}. `calls` holds (field, variables)
    per request and `docs` the raw documents, so a test can assert what left the process."""
    ENV: typing.ClassVar = {"WIZ_CLIENT_ID": "cid", "WIZ_CLIENT_SECRET": "sec"}
    _FIELD = re.compile(r"^\s*(?:query|mutation)\b[^{]*\{\s*(\w+)|^\s*\{\s*(\w+)")

    class error:
        def __init__(self, message):
            self.message = message

    def __init__(self, tid="tid", **fields):
        self.tid, self.fields, self.calls, self.docs, self.mints = tid, fields, [], [], 0

    def __call__(self, url, data, headers, attempts=3):
        if url == wz.AUTH_URL or url.endswith("/protocol/openid-connect/token"):
            self.mints += 1
            return {"access_token": _jwt(dc="dc", tid=self.tid, exp=int(wz.time.time()) + 3600)}
        m = self._FIELD.match(data["query"])
        field, variables = m.group(1) or m.group(2), data.get("variables") or {}
        self.calls.append((field, variables))
        self.docs.append(data["query"])
        answer = self.fields.get(field, {})
        if callable(answer):
            answer = answer(variables)
        if isinstance(answer, FakeWiz.error):
            return {"errors": [{"message": answer.message}], "data": None}
        return {"data": {field: answer}}

    def sent(self, field):
        return [v for f, v in self.calls if f == field]


class FakeKeycloak:
    """A realm behind _kc_call. `users` are the admin API's user records; membership lives in each
    record's "groups". `fail` makes every call answer that status. `calls` holds (method, path)."""
    ENV: typing.ClassVar = {"LAB_KEYCLOAK_ENDPOINT": "https://kc", "LAB_KEYCLOAK_REALM": "wiz",
                            "LAB_KEYCLOAK_ADMIN_USER": "admin", "LAB_KEYCLOAK_ADMIN_PWD": "pw"}

    def __init__(self, users=(), groups=("global-contributor",), fail=None):
        self.users, self.groups, self.fail, self.calls = [dict(u) for u in users], list(groups), fail, []

    def _user(self, uid):
        return next((u for u in self.users if u["id"] == uid), None)

    def __call__(self, method, url, token, body=None):
        u = urllib.parse.urlparse(url)
        parts = u.path.split("/admin/realms/", 1)[1].split("/")[1:]
        params = dict(urllib.parse.parse_qsl(u.query))
        self.calls.append((method, "/".join(parts)))
        if self.fail:
            return self.fail, b"boom"
        if parts == ["users"] and method == "GET":
            hit = [x for x in self.users if params.get("search") in (x["username"], x["email"])]
            return 200, json.dumps(hit).encode()
        if parts == ["users"] and method == "POST":
            self.users.append({"id": f"u{len(self.users) + 1}", "username": body["email"], "email": body["email"]})
            return 201, b""
        if parts == ["groups"] and method == "GET":
            hit = [{"id": f"g-{g}", "name": g} for g in self.groups if g == params.get("search")]
            return 200, json.dumps(hit).encode()
        if len(parts) == 2 and method == "DELETE":
            self.users = [x for x in self.users if x["id"] != parts[1]]
            return 204, b""
        if len(parts) == 3 and parts[2] == "reset-password":
            return 204, b""
        if len(parts) == 4 and parts[2] == "groups" and method == "PUT":
            self._user(parts[1]).setdefault("groups", []).append(parts[3][2:])
            return 204, b""
        if len(parts) == 3 and parts[2] == "groups" and method == "GET":
            return 200, json.dumps([{"name": g} for g in self._user(parts[1]).get("groups", [])]).encode()
        return 404, b"unrouted"


def parsed(fn, argv):
    """`argv` as the handler receives it from main(): its verb's parsed flags. A callable that is not a
    verb (a stand-in) gets the list itself."""
    verb = next((v for v, f in wz.VERBS.items() if f is fn), None)
    return wz.parse(verb, list(argv)) if verb else list(argv)


def call(fn, argv):
    return fn(parsed(fn, argv))


def exit_code(fn, argv=(), *, wiz=None, env=None, out=None, err=None, **patches):
    """Drive one handler as main() would and return its exit code: flags parsed, then the call. Output
    is captured into `out`/`err` or dropped. `env` replaces the environment. `patches` name wz
    attributes: a Mock replaces the attribute, another callable is its side_effect, anything else its
    return_value. `wiz` is a FakeWiz behind _post, with the credentials token_and_dc reads."""
    wz._TOKENS.clear()
    with contextlib.ExitStack() as st:
        if wiz is not None:
            env = {**(os.environ if env is None else env), **FakeWiz.ENV}
            st.enter_context(mock.patch.object(_owner("_post"), "_post", wiz))
        if env is not None:
            st.enter_context(mock.patch.dict(wz.os.environ, env, clear=True))
        for name, value in patches.items():
            if isinstance(value, mock.Mock):
                st.enter_context(mock.patch.object(_owner(name), name, value))
            elif callable(value):
                st.enter_context(mock.patch.object(_owner(name), name, side_effect=value))
            else:
                st.enter_context(mock.patch.object(_owner(name), name, return_value=value))
        st.enter_context(contextlib.redirect_stdout(io.StringIO() if out is None else out))
        st.enter_context(contextlib.redirect_stderr(io.StringIO() if err is None else err))
        with exits() as ended:
            call(fn, argv)
    return ended.code


def _conn(*nodes):
    return {"nodes": list(nodes), "totalCount": len(nodes)}


class InspectContract(unittest.TestCase):
    """What every `<noun> inspect` verb keeps: present → 0, absent → 1, a --require it does not grade → 2,
    a tenant error → 3, a malformed invocation → 2. One row per noun; a noun shipped without a row fails
    `test_every_inspect_verb_has_a_row`. Which enum satisfies which --require is the noun's own class."""

    ENV: typing.ClassVar = {"INSTRUQT_SESSION_ID": "x"}
    SENSOR: typing.ClassVar = {"id": "s1", "name": "lab-x", "status": "ACTIVE", "type": "LINUX_VIRTUAL_MACHINE"}
    WORKFLOW: typing.ClassVar = {"id": "w1", "name": "lab-x-night-watch", "enabled": True, "project": {"name": "p"},
                                 "steps": [{"id": "s1", "name": "Route", "type": "SWITCH_CASE"}]}
    RUN: typing.ClassVar = {"id": "r1", "status": "COMPLETED",
                            "steps": [{"status": "COMPLETED", "outboundEdge": "x",
                                       "step": {"name": "Route", "type": "SWITCH_CASE"}}]}
    DEPLOYMENT: typing.ClassVar = {"id": "dep1", "name": "lab-x-cli", "type": "WIZ_CLI"}
    SCAN: typing.ClassVar = {"id": "c1", "status": {"state": "DONE", "verdict": "FAILED_BY_POLICY"}}
    POLICY: typing.ClassVar = {"id": "pol-1", "name": "block-root"}
    OUTPOST: typing.ClassVar = {"id": "o1", "name": "lab-x", "status": "CONNECTED"}
    CONNECTOR: typing.ClassVar = {"id": "c1", "name": "lab-x-connector", "enabled": True, "status": "CONNECTED",
                                  "type": {"id": "aws"},
                                  "config": {"customerRoleARN": "arn:aws:iam::111111111111:role/WizAccess-Role"}}
    ROWS: typing.ClassVar = {
        ("connector", "inspect"): {"argv": ["--account-id", "111111111111"],
                                   "present": {"connectors": _conn(CONNECTOR)}, "absent": [{"connectors": _conn()}]},
        ("instance", "inspect"): {"argv": ["--account-id", "111111111111", "--type", "VIRTUAL_MACHINE"],
                                  "present": {"cloudResources": {"totalCount": 1}},
                                  "absent": [{"cloudResources": {"totalCount": 0}}]},
        ("sensor", "inspect"): {"argv": ["--name", "lab-x"],
                                "present": {"sensors": _conn(SENSOR)}, "absent": [{"sensors": _conn()}]},
        ("serviceaccount", "inspect"): {"argv": ["--name", "lab-x-cli"],
                                        "present": {"deployments": _conn(DEPLOYMENT)},
                                        "absent": [{"deployments": _conn()}]},
        ("code-scan", "inspect"): {"argv": ["--timeout", "0"],
                                   "present": {"cicdScans": _conn(SCAN)}, "absent": [{"cicdScans": _conn()}],
                                   "invalid": [["--tag-value", "bad value!"]]},
        ("policy", "inspect"): {"argv": ["--name", "block-root"],
                                "present": {"cicdScanPolicies": _conn(POLICY)},
                                "absent": [{"cicdScanPolicies": _conn()}], "invalid": [[]]},
        # The absent sensor with detections present is the scoping case: an unscoped count would grade 0.
        ("detection", "inspect"): {"argv": ["--name", "lab-x", "--rule-name", "R"],
                                   "present": {"sensors": _conn(SENSOR), "detections": {"totalCount": 3}},
                                   "absent": [{"sensors": _conn(SENSOR), "detections": {"totalCount": 0}},
                                              {"sensors": _conn(), "detections": {"totalCount": 3}}],
                                   "invalid": [["--name", "lab-x"]]},
        ("workflow", "inspect"): {"argv": ["--name", "lab-x"],
                                  "present": {"automationWorkflows": _conn(WORKFLOW)},
                                  "absent": [{"automationWorkflows": _conn()}]},
        ("workflow-run", "inspect"): {"argv": ["--name", "lab-x"],
                                      "present": {"automationWorkflows": _conn(WORKFLOW),
                                                  "automationWorkflowRuns": _conn(RUN)},
                                      "absent": [{"automationWorkflows": _conn(WORKFLOW),
                                                  "automationWorkflowRuns": _conn()},
                                                 {"automationWorkflows": _conn()}],
                                      "invalid": [["--name", "lab-x", "--require", "branch"]]},
        ("outpost", "inspect"): {"argv": ["--name", "lab-x"],
                                 "present": {"outposts": _conn(OUTPOST)}, "absent": [{"outposts": _conn()}]},
    }
    # Graded off another system, each in its own class.
    NOT_WIZ: typing.ClassVar = {("role", "inspect"): "CSP CLIs", ("user", "inspect"): "Keycloak"}

    def _code(self, verb, argv, fields):
        with mock.patch.object(wz.time, "sleep", lambda *_: None):
            return exit_code(wz.VERBS[verb], argv, wiz=FakeWiz(**fields), env=self.ENV)

    def test_every_inspect_verb_has_a_row(self):
        self.assertEqual({v for v in wz.VERBS if v[1] == "inspect"}, set(self.ROWS) | set(self.NOT_WIZ))

    def test_present_is_0_and_absent_is_1(self):
        for verb, row in self.ROWS.items():
            with self.subTest(verb=verb):
                self.assertEqual(self._code(verb, row["argv"], row["present"]), 0)
                for absent in row["absent"]:
                    self.assertEqual(self._code(verb, row["argv"], absent), 1, absent)

    def test_a_tenant_error_is_environment_3_never_learner_1(self):
        for verb, row in self.ROWS.items():
            with self.subTest(verb=verb):
                field = next(iter(row["present"]))
                self.assertEqual(self._code(verb, row["argv"], {field: FakeWiz.error("denied")}), 3)

    def test_a_require_the_verb_does_not_grade_is_2(self):
        for verb, row in self.ROWS.items():
            if "--require" in wz.FLAGS[verb]:
                with self.subTest(verb=verb):
                    self.assertEqual(self._code(verb, [*row["argv"], "--require", "bogus"], row["present"]), 2)

    def test_a_malformed_invocation_is_2(self):
        for verb, row in self.ROWS.items():
            for argv in row.get("invalid", []):
                with self.subTest(verb=verb, argv=argv):
                    self.assertEqual(self._code(verb, argv, row["present"]), 2)


def _mutations(wiz):
    return [f for f, _ in wiz.calls if f.startswith(("create", "update", "delete", "uninstall", "run"))]


class EnsureContract(unittest.TestCase):
    """SPEC.md §What `ensure` promises, one row per noun: absent → created (exit 0); present and matching →
    exit 0 with the row's own mutations (none, or delete-and-re-mint for a credential); a tenant error → 3
    with nothing mutated; a missing required flag → 2. What counts as drift is the noun's own class."""

    ENV: typing.ClassVar = {"INSTRUQT_SESSION_ID": "x"}
    CONNECTOR: typing.ClassVar = dict(InspectContract.CONNECTOR)
    SA: typing.ClassVar = {"id": "sa1", "name": "lab-x-sensor", "clientId": "cid", "clientSecret": "sec"}
    CLI: typing.ClassVar = {"clientSecret": "sec", "deployment": {
        "id": "dep2", "name": "lab-x-cli", "type": "WIZ_CLI", "object": {"serviceAccount": {"clientId": "cid"}}}}
    POLICY: typing.ClassVar = {"id": "pol-1", "name": "block-root", "params": {
        "severityThreshold": "HIGH", "countThreshold": 1, "cloudConfigurationRules": [{"id": "ctl-1"}]}}
    CTL: typing.ClassVar = {"id": "ctl-1", "name": "Last User Is 'root'", "severity": "HIGH"}
    OUTPOST: typing.ClassVar = {"id": "o1", "name": "lab-x", "status": "CONNECTED",
                                "allowedRegions": ["us-east-1"], "config": {"roleARN": "a"}}
    NOT_WIZ: typing.ClassVar = {("role", "ensure"): "CSP CLIs",
                                ("user", "ensure"): "Keycloak, KeycloakContract",
                                ("workflow-run", "ensure"): "fires a test run, converges nothing"}

    @classmethod
    def setUpClass(cls):
        cls.definition = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)  # noqa: SIM115
        json.dump({"steps": [], "triggers": []}, cls.definition)
        cls.definition.close()

    @classmethod
    def tearDownClass(cls):
        os.unlink(cls.definition.name)

    def rows(self):
        wf = InspectContract.WORKFLOW
        return {
            ("connector", "ensure"): {
                "argv": ["--account-id", "111111111111"],
                "absent": {"connectors": _conn(), "createConnector": {"connector": self.CONNECTOR}},
                "present": {"connectors": _conn(self.CONNECTOR)},
                "creates": ["createConnector"], "invalid": [[]]},
            ("sensor", "ensure"): {
                "argv": ["--name", "lab-x-sensor"],
                "absent": {"serviceAccounts": _conn(), "createServiceAccount": {"serviceAccount": self.SA}},
                "present": {"serviceAccounts": _conn(self.SA), "createServiceAccount": {"serviceAccount": self.SA}},
                "creates": ["createServiceAccount"],
                "present_mutates": ["deleteServiceAccount", "createServiceAccount"]},
            ("serviceaccount", "ensure"): {
                "argv": ["--name", "lab-x-cli"],
                "absent": {"deployments": _conn(), "createCliDeployment": self.CLI},
                "present": {"deployments": _conn(self.CLI["deployment"]), "createCliDeployment": self.CLI},
                "creates": ["createCliDeployment"], "present_mutates": ["deleteCliDeployment", "createCliDeployment"]},
            ("policy", "ensure"): {
                "argv": ["--name", "block-root"],
                "absent": {"cicdScanPolicies": _conn(), "cloudConfigurationRules": _conn(self.CTL),
                           "createCICDScanPolicy": {"scanPolicy": {"id": "pol-1", "name": "block-root"}}},
                "present": {"cicdScanPolicies": _conn(self.POLICY)},
                "creates": ["createCICDScanPolicy"], "invalid": [[]]},
            ("workflow", "ensure"): {
                "argv": ["--name", "lab-x", "--definition", self.definition.name],
                "absent": {"automationWorkflows": _conn(), "validateAutomationWorkflow": {"issues": []},
                           "createAutomationWorkflow": {"workflow": wf}},
                "present": {"automationWorkflows": _conn(wf), "validateAutomationWorkflow": {"issues": []},
                            "updateAutomationWorkflow": {"workflow": wf}},
                "creates": ["createAutomationWorkflow"], "present_mutates": ["updateAutomationWorkflow"],
                "invalid": [["--name", "lab-x"]]},
            ("outpost", "ensure"): {
                "argv": ["--name", "lab-x", "--role-arn", "a"],
                "absent": {"outposts": _conn(), "createOutpost": {"outpost": self.OUTPOST}},
                "present": {"outposts": _conn(self.OUTPOST)},
                "creates": ["createOutpost"], "invalid": [["--name", "lab-x"]]},
        }

    def _run(self, verb, argv, fields):
        wiz = FakeWiz(**fields)
        return exit_code(wz.VERBS[verb], argv, wiz=wiz, env=self.ENV), wiz

    def test_every_ensure_verb_has_a_row(self):
        self.assertEqual({v for v in wz.VERBS if v[1] == "ensure"}, set(self.rows()) | set(self.NOT_WIZ))

    def test_absent_is_created_and_present_is_converged(self):
        for verb, row in self.rows().items():
            with self.subTest(verb=verb):
                code, wiz = self._run(verb, row["argv"], row["absent"])
                self.assertEqual((code, _mutations(wiz)), (0, row["creates"]))
                code, wiz = self._run(verb, row["argv"], row["present"])
                self.assertEqual((code, _mutations(wiz)), (0, row.get("present_mutates", [])))

    def test_a_tenant_error_mutates_nothing_and_is_3(self):
        for verb, row in self.rows().items():
            with self.subTest(verb=verb):
                field = next(iter(row["absent"]))
                code, wiz = self._run(verb, row["argv"], {**row["absent"], field: FakeWiz.error("denied")})
                self.assertEqual((code, _mutations(wiz)), (3, []))

    def test_a_missing_required_flag_is_2_before_any_request(self):
        for verb, row in self.rows().items():
            for argv in row.get("invalid", []):
                with self.subTest(verb=verb, argv=argv):
                    code, wiz = self._run(verb, argv, row["absent"])
                    self.assertEqual((code, _mutations(wiz)), (2, []))


class DeleteContract(unittest.TestCase):
    """Every `<noun> delete`: present → the delete mutation, exit 0; absent → exit 0 and no mutation;
    a tenant error → 3 and no mutation."""

    ENV: typing.ClassVar = {"INSTRUQT_SESSION_ID": "x"}
    NOT_WIZ: typing.ClassVar = {("user", "delete"): "Keycloak, KeycloakContract"}
    ROWS: typing.ClassVar = {
        ("connector", "delete"): {"argv": ["--account-id", "111111111111"], "field": "connectors",
                                  "node": InspectContract.CONNECTOR, "deletes": ["deleteConnector"]},
        ("sensor", "delete"): {"argv": ["--name", "lab-x-sensor"], "field": "serviceAccounts",
                               "node": EnsureContract.SA, "deletes": ["deleteServiceAccount"]},
        ("serviceaccount", "delete"): {"argv": ["--name", "lab-x-cli"], "field": "deployments",
                                       "node": EnsureContract.CLI["deployment"], "deletes": ["deleteCliDeployment"]},
        ("policy", "delete"): {"argv": ["--name", "block-root"], "field": "cicdScanPolicies",
                               "node": EnsureContract.POLICY, "deletes": ["deleteCICDScanPolicy"]},
        ("outpost", "delete"): {"argv": ["--name", "lab-x"], "field": "outposts",
                                "node": dict(EnsureContract.OUTPOST, status="UNINSTALLED"),
                                "deletes": ["deleteOutpost"]},
    }

    def _run(self, verb, argv, fields):
        wiz = FakeWiz(**fields)
        return exit_code(wz.VERBS[verb], argv, wiz=wiz, env=self.ENV), wiz

    def test_every_delete_verb_has_a_row(self):
        self.assertEqual({v for v in wz.VERBS if v[1] == "delete"}, set(self.ROWS) | set(self.NOT_WIZ))

    def test_present_is_deleted_absent_is_a_no_op_and_an_error_mutates_nothing(self):
        for verb, row in self.ROWS.items():
            with self.subTest(verb=verb):
                code, wiz = self._run(verb, row["argv"], {row["field"]: _conn(row["node"])})
                self.assertEqual((code, _mutations(wiz)), (0, row["deletes"]))
                code, wiz = self._run(verb, row["argv"], {row["field"]: _conn()})
                self.assertEqual((code, _mutations(wiz)), (0, []))
                code, wiz = self._run(verb, row["argv"], {row["field"]: FakeWiz.error("denied")})
                self.assertEqual((code, _mutations(wiz)), (3, []))


class KeycloakContract(unittest.TestCase):
    """The user verbs against a realm: ensure creates or resets and always joins the group and publishes
    credentials; inspect grades membership; delete is idempotent; any unexpected status is 3."""

    ENV: typing.ClassVar = {"INSTRUQT_SESSION_ID": "s1", **FakeKeycloak.ENV}
    EMAIL = "lab-s1@titra-labs.ai"

    def _run(self, fn, argv, kc, out=None):
        return exit_code(fn, argv, wiz=FakeWiz(), env=self.ENV, out=out, _kc_call=kc)

    def test_ensure_creates_then_resets_and_publishes_both_times(self):
        kc = FakeKeycloak()
        for expect in ("POST users", "PUT users/u1/reset-password"):
            out = io.StringIO()
            self.assertEqual(self._run(wz.cmd_user_ensure, [], kc, out=out), 0)
            self.assertIn(expect, [f"{m} {p}" for m, p in kc.calls])
            self.assertIn("WIZ_USER=" + self.EMAIL, out.getvalue())
            self.assertRegex(out.getvalue(), r"WIZ_PWD=\S{8,}")
        self.assertEqual(kc.users[0]["groups"], ["global-contributor", "global-contributor"])

    def test_ensure_refuses_to_join_a_group_when_the_created_user_is_not_found(self):
        # A None re-lookup after a 201 used to PUT to users/None/groups/<gid>.
        kc = FakeKeycloak()
        kc.users = None  # a POST that "succeeds" but leaves nothing to find

        def kc_call(method, url, token, body=None):
            kc.calls.append((method, url))
            return (201, b"") if method == "POST" else (200, b"[]")
        self.assertEqual(self._run(wz.cmd_user_ensure, [], kc_call), 3)
        self.assertEqual([m for m, _ in kc.calls if m != "GET"], ["POST"])
        self.assertFalse([u for _, u in kc.calls if "/users/None/" in u])

    def test_inspect_grades_membership(self):
        member = {"id": "u1", "username": self.EMAIL, "email": self.EMAIL, "groups": ["global-contributor"]}
        for users, want in [([member], 0), ([dict(member, groups=[])], 1), ([], 1)]:
            with self.subTest(users=users):
                self.assertEqual(self._run(wz.cmd_user_inspect, [], FakeKeycloak(users)), want)

    def test_delete_is_idempotent(self):
        kc = FakeKeycloak([{"id": "u1", "username": self.EMAIL, "email": self.EMAIL}])
        self.assertEqual(self._run(wz.cmd_user_delete, [], kc), 0)
        self.assertIn(("DELETE", "users/u1"), kc.calls)
        self.assertEqual(self._run(wz.cmd_user_delete, [], kc), 0)
        self.assertEqual([c for c in kc.calls if c[0] == "DELETE"], [("DELETE", "users/u1")])

    def test_an_unexpected_status_is_environment_3(self):
        for fn in (wz.cmd_user_ensure, wz.cmd_user_inspect, wz.cmd_user_delete):
            with self.subTest(fn=fn.__name__):
                self.assertEqual(self._run(fn, [], FakeKeycloak(fail=503)), 3)

    def test_a_duplicate_exact_match_is_never_guessed(self):
        # `search` is a substring match; two exact hits mean the realm is inconsistent, not that the
        # first one is ours.
        dup = [{"id": "a", "username": self.EMAIL, "email": self.EMAIL},
               {"id": "b", "username": self.EMAIL, "email": self.EMAIL}]
        self.assertEqual(self._run(wz.cmd_user_delete, [], FakeKeycloak(dup)), 3)


class AuthorTools(unittest.TestCase):
    def test_login_url_is_the_override_or_built_from_the_tenant_or_3(self):
        out = io.StringIO()
        self.assertEqual(exit_code(wz.cmd_user_login_url, [], env={"WIZ_LOGIN_URL": "https://pinned"}, out=out), 0)
        self.assertEqual(out.getvalue().strip(), "https://pinned")
        out = io.StringIO()
        self.assertEqual(exit_code(wz.cmd_user_login_url, [], wiz=FakeWiz(tid="t1"), env={}, out=out), 0)
        self.assertIn("t1-34dq.auth.us-east-1.amazoncognito.com", out.getvalue())
        self.assertEqual(exit_code(wz.cmd_user_login_url, [], env={"WIZ_TENANT": "NOPE"}), 3)


class PureParsing(unittest.TestCase):
    def test_as_list(self):
        self.assertEqual(wz._as_list(None), [])
        self.assertEqual(wz._as_list("x"), ["x"])
        self.assertEqual(wz._as_list(["a", "b"]), ["a", "b"])

    def test_principals(self):
        self.assertEqual(wz._principals({"Principal": "svc"}), ["svc"])
        self.assertEqual(wz._principals({"Principal": {"AWS": ["a", "b"]}}), ["a", "b"])
        self.assertEqual(wz._principals({}), [])

    def test_grants_assume_role(self):
        for a in ("sts:AssumeRole", "sts:*", "*", "sts:Assume*"):
            self.assertTrue(wz._grants_assume_role({"Effect": "Allow", "Action": a}), a)
        self.assertFalse(wz._grants_assume_role({"Effect": "Deny", "Action": "sts:AssumeRole"}))
        self.assertFalse(wz._grants_assume_role({"Effect": "Allow", "Action": "s3:GetObject"}))

    def test_external_ids_operator_variants(self):
        # Breadth is load-bearing: every equality variant must be caught, or a role Wiz can assume
        # gets mis-graded as "no external id". StringLike must NOT match (it doesn't pin the value).
        tid = "6ca852a0"
        for op in ("StringEquals", "StringEqualsIgnoreCase", "ForAllValues:StringEquals"):
            stmt = _trust("d", tid, op=op)["Statement"][0]
            self.assertEqual(wz._external_ids(stmt), [tid], op)
        for key in ("sts:ExternalId", "STS:EXTERNALID", " sts:externalid "):
            stmt = _trust("d", tid, key=key)["Statement"][0]
            self.assertEqual(wz._external_ids(stmt), [tid], key)
        self.assertEqual(wz._external_ids(_trust("d", tid, op="StringLike")["Statement"][0]), [])

    def test_decode_trust_policy(self):
        pol = _trust("d", "x")
        self.assertEqual(wz._decode_trust_policy(pol), pol)                       # dict passthrough
        self.assertEqual(wz._decode_trust_policy(json.dumps(pol)), pol)           # json string
        self.assertEqual(wz._decode_trust_policy(urllib.parse.quote(json.dumps(pol))), pol)  # %-encoded
        self.assertIsNone(wz._decode_trust_policy("not json"))


class FlagParsing(unittest.TestCase):
    """`parse` is the one reader of argv: a flag's default, set and type live in FLAGS, not in handlers."""

    def test_present_absent_and_the_table_default(self):
        args = wz.parse(("code-scan", "inspect"), ["--tag-value", "v", "--timeout", "5"])
        self.assertEqual((args.tag_value, args.timeout, args.interval, args.require, args.session),
                         ("v", 5, 10, "published", None))

    def test_a_switch_is_a_bool(self):
        self.assertTrue(wz.parse(("user", "reap"), ["--session", "s1", "--commit"]).commit)
        self.assertFalse(wz.parse(("user", "reap"), ["--session", "s1"]).commit)

    def test_a_missing_value_a_bad_type_or_a_stray_word_is_invocation_error(self):
        for verb, argv in ((("role", "inspect"), ["--role-name"]),
                           (("code-scan", "inspect"), ["--timeout", "soon"]),
                           (("sensor", "inspect"), ["lab-x"])):
            with self.subTest(argv=argv), exits() as cm:
                wz.parse(verb, argv)
            self.assertEqual(cm.code, 2)

    def test_the_next_flag_is_never_the_value(self):
        # `reap --session --commit` must not reap a session named "--commit" with commit silently off.
        with exits() as cm:
            wz.parse(("user", "reap"), ["--session", "--commit"])
        self.assertEqual(cm.code, 2)


class CliHelper(unittest.TestCase):
    def test_a_missing_or_hung_binary_is_environment_3(self):
        # Never 2 (an uncaught exception reads as the learner's fault) and never past the platform's own
        # check timeout.
        for fault in (FileNotFoundError(), wz.subprocess.TimeoutExpired("aws", wz._CLI_TIMEOUT_S)):
            with self.subTest(fault=type(fault).__name__), \
                 mock.patch.object(wz.subprocess, "run", side_effect=fault), exits() as cm:
                wz._cli("aws", "version")
            self.assertEqual(cm.code, 3)

    def test_aws_gcp_az_delegate_to_cli(self):
        proc = _proc(0, "ok")
        with mock.patch.object(_owner("_cli"), "_cli", return_value=proc) as cli:
            wz._aws("sts", "get-caller-identity")
            wz._gcp("auth", "list")
            wz._az("account", "show")
        calls = [c[0] for c in cli.call_args_list]
        self.assertEqual(calls[0][0], "aws")
        self.assertEqual(calls[1][0], "gcloud")
        self.assertEqual(calls[2][0], "az")


class ExitCodeContract(unittest.TestCase):
    def _urlopen_returning(self, body):
        cm = mock.MagicMock()
        cm.__enter__.return_value.read.return_value = body
        return mock.MagicMock(return_value=cm)

    def test_post_success(self):
        with mock.patch.object(wz.urllib.request, "urlopen", self._urlopen_returning(b'{"ok": true}')):
            self.assertEqual(wz._post("https://x/", "d", {}), {"ok": True})

    def test_post_transport_retries_then_exit_3(self):
        op = mock.MagicMock(side_effect=urllib.error.URLError("boom"))
        with mock.patch.object(wz.urllib.request, "urlopen", op), mock.patch.object(wz.time, "sleep"), \
             exits() as cm:
            wz._post("https://x/", "d", {}, attempts=3)
        self.assertEqual(cm.code, 3)
        self.assertEqual(op.call_count, 3)  # retried, not one-shot

    def test_post_4xx_fails_fast(self):
        err = urllib.error.HTTPError("u", 400, "bad", None, io.BytesIO(b"nope"))
        op = mock.MagicMock(side_effect=err)
        with mock.patch.object(wz.urllib.request, "urlopen", op), mock.patch.object(wz.time, "sleep"), \
             exits() as cm:
            wz._post("https://x/", "d", {}, attempts=3)
        self.assertEqual(cm.code, 3)
        self.assertEqual(op.call_count, 1)  # 4xx is not transient — no retry

    def test_main_bad_verb_is_invocation_error(self):
        with mock.patch.object(wz.sys, "argv", ["wizlab", "bogus", "verb"]), exits() as cm:
            wz.main()
        self.assertEqual(cm.code, 2)

    def test_main_guards_uncaught_exception_as_2(self):
        boom = mock.MagicMock(side_effect=RuntimeError("kaboom"))
        with mock.patch.dict(wz.VERBS, {("session", "verify"): boom}), \
             mock.patch.object(wz.sys, "argv", ["wizlab", "session", "verify"]), \
             exits() as cm:
            wz.main()
        self.assertEqual(cm.code, 2)  # bug, not a raw traceback exiting 1

    def test_a_handler_that_returns_is_exit_0(self):
        # The contract main() adopts when handlers stop calling sys.exit: a plain return is success.
        self.assertEqual(exit_code(lambda argv: None, []), 0)
        self.assertEqual(exit_code(lambda argv: wz.die(3, "x"), []), 3)

    def test_the_fake_tenant_drives_the_real_transport(self):
        # A GraphQL error from the tenant reaches the handler through api(), not through a patched api.
        wiz = FakeWiz(deployments=FakeWiz.error("denied"))
        err = io.StringIO()
        code = exit_code(wz.cmd_serviceaccount_inspect, ["--name", "lab-x"], wiz=wiz, err=err)
        self.assertEqual(code, 3)
        self.assertIn("denied", err.getvalue())
        self.assertEqual([f for f, _ in wiz.calls], ["deployments"])

    def test_main_reports_a_wizlab_error_once_with_its_code(self):
        err = io.StringIO()
        with mock.patch.object(wz.sys, "argv", ["wizlab", "policy", "inspect"]), \
             contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            wz.main()
        self.assertEqual(cm.exception.code, 2)
        self.assertEqual(err.getvalue().count("wizlab:"), 1)
        self.assertIn("--name", err.getvalue())

    def test_a_flag_the_verb_does_not_read_is_invocation_error_2(self):
        # A misspelled flag was ignored, so a check graded on the default it meant to override.
        err = io.StringIO()
        with mock.patch.object(wz.sys, "argv", ["wizlab", "sensor", "inspect", "--requier", "active"]), \
             contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            wz.main()
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("--requier", err.getvalue())
        self.assertIn("--require", err.getvalue())

    def test_every_verb_declares_its_flags_and_every_flag_read_is_declared(self):
        self.assertEqual(set(wz.FLAGS), set(wz.VERBS))
        src = "".join(pathlib.Path(m.__file__).read_text() for m in wz.MODULES)
        read = {"--" + a.replace("_", "-") for a in re.findall(r"\bargs\.([a-z_]+)", src)}
        declared = set().union(*wz.FLAGS.values())
        self.assertEqual(read - declared, set())

    def test_check_collapses_every_failure_to_1_and_names_the_real_code(self):
        err = io.StringIO()
        with mock.patch.dict(wz.VERBS, {("session", "verify"): lambda argv: wz.die(3, "no tenant")}), \
             mock.patch.object(wz.sys, "argv", ["wizlab", "--check", "session", "verify"]), \
             contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            wz.main()
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("no tenant", err.getvalue())
        self.assertIn("exit 3", err.getvalue())
        with mock.patch.dict(wz.VERBS, {("session", "verify"): lambda argv: sys.exit(1)}), \
             mock.patch.object(wz.sys, "argv", ["wizlab", "--check", "session", "verify"]), \
             self.assertRaises(SystemExit) as cm:
            wz.main()
        self.assertEqual(cm.exception.code, 1)
        with mock.patch.dict(wz.VERBS, {("session", "verify"): lambda argv: None}), \
             mock.patch.object(wz.sys, "argv", ["wizlab", "--check", "session", "verify"]):
            self.assertIsNone(wz.main())

    def test_main_exits_0_when_the_handler_returns(self):
        with mock.patch.dict(wz.VERBS, {("session", "verify"): lambda argv: None}), \
             mock.patch.object(wz.sys, "argv", ["wizlab", "session", "verify"]):
            self.assertIsNone(wz.main())

    def test_one_token_serves_every_call_in_a_process(self):
        # session verify used to mint twice back to back; the cache holds until a minute before `exp`.
        wiz = FakeWiz(connectors={"totalCount": 0})
        self.assertEqual(exit_code(wz.cmd_session_verify, [], wiz=wiz), 0)
        self.assertEqual(wiz.mints, 1)
        self.assertGreaterEqual(len(wiz.calls), 1)

    def test_an_expiring_token_is_reminted(self):
        wiz, clock = FakeWiz(), {"t": 1000.0}
        with mock.patch.object(_owner("_post"), "_post", wiz), \
             mock.patch.dict(wz.os.environ, FakeWiz.ENV, clear=True), \
             mock.patch.object(wz.time, "time", lambda: clock["t"]):
            wz._TOKENS.clear()
            wz.token_and_dc()
            wz.token_and_dc()
            self.assertEqual(wiz.mints, 1)
            clock["t"] += 3600 - 30  # inside the minute before exp
            wz.token_and_dc()
        self.assertEqual(wiz.mints, 2)
        wz._TOKENS.clear()


class RoleInspectGrading(unittest.TestCase):
    DELEGATOR = "arn:aws:iam::851725410668:role/prod-us100-AssumeRoleDelegator"
    TID = "6ca852a0-af83-4f2d-9da9-f2f3bd1d23a3"

    def _run(self, aws_proc, delegator=DELEGATOR, tid=TID):
        with mock.patch.object(_owner("_aws"), "_aws", return_value=aws_proc), \
             mock.patch.object(_owner("_wiz_delegator"), "_wiz_delegator", return_value=(delegator, tid)), \
             exits() as cm:
            call(wz.cmd_role_inspect, [])
        return cm.code

    def test_valid_trust_exit_0(self):
        role = {"Role": {"AssumeRolePolicyDocument": _trust(self.DELEGATOR, self.TID)}}
        self.assertEqual(self._run(_proc(0, json.dumps(role))), 0)

    def test_wrong_external_id_exit_1(self):
        role = {"Role": {"AssumeRolePolicyDocument": _trust(self.DELEGATOR, "WRONG-ID")}}
        self.assertEqual(self._run(_proc(0, json.dumps(role))), 1)

    def test_missing_role_exit_1(self):
        self.assertEqual(self._run(_proc(1, "", "NoSuchEntity: not found")), 1)

    def test_missing_creds_exit_3_not_1(self):
        # Anything but NoSuchEntity is environment: "no credentials" must never grade as
        # "learner wrong".
        self.assertEqual(self._run(_proc(255, "", "Unable to locate credentials")), 3)
        self.assertEqual(self._run(_proc(255, "", "ExpiredToken: token is expired")), 3)

    @staticmethod
    def _stmt(principal, external_id):
        return {"Effect": "Allow", "Principal": {"AWS": principal}, "Action": "sts:AssumeRole",
                "Condition": {"StringEquals": {"sts:ExternalId": external_id}}}

    def _multi(self, *statements):
        role = {"Role": {"AssumeRolePolicyDocument": {"Statement": list(statements)}}}
        return self._run(_proc(0, json.dumps(role)))

    def test_a_split_principal_and_external_id_do_not_combine(self):
        # AWS evaluates a statement as a unit: the delegator under a wrong external id, plus the right
        # external id for someone else, authorizes nobody — and used to grade 0.
        self.assertEqual(self._multi(self._stmt(self.DELEGATOR, "WRONG-ID"),
                                     self._stmt("arn:aws:iam::222222222222:role/Other", self.TID)), 1)

    def test_one_fully_correct_statement_among_others_is_valid(self):
        self.assertEqual(self._multi(self._stmt("arn:aws:iam::222222222222:role/Other", "OTHER-ID"),
                                     self._stmt(self.DELEGATOR, self.TID)), 0)

    def test_the_delegators_own_statement_must_carry_the_condition(self):
        unconditioned = {"Effect": "Allow", "Principal": {"AWS": self.DELEGATOR}, "Action": "sts:AssumeRole"}
        self.assertEqual(self._multi(unconditioned, self._stmt("arn:aws:iam::2:role/O", self.TID)), 1)


class Naming(unittest.TestCase):
    def test_stem_is_session_scoped(self):
        self.assertEqual(wz._lab_stem("abc123"), "lab-abc123")

    def test_session_id_from_flag_then_env(self):
        self.assertEqual(wz._session_id(wz.parse(("user", "inspect"), ["--session", "flagid"])), "flagid")
        with mock.patch.dict(wz.os.environ, {"INSTRUQT_SESSION_ID": "envid"}, clear=False):
            self.assertEqual(wz._session_id(wz.parse(("user", "inspect"), [])), "envid")

    def test_session_id_missing_is_invocation_error(self):
        with mock.patch.dict(wz.os.environ, {}, clear=True), exits() as cm:
            wz._session_id(wz.parse(("user", "inspect"), []))
        self.assertEqual(cm.code, 2)

    def test_tenant_keyed_env_wins_over_the_tenant_less_name(self):
        env = {"WIZ_TENANT": "T2", "WIZ_T2_CLIENT_ID": "keyed", "WIZ_CLIENT_ID": "plain"}
        with mock.patch.dict(wz.os.environ, env, clear=True):
            self.assertEqual(wz._tenant_env("CLIENT_ID"), "keyed")
        with mock.patch.dict(wz.os.environ, {"WIZ_CLIENT_ID": "plain"}, clear=True):
            self.assertEqual((wz._tenant(), wz._tenant_env("CLIENT_ID")), (wz._DEFAULT_TENANT, "plain"))

    def test_default_names_hang_off_the_session_stem(self):
        for argv, want in ((["--session", "s1"], "lab-s1-cli"), (["--session", "s1", "--name", "mine"], "mine")):
            self.assertEqual(wz._named(wz.parse(("sensor", "delete"), argv), "-cli"), want)

    def test_user_email_keyed_on_session(self):
        args = wz.parse(("user", "inspect"), ["--session", "s1"])
        self.assertEqual(wz._lab_user_email(args)[0], "lab-s1@titra-labs.ai")


class ConnectorAndReaperSafety(unittest.TestCase):
    def _api(self, find_nodes, bytype_nodes=None):
        def side(query, variables):
            if query == wz.FIND:
                return {"connectors": {"nodes": find_nodes}}, "tid"
            if query == wz.BY_TYPE:
                return {"connectors": {"nodes": bytype_nodes or []}}, "tid"
            return {}, "tid"
        return side

    def test_find_connector_ranks_active_before_stale(self):
        nodes = [
            {"id": "e", "enabled": True, "status": "ERROR", "type": {"id": "aws"}, "config": {}},
            {"id": "c", "enabled": True, "status": "CONNECTED", "type": {"id": "aws"}, "config": {}},
        ]
        with mock.patch.object(_owner("api"), "api", side_effect=self._api(nodes)):
            self.assertEqual(wz.find_connector("111111111111")[0]["status"], "CONNECTED")

    def test_find_connector_by_type_fallback_when_no_parent(self):
        find = [{"id": "builtin", "type": {"id": "self-hosted"}, "config": {}}]  # no aws parent yet
        bytype = [{"id": "a", "enabled": True, "status": "CONNECTED", "type": {"id": "aws"},
                   "config": {"customerRoleARN": "arn:aws:iam::111111111111:role/WizAccess-Role"}}]
        with mock.patch.object(_owner("api"), "api", side_effect=self._api(find, bytype)):
            self.assertEqual([n["id"] for n in wz.find_connector("111111111111")], ["a"])

    HANDLER: typing.ClassVar = {"list": "reports", "filter": "search", "delete": "deleteReport",
                                "soft": False, "deleter": None}

    def test_reap_one_refuses_ambiguous_match(self):
        # Shared-tenant safety: >1 name match must skip, never delete — even with --commit.
        with mock.patch.object(_owner("_reap_handler"), "_reap_handler", return_value=self.HANDLER), \
             mock.patch.object(_owner("_reap_find"), "_reap_find", return_value=(None, 2, None)):
            outcome, review = wz._reap_one("tok", "dc", "CreateServiceAccount", "lab-s1-sa", True)
        self.assertEqual(outcome, wz.FAILED)  # the resource is still there
        self.assertIn("matched 2", review)

    def test_reap_one_treats_an_already_gone_resource_as_absent(self):
        # The reap window overlaps by design, so a second pass over a reaped session finds the audit
        # Create with no resource behind it. That must not need review, and must not keep the user.
        with mock.patch.object(_owner("_reap_handler"), "_reap_handler", return_value=self.HANDLER), \
             mock.patch.object(_owner("_reap_find"), "_reap_find", return_value=(None, 0, None)):
            self.assertEqual(wz._reap_one("tok", "dc", "CreateReport", "lab-s1-r", True), (wz.ABSENT, None))

    def test_an_unhandled_type_is_unknown_and_does_not_block(self):
        # Handler coverage is partial by construction: the generic plural+search handler misses most
        # create types, so blocking a miss would fail every reap and retain every user.
        with mock.patch.object(_owner("_reap_handler"), "_reap_handler", return_value=self.HANDLER), \
             mock.patch.object(_owner("_reap_find"), "_reap_find", return_value=(None, None, "no such field")):
            outcome, review = wz._reap_one("tok", "dc", "CreateWidget", "lab-s1-w", True)
        self.assertEqual(outcome, wz.UNKNOWN)
        self.assertIn("no handler", review)
        self.assertNotIn(wz.UNKNOWN, wz._REAP_BLOCKING)

    def test_an_audit_entry_with_no_name_is_unknown(self):
        outcome, review = wz._reap_one("tok", "dc", "CreateWidget", None, True)
        self.assertEqual(outcome, wz.UNKNOWN)
        self.assertIn("no name in input", review)

    SA_HANDLER: typing.ClassVar = {"list": "serviceAccounts", "filter": "name", "soft": True,
                                   "delete": "deleteServiceAccount", "deleter": None}

    def test_a_cli_deployments_service_account_is_reaped_through_its_deployment(self):
        # deleteServiceAccount rejects an internal type:CLI account ("Internal service account cannot
        # be deleted"), so the uniform path is a permanent FAILED that retains the session's user.
        name = "lab-s1-cli-deployment-26dc83b5-503a-4725-a087-0764b1ef671d"
        sent = []

        def _gql(_tok, _dc, query, variables=None):
            sent.append(query)
            if "deployments" in query:
                return {"deployments": {"nodes": [{"id": "dep1", "name": "lab-s1-cli"}]}}, None
            return {}, None

        with mock.patch.object(_owner("_gql"), "_gql", side_effect=_gql), \
             mock.patch.object(_owner("_reap_find"), "_reap_find", return_value=(None, 0, None)):
            outcome, detail = wz._reap_service_account("tok", "dc", self.SA_HANDLER, "sa1", name)
        self.assertEqual((outcome, detail), (wz.REMOVED, None))
        self.assertTrue(any("deleteCliDeployment" in q for q in sent))
        self.assertFalse(any("deleteServiceAccount" in q for q in sent))

    def test_a_non_cli_service_account_still_takes_the_uniform_delete(self):
        # The sensor account comes from createServiceAccount and is deletable directly.
        with mock.patch.object(_owner("_reap_delete_uniform"), "_reap_delete_uniform",
                               return_value=(wz.REMOVED, None)) as uni:
            wz._reap_service_account("tok", "dc", self.SA_HANDLER, "sa1", "lab-s1-sensor")
        uni.assert_called_once()

    def test_a_cli_service_account_whose_deployment_is_gone_does_not_block(self):
        # Nothing left can delete the record, so blocking would retain the user with no pass able to
        # clear it.
        name = "lab-s1-cli-deployment-26dc83b5-503a-4725-a087-0764b1ef671d"
        with mock.patch.object(_owner("_gql"), "_gql", return_value=({"deployments": {"nodes": []}}, None)):
            outcome, detail = wz._reap_service_account("tok", "dc", self.SA_HANDLER, "sa1", name)
        self.assertEqual(outcome, wz.UNKNOWN)
        self.assertIn("lab-s1-cli", detail)
        self.assertNotIn(wz.UNKNOWN, wz._REAP_BLOCKING)

    def test_reap_enumeration_surfaces_graphql_errors(self):
        with mock.patch.object(_owner("_gql"), "_gql", return_value=({}, [{"message": "denied"}])):
            actions, alert = wz._reap_enumerate("tok", "dc", "lab-s1@example.com", 60)
        self.assertEqual(actions, [])
        self.assertIn("denied", alert)

    def _reap(self, outcome_or_actions, sweep=None):
        """cmd_reap over one audit outcome, with the sweep stubbed out. Returns the exit code."""
        one = (outcome_or_actions, "review") if isinstance(outcome_or_actions, str) else None
        actions = [("CreateWidget", "lab-s1-w")] if one else []
        with mock.patch.object(_owner("token_and_dc"), "token_and_dc", return_value=("tok", "dc", "tid")), \
             mock.patch.object(_owner("_reap_enumerate"), "_reap_enumerate", return_value=(actions, None)), \
             mock.patch.object(_owner("_reap_one"), "_reap_one", return_value=one), \
             mock.patch.object(_owner("_reap_sweep_type"), "_reap_sweep_type", return_value=sweep or wz.Counter()), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
             exits() as cm:
            call(wz.cmd_reap, ["--session", "s1", "--commit"])
        return cm.code

    def test_committed_reap_exits_3_when_enumeration_is_incomplete(self):
        with mock.patch.object(_owner("token_and_dc"), "token_and_dc", return_value=("tok", "dc", "tid")), \
             mock.patch.object(_owner("_reap_enumerate"), "_reap_enumerate", return_value=([], "denied")), \
             mock.patch.object(_owner("_reap_sweep_type"), "_reap_sweep_type", return_value=wz.Counter()), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()), \
             exits() as cm:
            call(wz.cmd_reap, ["--session", "s1", "--commit"])
        self.assertEqual(cm.code, 3)

    def test_which_outcomes_keep_the_user_and_the_retry(self):
        # Exit 3 is the only signal the reaper acts on: DEFERRED and FAILED earn one more daily pass.
        # Residue we cannot act on (UNKNOWN) is not cleanup that failed, or the reaper would retain
        # every lab-<sid>@ user it was built to delete.
        for outcome, want in [(wz.REMOVED, 0), (wz.ABSENT, 0), (wz.UNKNOWN, 0), (wz.DEFERRED, 3), (wz.FAILED, 3)]:
            with self.subTest(outcome=outcome):
                self.assertEqual(self._reap(outcome), want)
        self.assertEqual(self._reap(None, sweep=wz.Counter({wz.FAILED: 1})), 3)  # a sweep that did not finish

    def test_reap_one_reports_failed_when_delete_does_not_remove_resource(self):
        found = [("id1", 1, None), ("id1", 1, None)]
        with mock.patch.object(_owner("_reap_handler"), "_reap_handler", return_value=self.HANDLER), \
             mock.patch.object(_owner("_reap_find"), "_reap_find", side_effect=found), \
             mock.patch.object(_owner("_gql"), "_gql", return_value=({}, [{"message": "denied"}])):
            outcome, review = wz._reap_one("tok", "dc", "CreateReport", "lab-s1-report", True)
        self.assertEqual(outcome, wz.FAILED)
        self.assertIn("denied", review)

    def test_a_delete_id_is_a_variable_never_spliced_into_the_document(self):
        hostile = 'x" }) { _stub } } mutation { deleteTenant(input: { id: "y'
        sent = []

        def gql(tok, dc, query, variables=None):
            sent.append((query, variables))
            return {}, []

        with mock.patch.object(_owner("_gql"), "_gql", gql), \
             mock.patch.object(_owner("_reap_find"), "_reap_find", return_value=(None, 0, None)):
            outcome, _ = wz._reap_delete_uniform("tok", "dc", self.HANDLER, hostile, "lab-s1-report")
        self.assertEqual(outcome, wz.REMOVED)
        (query, variables), = sent
        self.assertIn(self.HANDLER["delete"], query)
        self.assertNotIn(hostile, query)
        self.assertEqual(variables, {"id": hostile})

    def test_committed_reap_counts_removals_the_sweep_could_not_have_named(self):
        actions = [("CreateReport", "lab-s1-r"), ("CreateReport", "Q3 report"), ("CreateReport", "old")]
        outcomes = [(wz.REMOVED, None), (wz.REMOVED, None), (wz.ABSENT, None)]
        err = io.StringIO()
        with mock.patch.object(_owner("token_and_dc"), "token_and_dc", return_value=("tok", "dc", "tid")), \
             mock.patch.object(_owner("_reap_enumerate"), "_reap_enumerate", return_value=(actions, None)), \
             mock.patch.object(_owner("_reap_one"), "_reap_one", side_effect=outcomes), \
             mock.patch.object(_owner("_reap_sweep_type"), "_reap_sweep_type",
                               return_value=wz.Counter({wz.REMOVED: 1})), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err), \
             exits() as cm:
            call(wz.cmd_reap, ["--session", "s1", "--commit"])
        self.assertEqual(cm.code, 0)
        swept = len(wz._SWEEP_TYPES)
        self.assertIn(f"# {2 + swept} removed (1 audit-only), 1 absent", err.getvalue())

    def _outpost_reap(self, status, delete_err=None, status_err=None):
        """Drives the real _reap_outpost. Returns (outcome, detail, mutations issued)."""
        sent = []

        def gql(tok, dc, query, variables=None):
            if query is wz.OUTPOST_Q:
                if status_err:
                    return {}, [{"message": status_err}]
                return {"outpost": ({"id": "o1", "status": status} if status else None)}, []
            sent.append("delete" if query is wz.DELETE_OUTPOST else "uninstall")
            return {}, ([{"message": delete_err}] if delete_err else [])

        with mock.patch.object(_owner("_gql"), "_gql", gql):
            outcome, detail = wz._reap_outpost("tok", "dc", None, "o1", "lab-s1")
        return outcome, detail, sent

    def test_a_live_outpost_is_uninstalled_then_deferred_never_deleted(self):
        # deleteOutpost fails on a live record, so the sweep must not try it; the reaper also must not
        # sit in a poll loop inside a cron container.
        outcome, _detail, sent = self._outpost_reap("CONNECTED")
        self.assertEqual((outcome, sent), (wz.DEFERRED, ["uninstall"]))

    def test_an_uninstalling_outpost_is_deferred_without_a_second_uninstall(self):
        # Every status carrying UNINSTALL is in or past the flow, and the second uninstall is the call
        # that refuses — so a PARTIALLY_UNINSTALLED record defers on its own, not on a refused mutation.
        for status in ("UNINSTALLING", "PARTIALLY_UNINSTALLED"):
            outcome, _detail, sent = self._outpost_reap(status)
            self.assertEqual((outcome, sent), (wz.DEFERRED, []), status)

    def test_an_uninstalled_outpost_is_deleted(self):
        for status in wz._OUTPOST_DELETABLE:
            outcome, _detail, sent = self._outpost_reap(status)
            self.assertEqual((outcome, sent), (wz.REMOVED, ["delete"]), status)

    def test_an_absent_outpost_is_absent_not_failed(self):
        self.assertEqual(self._outpost_reap(None)[0], wz.ABSENT)

    def test_an_outpost_delete_that_errors_is_failed(self):
        self.assertEqual(self._outpost_reap("UNINSTALLED", delete_err="internal error")[0], wz.FAILED)

    def test_an_unreadable_outpost_status_is_failed_not_deferred(self):
        # A guaranteed type whose state we cannot read has not been proven clean.
        self.assertEqual(self._outpost_reap("CONNECTED", status_err="denied")[0], wz.FAILED)

    def test_the_sweep_routes_outpost_through_its_own_deleter(self):
        self.assertIn("Outpost", wz._SWEEP_TYPES)
        self.assertIs(wz._reap_handler("Outpost")["deleter"], wz._reap_outpost)
        self.assertIsNone(wz._reap_handler("Report")["deleter"])

    def test_kc_session_bundles_setup_in_field_order(self):
        # The three user verbs unpack this positionally, so field ORDER is the contract: a swap of
        # token/email would silently send the token as the lookup key.
        with mock.patch.object(_owner("_kc_env"), "_kc_env", return_value=("http://kc", "realm", "admin", "pw")), \
             mock.patch.object(_owner("_kc_token"), "_kc_token", return_value="tok"):
            s = wz._kc_session(wz.parse(("user", "inspect"), ["--session", "s1"]))
        self.assertEqual((s.endpoint, s.realm, s.token), ("http://kc", "realm", "tok"))
        self.assertEqual((s.email, s.name), ("lab-s1@titra-labs.ai", "lab-s1"))
        self.assertEqual(tuple(s), ("http://kc", "realm", "tok", "lab-s1@titra-labs.ai", "lab-s1"))

class Pagination(unittest.TestCase):
    """Every lookup that feeds a delete or an ==1 guard walks the whole connection, and a walk it cannot
    finish is a refusal, never "absent"."""

    @staticmethod
    def _pages(field, *pages, cursors=None):
        """An api() fake serving `pages` in order, keyed by the `after` variable it receives."""
        cursors = cursors or [f"c{i}" for i in range(len(pages))]
        by_after = {None: 0, **{cursors[i]: i + 1 for i in range(len(pages) - 1)}}
        seen = []

        def side(query, variables):
            seen.append(variables.get("after"))
            i = by_after[variables.get("after")]
            last = i == len(pages) - 1
            return {field: {"nodes": pages[i],
                            "pageInfo": {"hasNextPage": not last, "endCursor": None if last else cursors[i]}}}, "tid"
        side.seen = seen
        return side

    def test_an_exact_match_past_the_first_page_is_found(self):
        page1 = [{"id": str(i), "name": f"lab-s1-sensor-{i}"} for i in range(50)]
        side = self._pages("serviceAccounts", page1, [{"id": "sa", "name": "lab-s1-sensor"}])
        with mock.patch.object(_owner("api"), "api", side_effect=side):
            self.assertEqual(wz._find_sa("lab-s1-sensor")["id"], "sa")
        self.assertEqual(side.seen, [None, "c0"])

    def test_a_duplicate_past_the_first_page_is_deleted_by_ensure(self):
        # Before paging, `live[1:]` was computed on one page and a duplicate on the next survived.
        live = [{"id": "w1", "name": "lab-x", "enabled": True}], [{"id": "w2", "name": "lab-x", "enabled": False}]
        side = self._pages("automationWorkflows", *live)
        with mock.patch.object(_owner("api"), "api", side_effect=side):
            hits = wz._resolve_workflows("lab-x", exact=True)
        self.assertEqual([h["id"] for h in hits], ["w1", "w2"])

    def test_a_server_that_ignores_after_is_environment_3_not_absent(self):
        # Same page, same cursor, forever: without the guard the loop never ends or, capped, concludes
        # "absent" on a set it never finished reading.
        def side(query, variables):
            return {"cicdScanPolicies": {"nodes": [{"id": "p", "name": "other"}],
                                         "pageInfo": {"hasNextPage": True, "endCursor": "same"}}}, "tid"
        with mock.patch.object(_owner("api"), "api", side_effect=side), exits() as cm:
            wz._find_policy("fixture")
        self.assertEqual(cm.code, 3)

    def test_has_next_page_without_a_cursor_is_environment_3(self):
        def side(query, variables):
            return {"outposts": {"nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": None}}}, "tid"
        with mock.patch.object(_owner("api"), "api", side_effect=side), exits() as cm:
            wz._resolve_outpost("lab-s1")
        self.assertEqual(cm.code, 3)

    def test_the_page_cap_is_a_refusal(self):
        def side(query, variables):
            n = int(variables.get("after") or 0)
            return {"sensors": {"nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": str(n + 1)}}}, "tid"
        with mock.patch.object(_owner("api"), "api", side_effect=side) as api, exits() as cm:
            wz._resolve_sensor("lab-s1")
        self.assertEqual(cm.code, 3)
        self.assertEqual(api.call_count, wz._PAGE_CAP)

    def test_a_response_without_page_info_is_one_complete_page(self):
        with mock.patch.object(_owner("api"), "api", return_value=({"serviceAccounts": {"nodes": []}}, "tid")) as api:
            self.assertIsNone(wz._find_sa("lab-s1-sensor"))
        api.assert_called_once()

    # --- the reaper's list, through _gql (errors returned, never die)
    HANDLER: typing.ClassVar = {"list": "reports", "filter": "search", "delete": "deleteReport",
                                "soft": False, "deleter": None}

    def _gql_pages(self, *pages):
        side = self._pages("reports", *pages)
        return lambda tok, dc, q, v=None: (side(q, v or {})[0], []), side

    def test_the_sweep_walks_every_page_before_deleting(self):
        # The design review's F4 probe, inverted: 100 stem-named reports on page one and 3 on page two
        # are all swept, and the second request carries the first page's cursor.
        page1 = [{"id": str(i), "name": f"lab-s1-{i}"} for i in range(100)]
        page2 = [{"id": f"x{i}", "name": f"lab-s1-x{i}"} for i in range(3)]
        gql, side = self._gql_pages(page1, page2)
        with mock.patch.object(_owner("_gql"), "_gql", side_effect=gql), contextlib.redirect_stdout(io.StringIO()):
            tally = wz._reap_sweep_type("tok", "dc", "Report", "lab-s1", False)
        self.assertEqual(tally, wz.Counter({wz.REMOVED: 103}))
        self.assertEqual(side.seen, [None, "c0"])

    def test_a_sweep_that_cannot_finish_deletes_nothing_and_fails(self):
        def gql(tok, dc, q, v=None):
            if "mutation" in q:
                self.fail("a delete was issued from a partial list")
            return {"reports": {"nodes": [{"id": "r", "name": "lab-s1-r"}],
                                "pageInfo": {"hasNextPage": True, "endCursor": "same"}}}, []
        with mock.patch.object(_owner("_gql"), "_gql", side_effect=gql), contextlib.redirect_stdout(io.StringIO()):
            tally = wz._reap_sweep_type("tok", "dc", "Report", "lab-s1", True)
        self.assertEqual(tally, wz.Counter({wz.FAILED: 1}))

    def test_the_exact_one_guard_counts_across_pages(self):
        gql, _ = self._gql_pages([{"id": "a", "name": "lab-s1-r"}], [{"id": "b", "name": "lab-s1-r"}])
        with mock.patch.object(_owner("_gql"), "_gql", side_effect=gql):
            rid, count, err = wz._reap_find("tok", "dc", self.HANDLER, "lab-s1-r")
        self.assertEqual((rid, count, err), (None, 2, None))

    def test_audit_enumeration_stops_at_the_cap_with_an_alert(self):
        def gql(tok, dc, q, v=None):
            n = int((v or {}).get("after") or 0)
            return {"auditLogEntries": {"nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": str(n + 1)}}}, []
        with mock.patch.object(_owner("_gql"), "_gql", side_effect=gql):
            actions, alert = wz._reap_enumerate("tok", "dc", "lab-s1@example.com", 60)
        self.assertEqual(actions, [])
        self.assertIn(f"more than {wz._PAGE_CAP} pages", alert)

class CloudSelection(unittest.TestCase):
    """The AWS-blindness this fixes failed in the worst direction: a live CONNECTED GCP connector
    graded as "no connector targets", telling a learner they hadn't done what they had just done."""
    GCP_NODE: typing.ClassVar = {"id": "g", "name": "lab-s1-connector", "enabled": True,
                                 "status": "CONNECTED", "type": {"id": "gcp"},
                                 "config": {"projectId": "wiz-lab-42"}}

    def test_default_is_aws_and_bad_value_is_invocation_error(self):
        self.assertEqual(wz.parse(("connector", "inspect"), []).cloud, "aws")
        self.assertEqual(wz.parse(("connector", "inspect"), ["--cloud", "gcp"]).cloud, "gcp")
        with exits() as cm:
            wz.parse(("connector", "inspect"), ["--cloud", "oracle"])
        self.assertEqual(cm.code, 2)

    def test_gcp_connector_found_only_when_cloud_is_gcp(self):
        with mock.patch.object(_owner("api"), "api", return_value=({"connectors": {"nodes": [self.GCP_NODE]}}, "tid")):
            self.assertEqual([n["id"] for n in wz.find_connector("wiz-lab-42", "gcp")], ["g"])
            self.assertEqual(wz.find_connector("wiz-lab-42", "aws"), [])  # the old bug, locked down

    def test_gcp_inspect_healthy_exit_0(self):
        with mock.patch.object(_owner("find_connector"), "find_connector", return_value=[self.GCP_NODE]), \
             exits() as cm:
            call(wz.cmd_connector_inspect, ["--cloud", "gcp", "--account-id", "wiz-lab-42", "--require", "healthy"])
        self.assertEqual(cm.code, 0)

    def test_gcp_ensure_is_create_if_absent_never_patch(self):
        # No customerRoleARN equivalent to drift, so an existing connector is a no-op, not an update.
        with mock.patch.object(_owner("find_connector"), "find_connector", return_value=[self.GCP_NODE]), \
             mock.patch.object(_owner("api"), "api") as api, exits() as cm:
            call(wz.cmd_connector_ensure, ["--cloud", "gcp", "--account-id", "wiz-lab-42"])
        self.assertEqual(cm.code, 0)
        api.assert_not_called()

    def test_gcp_create_payload_is_managed_identity_with_empty_scopes(self):
        created = {"createConnector": {"connector": {"id": "n", "name": "lab-s1-connector", "status": "INITIAL"}}}
        with mock.patch.object(_owner("find_connector"), "find_connector", return_value=[]), \
             mock.patch.object(_owner("api"), "api", return_value=(created, "tid")) as api, \
             exits() as cm:
            call(wz.cmd_connector_ensure, ["--cloud", "gcp", "--account-id", "wiz-lab-42", "--session", "s1"])
        self.assertEqual(cm.code, 0)
        payload = api.call_args[0][1]["input"]
        self.assertEqual(payload["type"], "gcp")
        self.assertEqual(payload["authParams"], {"isManagedIdentity": True, "project_id": "wiz-lab-42"})
        self.assertTrue(all(v == [] for v in payload["extraConfig"].values()))
        self.assertNotIn("customerRoleARN", json.dumps(payload))

    def test_unsupported_paths_refuse_rather_than_guess(self):
        with exits() as cm:
            call(wz.cmd_connector_ensure, ["--cloud", "azure", "--account-id", "sub-1"])
        self.assertEqual(cm.code, 2)
        with exits() as cm:  # provisioning belongs to terraform, not wizlab
            call(wz.cmd_role_ensure, ["--cloud", "gcp"])
        self.assertEqual(cm.code, 2)


class TransientGraphqlErrors(unittest.TestCase):
    """Wiz returns a transient fault as a GraphQL error with HTTP 200, so _post's 5xx retry never sees
    it. Unretried, a read that 500s becomes "you didn't do the work" on a learner's screen."""

    def _post_returning(self, *responses):
        return mock.MagicMock(side_effect=list(responses))

    def test_transient_read_error_is_retried_then_succeeds(self):
        boom = {"errors": [{"message": "Internal server error"}], "data": None}
        ok = {"data": {"connectors": {"totalCount": 1}}}
        post = self._post_returning(boom, boom, ok)
        with mock.patch.object(_owner("token_and_dc"), "token_and_dc", return_value=("t", "dc", "tid")), \
             mock.patch.object(_owner("_post"), "_post", post), mock.patch.object(wz.time, "sleep"):
            data, _ = wz.api("query Q { connectors { totalCount } }", {})
        self.assertEqual(data["connectors"]["totalCount"], 1)
        self.assertEqual(post.call_count, 3)

    def test_a_mutation_is_never_retried(self):
        # It may have applied; retrying could create a second connector.
        boom = {"errors": [{"message": "Internal server error"}], "data": None}
        post = self._post_returning(boom, boom, boom)
        with mock.patch.object(_owner("token_and_dc"), "token_and_dc", return_value=("t", "dc", "tid")), \
             mock.patch.object(_owner("_post"), "_post", post), mock.patch.object(wz.time, "sleep"), \
             exits() as cm:
            wz.api("mutation M { createConnector { id } }", {})
        self.assertEqual(cm.code, 3)
        self.assertEqual(post.call_count, 1)

    def test_a_real_error_is_not_retried(self):
        bad = {"errors": [{"message": "Resource not found"}], "data": None}
        post = self._post_returning(bad, bad, bad)
        with mock.patch.object(_owner("token_and_dc"), "token_and_dc", return_value=("t", "dc", "tid")), \
             mock.patch.object(_owner("_post"), "_post", post), exits() as cm:
            wz.api("query Q { x }", {})
        self.assertEqual(cm.code, 3)
        self.assertEqual(post.call_count, 1)


class MutationSubmissionBudget(unittest.TestCase):
    """A mutation is SENT once. Wiz applies it before answering, so a 503, a timeout or an undecodable
    body leaves the outcome unknown, and a resend creates the second connector. Reads keep the retry.
    These drive the real transport — mocking `_post` away is what let the retry hide underneath it."""

    def setUp(self):
        self.enterContext(mock.patch.object(wz.time, "sleep"))
        self.enterContext(mock.patch.object(_owner("token_and_dc"), "token_and_dc", return_value=("t", "dc", "tid")))

    def _sends(self, fn, *args, side_effect=None, body=None):
        """Returns (exit_code, submissions, message)."""
        if side_effect is None:
            cm = mock.MagicMock()
            cm.__enter__.return_value.read.return_value = body
            op = mock.MagicMock(return_value=cm)
        else:
            op = mock.MagicMock(side_effect=side_effect)
        err = io.StringIO()
        with mock.patch.object(wz.urllib.request, "urlopen", op), \
             contextlib.redirect_stderr(err), exits() as cm_exit:
            fn(*args)
        return cm_exit.code, op.call_count, err.getvalue()

    @staticmethod
    def _http(code):
        return urllib.error.HTTPError("https://x/", code, "boom", None, io.BytesIO(b"unavailable"))

    MUTATION = "mutation M { createConnector { id } }"

    def test_the_rule_is_a_property_of_the_document(self):
        self.assertEqual(wz._submissions("query Q { x }"), 3)
        self.assertEqual(wz._submissions("  { x }"), 3)  # anonymous query
        self.assertEqual(wz._submissions(self.MUTATION), 1)
        self.assertEqual(wz._submissions("subscription S { x }"), 1)  # unrecognised counts as unsafe

    def test_a_mutation_is_submitted_once_after_http_503(self):
        code, sends, msg = self._sends(wz.api, self.MUTATION, {}, side_effect=self._http(503))
        self.assertEqual((code, sends), (3, 1))
        self.assertIn("not resubmitted", msg)

    def test_a_mutation_is_submitted_once_after_a_timeout(self):
        code, sends, _ = self._sends(wz.api, self.MUTATION, {}, side_effect=TimeoutError("timed out"))
        self.assertEqual((code, sends), (3, 1))

    def test_a_mutation_is_submitted_once_when_the_response_will_not_decode(self):
        code, sends, _ = self._sends(wz.api, self.MUTATION, {}, body=b"<html>502 Bad Gateway</html>")
        self.assertEqual((code, sends), (3, 1))

    def test_a_read_still_resends_after_503(self):
        code, sends, _ = self._sends(wz.api, "query Q { connectors { id } }", {},
                                     side_effect=self._http(503))
        self.assertEqual((code, sends), (3, 3))

    def test_a_reap_delete_is_submitted_once(self):
        delete = 'mutation { deleteReport(input: { id: "r1" }) { _stub } }'
        code, sends, _ = self._sends(wz._gql, "tok", "dc", delete, side_effect=self._http(503))
        self.assertEqual((code, sends), (3, 1))


class ConnectorLookupLayers(unittest.TestCase):
    """The account LINK lags create by 1-2 min, so the fallbacks decide what a check reports during
    the window a learner is most likely to click Check."""
    GCP: typing.ClassVar = {"id": "g", "name": "lab-s1-connector", "enabled": True,
                            "status": "CONNECTED", "type": {"id": "gcp"},
                            "config": {"projectId": "proj-1"}}

    def _api(self, find=None, search=None, bytype=None, total=0):
        def side(query, variables):
            if query == wz.FIND:
                return {"connectors": {"nodes": find or []}}, "tid"
            if query == wz.SEARCH:
                return {"connectors": {"nodes": search or [], "totalCount": len(search or [])}}, "tid"
            if query == wz.BY_TYPE:
                return {"connectors": {"nodes": bytype or [], "totalCount": total}}, "tid"
            return {}, "tid"
        return side

    def test_name_search_covers_the_pre_link_window(self):
        # Nothing linked yet, but the stem finds it — and BY_TYPE is never consulted.
        api = mock.MagicMock(side_effect=self._api(find=[], search=[self.GCP]))
        with mock.patch.object(_owner("api"), "api", api):
            self.assertEqual([n["id"] for n in wz.find_connector("proj-1", "gcp", "lab-s1")], ["g"])
        self.assertNotIn(wz.BY_TYPE, [c[0][0] for c in api.call_args_list])

    def test_search_result_must_still_target_the_account(self):
        # A same-stem connector for a DIFFERENT project is not this lab's connector.
        other = {**self.GCP, "config": {"projectId": "proj-2"}}
        with mock.patch.object(_owner("api"), "api", side_effect=self._api(search=[other], total=1)):
            self.assertEqual(wz.find_connector("proj-1", "gcp", "lab-s1"), [])

    def test_search_ignores_child_deployments(self):
        child = {"id": "c", "name": "GAR in lab-s1-connector", "enabled": True, "status": "CONNECTED",
                 "type": {"id": "gar"}, "config": {"projectId": "proj-1"}}
        with mock.patch.object(_owner("api"), "api", side_effect=self._api(search=[child, self.GCP])):
            self.assertEqual([n["id"] for n in wz.find_connector("proj-1", "gcp", "lab-s1")], ["g"])

    def test_beyond_the_page_is_environment_3_not_learner_1(self):
        # Past BY_TYPE_PAGE, "no match" stops meaning "absent". Reporting 1 would tell a learner they
        # did nothing; it would also let `ensure` create a duplicate of a connector it cannot see.
        with mock.patch.object(_owner("api"), "api", side_effect=self._api(total=wz.BY_TYPE_PAGE + 1)), \
             exits() as cm:
            wz.find_connector("proj-1", "gcp", None)
        self.assertEqual(cm.code, 3)

    def test_within_the_page_absence_is_still_learner_state(self):
        with mock.patch.object(_owner("api"), "api", side_effect=self._api(total=wz.BY_TYPE_PAGE)):
            self.assertEqual(wz.find_connector("proj-1", "gcp", None), [])

    def test_stem_is_optional_and_never_dies(self):
        with mock.patch.dict(wz.os.environ, {}, clear=True):
            self.assertIsNone(wz._stem_opt(wz.parse(("connector", "inspect"), [])))
        self.assertEqual(wz._stem_opt(wz.parse(("connector", "inspect"), ["--session", "s1"])), "lab-s1")


class AzureConnector(unittest.TestCase):
    SUB = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    def test_uppercased_subscription_id_is_normalised(self):
        # An uppercased GUID returns totalCount 0 from Wiz rather than an error, so a check would
        # grade a healthy subscription as empty. AWS digits and GCP project ids pass through.
        self.assertEqual(wz._norm_account(self.SUB.upper()), self.SUB)
        self.assertEqual(wz._norm_account("111111111111"), "111111111111")
        self.assertEqual(wz._norm_account("wiz-lab-42"), "wiz-lab-42")

    def test_ensure_needs_a_tenant_id(self):
        with mock.patch.dict(wz.os.environ, {}, clear=True), exits() as cm:
            call(wz.cmd_connector_ensure, ["--cloud", "azure", "--account-id", self.SUB])
        self.assertEqual(cm.code, 2)

    def test_ensure_payload_is_managed_identity_with_subscription_and_tenant(self):
        created = {"createConnector": {"connector": {"id": "n", "name": "lab-s1-connector", "status": "INITIAL"}}}
        with mock.patch.object(_owner("find_connector"), "find_connector", return_value=[]), \
             mock.patch.object(_owner("api"), "api", return_value=(created, "tid")) as api, \
             exits() as cm:
            call(wz.cmd_connector_ensure, ["--cloud", "azure", "--account-id", self.SUB,
                                     "--tenant-id", "dir-1", "--session", "s1"])
        self.assertEqual(cm.code, 0)
        payload = api.call_args[0][1]["input"]
        self.assertEqual(payload["type"], "azure")
        self.assertEqual(payload["authParams"],
                         {"isManagedIdentity": True, "subscriptionId": self.SUB, "tenantId": "dir-1"})
        self.assertEqual(len(payload["extraConfig"]), 6)
        self.assertTrue(all(v == [] for v in payload["extraConfig"].values()))


class AzureRoleInspect(unittest.TestCase):
    SUB = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    OID = "11111111-2222-3333-4444-555555555555"

    DEFAULT_ARGV: typing.ClassVar = ["--cloud", "azure", "--account-id", SUB, "--role-name", "WizCustomRole"]

    def _run(self, proc, argv=None, env=None):
        env = {"WIZ_TBCMP_AZURE_APP_OBJECT_ID": self.OID} if env is None else env
        with mock.patch.object(_owner("_az"), "_az", return_value=proc) as az, \
             mock.patch.dict(wz.os.environ, env, clear=True), \
             exits() as cm:
            call(wz.cmd_role_inspect, argv if argv is not None else self.DEFAULT_ARGV)
        return cm.code, az

    def test_both_roles_assigned_exit_0(self):
        code, az = self._run(_proc(0, json.dumps(["Reader", "WizCustomRole"])))
        self.assertEqual(code, 0)
        # --fill-principal-name false is load-bearing: the default resolves names via Graph, which
        # Entra denies on this lease, so the call would fail for a non-learner reason.
        self.assertIn("--fill-principal-name", az.call_args[0])
        self.assertIn("false", az.call_args[0])

    def test_missing_role_name_flag_is_invocation_error(self):
        with mock.patch.dict(wz.os.environ, {"WIZ_TBCMP_AZURE_APP_OBJECT_ID": self.OID}, clear=True), \
             exits() as cm:
            call(wz.cmd_role_inspect, ["--cloud", "azure", "--account-id", self.SUB])
        self.assertEqual(cm.code, 2)

    def test_missing_role_exit_1(self):
        self.assertEqual(self._run(_proc(0, json.dumps(["Reader"])))[0], 1)

    def test_session_scoped_custom_role_name(self):
        argv = ["--cloud", "azure", "--account-id", self.SUB, "--role-name", "lab-s1-WizCustomRole"]
        self.assertEqual(self._run(_proc(0, json.dumps(["Reader", "lab-s1-WizCustomRole"])), argv)[0], 0)
        self.assertEqual(self._run(_proc(0, json.dumps(["Reader", "WizCustomRole"])), argv)[0], 1)

    def test_az_failure_is_environment_3(self):
        self.assertEqual(self._run(_proc(1, "", "AuthorizationFailed"))[0], 3)

    def test_missing_operator_secret_is_environment_3(self):
        self.assertEqual(self._run(_proc(0, "[]"), env={})[0], 3)


class WizTenantFacts(unittest.TestCase):
    def _run(self, params, tid="tid-1"):
        with mock.patch.object(_owner("api"), "api", return_value=({"managedIdentityParameters": params}, tid)), \
             mock.patch.dict(wz.os.environ, {}, clear=True), \
             mock.patch.object(wz.sys, "stdout", io.StringIO()) as out, \
             exits() as cm:
            call(wz.cmd_wiz_tenant, [])
        return cm.code, out.getvalue()

    def test_emits_gcp_service_account(self):
        code, text = self._run({"aws": {}, "gcp": {"serviceAccountEmail": "wizabc@prod-us100.iam.gserviceaccount.com"}})
        self.assertEqual(code, 0)
        self.assertIn("WIZ_GCP_SERVICE_ACCOUNT=wizabc@prod-us100.iam.gserviceaccount.com", text)

    def test_gcp_only_tenant_does_not_die_on_missing_aws_delegator(self):
        # The coupling this replaced would have failed a GCP lab for an absent AWS fact.
        code, text = self._run({"aws": {}, "gcp": {"serviceAccountEmail": "wizabc@prod-us100.iam.gserviceaccount.com"}})
        self.assertEqual(code, 0)
        self.assertNotIn("WIZ_REMOTE_ARN", text)  # empty facts are omitted, not emitted blank

    def test_no_facts_at_all_is_environment_3(self):
        # No aws delegator, no gcp SA AND no tid in the token — nothing a caller could consume.
        self.assertEqual(self._run({"aws": {}, "gcp": {}}, tid=None)[0], 3)


class SessionVerifyCsp(unittest.TestCase):
    def _mock_wiz(self):
        return mock.patch.object(_owner("token_and_dc"), "token_and_dc", return_value=("tok", "dc", "tid"))

    def _mock_api(self):
        return mock.patch.object(_owner("api"), "api", return_value=({"connectors": {"totalCount": 0}}, "tid"))

    def test_no_cloud_skips_csp_probe(self):
        with self._mock_wiz(), self._mock_api(), \
             mock.patch.object(_owner("_aws"), "_aws") as csp, \
             exits() as cm:
            call(wz.cmd_session_verify, [])
        self.assertEqual(cm.code, 0)
        csp.assert_not_called()

    def test_unknown_cloud_is_invocation_error(self):
        with self._mock_wiz(), self._mock_api(), exits() as cm:
            call(wz.cmd_session_verify, ["--cloud", "oracle"])
        self.assertEqual(cm.code, 2)

    def test_missing_csp_vars_exit_3(self):
        with self._mock_wiz(), self._mock_api(), \
             mock.patch.dict(wz.os.environ, {}, clear=True), \
             exits() as cm:
            call(wz.cmd_session_verify, ["--cloud", "aws"])
        self.assertEqual(cm.code, 3)

    def test_csp_probe_failure_exit_3(self):
        env = {"AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "y"}
        with self._mock_wiz(), self._mock_api(), \
             mock.patch.dict(wz.os.environ, env, clear=True), \
             mock.patch.object(_owner("_aws"), "_aws", return_value=_proc(1, "", "ExpiredToken")), \
             exits() as cm:
            call(wz.cmd_session_verify, ["--cloud", "aws"])
        self.assertEqual(cm.code, 3)

    def test_csp_probe_success_exit_0(self):
        env = {"AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "y"}
        with self._mock_wiz(), self._mock_api(), \
             mock.patch.dict(wz.os.environ, env, clear=True), \
             mock.patch.object(_owner("_aws"), "_aws", return_value=_proc(0, '{"Account": "123456789012"}')), \
             exits() as cm:
            call(wz.cmd_session_verify, ["--cloud", "aws"])
        self.assertEqual(cm.code, 0)

    def _verify(self, cloud, env, proc, args=()):
        binary = {"aws": "_aws", "gcp": "_gcp", "azure": "_az"}[cloud]
        with self._mock_wiz(), self._mock_api(), \
             mock.patch.dict(wz.os.environ, env, clear=True), \
             mock.patch.object(_owner(binary), binary, return_value=proc), \
             contextlib.redirect_stdout(io.StringIO()), exits() as cm:
            call(wz.cmd_session_verify, ["--cloud", cloud, *args])
        return cm.code

    GCP_ENV: typing.ClassVar = {"GOOGLE_CREDENTIALS": "{}", "GOOGLE_PROJECT": "wiz-lab-42"}
    AWS_ENV: typing.ClassVar = {"AWS_ACCESS_KEY_ID": "x", "AWS_SECRET_ACCESS_KEY": "y"}

    def test_gcp_with_no_active_account_is_environment_3(self):
        # `gcloud auth list` exits 0 with `[]` when nothing activated, which used to pass verification
        # and left a terraform apply mid-lab to discover there are no credentials.
        self.assertEqual(self._verify("gcp", self.GCP_ENV, _proc(0, "[]")), 3)
        revoked = '[{"account": "a@b.iam.gserviceaccount.com", "status": ""}]'
        self.assertEqual(self._verify("gcp", self.GCP_ENV, _proc(0, revoked)), 3)

    def test_gcp_with_an_active_account_exits_0(self):
        active = '[{"account": "a@b.iam.gserviceaccount.com", "status": "ACTIVE"}]'
        self.assertEqual(self._verify("gcp", self.GCP_ENV, _proc(0, active)), 0)

    def test_unparseable_identity_is_environment_3(self):
        self.assertEqual(self._verify("gcp", self.GCP_ENV, _proc(0, "Updates are available")), 3)
        self.assertEqual(self._verify("aws", self.AWS_ENV, _proc(0, "")), 3)

    def test_aws_credentials_for_another_account_cannot_pass(self):
        proc = _proc(0, '{"Account": "999999999999"}')
        self.assertEqual(self._verify("aws", self.AWS_ENV, proc, ["--account-id", "123456789012"]), 3)
        self.assertEqual(self._verify("aws", self.AWS_ENV, proc, ["--account-id", "999999999999"]), 0)

    def test_azure_must_be_logged_in_to_the_declared_subscription(self):
        env = {"ARM_CLIENT_ID": "c", "ARM_CLIENT_SECRET": "s", "ARM_TENANT_ID": "t",
               "ARM_SUBSCRIPTION_ID": "5da4a5ee-0000-0000-0000-000000000001"}
        self.assertEqual(self._verify("azure", env, _proc(0, '{"id": "other-subscription"}')), 3)
        self.assertEqual(self._verify("azure", env, _proc(0, '{"id": "5DA4A5EE-0000-0000-0000-000000000001"}')), 0)


class GcpRoleInspect(unittest.TestCase):
    SA = "wizdeadbeef@prod-us100.iam.gserviceaccount.com"

    def _policy(self, roles, member=None):
        member = member or f"serviceAccount:{self.SA}"
        return json.dumps({"bindings": [{"role": r, "members": [member]} for r in roles]})

    def _run(self, proc, sa=SA):
        with mock.patch.object(_owner("_gcp"), "_gcp", return_value=proc), \
             mock.patch.object(_owner("_wiz_gcp_sa"), "_wiz_gcp_sa", return_value=sa), \
             exits() as cm:
            call(wz.cmd_role_inspect, ["--cloud", "gcp", "--account-id", "wiz-lab-42"])
        return cm.code

    def test_all_five_bound_exit_0(self):
        self.assertEqual(self._run(_proc(0, self._policy(wz.GCP_WIZ_ROLES))), 0)

    def test_one_missing_exit_1(self):
        self.assertEqual(self._run(_proc(0, self._policy(wz.GCP_WIZ_ROLES[:-1]))), 1)

    def test_bound_to_a_different_member_exit_1(self):
        other = self._policy(wz.GCP_WIZ_ROLES, member="user:someone@example.com")
        self.assertEqual(self._run(_proc(0, other)), 1)

    def test_gcloud_failure_is_environment_3(self):
        # No "not found" here means learner state: the project IS the lease, so any failure is env.
        self.assertEqual(self._run(_proc(1, "", "PERMISSION_DENIED")), 3)

    def test_empty_tenant_sa_is_environment_3(self):
        self.assertEqual(self._run(_proc(0, self._policy(wz.GCP_WIZ_ROLES)), sa=""), 3)


class SensorDetectionGrading(unittest.TestCase):
    """SensorStatus is ACTIVE/INACTIVE only, and `search` is a server-side substring."""
    ACTIVE: typing.ClassVar = [{"id": "s1", "name": "lab-x", "status": "ACTIVE", "type": "LINUX_VIRTUAL_MACHINE"}]

    def _exit(self, argv, sensors):
        return exit_code(wz.cmd_sensor_inspect, argv, wiz=FakeWiz(sensors=_conn(*sensors)))

    def test_active_is_the_only_status_that_satisfies_active(self):
        argv = ["--name", "lab-x", "--require", "active"]
        self.assertEqual(self._exit(argv, self.ACTIVE), 0)
        self.assertEqual(self._exit(argv, [dict(self.ACTIVE[0], status="INACTIVE")]), 1)

    def test_name_matches_exactly_not_substring(self):
        # A longer name that merely contains the stem is a neighbour session's sensor.
        self.assertEqual(self._exit(["--name", "lab-x"], [dict(self.ACTIVE[0], id="s2", name="lab-xyz")]), 1)


class WorkflowGrading(unittest.TestCase):
    """Locks two things a live tenant proved and a reader would otherwise get wrong: `enabled` is the
    only publish signal (activeVersion/draftVersion/versions read null/0 on every workflow), and the
    graded branch comes from a run's outboundEdge on a SWITCH_CASE step, not from the definition."""

    LIVE: typing.ClassVar = [{"id": "w1", "name": "lab-x-night-watch", "enabled": True,
                              "project": {"name": "p"},
                              "steps": [{"id": "s1", "name": "Route", "type": "SWITCH_CASE"}]}]

    def _wiz(self, wf_nodes, run_nodes=(), issues=(), **fields):
        return FakeWiz(automationWorkflows={"nodes": wf_nodes}, automationWorkflowRuns={"nodes": list(run_nodes)},
                       validateAutomationWorkflow={"issues": list(issues)},
                       createAutomationWorkflow={"workflow": dict(self.LIVE[0], id="w1")},
                       updateAutomationWorkflow={"workflow": {"id": "w1", "name": "lab-x", "enabled": True}}, **fields)

    def _run(self, edge, stype="SWITCH_CASE"):
        return {"id": "r1", "status": "COMPLETED",
                "steps": [{"status": "COMPLETED", "outboundEdge": edge,
                           "step": {"name": "Route", "type": stype}}]}

    def _exit(self, fn, argv, wiz):
        return exit_code(fn, argv, wiz=wiz)

    def test_published_is_enabled_only(self):
        argv = ["--name", "lab-x", "--require", "published"]
        self.assertEqual(self._exit(wz.cmd_workflow_inspect, argv, self._wiz(self.LIVE)), 0)
        saved = [dict(self.LIVE[0], enabled=False)]
        self.assertEqual(self._exit(wz.cmd_workflow_inspect, argv, self._wiz(saved)), 1)

    def test_stem_matches_by_prefix_but_exact_name_pins(self):
        # The guide tells a learner to type lab-<sid>-night-watch, so the stem must match a suffixed
        # name. --exact-name is for a caller that knows the whole thing.
        self.assertEqual(self._exit(wz.cmd_workflow_inspect, ["--name", "lab-x"], self._wiz(self.LIVE)), 0)
        self.assertEqual(
            self._exit(wz.cmd_workflow_inspect, ["--name", "lab-x", "--exact-name"], self._wiz(self.LIVE)), 1)

    def test_enabled_outranks_disabled_leftover_on_same_stem(self):
        nodes = [dict(self.LIVE[0], id="old", enabled=False), self.LIVE[0]]
        self.assertEqual(
            self._exit(wz.cmd_workflow_inspect, ["--name", "lab-x", "--require", "published"],
                       self._wiz(nodes)), 0)

    def test_run_branch_grades_the_edge_taken(self):
        argv = ["--name", "lab-x", "--require", "branch", "--branch", "Malicious"]
        self.assertEqual(
            self._exit(wz.cmd_workflowrun_inspect, argv, self._wiz(self.LIVE, [self._run("Malicious")])), 0)
        self.assertEqual(
            self._exit(wz.cmd_workflowrun_inspect, argv, self._wiz(self.LIVE, [self._run("default")])), 1)

    def test_run_branch_grades_every_step_type(self):
        # A CONDITION leaves by true/false and an unbranched step by main; a verb that read only
        # SWITCH_CASE edges printed "none" on a run that demonstrably took the false edge.
        argv = ["--name", "lab-x", "--require", "branch", "--branch", "false"]
        self.assertEqual(
            self._exit(wz.cmd_workflowrun_inspect, argv, self._wiz(self.LIVE, [self._run("false", "CONDITION")])), 0)
        self.assertEqual(
            self._exit(wz.cmd_workflowrun_inspect, argv, self._wiz(self.LIVE, [self._run("true", "CONDITION")])), 1)

    def _failed_step_run(self, edge="error", status="FAILED"):
        return {"id": "r2", "status": "COMPLETED",
                "steps": [{"status": status, "outboundEdge": edge, "step": {"name": "Look up team", "type": "ECHO"}},
                          {"status": "COMPLETED", "outboundEdge": "main", "step": {"name": "Alert", "type": "ECHO"}}]}

    def test_run_error_path_needs_a_failed_step_inside_a_completed_run(self):
        # Every failed step reads outboundEdge "error", edge or no edge; the run filter admits COMPLETED
        # runs only, so FAILED-step-in-COMPLETED-run is what proves the edge was followed.
        argv = ["--name", "lab-x", "--require", "error-path"]
        self.assertEqual(
            self._exit(wz.cmd_workflowrun_inspect, argv, self._wiz(self.LIVE, [self._failed_step_run()])), 0)
        self.assertEqual(
            self._exit(wz.cmd_workflowrun_inspect, argv, self._wiz(self.LIVE, [self._run("main", "ECHO")])), 1)
        self.assertEqual(
            self._exit(wz.cmd_workflowrun_inspect, argv,
                       self._wiz(self.LIVE, [self._failed_step_run(status="COMPLETED")])), 1)
        self.assertEqual(self._exit(wz.cmd_workflowrun_inspect, argv, self._wiz(self.LIVE, [])), 1)

    def test_run_wait_dies_on_the_enum_spelling_of_canceled(self):
        # CANCELLED would never match, so a cancelled run would poll to the deadline instead of exiting 3
        # on the first read.
        wiz = self._wiz(self.LIVE, [{"id": "r1", "status": "CANCELED", "steps": []}])
        with mock.patch.object(wz.time, "sleep") as slept:
            code = exit_code(lambda argv: wz._wait_for_run("r1", timeout=60, interval=2), [], wiz=wiz)
        self.assertEqual(code, 3)
        slept.assert_not_called()

    def test_ensure_needs_a_readable_definition(self):
        self.assertEqual(self._exit(wz.cmd_workflow_ensure, ["--name", "lab-x"], self._wiz(self.LIVE)), 2)
        self.assertEqual(
            self._exit(wz.cmd_workflow_ensure, ["--name", "lab-x", "--definition", "/nope.json"],
                       self._wiz(self.LIVE)), 2)

    def test_run_ensure_rejects_unknown_initial_step(self):
        argv = ["--name", "lab-x", "--initial-step", "Nope", "--data", "/nope.json"]
        self.assertEqual(self._exit(wz.cmd_workflowrun_ensure, argv, self._wiz(self.LIVE)), 2)

    def _definition_file(self):
        d = tempfile.mkdtemp()
        path = pathlib.Path(d) / "wf.json"
        path.write_text(json.dumps({"steps": [], "triggers": []}))
        self.addCleanup(shutil.rmtree, d)
        return str(path)

    def test_ensure_refuses_to_submit_an_invalid_definition(self):
        # An invalid definition is the caller's bug, not the environment's: exit 2, and the create is
        # never sent — the API's own refusal names neither the step nor the field.
        argv = ["--name", "lab-x", "--definition", self._definition_file()]
        issues = [{"target": {"stepId": "route"}, "message": "field 'name' does not exist in expression"}]
        self.assertEqual(self._exit(wz.cmd_workflow_ensure, argv, self._wiz(self.LIVE, issues=issues)), 2)

    def test_dry_run_reports_issues_without_mutating(self):
        argv = ["--name", "lab-x", "--definition", self._definition_file(), "--dry-run"]
        issues = [{"target": {"triggerId": "eventThreats"}, "message": "outbound edge references non-existent step"}]
        self.assertEqual(self._exit(wz.cmd_workflow_ensure, argv, self._wiz(self.LIVE, issues=issues)), 1)
        self.assertEqual(self._exit(wz.cmd_workflow_ensure, argv, self._wiz(self.LIVE)), 0)

    def test_issue_line_survives_a_workflow_level_target(self):
        # The workflow member of the target union carries only `_stub`, so nothing names a step.
        self.assertEqual(wz._issue_line({"target": {"_stub": None}, "message": "m"}), "workflow: m")
        self.assertEqual(wz._issue_line({"message": "m"}), "workflow: m")

    def _ensure(self, wf_nodes, *extra):
        """The tenant after one `workflow ensure` on a valid definition."""
        wiz = self._wiz(wf_nodes)
        exit_code(wz.cmd_workflow_ensure, ["--name", "lab-x", "--definition", self._definition_file(), *extra], wiz=wiz)
        return wiz

    @staticmethod
    def _mutations(wiz):
        return [f for f, _ in wiz.calls if f.startswith(("create", "update", "delete", "publish", "revert"))]

    def test_ensure_sends_no_version_bearing_mutation(self):
        # The version refusal (SPEC.md) covers EVERY version-bearing path: the first fix removed the
        # publish call and left updateAutomationWorkflowDraft, so the same defect failed a second play.
        # This asserts the class, not the one call.
        for nodes in ([], self.LIVE):
            docs = "\n".join(self._ensure(nodes).docs)
            for banned in ("publishAutomationWorkflowVersion", "updateAutomationWorkflowDraft",
                           "revertAutomationWorkflowToVersion", "automationWorkflowVersion("):
                self.assertNotIn(banned, docs, f"sent with nodes={bool(nodes)}")

    def test_ensure_patches_a_live_workflow_and_keeps_its_id(self):
        # A rebuild drops the test runs earlier activities graded; the patch keeps the workflow id, and
        # the patch type has no projectId key.
        wiz = self._ensure(self.LIVE, "--project-id", "p1")
        sent = wiz.sent("updateAutomationWorkflow")[0]["input"]
        self.assertEqual(sent["id"], "w1")
        self.assertNotIn("projectId", sent["patchStrict"])
        self.assertTrue(sent["patchStrict"]["enabled"])

    def test_ensure_creates_when_absent_and_deletes_only_a_duplicate(self):
        self.assertEqual(self._mutations(self._ensure([])), ["createAutomationWorkflow"])
        self.assertEqual(self._mutations(self._ensure(self.LIVE)), ["updateAutomationWorkflow"])
        dup = [*self.LIVE, {**self.LIVE[0], "id": "w2"}]
        self.assertEqual(self._mutations(self._ensure(dup)), ["deleteAutomationWorkflow", "updateAutomationWorkflow"])

    def test_run_inspect_spans_every_workflow_on_the_stem(self):
        # Two attempts on one stem: the edge lives on the second, and grading only the first would fail a
        # learner who got there.
        two = [dict(self.LIVE[0], id="w1", name="lab-x-night-watch"),
               dict(self.LIVE[0], id="w2", name="lab-x-night-watch-2")]
        wiz = self._wiz(two, [self._run("Malicious")])
        argv = ["--name", "lab-x", "--require", "branch", "--branch", "Malicious"]
        self.assertEqual(self._exit(wz.cmd_workflowrun_inspect, argv, wiz), 0)
        self.assertEqual(sorted(wiz.sent("automationWorkflowRuns")[0]["f"]["workflowId"]["equals"]), ["w1", "w2"])


class OutpostGrading(unittest.TestCase):
    """Locks the OutpostStatus grading table and the uninstall->wait->delete order. A refactor that
    reorders the reap would silently leave an Outpost record behind, which no lab check would
    catch."""

    def _wiz(self, status, after=None, scans=None):
        """after: statuses `outpost(id)` returns on successive polls, for the uninstall wait.
        scans: one (successful, failed) pair per daily bucket the scan-metrics trend reports."""
        seq = list(after or [])
        nodes = [] if status is None else [{"id": "o1", "name": "lab-x", "status": status,
                                             "allowedRegions": ["us-east-1"], "config": {"roleARN": "a"}}]
        pts = [{"timestamp": f"d{i}", "aggregatedMetrics": {"totalScansCount": s + f, "successfulScansCount": s,
                                                            "failedScansCount": f}}
               for i, (s, f) in enumerate(scans or [])]

        def by_id(variables):
            st = seq.pop(0) if seq else status
            return None if st == "GONE" else {"id": "o1", "status": st}
        return FakeWiz(outposts={"nodes": nodes, "totalCount": len(nodes)}, outpost=by_id,
                       resourceScanMetricsTrend={"dataPoints": pts},
                       createOutpost={"outpost": {"id": "o1", "name": "lab-x", "status": "INITIALIZING"}})

    def _exit(self, fn, argv, wiz):
        # A fake clock, not just a no-op sleep: the uninstall wait is bounded by time.monotonic(), so a
        # patched-out sleep alone would spin on the real clock for the whole --timeout.
        clock = {"t": 1000.0}
        with mock.patch.object(wz.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s)), \
             mock.patch.object(wz.time, "monotonic", lambda: clock["t"]):
            return exit_code(fn, argv, wiz=wiz)

    @staticmethod
    def _mutations(wiz):
        return [f for f, _ in wiz.calls if f.startswith(("create", "uninstall", "delete"))]

    def test_inspect_grades_the_enum_not_the_ui_word(self):
        for status, require, want in [
            ("CONNECTED", "connected", 0),
            ("CONNECTED", "initialized", 0),      # CONNECTED is a superset of INITIALIZED
            ("INITIALIZED", "connected", 1),
            ("INITIALIZED", "initialized", 0),
            ("INITIALIZING", "initialized", 1),   # a fresh createOutpost lands here
            ("UNINSTALLED", "initialized", 1),
            ("UNINSTALLED", "exists", 0),
            ("ERROR", "connected", 1),
        ]:
            with self.subTest(status=status, require=require):
                argv = ["--name", "lab-x", "--require", require]
                self.assertEqual(self._exit(wz.cmd_outpost_inspect, argv, self._wiz(status)), want)

    def test_scanned_needs_a_successful_scan_not_just_connected(self):
        # A CONNECTED Outpost commonly has scanned nothing, so CONNECTED must not satisfy --require
        # scanned. Failed-only is also not satisfied (it means the node pool cannot snapshot), and
        # the daily buckets are summed across the window.
        for scans, want in [([], 1), ([(0, 0), (0, 0)], 1), ([(0, 3)], 1), ([(0, 0), (1, 0)], 0), ([(5, 2)], 0)]:
            with self.subTest(scans=scans):
                self.assertEqual(
                    self._exit(wz.cmd_outpost_inspect, ["--name", "lab-x", "--require", "scanned"],
                               self._wiz("CONNECTED", scans=scans)), want)

    def test_scanned_absent_outpost_never_queries_metrics(self):
        wiz = self._wiz(None, scans=[(9, 0)])
        self.assertEqual(self._exit(wz.cmd_outpost_inspect, ["--name", "lab-x", "--require", "scanned"], wiz), 1)
        self.assertEqual(wiz.sent("resourceScanMetricsTrend"), [])

    def test_inspect_name_matches_exactly_not_substring(self):
        # `search` is substring server-side; a neighbour session's longer name must not grade this one.
        wiz = FakeWiz(outposts={"nodes": [{"id": "o2", "name": "lab-xyz", "status": "CONNECTED"}]})
        self.assertEqual(self._exit(wz.cmd_outpost_inspect, ["--name", "lab-x"], wiz), 1)

    def test_ensure_needs_role_arn(self):
        self.assertEqual(self._exit(wz.cmd_outpost_ensure, ["--name", "lab-x"], self._wiz(None)), 2)

    def test_ensure_never_recreates_and_refuses_a_role_or_region_change(self):
        # A role change is a knowing delete-and-recreate: 3 naming the difference, no mutation.
        for argv, want in [(["--name", "lab-x", "--role-arn", "a"], 0),
                           (["--name", "lab-x", "--role-arn", "a", "--region", "us-east-1"], 0),
                           (["--name", "lab-x", "--role-arn", "other"], 3),
                           (["--name", "lab-x", "--role-arn", "a", "--region", "eu-west-1"], 3)]:
            with self.subTest(argv=argv):
                wiz = self._wiz("CONNECTED")
                self.assertEqual(self._exit(wz.cmd_outpost_ensure, argv, wiz), want)
                self.assertEqual(self._mutations(wiz), [])

    def test_ensure_posts_role_arn_inside_aws_config(self):
        wiz = self._wiz(None)
        self.assertEqual(self._exit(wz.cmd_outpost_ensure, ["--name", "lab-x", "--role-arn", "arn:r"], wiz), 0)
        inp = wiz.sent("createOutpost")[0]["input"]
        self.assertEqual(inp["awsConfig"]["roleARN"], "arn:r")   # caps, nested — the capture's shape
        self.assertEqual(inp["allowedRegions"], ["us-east-1"])

    def test_delete_uninstalls_first_waits_then_deletes(self):
        # deleteOutpost on a live Outpost is a server-side internal error, so it is never attempted
        # first; an already-UNINSTALLED record skips the uninstall. A status already carrying UNINSTALL
        # takes no second uninstall — that call is the one that refuses. PARTIALLY_UNINSTALLED reaches
        # no deletable state and deleteOutpost refuses it, so it is state (1), never environment (3).
        both = ["uninstallOutpost", "deleteOutpost"]
        for status, after, want, code in [
            ("INITIALIZED", ["UNINSTALLING", "UNINSTALLED"], both, 0),
            ("CONNECTED", ["UNINSTALLED"], both, 0),
            ("UNINSTALLED", [], ["deleteOutpost"], 0),
            ("UNINSTALLATION_FAILED", [], ["deleteOutpost"], 0),
            ("UNINSTALLING", ["UNINSTALLED"], ["deleteOutpost"], 0),
            ("PARTIALLY_UNINSTALLED", [], [], 1),
            ("INITIALIZED", ["PARTIALLY_UNINSTALLED"], ["uninstallOutpost"], 1),
            (None, [], [], 0),
        ]:
            with self.subTest(status=status):
                wiz = self._wiz(status, after=after)
                self.assertEqual(self._exit(wz.cmd_outpost_delete, ["--name", "lab-x"], wiz), code)
                self.assertEqual(self._mutations(wiz), want)

    def test_delete_exits_0_when_uninstall_outlives_the_wait(self):
        # Best-effort: the EKS infra dies with the lease, so a stuck record must not fail the reaper.
        wiz = self._wiz("INITIALIZED", after=["UNINSTALLING"] * 40)
        self.assertEqual(self._exit(wz.cmd_outpost_delete, ["--name", "lab-x", "--timeout", "60"], wiz), 0)
        self.assertEqual(self._mutations(wiz), ["uninstallOutpost"])


class OutpostConnectorBinding(unittest.TestCase):
    """Locks phase 2 of the Outpost deploy. The failure it guards has no lifecycle signal at all: a
    connector with no `outpost` still reaches CONNECTED, and its Outpost holds INITIALIZED with
    clusters null and errorCode null, so every status a check could read says success."""

    def _node(self, outpost=None):
        return {"id": "c1", "name": "lab-x-connector", "enabled": True, "status": "CONNECTED",
                "type": {"id": "aws"}, "outpost": outpost,
                "config": {"customerRoleARN": "arn:aws:iam::111111111111:role/WizAccess-Role"}}

    def _exit(self, fn, argv, node=None, outposts=None, api=None):
        return exit_code(fn, argv, find_connector=[node] if node else [], _resolve_outpost=outposts,
                         api=api or (lambda q, v: ({}, "tid")))

    # --session, because resolving "which Outpost should this be bound to" goes through the same
    # session stem the Outpost was named on.
    ARGS: typing.ClassVar = ["--account-id", "111111111111", "--require", "outpost-bound",
                             "--session", "x"]

    def test_unbound_connector_fails_despite_being_connected(self):
        self.assertEqual(self._exit(wz.cmd_connector_inspect, self.ARGS, self._node(None),
                                    {"id": "o1"}), 1)

    def test_bound_to_someone_elses_outpost_fails(self):
        # Two concurrent leases in one tenant: binding the neighbour's Outpost builds their cluster.
        self.assertEqual(self._exit(wz.cmd_connector_inspect, self.ARGS,
                                    self._node({"id": "o2", "name": "lab-y"}), {"id": "o1"}), 1)

    def test_bound_to_the_session_outpost_passes(self):
        self.assertEqual(self._exit(wz.cmd_connector_inspect, self.ARGS,
                                    self._node({"id": "o1", "name": "lab-x"}), {"id": "o1"}), 0)

    def test_explicit_outpost_id_needs_no_name_lookup(self):
        with mock.patch.object(_owner("_resolve_outpost"), "_resolve_outpost") as resolve:
            self.assertEqual(self._exit(wz.cmd_connector_inspect,
                                        [*self.ARGS, "--outpost-id", "o1"],
                                        self._node({"id": "o1", "name": "lab-x"})), 0)
        resolve.assert_not_called()

    def test_no_outpost_at_all_is_exit_1_not_an_error(self):
        # The learner skipped phase 1: a check, so 1 — never 2/3, which a lab would have to remap.
        self.assertEqual(self._exit(wz.cmd_connector_inspect, self.ARGS, self._node(None), None), 1)

    def test_outpost_id_without_scanner_role_is_exit_2(self):
        # Bound with no scanner role the connector converges and never scans a disk, so refuse to
        # create one rather than ship a lab that grades CONNECTED and scans nothing.
        self.assertEqual(self._exit(wz.cmd_connector_ensure,
                                    ["--account-id", "111111111111", "--outpost-id", "o1"]), 2)

    def test_scanner_role_without_outpost_id_is_exit_2(self):
        self.assertEqual(self._exit(wz.cmd_connector_ensure,
                                    ["--account-id", "111111111111", "--scanner-role-arn", "arn:s"]), 2)

    def test_create_payload_carries_all_three_auth_params(self):
        created = {"createConnector": {"connector": {"id": "c1", "name": "lab-x-connector",
                                                     "status": "INITIAL"}}}
        calls = []

        def api(query, variables):
            calls.append((query, variables))
            return created, "tid"
        self.assertEqual(self._exit(wz.cmd_connector_ensure,
                                    ["--account-id", "111111111111", "--session", "x",
                                     "--role-arn", "arn:r", "--outpost-id", "o1",
                                     "--scanner-role-arn", "arn:s"], api=api), 0)
        auth = calls[0][1]["input"]["authParams"]
        self.assertEqual(auth, {"customerRoleARN": "arn:r", "outpostId": "o1",
                                "diskAnalyzer": {"scanner": {"roleARN": "arn:s"}}})

    def test_ensure_binds_by_name_the_way_the_console_dropdown_does(self):
        created = {"createConnector": {"connector": {"id": "c1", "name": "lab-x-connector",
                                                     "status": "INITIAL"}}}
        calls = []

        def api(query, variables):
            calls.append((query, variables))
            return created, "tid"
        self.assertEqual(self._exit(wz.cmd_connector_ensure,
                                    ["--account-id", "111111111111", "--session", "x",
                                     "--outpost-name", "lab-x", "--scanner-role-arn", "arn:s"],
                                    outposts={"id": "o1"}, api=api), 0)
        self.assertEqual(calls[0][1]["input"]["authParams"]["outpostId"], "o1")

    def test_ensure_refuses_to_create_an_unbindable_connector(self):
        # Phase 1 never happened. Exit 3, not 1: this is a solve/setup path, and a connector created
        # unbound here would reach CONNECTED and quietly scan nothing.
        self.assertEqual(self._exit(wz.cmd_connector_ensure,
                                    ["--account-id", "111111111111", "--session", "x",
                                     "--outpost-name", "lab-x", "--scanner-role-arn", "arn:s"],
                                    outposts=None), 3)

    def test_ensure_is_a_no_op_when_already_bound_to_that_outpost(self):
        with mock.patch.object(_owner("find_connector"), "find_connector",
                               return_value=[self._node({"id": "o1", "name": "lab-x"})]), \
             mock.patch.object(_owner("api"), "api") as api, exits() as cm:
            call(wz.cmd_connector_ensure, ["--account-id", "111111111111", "--outpost-id", "o1",
                                     "--scanner-role-arn", "arn:s", "--role-arn",
                                     "arn:aws:iam::111111111111:role/WizAccess-Role"])
        self.assertEqual(cm.code, 0)
        api.assert_not_called()

    def test_ensure_binds_an_existing_unbound_connector(self):
        calls = []
        self.assertEqual(self._exit(wz.cmd_connector_ensure,
                                    ["--account-id", "111111111111", "--outpost-id", "o1",
                                     "--scanner-role-arn", "arn:s", "--role-arn", "arn:r"],
                                    self._node(None),
                                    api=lambda q, v: (calls.append((q, v)), ({}, "tid"))[1]), 0)
        patch = calls[0][1]["input"]["patch"]["authParams"]
        self.assertEqual(patch["outpostId"], "o1")
        self.assertEqual(patch["diskAnalyzer"], {"scanner": {"roleARN": "arn:s"}})


class ServiceAccountGrading(unittest.TestCase):
    """The on-the-fly wizcli credential is a CLI DEPLOYMENT (createCliDeployment) — createServiceAccount
    (type:CLI) is rejected live. Lock the delete-then-mint convergence, the WIZ_CLIENT_ID/SECRET emit
    (clientId from the deployment's SA, secret from the payload), and the exit contract."""

    ENV: typing.ClassVar = {"INSTRUQT_SESSION_ID": "x"}

    def _wiz(self, existing=False, cid="cidX", sec="secX"):
        return FakeWiz(
            deployments={"nodes": [{"id": "dep1", "name": "lab-x-cli", "type": "WIZ_CLI"}] if existing else []},
            createCliDeployment={"clientSecret": sec, "deployment": {
                "id": "dep2", "name": "lab-x-cli", "type": "WIZ_CLI",
                "object": {"serviceAccount": {"name": "lab-x-cli-deployment-u", "clientId": cid}}}},
            deleteCliDeployment={"id": "dep1"})

    def _run(self, fn, argv, wiz):
        out = io.StringIO()
        return exit_code(fn, argv, wiz=wiz, env=self.ENV, out=out), out.getvalue()

    def test_ensure_creates_and_emits_client_creds(self):
        code, out = self._run(wz.cmd_serviceaccount_ensure, [], self._wiz(existing=False))
        self.assertEqual(code, 0)
        self.assertIn("WIZ_CLIENT_ID=cidX", out)
        self.assertIn("WIZ_CLIENT_SECRET=secX", out)

    def test_ensure_deletes_existing_before_minting(self):
        # Both credential verbs: the secret is shown once, so an existing account is replaced and every
        # run emits credentials (SPEC.md §What `ensure` promises).
        wiz = self._wiz(existing=True)
        code, _ = self._run(wz.cmd_serviceaccount_ensure, [], wiz)
        self.assertEqual(code, 0)
        mutations = [f for f, _ in wiz.calls if f.startswith(("delete", "create"))]
        self.assertEqual(mutations, ["deleteCliDeployment", "createCliDeployment"])
        sa = {"id": "sa1", "name": "lab-x-sensor", "clientId": "cidS", "clientSecret": "secS"}
        wiz = FakeWiz(serviceAccounts={"nodes": [sa]}, createServiceAccount={"serviceAccount": sa})
        code, out = self._run(wz.cmd_sensor_ensure, [], wiz)
        self.assertEqual(code, 0)
        self.assertEqual([f for f, _ in wiz.calls if f.startswith(("delete", "create"))],
                         ["deleteServiceAccount", "createServiceAccount"])
        self.assertEqual(wiz.sent("deleteServiceAccount"), [{"id": "sa1"}])
        self.assertIn("WIZ_API_CLIENT_SECRET=secS", out)

    def test_ensure_missing_creds_is_environment_3(self):
        code, _ = self._run(wz.cmd_serviceaccount_ensure, [], self._wiz(existing=False, cid=None))
        self.assertEqual(code, 3)

    def test_delete_by_name_and_noop_when_absent(self):
        self.assertEqual(self._run(wz.cmd_serviceaccount_delete, [], self._wiz(existing=True))[0], 0)
        self.assertEqual(self._run(wz.cmd_serviceaccount_delete, [], self._wiz(existing=False))[0], 0)


    def test_delete_by_id_sends_the_id_as_a_variable(self):
        hostile = 'dep1" }) { id } } mutation { deleteTenant(input: { id: "t'
        wiz = self._wiz()
        code, _ = self._run(wz.cmd_serviceaccount_delete, ["--id", hostile], wiz)
        self.assertEqual(code, 0)
        self.assertEqual(wiz.sent("deleteCliDeployment"), [{"id": hostile}])
        self.assertNotIn(hostile, "".join(wiz.docs))


class CodeScanGrading(unittest.TestCase):
    """code-scan inspect grades the TENANT verdict (WARN_BY_POLICY exits 0 at the CLI, so the exit
    code can't tell a finding from a pass). Lock pass/fail and the bounded poll."""

    ENV: typing.ClassVar = {"INSTRUQT_SESSION_ID": "x"}

    def _wiz(self, seq):
        it = iter(seq)  # one node-or-None per successive cicdScans call

        def scans(variables):
            node = next(it)
            return {"nodes": [node] if node else [], "totalCount": 1 if node else 0}
        return FakeWiz(cicdScans=scans)

    def _node(self, state="DONE", verdict=None):
        return {"id": "c1", "status": {"state": state, "verdict": verdict}}

    def _exit(self, argv, wiz):
        with mock.patch.object(wz.time, "sleep", lambda *_: None):
            return exit_code(wz.cmd_codescan_inspect, argv, wiz=wiz, env=self.ENV)

    def test_pass_grades_the_verdict(self):
        for verdict, want in [("PASSED_BY_POLICY", 0), ("FAILED_BY_POLICY", 1)]:
            with self.subTest(verdict=verdict):
                self.assertEqual(self._exit(["--require", "pass"], self._wiz([self._node(verdict=verdict)])), want)

    def test_pass_polls_a_running_scan_to_its_verdict(self):
        seq = [self._node(state="IN_PROGRESS"), self._node(verdict="PASSED_BY_POLICY")]
        self.assertEqual(self._exit(["--require", "pass", "--interval", "0"], self._wiz(seq)), 0)


class PolicyGrading(unittest.TestCase):
    """policy ensure builds a BLOCK/CLI IaC policy scoped to the live-resolved Dockerfile control.
    Lock idempotency, the input shape (enforcement + single-rule scope), and the exit contract."""

    ENV: typing.ClassVar = {"INSTRUQT_SESSION_ID": "x"}
    CTL: typing.ClassVar = [{"id": "ctl-1", "name": "Last User Is 'root'", "severity": "HIGH"}]
    LIVE: typing.ClassVar = {"id": "pol-1", "name": "block-root",
                             "params": {"severityThreshold": "HIGH", "countThreshold": 1,
                                        "cloudConfigurationRules": [{"id": "ctl-1"}]}}

    def _wiz(self, existing=False, control=None, created_id="pol-1"):
        control = self.CTL if control is None else control
        return FakeWiz(
            cicdScanPolicies={"nodes": [self.LIVE] if existing else []},
            cloudConfigurationRules={"nodes": control},
            createCICDScanPolicy={"scanPolicy": {"id": created_id, "name": "block-root"} if created_id else {}},
            deleteCICDScanPolicy={"id": "pol-1"})

    def _exit(self, fn, argv, wiz):
        return exit_code(fn, argv, wiz=wiz, env=self.ENV)

    def test_ensure_leaves_a_matching_fixture_and_refuses_a_differing_one(self):
        # A shared tenant fixture other labs grade against changes deliberately, never under a solve:
        # flags that match or were not named exit 0 with no mutation; a differing flag is 3, no mutation.
        for argv, want in [(["--name", "block-root"], 0),
                           (["--name", "block-root", "--severity", "HIGH", "--count-threshold", "1",
                             "--rule-id", "ctl-1"], 0),
                           (["--name", "block-root", "--count-threshold", "2"], 3),
                           (["--name", "block-root", "--severity", "CRITICAL"], 3),
                           (["--name", "block-root", "--rule-id", "ctl-9"], 3)]:
            with self.subTest(argv=argv):
                wiz = self._wiz(existing=True)
                self.assertEqual(self._exit(wz.cmd_policy_ensure, argv, wiz), want)
                self.assertEqual([f for f, _ in wiz.calls if f.startswith(("create", "update", "delete"))], [])

    def test_ensure_creates_scoped_block_cli_policy(self):
        wiz = self._wiz(existing=False)
        self.assertEqual(self._exit(wz.cmd_policy_ensure, ["--name", "block-root"], wiz), 0)
        inp = wiz.sent("createCICDScanPolicy")[0]["input"]
        self.assertEqual(inp["policyLifecycleEnforcements"],
                         [{"enforcementMethod": "BLOCK", "deploymentLifecycle": "CLI"}])
        self.assertEqual(inp["iacParams"]["cloudConfigurationRules"], ["ctl-1"])
        self.assertEqual(inp["iacParams"]["severityThreshold"], "HIGH")
        self.assertEqual(inp["iacParams"]["countThreshold"], 1)  # 0 is rejected live
        self.assertFalse(inp["default"])

    def test_ensure_control_absent_is_environment_3(self):
        self.assertEqual(self._exit(wz.cmd_policy_ensure, ["--name", "b"], self._wiz(existing=False, control=[])), 3)

    def test_ensure_refuses_a_control_that_is_not_the_documented_one(self):
        # `search` is a server-side contains. Scoping the policy to whatever it returned first built a
        # fixture that blocks on another condition while the lab still tells the learner to fix USER.
        other = [{"id": "ctl-9", "name": "Last User Is Not Declared", "severity": "HIGH"}]
        self.assertEqual(self._exit(wz.cmd_policy_ensure, ["--name", "b"], self._wiz(False, control=other)), 3)

    def test_ensure_refuses_two_controls_with_the_documented_name(self):
        dupes = [dict(self.CTL[0]), {"id": "ctl-2", "name": "Last User Is 'root'", "severity": "HIGH"}]
        self.assertEqual(self._exit(wz.cmd_policy_ensure, ["--name", "b"], self._wiz(False, control=dupes)), 3)

    def test_rule_id_override_scopes_without_a_lookup(self):
        wiz = self._wiz(existing=False, control=[])
        argv = ["--name", "block-root", "--rule-id", "ctl-chosen"]
        self.assertEqual(self._exit(wz.cmd_policy_ensure, argv, wiz), 0)
        created = wiz.sent("createCICDScanPolicy")[0]["input"]
        self.assertEqual(created["iacParams"]["cloudConfigurationRules"], ["ctl-chosen"])
        self.assertEqual(wiz.sent("cloudConfigurationRules"), [])

    def test_ensure_create_no_id_is_environment_3(self):
        wiz = self._wiz(existing=False, created_id=None)
        self.assertEqual(self._exit(wz.cmd_policy_ensure, ["--name", "b"], wiz), 3)

    def test_delete_found_and_noop_when_absent(self):
        self.assertEqual(self._exit(wz.cmd_policy_delete, ["--name", "block-root"], self._wiz(existing=True)), 0)
        self.assertEqual(self._exit(wz.cmd_policy_delete, ["--name", "block-root"], self._wiz(existing=False)), 0)


class RunnerFloor(unittest.TestCase):
    """`session verify --min-runner` turns a lab's floor comment into something that fails."""

    def _verify(self, args, env):
        with mock.patch.dict(wz.os.environ, env, clear=False), \
             mock.patch.object(_owner("token_and_dc"), "token_and_dc", return_value=("tok", "dc", "tid")), \
             mock.patch.object(_owner("api"), "api", return_value=({"connectors": {"totalCount": 0}}, "tid")), \
             contextlib.redirect_stdout(io.StringIO()) as out, \
             contextlib.redirect_stderr(io.StringIO()) as err, \
             exits() as cm:
            call(wz.cmd_session_verify, args)
        return cm.code, out.getvalue(), err.getvalue()

    def test_version_compares_as_ints_not_lexicographically(self):
        # The whole point: "v0.1.9" > "v0.1.36" as strings, so a floor of v0.1.29 would pass on v0.1.9.
        self.assertLess(wz._version("v0.1.9"), wz._version("v0.1.36"))
        self.assertEqual(wz._version("v0.1.36"), (0, 1, 36))
        self.assertIsNone(wz._version("latest"))

    def test_a_pin_at_or_above_the_floor_passes(self):
        for tag in ("v0.1.29", "v0.1.37"):
            code, _out, _err = self._verify(["--min-runner", "v0.1.29"], {"TE_RUNNER_TAG": tag})
            self.assertEqual(code, 0, tag)

    def test_a_pin_below_the_floor_is_an_invocation_error_not_a_learner_failure(self):
        # 2, so a validator reads FAIL (the lab's own pin is wrong) rather than INCONCLUSIVE.
        code, _out, err = self._verify(["--min-runner", "v0.1.33"], {"TE_RUNNER_TAG": "v0.1.29"})
        self.assertEqual(code, 2)
        self.assertIn("below this lab's floor", err)

    def test_the_floor_is_checked_before_any_credential_is_spent(self):
        with mock.patch.dict(wz.os.environ, {"TE_RUNNER_TAG": "v0.1.29"}, clear=False), \
             mock.patch.object(_owner("token_and_dc"), "token_and_dc") as tok, \
             contextlib.redirect_stderr(io.StringIO()), exits() as cm:
            call(wz.cmd_session_verify, ["--min-runner", "v0.1.33"])
        self.assertEqual(cm.code, 2)
        tok.assert_not_called()

    def test_a_runner_that_cannot_name_itself_is_environment_never_a_pass(self):
        code, _out, err = self._verify(["--min-runner", "v0.1.29"], {"TE_RUNNER_TAG": ""})
        self.assertEqual(code, 3)
        self.assertIn("unknowable", err)

    def test_a_floor_that_is_not_a_version_is_an_invocation_error(self):
        code, _out, _err = self._verify(["--min-runner", "latest"], {"TE_RUNNER_TAG": "v0.1.37"})
        self.assertEqual(code, 2)

    def test_no_floor_flag_leaves_verify_unchanged(self):
        code, out, _err = self._verify([], {"TE_RUNNER_TAG": "v0.1.37", "TE_RUNNER_REV": "deadbeefcafe"})
        self.assertEqual(code, 0)
        # Check 1's line is the only record of what a play actually ran.
        self.assertIn("runner=v0.1.37@deadbee", out)

    def test_identity_falls_back_to_pid_1_when_the_shell_env_was_scrubbed(self):
        # sshd's sessions carry none of the image's ENV, so a validator over the tailnet would fail
        # every floor as exit 3 without this.
        environ = b"PATH=/usr/bin\x00TE_RUNNER_TAG=v0.1.38\x00TE_RUNNER_REV=abc1234\x00"
        with mock.patch.dict(wz.os.environ, {"TE_RUNNER_TAG": "", "TE_RUNNER_REV": ""}, clear=False), \
             mock.patch.object(wz.pathlib.Path, "read_bytes", lambda self: environ):
            self.assertEqual(wz._runner_id(), ("v0.1.38", "abc1234"))

    def test_the_shell_env_wins_over_pid_1(self):
        with mock.patch.dict(wz.os.environ, {"TE_RUNNER_TAG": "v0.1.40", "TE_RUNNER_REV": "dd"}), \
             mock.patch.object(wz.pathlib.Path, "read_bytes",
                               lambda self: b"TE_RUNNER_TAG=v0.1.1\x00"):
            self.assertEqual(wz._runner_id(), ("v0.1.40", "dd"))

    def test_no_pid_1_to_read_is_an_absent_identity_not_a_crash(self):
        with mock.patch.dict(wz.os.environ, {"TE_RUNNER_TAG": "", "TE_RUNNER_REV": ""}, clear=False), \
             mock.patch.object(wz.pathlib.Path, "read_bytes",
                               lambda self: (_ for _ in ()).throw(OSError("no /proc"))):
            self.assertEqual(wz._runner_id(), ("", ""))

    def test_an_unidentified_runner_still_verifies_without_the_flag(self):
        code, out, _err = self._verify([], {"TE_RUNNER_TAG": "", "TE_RUNNER_REV": ""})
        self.assertEqual(code, 0)
        self.assertIn("runner=unknown", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
