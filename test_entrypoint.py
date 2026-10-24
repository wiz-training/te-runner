#!/usr/bin/env python3
import os
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent


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
