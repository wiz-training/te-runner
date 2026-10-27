"""Structural and shell-step tests for `.github/workflows/build.yml` and `reap.yml`.

PyYAML is not in `requirements-dev.txt`, so `image` job steps are parsed from the workflow indentation with
stdlib regexes; structural step-order checks plus running the extracted `run:` shell blocks are what a
shimmed build test can assert without running GitHub Actions.
"""
import pathlib
import re
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
BUILD_YML = ROOT / ".github" / "workflows" / "build.yml"
REAP_YML = ROOT / ".github" / "workflows" / "reap.yml"


def _live(text):
    return "\n".join(ln.split("#", 1)[0].rstrip() if not ln.lstrip().startswith("#") else ""
                     for ln in text.splitlines())


def _image_steps(text):
    """`[(label, body)]` for each step under `jobs.image.steps` in `build.yml`."""
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    start = next(i for i, ln in enumerate(lines) if ln.strip() == "steps:") + 1
    steps = []
    for ln in lines[start:]:
        if ln.startswith("      - "):
            steps.append([ln[8:]])
        elif steps and (not ln.strip() or ln.startswith("        ")):
            steps[-1].append(ln[8:] if ln.startswith("        ") else "")
        elif ln.strip():
            break
    out = []
    for raw in steps:
        body = "\n".join(raw).strip()
        m = re.match(r"name:\s*(.+)", raw[0]) or re.match(r"run:\s*(.+)", raw[0]) or re.match(r"uses:\s*(.+)", raw[0])
        out.append((m.group(1).strip() if m else raw[0].strip(), body))
    return out


def _run_block(step_body):
    m = re.search(r"(?:^|\n)run:\s*\|\n(.*)", step_body, re.DOTALL)
    if m:
        return "\n".join(ln[2:] if ln.startswith("  ") else ln for ln in m.group(1).splitlines())
    m = re.match(r"run:\s*(.+)", step_body)
    return m.group(1) if m else ""


def _dockerfile_tar_extractions(text):
    uncommented = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    joined = re.sub(r"\\\n\s*", " ", uncommented)
    out = []
    for line in joined.splitlines():
        if not re.match(r"^\s*RUN\s+", line):
            continue
        for seg in re.split(r"&&|\|\||;|\|", re.sub(r"^\s*RUN\s+", "", line)):
            tokens = seg.strip().split()
            if tokens and tokens[0] == "tar" and any(
                t == "--extract" or (t.startswith("-") and not t.startswith("--") and "x" in t) or (i == 1 and "x" in t)
                for i, t in enumerate(tokens[1:], start=1)
            ):
                out.append(seg.strip())
    return out


class DockerfileTests(unittest.TestCase):
    """GNU tar as root defaults to --same-owner: go-containerregistry v0.20.6 packs crane as 1001:118
    (tailscale 1.102.3 is 0:0), and any archive member packed by UID 1000 would extract into
    /usr/local/bin owned by learner (UID 1000)."""

    def test_every_tar_extraction_passes_no_same_owner(self):
        cmds = _dockerfile_tar_extractions((ROOT / "Dockerfile").read_text())
        self.assertGreaterEqual(len(cmds), 2)
        for cmd in cmds:
            with self.subTest(cmd=cmd):
                self.assertIn("--no-same-owner", cmd.split())
        sample = "RUN curl -sSfL u -o /tmp/a.tgz && tar -xzf /tmp/a.tgz -C /usr/local/bin && tar xzf /tmp/b.tgz\n"
        self.assertEqual(len(_dockerfile_tar_extractions(sample)), 2)


