#!/usr/bin/env python3
import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent


class BootTimestamp(unittest.TestCase):
    """At container start, the entrypoint stamps `/run/lab/boot_at` (mode 0644) with RFC3339 UTC so
    `wizlab correlation inspect --since-boot` rejects stale correlation edges from prior plays."""

    def test_writes_utc_rfc3339_boot_at_with_mode_0644(self):
        with tempfile.TemporaryDirectory() as td:
            run_lab = pathlib.Path(td) / "run" / "lab"
            env = {**os.environ, "RUN_LAB_DIR": str(run_lab), "TS_AUTHKEY": "", "TE_DEV_SSH_PUBKEY": "",
                   "GOOGLE_CREDENTIALS": "", "ARM_CLIENT_ID": ""}
            result = subprocess.run(["/bin/sh", str(ROOT / "entrypoint.sh"), "true"], env=env,
                                    capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            boot_at = run_lab / "boot_at"
            self.assertTrue(boot_at.is_file())
            self.assertEqual(stat.S_IMODE(boot_at.stat().st_mode), 0o644)
            self.assertRegex(boot_at.read_text(), r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\n$")


class WizlabGrant(unittest.TestCase):
    """A setup exec that omits module.tenant.output.env must fail at wizlab-grant (exit 3) instead of
    arming /run/lab/learner-verbs with an unauthenticated snapshot that fails only when the learner runs
    wizlab."""

    ROWS = (
        # args, env, expected exit code, expected learner-verbs content (None = file absent)
        ((), {"WIZ_TBCMP_CLIENT_ID": "id"}, 2, None),
        (("session verify",), {}, 3, None),
        (("session verify",), {"WIZ_CLIENT_ID": ""}, 3, None),
        (("session verify",), {"WIZ_TENANT": "TE", "WIZ_IDP_LOGIN_CLIENT_ID": "idp-id",
                               "WIZ_TE_IDP_LOGIN_CLIENT_ID": "te-idp-id", "WIZ_TE_SSO_CLIENT_ID": "sso-id",
                               "WIZ_TE_MCP_CLIENT_ID": "mcp-id"}, 3, None),
        (("session verify",), {"WIZ_CLIENT_ID": "id"}, 0, "session verify\n"),
        (("session verify",), {"WIZ_TENANT": "TS_PROD", "WIZ_TS_PROD_CLIENT_ID": "id"}, 0, "session verify\n"),
        (("session verify", "connector inspect"),
         {"WIZ_TBCMP_CLIENT_ID": "id", "WIZ_CLIENT_ID": "", "UNRELATED": "x"}, 0,
         "session verify\nconnector inspect\n"),
    )

    def test_grant_requires_client_id_before_arming_verbs(self):
        src = (ROOT / "bin" / "wizlab-grant").read_text()
        self.assertIn("/run/lab", src)
        clean = {k: v for k, v in os.environ.items() if not k.startswith("WIZ_")}
        for args, extra_env, want_rc, want_verbs in self.ROWS:
            with self.subTest(args=args, env=sorted(extra_env)), tempfile.TemporaryDirectory() as td:
                tmp = pathlib.Path(td)
                run_lab = tmp / "run" / "lab"
                script = tmp / "wizlab-grant"
                script.write_text(src.replace("/run/lab", str(run_lab)))
                script.chmod(0o755)
                result = subprocess.run(["/bin/bash", str(script), *args], env={**clean, **extra_env},
                                        capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, want_rc, result.stderr)
                verbs = run_lab / "learner-verbs"
                if want_verbs is None:
                    self.assertFalse(verbs.exists())
                else:
                    self.assertEqual(verbs.read_text(), want_verbs)
                    wiz_env = run_lab / "wiz.env"
                    self.assertEqual(stat.S_IMODE(wiz_env.stat().st_mode), 0o600)
                    self.assertNotIn("UNRELATED=", wiz_env.read_text())
                    self.assertNotIn("WIZ_CLIENT_ID=''\n", wiz_env.read_text())
                if want_rc == 3:
                    self.assertIn("no WIZ_<TENANT>_CLIENT_ID", result.stderr)
                    self.assertNotIn("will exit 3", result.stderr)


class CredentialFile(unittest.TestCase):
    """A CSP secret reaches its CLI through a 0600 file the entrypoint removes even when the login fails,
    never through argv (readable from /proc by any local user). One row per CLI; the stub exits 1 on the
    login and records the mode and path of the file it was handed."""

    ROWS = (
        # binary, env, stub body: records `mode path` of the credential file into $CAPTURE, then fails.
        # GNU coreutils is `stat -c '%a %n'`; BSD/macOS is `stat -f '%Lp %N'`.
        ("gcloud", {"GOOGLE_CREDENTIALS": '{"private_key":"secret"}'},
         "case $1 in auth) (stat -c '%a %n' \"$4\" 2>/dev/null || stat -f '%Lp %N' \"$4\") > \"$CAPTURE\";"
         " exit 1 ;; *) exit 0 ;; esac\n"),
        ("az", {"ARM_CLIENT_ID": "i", "ARM_CLIENT_SECRET": "s", "ARM_TENANT_ID": "t", "ARM_SUBSCRIPTION_ID": "u"},
         "case $1 in login) for a in \"$@\"; do case $a in @*)"
         " (stat -c '%a %n' \"${a#@}\" 2>/dev/null || stat -f '%Lp %N' \"${a#@}\") > \"$CAPTURE\";; esac; done;"
         " exit 1 ;; *) exit 0 ;; esac\n"),
    )

    def test_failed_login_still_removes_mode_600_file(self):
        for binary, env, body in self.ROWS:
            with self.subTest(binary=binary), tempfile.TemporaryDirectory() as td:
                tmp = pathlib.Path(td)
                capture = tmp / "capture"
                stub = tmp / binary
                stub.write_text("#!/bin/sh\n" + body)
                stub.chmod(0o755)
                run_env = {**os.environ, "PATH": f"{tmp}:{os.environ['PATH']}", "CAPTURE": str(capture), **env}
                result = subprocess.run(["/bin/sh", str(ROOT / "entrypoint.sh"), "true"], env=run_env,
                                        capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)
                mode, key = capture.read_text().strip().split(" ", 1)
                self.assertEqual(mode, "600")
                self.assertFalse(pathlib.Path(key).exists())


class LearnerWrapper(unittest.TestCase):
    """End-to-end round-trip across wizlab-grant -> wizlab-learner -> wizlab.cli.main without sudo:
    granted verb pairs source the %q-quoted 0600 snapshot (preserving multiline and shell-metacharacter
    values) and exec wizlab, while ungranted pairs and pre-grant calls stop at the wrapper with exit 2."""

    def test_grant_snapshot_and_learner_wrapper_positive_path(self):
        grant_src = (ROOT / "bin" / "wizlab-grant").read_text()
        learner_src = (ROOT / "bin" / "wizlab-learner").read_text()
        self.assertIn("/run/lab", grant_src)
        self.assertIn("/run/lab", learner_src)
        self.assertIn("/usr/local/bin/wizlab", learner_src)
        with tempfile.TemporaryDirectory() as td:
            tmp = pathlib.Path(td)
            run_lab = tmp / "run" / "lab"
            wizlab_bin = tmp / "wizlab"
            captured = tmp / "captured.json"
            wizlab_bin.write_text(
                f"#!{sys.executable}\n"
                "import json, os, pathlib, sys\n"
                f"sys.path.insert(0, {str(ROOT)!r})\n"
                "if out := os.getenv('CAPTURE_ENV'):\n"
                "    pathlib.Path(out).write_text(json.dumps(dict(os.environ)))\n"
                "from wizlab.cli import main\n"
                "main()\n"
            )
            wizlab_bin.chmod(0o755)
            grant = tmp / "wizlab-grant"
            grant.write_text(grant_src.replace("/run/lab", str(run_lab)))
            grant.chmod(0o755)
            learner = tmp / "wizlab-learner"
            learner.write_text(
                learner_src.replace("/run/lab", str(run_lab)).replace("/usr/local/bin/wizlab", str(wizlab_bin)))
            learner.chmod(0o755)

            base = {k: v for k, v in os.environ.items()
                    if not k.startswith(("WIZ_", "AWS_", "GOOGLE_", "ARM_", "OKTA_", "TE_RUNNER_"))}
            learner_env = {**base, "TE_RUNNER_TAG": "v0.1.88", "TE_RUNNER_REV": "3986dec",
                           "CAPTURE_ENV": str(captured)}

            pre = subprocess.run(["/bin/bash", str(learner), "session", "verify", "--min-runner", "v99.0.0"],
                                 env=learner_env, capture_output=True, text=True, check=False)
            self.assertEqual(pre.returncode, 2)
            self.assertIn("wizlab-grant did not run", pre.stderr)
            self.assertFalse(captured.exists())

            secret_val = "s$ecret 'line1'\n`uname` line2"
            gcp_val = '{"private_key":"-----BEGIN KEY-----\\nabc$1\\n-----END KEY-----"}'
            grant_env = {**base, "WIZ_TENANT": "T", "WIZ_T_CLIENT_ID": "cid", "WIZ_T_CLIENT_SECRET": secret_val,
                         "GOOGLE_CREDENTIALS": gcp_val, "AWS_DEFAULT_REGION": "us-east-1", "UNLISTED_SECRET": "drop"}
            g = subprocess.run(["/bin/bash", str(grant), "session verify"],
                               env=grant_env, capture_output=True, text=True, check=False)
            self.assertEqual(g.returncode, 0, g.stderr)

            denied = subprocess.run(["/bin/bash", str(learner), "connector", "inspect"],
                                    env=learner_env, capture_output=True, text=True, check=False)
            self.assertEqual(denied.returncode, 2)
            self.assertIn("'connector inspect' is not permitted", denied.stderr)
            self.assertFalse(captured.exists())

            pos = subprocess.run(["/bin/bash", str(learner), "session", "verify", "--min-runner", "v99.0.0"],
                                 env=learner_env, capture_output=True, text=True, check=False)
            self.assertEqual(pos.returncode, 2)
            self.assertIn("below this lab's floor", pos.stderr)
            seen = json.loads(captured.read_text())
            self.assertEqual(seen.get("WIZ_T_CLIENT_ID"), "cid")
            self.assertEqual(seen.get("WIZ_T_CLIENT_SECRET"), secret_val)
            self.assertEqual(seen.get("GOOGLE_CREDENTIALS"), gcp_val)
            self.assertEqual(seen.get("AWS_DEFAULT_REGION"), "us-east-1")
            self.assertNotIn("UNLISTED_SECRET", seen)

            chk = subprocess.run(["/bin/bash", str(learner), "--check", "session", "verify", "--min-runner", "v99.0.0"],
                                 env=learner_env, capture_output=True, text=True, check=False)
            self.assertEqual(chk.returncode, 1)
            self.assertIn("below this lab's floor", chk.stderr)
            self.assertIn("wizlab: exit 2", chk.stderr)

    def test_learner_wrapper_enforces_newline_and_per_verb_flag_allowlist(self):
        grant_src = (ROOT / "bin" / "wizlab-grant").read_text()
        learner_src = (ROOT / "bin" / "wizlab-learner").read_text()
        with tempfile.TemporaryDirectory() as td:
            tmp = pathlib.Path(td)
            run_lab = tmp / "run" / "lab"
            wizlab_bin = tmp / "wizlab"
            captured = tmp / "captured.json"
            wizlab_bin.write_text(
                f"#!{sys.executable}\n"
                "import json, pathlib, sys\n"
                f"pathlib.Path({str(captured)!r}).write_text(json.dumps(sys.argv[1:]))\n"
            )
            wizlab_bin.chmod(0o755)
            grant = tmp / "wizlab-grant"
            grant.write_text(grant_src.replace("/run/lab", str(run_lab)))
            grant.chmod(0o755)
            learner = tmp / "wizlab-learner"
            learner.write_text(
                learner_src.replace("/run/lab", str(run_lab)).replace("/usr/local/bin/wizlab", str(wizlab_bin)))
            learner.chmod(0o755)
            env = {k: v for k, v in os.environ.items() if not k.startswith("WIZ_")}
            g = subprocess.run(
                ["/bin/bash", str(grant), "session verify", "connector inspect", "role inspect",
                 "k8sconnector ensure", "container inspect", "sensor inspect"],
                env={**env, "WIZ_CLIENT_ID": "cid"}, capture_output=True, text=True, check=False,
            )
            self.assertEqual(g.returncode, 0, g.stderr)

            for bad_argv, want_err in (
                (["session", "verify\nconnector inspect"], "newline in argument is not permitted"),
                (["session", "verify", "--min-runner", "v0.1.0\nx"], "newline in argument is not permitted"),
                (["k8sconnector", "ensure", "--cluster-arn", "arn:aws:eks:us-east-1:111111111111:cluster/lab-other",
                  "--enabled", "false"], "flag '--cluster-arn' is not permitted"),
                (["k8sconnector", "ensure", "--cluster-arn=arn:aws:eks:us-east-1:111111111111:cluster/lab-other"],
                 "flag '--cluster-arn' is not permitted"),
                (["k8sconnector", "ensure", "--session", "other"], "flag '--session' is not permitted"),
                (["k8sconnector", "ensure", "--name", "lab-other-k8s"], "flag '--name' is not permitted"),
                (["connector", "inspect", "--session", "other"], "flag '--session' is not permitted"),
                (["connector", "inspect", "--outpost-id", "o-1"], "flag '--outpost-id' is not permitted"),
                (["--check", "session", "verify", "--session", "other"], "flag '--session' is not permitted"),
                (["sensor", "inspect", "--require", "active"], "has no learner flag allowlist"),
            ):
                with self.subTest(bad_argv=bad_argv):
                    captured.unlink(missing_ok=True)
                    r = subprocess.run(["/bin/bash", str(learner), *bad_argv],
                                       env=env, capture_output=True, text=True, check=False)
                    self.assertEqual(r.returncode, 2, r.stderr)
                    self.assertIn(want_err, r.stderr)
                    self.assertFalse(captured.exists())

            for ok_argv in (
                ["--check", "session", "verify", "--min-runner", "v0.1.88", "--cloud", "aws",
                 "--account-id", "111111111111"],
                ["k8sconnector", "ensure", "--cluster", "lab-x", "--enabled", "true"],
                ["connector", "inspect", "--account-id", "111111111111", "--require", "healthy",
                 "--cloud", "aws", "--outpost-name", "lab-x"],
                ["container", "inspect", "--account-id", "111111111111", "--image-contains", "lab-x",
                 "--require-image"],
                ["role", "inspect", "--cloud", "aws", "--account-id", "111111111111",
                 "--role-name", "WizAccess-Role", "--trusts-service", "eks.amazonaws.com"],
            ):
                with self.subTest(ok_argv=ok_argv):
                    captured.unlink(missing_ok=True)
                    r = subprocess.run(["/bin/bash", str(learner), *ok_argv],
                                       env=env, capture_output=True, text=True, check=False)
                    self.assertEqual(r.returncode, 0, r.stderr)
                    self.assertEqual(json.loads(captured.read_text()), ok_argv)


class DevAccessGate(unittest.TestCase):
    """The gate is the credential's shape, not its presence. A lab that keeps its dev-access block live
    holds the placeholder `off` in both secrets between leases (the team store rejects an empty value), and
    a grader booting on it must neither join the tailnet nor start sshd. One row per (authkey, pubkey)."""

    ROWS = (
        # TS_AUTHKEY,        TE_DEV_SSH_PUBKEY,          tailnet joined, pubkey installed
        ("",                 "",                         False, False),  # learner track: neither wired
        ("off",              "off",                      False, False),  # parked between leases
        ("tskey-auth-kAAAA", "off",                      True,  False),  # key rotated, pubkey not yet
        ("tskey-auth-kAAAA", "ssh-ed25519 AAAAC3 lease", True,  True),   # a live lease
    )

    def _boot(self, authkey, pubkey):
        with tempfile.TemporaryDirectory() as td:
            tmp = pathlib.Path(td)
            capture = tmp / "capture"
            capture.touch()
            # Every binary the dev-access branch reaches through PATH records its name; the absolute
            # /usr/sbin/sshd and the /root and /var writes fail harmlessly as non-root (no set -e).
            for name in ("tailscaled", "tailscale", "install", "ssh-keygen", "tmux", "sleep"):
                stub = tmp / name
                stub.write_text(f"#!/bin/sh\necho {name} >> \"$CAPTURE\"\nexit 0\n")
                stub.chmod(0o755)
            env = {**os.environ, "PATH": f"{tmp}:{os.environ['PATH']}", "CAPTURE": str(capture),
                   "TS_AUTHKEY": authkey, "TE_DEV_SSH_PUBKEY": pubkey}
            result = subprocess.run(["/bin/sh", str(ROOT / "entrypoint.sh"), "true"], env=env,
                                    capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            return set(capture.read_text().split())

    def test_gate_rows(self):
        for authkey, pubkey, joined, installed in self.ROWS:
            with self.subTest(authkey=authkey, pubkey=pubkey):
                called = self._boot(authkey, pubkey)
                self.assertEqual("tailscale" in called, joined)
                self.assertEqual("install" in called, installed)


class LearnerCloudAuth(unittest.TestCase):
    """The learner terminal opens as `learner` (HOME=/home/learner), and neither gcloud nor az reads the
    container's lease env vars directly. Each login block that runs and populates its config dir mirrors
    root's config dir into /home/learner (`cp -a src/. dst/` so a second run never nests) and chowns it
    to learner:learner; a block that did not run or created no config dir copies nothing."""

    ROWS = (
        # env, create_dirs, expected (cp + chown) lines
        ({}, True, []),
        ({"GOOGLE_CREDENTIALS": '{"type":"service_account"}'}, False, []),
        ({"ARM_CLIENT_ID": "i", "ARM_CLIENT_SECRET": "s", "ARM_TENANT_ID": "t", "ARM_SUBSCRIPTION_ID": "u"},
         False, []),
        ({"GOOGLE_CREDENTIALS": '{"type":"service_account"}', "GOOGLE_PROJECT": "p"}, True,
         ["cp -a <home>/.config/gcloud/. /home/learner/.config/gcloud/",
          "chown -R learner:learner /home/learner/.config/gcloud"]),
        ({"ARM_CLIENT_ID": "i", "ARM_CLIENT_SECRET": "s", "ARM_TENANT_ID": "t", "ARM_SUBSCRIPTION_ID": "u"}, True,
         ["cp -a <home>/.azure/. /home/learner/.azure/",
          "chown -R learner:learner /home/learner/.azure"]),
        ({"GOOGLE_CREDENTIALS": '{"type":"service_account"}',
          "ARM_CLIENT_ID": "i", "ARM_CLIENT_SECRET": "s", "ARM_TENANT_ID": "t", "ARM_SUBSCRIPTION_ID": "u"}, True,
         ["cp -a <home>/.config/gcloud/. /home/learner/.config/gcloud/",
          "chown -R learner:learner /home/learner/.config/gcloud",
          "cp -a <home>/.azure/. /home/learner/.azure/",
          "chown -R learner:learner /home/learner/.azure"]),
    )

    def _boot(self, extra_env, create_dirs, home_is_learner=False):
        with tempfile.TemporaryDirectory() as td:
            tmp = pathlib.Path(td)
            home = tmp / ("learner" if home_is_learner else "root")
            home.mkdir()
            script = ROOT / "entrypoint.sh"
            if home_is_learner:
                script = tmp / "entrypoint.sh"
                script.write_text((ROOT / "entrypoint.sh").read_text().replace("/home/learner", str(home)))
            capture = tmp / "capture"
            capture.touch()
            gcloud_body = "mkdir -p \"$HOME/.config/gcloud\"\nexit 0\n" if create_dirs else "exit 1\n"
            az_body = "mkdir -p \"$HOME/.azure\"\nexit 0\n" if create_dirs else "exit 1\n"
            for name, body in (("gcloud", gcloud_body), ("az", az_body), ("install", "exit 0\n")):
                stub = tmp / name
                stub.write_text("#!/bin/sh\n" + body)
                stub.chmod(0o755)
            for name in ("cp", "chown"):
                stub = tmp / name
                stub.write_text(f"#!/bin/sh\necho \"{name} $*\" >> \"$CAPTURE\"\nexit 0\n")
                stub.chmod(0o755)
            clean = {k: v for k, v in os.environ.items()
                     if not k.startswith(("GOOGLE_", "ARM_", "TS_", "TE_DEV_"))}
            env = {**clean, "HOME": str(home), "RUN_LAB_DIR": str(tmp / "run"),
                   "PATH": f"{tmp}:{os.environ['PATH']}", "CAPTURE": str(capture), **extra_env}
            result = subprocess.run(["/bin/sh", str(script), "true"], env=env,
                                    capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            if home_is_learner and create_dirs:
                self.assertTrue((home / ".config" / "gcloud").is_dir())
                self.assertTrue((home / ".azure").is_dir())
            return [ln.replace(str(home), "<home>") for ln in capture.read_text().splitlines()]

    def test_login_blocks_mirror_and_chown_config_for_learner(self):
        for extra_env, create_dirs, want in self.ROWS:
            with self.subTest(keys=sorted(extra_env), create_dirs=create_dirs):
                self.assertEqual(self._boot(extra_env, create_dirs), want)

    def test_skips_self_copy_when_home_is_learner(self):
        both_env = self.ROWS[-1][0]
        self.assertEqual(self._boot(both_env, True, home_is_learner=True), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
