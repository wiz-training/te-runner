"""Contract table and workflow tests for `scripts/secrets_sync.py` and `.github/workflows/secrets-sync.yml`."""
import base64
import contextlib
import importlib.util
import io
import os
import pathlib
import subprocess
import sys
import typing
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_build_workflow import ROOT, _image_steps, _live, _run_block

_SPEC = importlib.util.spec_from_file_location("secrets_sync", ROOT / "scripts" / "secrets_sync.py")
secrets_sync = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(secrets_sync)

SECRETS_SYNC_YML = ROOT / ".github" / "workflows" / "secrets-sync.yml"

LABKIT_NAMES = (
    "AIRTABLE_FEEDBACK_WEBHOOK",
    "OKTA_WF_CLIENT_TOKEN",
    "OKTA_WF_INVOKE_URL",
    "WIZ_LABS_GH_APP",
    "WIZ_TBCMP_AZURE_APP_OBJECT_ID",
    "WIZ_TBCMP_CLIENT_ID",
    "WIZ_TBCMP_CLIENT_SECRET",
    "WIZ_TBCMP_LOGIN_URL",
    "WIZ_TE_CLIENT_ID",
    "WIZ_TE_CLIENT_SECRET",
    "WIZ_TE_LOGIN_URL",
    "WIZ_TS_PROD_CLIENT_ID",
    "WIZ_TS_PROD_SECRET",
)
GAMEKIT_PATHS = (
    ("/gamekit/admin_token", "GAMEKIT_ADMIN_TOKEN"),
    ("/gamekit/lab_token", "GAMEKIT_LAB_TOKEN"),
    ("/gamekit/super_token", "GAMEKIT_SUPER_TOKEN"),
)
ALL_16_NAMES = (*LABKIT_NAMES, *(iq for _, iq in GAMEKIT_PATHS))


class FakeSSM:
    """System-boundary stub for boto3 SSM (`get_parameters_by_path`, `get_parameters`, `describe_parameters`)."""

    def __init__(self, overrides=None, descriptions=None):
        overrides = overrides or {}
        self.descriptions = (
            {f"/te-labkit/{n}": f"desc for {n}" for n in LABKIT_NAMES}
            if descriptions is None
            else dict(descriptions)
        )
        self.by_path = [
            {"Name": f"/te-labkit/{n}", "Value": overrides.get(n, f"val-{n.lower()}")}
            for n in LABKIT_NAMES
        ]
        self.extras = {
            path: {"Name": path, "Value": overrides.get(iq, f"val-{iq.lower()}")}
            for path, iq in GAMEKIT_PATHS
        }
        self.calls = []

    def describe_parameters(self, **kwargs):
        self.calls.append(("describe_parameters", kwargs))
        prefix = kwargs["ParameterFilters"][0]["Values"][0]
        rows = [
            {"Name": k, "Description": v}
            for k, v in self.descriptions.items()
            if k.startswith(prefix)
        ]
        return {"Parameters": rows}

    def get_parameters_by_path(self, **kwargs):
        self.calls.append(("get_parameters_by_path", kwargs))
        return {"Parameters": list(self.by_path)}

    def get_parameters(self, **kwargs):
        self.calls.append(("get_parameters", kwargs))
        return {"Parameters": [self.extras[n] for n in kwargs["Names"] if n in self.extras]}


class FakeInstruqt:
    """System-boundary stub for `_instruqt(query, variables)`."""

    def __init__(self, existing=None):
        self.existing = list(ALL_16_NAMES if existing is None else existing)
        self.queries = []
        self.mutations = []

    def __call__(self, query, variables):
        if "teamSecrets" in query:
            self.queries.append((query, variables))
            return {"teamSecrets": [{"name": n} for n in self.existing]}
        if "upsertTeamSecret" in query:
            self.mutations.append((query, variables))
            return {"upsertTeamSecret": {"name": variables["n"]}}
        raise AssertionError(f"unexpected GraphQL document: {query}")


