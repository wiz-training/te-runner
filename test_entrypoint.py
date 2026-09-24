#!/usr/bin/env python3
import os
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent


class GcpCredentialFile(unittest.TestCase):
    def test_failed_login_still_removes_mode_600_key_file(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = pathlib.Path(td)
            capture = tmp / "capture"
            gcloud = tmp / "gcloud"
            gcloud.write_text(
                "#!/bin/sh\n"
                "case $1 in\n"
                "  auth) key=${4#--key-file}; stat -c '%a %n' \"$key\" > \"$CAPTURE\"; exit 1 ;;\n"
                "  *) exit 0 ;;\n"
                "esac\n"
            )
            gcloud.chmod(0o755)
            env = {
                **os.environ,
                "PATH": f"{tmp}:{os.environ['PATH']}",
                "CAPTURE": str(capture),
                "GOOGLE_CREDENTIALS": '{"private_key":"secret"}',
            }
            result = subprocess.run(["/bin/sh", str(ROOT / "entrypoint.sh"), "true"], env=env,
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
