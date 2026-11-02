#!/usr/bin/env python3
"""Generate or check the `### Verb floors` table in `wizlab/SPEC.md` from git tag history.

Usage: `verb-floors.py [--repo <dir>] [--spec <file>] (--check | --write | --self-test)`

Walks `git tag --list 'v*'` in version order, reads `wizlab/cli.py` (or `wizlab/wizlab` before the
v0.1.48 package split) at each tag via `git show`, parses `VERBS`, and records the first tag each
`(noun, verb)` in `wizlab/cli.py` appears in.

A verb in no tag renders as `vNEXT`. `--check` also accepts `vNEXT` for a verb whose first tag is the
newest tag, so the commit that adds a verb passes CI before its tag exists and still passes once the tag
points at it; `--write` after the release replaces `vNEXT` with the tag, and a later tag makes a leftover
`vNEXT` a diff again.

Exit codes: 0 in sync / written / self-test pass · 1 SPEC block differs (`--check`) or self-test
failure · 2 usage error or missing markers in SPEC · 3 git error or clone has no `v*` tags.
"""
import contextlib
import difflib
import io
import pathlib
import re
import subprocess
import sys
import tempfile

NEXT = "vNEXT"
BEGIN_MARKER = "<!-- verb-floors:begin -->"
END_MARKER = "<!-- verb-floors:end -->"
RUNNER_PATHS = ("wizlab/cli.py", "wizlab/wizlab")
TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
VERB_RE = re.compile(r'\(\s*"([a-z0-9-]+)"\s*,\s*"([a-z0-9-]+)"\s*\)\s*:')


def version_key(tag):
    m = TAG_RE.match(tag)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def parse_verbs(source):
    """Ordered `(noun, verb)` pairs from `VERBS = { ... }` in one wizlab source."""
    head = source.split("\nFLAGS = {", 1)[0]
    _, _, body = head.partition("VERBS = {")
    target = body.split("\n}\n", 1)[0] if body else head
    seen = []
    for m in VERB_RE.finditer(target):
        pair = (m.group(1), m.group(2))
        if pair not in seen:
            seen.append(pair)
    return seen


def list_tags(repo):
    r = subprocess.run(
        ["git", "-C", str(repo), "tag", "--list", "v*"],
        capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        raise OSError(f"git tag failed in {repo}: {r.stderr.strip()}")
    sh = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--is-shallow-repository"],
        capture_output=True, text=True, check=False,
    )
    shallow = sh.stdout.strip() == "true"
    tags = [t.strip() for t in r.stdout.splitlines() if version_key(t.strip()) is not None]
    tags.sort(key=version_key)
    if shallow or "v0.1.0" not in tags:
        raise OSError(f"history incomplete: {len(tags)} tags, shallow={shallow}; fetch --tags --unshallow")
    return tags


def tag_source(repo, tag):
    for path in RUNNER_PATHS:
        r = subprocess.run(
            ["git", "-C", str(repo), "show", f"{tag}:{path}"],
            capture_output=True, text=True, check=False,
        )
        if r.returncode == 0:
            return r.stdout
    raise OSError(f"{repo}: cannot read {tag}:{' or '.join(RUNNER_PATHS)}")


def compute_floors(repo):
    """Return `([((noun, verb), first_tag), ...], tags)` for the current `wizlab/cli.py` `VERBS`."""
    repo = pathlib.Path(repo)
    cli_path = repo / "wizlab" / "cli.py"
    if not cli_path.is_file():
        raise OSError(f"{cli_path}: not found")
    current_verbs = parse_verbs(cli_path.read_text(encoding="utf-8"))
    tags = list_tags(repo)
    first_seen = {}
    for tag in tags:
        for pair in parse_verbs(tag_source(repo, tag)):
            if pair not in first_seen:
                first_seen[pair] = tag
    rows = [((noun, verb), first_seen.get((noun, verb), NEXT)) for noun, verb in current_verbs]
    return rows, tags


def accepted_block(actual, rows, newest_tag):
    """The computed block with `vNEXT` kept wherever SPEC says `vNEXT` and the verb's first tag is the newest."""
    spec_next = {
        (m.group(1), m.group(2))
        for m in re.finditer(r"^\|\s*([a-z0-9-]+)\s*\|\s*([a-z0-9-]+)\s*\|\s*" + NEXT + r"\s*\|$", actual, re.M)
    }
    return render_block([
        (pair, NEXT if tag == newest_tag and pair in spec_next else tag) for pair, tag in rows
    ])


def render_block(rows):
    lines = [
        BEGIN_MARKER,
        "| noun | verb | first tag |",
        "|---|---|---|",
        *[f"| {noun} | {verb} | {tag} |" for (noun, verb), tag in rows],
        END_MARKER,
    ]
    return "\n".join(lines)


def extract_block(spec_text):
    start = spec_text.find(BEGIN_MARKER)
    end = spec_text.find(END_MARKER)
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"missing {BEGIN_MARKER} .. {END_MARKER} markers")
    end += len(END_MARKER)
    return spec_text[start:end], start, end


def check_spec(repo, spec_path):
    spec_text = pathlib.Path(spec_path).read_text(encoding="utf-8")
    actual, _, _ = extract_block(spec_text)
    rows, tags = compute_floors(repo)
    expected = accepted_block(actual, rows, tags[-1])
    if actual != expected:
        diff = difflib.unified_diff(
            actual.splitlines(),
            expected.splitlines(),
            fromfile=str(spec_path),
            tofile="computed (git tags)",
            lineterm="",
        )
        print("\n".join(diff))
        return 1
    print(f"verb-floors: ok ({len(rows)} verbs across {len(tags)} tags, {tags[0]}..{tags[-1]})")
    return 0