class SecretsSyncTests(unittest.TestCase):
    # (label, argv, overrides, descriptions, existing_iq, want_rc, want_upserted, want_out_lines, want_absent_out)
    SYNC_ROWS: typing.ClassVar = (
        (
            "all 16 upserted",
            [],
            {},
            None,
            None,
            0,
            list(ALL_16_NAMES),
            [*(f"upserted {n}" for n in ALL_16_NAMES), "# 16 upserted, 0 failed"],
            ["# not managed by SSM:", "SKIP "],
        ),
        (
            "dry-run performs no mutation",
            ["--dry-run"],
            {},
            {"/te-labkit/AIRTABLE_FEEDBACK_WEBHOOK": "Airtable webhook"},
            None,
            0,
            [],
            [
                "would upsert AIRTABLE_FEEDBACK_WEBHOOK (desc: yes)",
                "would upsert GAMEKIT_ADMIN_TOKEN (desc: no)",
            ],
            ["upserted ", "# "],
        ),
        (
            "empty value skipped and fails the run",
            [],
            {"WIZ_TE_CLIENT_SECRET": ""},
            None,
            None,
            1,
            [n for n in ALL_16_NAMES if n != "WIZ_TE_CLIENT_SECRET"],
            [
                "SKIP WIZ_TE_CLIENT_SECRET: empty value in SSM",
                "# 15 upserted, 1 failed",
            ],
            ["upserted WIZ_TE_CLIENT_SECRET"],
        ),
        (
            "--only filters",
            ["--only", "WIZ_TE_CLIENT_ID,GAMEKIT_LAB_TOKEN"],
            {},
            None,
            None,
            0,
            ["WIZ_TE_CLIENT_ID", "GAMEKIT_LAB_TOKEN"],
            [
                "upserted WIZ_TE_CLIENT_ID",
                "upserted GAMEKIT_LAB_TOKEN",
                "# 2 upserted, 0 failed",
            ],
            ["upserted AIRTABLE_FEEDBACK_WEBHOOK", "# not managed by SSM:"],
        ),
        (
            "unmanaged names reported",
            ["--dry-run"],
            {},
            None,
            [*ALL_16_NAMES, "WIZ_DEMO_CLIENT_ID", "TS_AUTHKEY_DEV_AWS"],
            0,
            [],
            ["# not managed by SSM: TS_AUTHKEY_DEV_AWS, WIZ_DEMO_CLIENT_ID"],
            ["deleteTeamSecret"],
        ),
    )

    def test_sync_contract_rows(self):
        for (
            label,
            argv,
            overrides,
            descriptions,
            existing_iq,
            want_rc,
            want_upserted,
            want_out,
            want_absent,
        ) in self.SYNC_ROWS:
            with self.subTest(case=label):
                ssm = FakeSSM(overrides=overrides, descriptions=descriptions)
                iq = FakeInstruqt(existing=existing_iq)
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    rc = secrets_sync.main(argv, ssm=ssm, send=iq)
                stdout = out.getvalue()
                self.assertEqual(rc, want_rc, stdout)
                self.assertEqual([v["n"] for _, v in iq.mutations], want_upserted)
                for line in want_out:
                    self.assertIn(line, stdout)
                for forbidden in want_absent:
                    self.assertNotIn(forbidden, stdout)

    def test_upsert_sends_base64_of_ssm_plaintext_and_maps_missing_description_to_null(self):
        """Instruqt stores `secret` verbatim and decodes base64 at sandbox build (`illegal base64 data`
        when raw plaintext is sent, as `wizops/lease.py::_upsert_secret` documents)."""
        ssm = FakeSSM(
            overrides={"WIZ_TE_CLIENT_ID": "raw-te-id", "GAMEKIT_ADMIN_TOKEN": "raw-gk-admin"},
            descriptions={"/te-labkit/WIZ_TE_CLIENT_ID": "Wiz TE client id"},
        )
        iq = FakeInstruqt()
        with contextlib.redirect_stdout(io.StringIO()):
            rc = secrets_sync.main(["--only", "WIZ_TE_CLIENT_ID,GAMEKIT_ADMIN_TOKEN"], ssm=ssm, send=iq)
        self.assertEqual(rc, 0)
        sent = {v["n"]: v for _, v in iq.mutations}
        self.assertEqual(
            sent["WIZ_TE_CLIENT_ID"],
            {
                "t": "wiz",
                "n": "WIZ_TE_CLIENT_ID",
                "d": "Wiz TE client id",
                "s": base64.b64encode(b"raw-te-id").decode(),
            },
        )
        self.assertEqual(
            sent["GAMEKIT_ADMIN_TOKEN"],
            {
                "t": "wiz",
                "n": "GAMEKIT_ADMIN_TOKEN",
                "d": None,
                "s": base64.b64encode(b"raw-gk-admin").decode(),
            },
        )

    def test_workflow_is_manual_only_and_checks_required_secrets(self):
        text = SECRETS_SYNC_YML.read_text()
        live = _live(text)
        self.assertIn("workflow_dispatch:", live)
        self.assertNotIn("schedule:", live)
        self.assertIn("\n  sync:\n", live)
        steps = dict(_image_steps(text))
        check_script = _run_block(steps["check required secrets"])
        full = {
            "INSTRUQT_TOKEN": "iq",
            "AWS_SSM_SYNC_ACCESS_KEY_ID": "id",
            "AWS_SSM_SYNC_SECRET_ACCESS_KEY": "sec",
        }
        for missing in ("AWS_SSM_SYNC_ACCESS_KEY_ID", "AWS_SSM_SYNC_SECRET_ACCESS_KEY", "INSTRUQT_TOKEN", None):
            with self.subTest(missing=missing):
                env = {**os.environ, **full, **({missing: ""} if missing else {})}
                res = subprocess.run(
                    ["sh", "-e", "-c", check_script],
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(res.returncode, 1 if missing else 0, res.stdout + res.stderr)
                if missing and missing.startswith("AWS_"):
                    self.assertIn("AWS_SSM_SYNC_ACCESS_KEY_ID", res.stdout + res.stderr)
                    self.assertIn("AWS_SSM_SYNC_SECRET_ACCESS_KEY", res.stdout + res.stderr)
                elif missing:
                    self.assertIn(missing, res.stdout + res.stderr)


if __name__ == "__main__":
    unittest.main()
