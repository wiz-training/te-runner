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


if __name__ == "__main__":
    unittest.main()
