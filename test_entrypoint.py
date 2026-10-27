#!/usr/bin/env python3
import os
import pathlib
import stat
import subprocess
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
        (("session verify",), {"WIZ_CLIENT_ID": "id"}, 0, "session verify\n"),
        (("session verify", "connector inspect"), {"WIZ_TBCMP_CLIENT_ID": "id", "UNRELATED": "x"}, 0,
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

    def _boot(self, extra_env, create_dirs):
        with tempfile.TemporaryDirectory() as td:
            tmp = pathlib.Path(td)
            home = tmp / "root"
            home.mkdir()
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
            env = {**clean, "HOME": str(home), "PATH": f"{tmp}:{os.environ['PATH']}",
                   "CAPTURE": str(capture), **extra_env}
            result = subprocess.run(["/bin/sh", str(ROOT / "entrypoint.sh"), "true"], env=env,
                                    capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            return [ln.replace(str(home), "<home>") for ln in capture.read_text().splitlines()]

    def test_login_blocks_mirror_and_chown_config_for_learner(self):
        for extra_env, create_dirs, want in self.ROWS:
            with self.subTest(keys=sorted(extra_env), create_dirs=create_dirs):
                self.assertEqual(self._boot(extra_env, create_dirs), want)


if __name__ == "__main__":
    unittest.main(verbosity=2)