def write_spec(repo, spec_path):
    spec_file = pathlib.Path(spec_path)
    spec_text = spec_file.read_text(encoding="utf-8")
    _, start, end = extract_block(spec_text)
    rows, tags = compute_floors(repo)
    rendered = render_block(rows)
    spec_file.write_text(spec_text[:start] + rendered + spec_text[end:], encoding="utf-8")
    print(f"verb-floors: wrote {len(rows)} rows ({tags[0]}..{tags[-1]}) to {spec_file}")
    return 0


def self_test():
    with tempfile.TemporaryDirectory() as tmp:
        repo = pathlib.Path(tmp)
        (repo / "wizlab").mkdir()
        spec = repo / "wizlab" / "SPEC.md"
        cli = repo / "wizlab" / "cli.py"
        spec.write_text(f"# spec\n{BEGIN_MARKER}\n{END_MARKER}\n", encoding="utf-8")

        def git(*args):
            subprocess.run(
                ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@e", *args],
                check=True, capture_output=True, text=True,
            )

        git("init", "-q")
        cli.write_text('VERBS = {\n    ("session", "verify"): cmd_v,\n}\n', encoding="utf-8")
        git("add", "wizlab/cli.py")
        git("commit", "-q", "-m", "init")

        for bad_tag in (None, "v0.1.83"):
            if bad_tag:
                git("tag", bad_tag)
            try:
                compute_floors(repo)
                print(f"self-test FAIL: expected OSError with incomplete history (tag={bad_tag})", file=sys.stderr)
                return 1
            except OSError as e:
                if "history incomplete:" not in str(e):
                    print(f"self-test FAIL: unexpected error {e}", file=sys.stderr)
                    return 1
            if bad_tag:
                git("tag", "-d", bad_tag)

        git("tag", "v0.1.0")
        cli.write_text(
            'VERBS = {\n    ("session", "verify"): cmd_v,\n    ("sensor", "inspect"): cmd_s,\n}\n',
            encoding="utf-8",
        )
        git("add", "wizlab/cli.py")
        git("commit", "-q", "-m", "add sensor inspect")
        git("tag", "v0.1.1")

        rows, tags = compute_floors(repo)
        want = [(("session", "verify"), "v0.1.0"), (("sensor", "inspect"), "v0.1.1")]
        if rows != want or tags != ["v0.1.0", "v0.1.1"]:
            print(f"self-test FAIL: got rows={rows} tags={tags}", file=sys.stderr)
            return 1

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc_stale = check_spec(repo, spec)
        if rc_stale != 1 or "+| sensor | inspect | v0.1.1 |" not in buf.getvalue():
            print("self-test FAIL: expected --check exit 1 with diff on stale block", file=sys.stderr)
            return 1

        with contextlib.redirect_stdout(io.StringIO()):
            rc_write = write_spec(repo, spec)
            rc_ok = check_spec(repo, spec)
        if rc_write != 0 or rc_ok != 0:
            print("self-test FAIL: expected --write then --check to exit 0", file=sys.stderr)
            return 1

        # Release flow: a verb added after the newest tag is vNEXT; the row stays valid when the next tag
        # lands on it, and goes stale once a newer tag exists.
        cli.write_text(
            'VERBS = {\n    ("session", "verify"): cmd_v,\n    ("sensor", "inspect"): cmd_s,\n'
            '    ("policy", "ensure"): cmd_p,\n}\n',
            encoding="utf-8",
        )
        git("add", "wizlab/cli.py")
        git("commit", "-q", "-m", "add policy ensure")
        with contextlib.redirect_stdout(io.StringIO()):
            write_spec(repo, spec)
        if f"| policy | ensure | {NEXT} |" not in spec.read_text(encoding="utf-8"):
            print("self-test FAIL: expected --write to render an untagged verb as vNEXT", file=sys.stderr)
            return 1
        expect = ((None, 0), ("v0.1.2", 0), ("v0.1.3", 1))
        for tag, want_rc in expect:
            if tag:
                git("tag", tag)
            with contextlib.redirect_stdout(io.StringIO()):
                rc = check_spec(repo, spec)
            if rc != want_rc:
                print(f"self-test FAIL: vNEXT row with tag={tag}: --check exit {rc}, want {want_rc}", file=sys.stderr)
                return 1
        with contextlib.redirect_stdout(io.StringIO()):
            write_spec(repo, spec)
            rc = check_spec(repo, spec)
        if rc != 0 or "| policy | ensure | v0.1.2 |" not in spec.read_text(encoding="utf-8"):
            print("self-test FAIL: expected --write to pin vNEXT to v0.1.2", file=sys.stderr)
            return 1

    print("self-test: ok (4 tags, 3 verbs, vNEXT release flow)")
    return 0


def main(argv):
    repo = pathlib.Path(__file__).resolve().parent.parent
    spec = None
    mode = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--repo" and i + 1 < len(argv):
            repo = pathlib.Path(argv[i + 1])
            i += 2
        elif arg == "--spec" and i + 1 < len(argv):
            spec = pathlib.Path(argv[i + 1])
            i += 2
        elif arg in ("--check", "--write", "--self-test") and mode is None:
            mode = arg
            i += 1
        else:
            print(__doc__.strip().splitlines()[2], file=sys.stderr)
            return 2
    if mode is None:
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        return 2
    if mode == "--self-test":
        return self_test()
    if spec is None:
        spec = repo / "wizlab" / "SPEC.md"
    try:
        return check_spec(repo, spec) if mode == "--check" else write_spec(repo, spec)
    except ValueError as e:
        print(f"verb-floors: {e}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"verb-floors: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