class BuildWorkflowTests(unittest.TestCase):
    def test_reap_yml_pins_the_floating_tag(self):
        live = _live(REAP_YML.read_text())
        containers = [m.group(1).strip() for m in re.finditer(r"^\s*container:\s*(.+)$", live, re.MULTILINE)]
        self.assertEqual(containers, ["ghcr.io/wiz-training/te-runner:reap"])

    def test_reap_tag_advances_only_after_publish(self):
        steps = _image_steps(BUILD_YML.read_text())
        labels = [label for label, _ in steps]
        self.assertEqual(labels[-3:], ["smoke-test the image", 'docker push "$IMG"', "advance :reap"])
        last_label, last_body = steps[-1]
        self.assertEqual(last_label, "advance :reap")
        run_cmd = _run_block(last_body).strip()
        self.assertIn('docker tag "$IMG" "ghcr.io/${{ github.repository }}:reap"', run_cmd)
        self.assertIn('docker push "ghcr.io/${{ github.repository }}:reap"', run_cmd)
        self.assertLess(run_cmd.index("docker tag"), run_cmd.index("docker push"))
        for label, body in steps[:-1]:
            self.assertNotIn(":reap", _run_block(body) if label != "reap.yml pins :reap" else "")

    def test_build_refuses_without_reap_pin(self):
        steps = dict(_image_steps(BUILD_YML.read_text()))
        self.assertIn("reap.yml pins :reap", steps)
        script = _run_block(steps["reap.yml pins :reap"])
        self.assertIn(r"te-runner:reap\b", script)
        ok = subprocess.run(["sh", "-e", "-c", script], cwd=ROOT, capture_output=True, text=True, check=False)
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        with tempfile.TemporaryDirectory() as d:
            wf = pathlib.Path(d) / ".github" / "workflows"
            wf.mkdir(parents=True)
            (wf / "reap.yml").write_text("container: ghcr.io/wiz-training/te-runner:v0.1.81\n")
            bad = subprocess.run(["sh", "-e", "-c", script], cwd=d, capture_output=True, text=True, check=False)
            self.assertEqual(bad.returncode, 1)
            self.assertIn("::error::", bad.stdout + bad.stderr)

    def test_smoke_test_exercises_granted_learner_verb_past_wrapper(self):
        """Before IMAGE-3, smoke-test checked learner exit 2 before wizlab-grant and ungranted
        `connector inspect` after wizlab-grant, never executing wizlab-learner's `. /run/lab/wiz.env`
        and `exec /usr/local/bin/wizlab "$@"` positive path."""
        steps = dict(_image_steps(BUILD_YML.read_text()))
        smoke = (_run_block(steps["smoke-test the image"])
                 .replace("${{ github.ref_name }}", "v0.1.88")
                 .replace("${{ github.sha }}", "3986dec"))
        with tempfile.TemporaryDirectory() as td:
            tmp = pathlib.Path(td)
            grant_inner = tmp / "grant_inner.sh"
            docker = tmp / "docker"
            docker.write_text(
                "#!/bin/sh\n"
                "prev2=''; prev=''\n"
                "for a in \"$@\"; do\n"
                "  if [ \"$prev2\" = \"sh\" ] && [ \"$prev\" = \"-c\" ]; then\n"
                "    sh -n -c \"$a\" || exit 1\n"
                "    case \"$a\" in *wizlab-grant*) printf '%s\\n' \"$a\" > \"$GRANT_INNER\" ;; esac\n"
                "    exit 0\n"
                "  fi\n"
                "  prev2=\"$prev\"; prev=\"$a\"\n"
                "done\n"
                "case \"$*\" in *\" wizlab\"*) exit 2 ;; esac\n"
                "exit 0\n"
            )
            docker.chmod(0o755)
            env = {"PATH": f"{tmp}:/usr/bin:/bin", "IMG": "ghcr.io/wiz-training/te-runner:v0.1.88",
                   "GRANT_INNER": str(grant_inner)}
            outer = subprocess.run(["sh", "-e", "-c", smoke], env=env, capture_output=True, text=True, check=False)
            self.assertEqual(outer.returncode, 0, outer.stdout + outer.stderr)
            self.assertTrue(grant_inner.is_file())

            run_lab = tmp / "run" / "lab"
            inner = grant_inner.read_text().replace("/run/lab", str(run_lab))
            grant_bin = tmp / "wizlab-grant"
            grant_bin.write_text((ROOT / "bin" / "wizlab-grant").read_text().replace("/run/lab", str(run_lab)))
            grant_bin.chmod(0o755)
            su_calls = tmp / "su_calls"
            su_bin = tmp / "su"
            su_bin.write_text(
                "#!/bin/sh\n"
                "# su -s /bin/bash learner -c <cmd>\n"
                "cmd=\"$5\"\n"
                "printf '%s\\n' \"$cmd\" >> \"$SU_CALLS\"\n"
                "case \"$cmd\" in\n"
                "  \"wizlab session verify --min-runner v99.0.0\")\n"
                "    if [ \"${FLOOR_TO_STDOUT:-0}\" = \"1\" ]; then printf '%s\\n' \"$FLOOR_ERR\";\n"
                "    else printf '%s\\n' \"$FLOOR_ERR\" >&2; fi\n"
                "    exit \"$FLOOR_RC\" ;;\n"
                "  \"wizlab connector inspect\") exit 2 ;;\n"
                "  \"printenv \"*) exit 1 ;;\n"
                "  *) exit 99 ;;\n"
                "esac\n"
            )
            su_bin.chmod(0o755)

            def run_inner(floor_rc, floor_err, to_stdout="0"):
                su_calls.write_text("")
                return subprocess.run(
                    ["sh", "-e", "-c", inner],
                    env={"PATH": f"{tmp}:/usr/bin:/bin", "SU_CALLS": str(su_calls),
                         "FLOOR_RC": str(floor_rc), "FLOOR_ERR": floor_err, "FLOOR_TO_STDOUT": to_stdout},
                    capture_output=True, text=True, check=False,
                )

            ok = run_inner(2, "wizlab: runner v0.1.88 is below this lab's floor v99.0.0")
            self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
            self.assertIn("wizlab session verify --min-runner v99.0.0", su_calls.read_text().splitlines())
            self.assertEqual(run_inner(2, "wizlab: 'session verify' is not permitted").returncode, 1)
            self.assertEqual(run_inner(3, "wizlab: runner v0.1.88 is below this lab's floor v99.0.0").returncode, 1)
            self.assertEqual(
                run_inner(2, "wizlab: runner v0.1.88 is below this lab's floor v99.0.0", to_stdout="1").returncode, 1)


if __name__ == "__main__":
    unittest.main()
