#!/usr/bin/env python3
"""Claude Code PostToolUse hook: work done lands in te-docs' inbox without anyone remembering to write it.

Reads the Bash tool call from stdin. When the command opened a PR (`gh pr create`) or pushed a release tag
(`git push <remote> vX.Y.Z`), it writes `inbox/<date>-<repo>-<branch|tag>.md` — repo, branch, the PR title
and body or the tag, the commits since `origin/main` (or since the previous tag), the diff stat — as one
commit on te-docs' `inbox` branch, built with a temporary index so te-docs' working tree is never touched,
and pushes that branch. `main` stays PR-only; `/doc` merges `origin/inbox` before curating. te-docs is
`$TE_DOCS` or `<project>/../te-docs`; absent, one warning line and nothing else. Never blocks: exit 0.

Reproducer: `python3 scripts/hooks/inbox-note.py --self-test` builds a throwaway origin, te-docs and source
repo, fires a PR and a tag, and reads both notes back from the origin's `inbox` branch.
"""
import datetime
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
import tempfile

TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")
NOTE_LIMIT = 20000


def _run(args, cwd=None, env=None, input_=None):
    return subprocess.run(args, cwd=cwd, env=env, input=input_, capture_output=True, text=True, timeout=60)


def _git(repo, *args, env=None, input_=None):
    return _run(["git", "-C", str(repo), *args], env=env, input_=input_)


def leading_cd(command):
    """The directory a leading `cd <dir> &&` moves into, or None."""
    m = re.match(r"\s*cd\s+(\S+)\s*&&", command)
    return m.group(1).strip("'\"") if m else None


def classify(command):
    """('pr', title, body, head, slug) | ('tag', tag, '', None, None) | None when not recorded."""
    body = ""
    if "<<" in command:
        head, _, rest = command.partition("<<")
        marker = rest.split("\n", 1)[0].strip().strip("'\"-")
        lines = rest.split("\n")[1:]
        body = "\n".join(line for line in lines if line.strip() != marker).strip()
        command = head
    for segment in re.split(r"\s*(?:&&|\|\||;|\|)\s*", command):
        try:
            argv = shlex.split(segment)
        except ValueError:
            argv = segment.split()
        if len(argv) >= 3 and argv[0] == "gh" and argv[1:3] == ["pr", "create"]:
            title, head, slug = "", None, None
            for i, a in enumerate(argv):
                nxt = argv[i + 1] if i + 1 < len(argv) else ""
                if a in ("--title", "-t"):
                    title = nxt
                elif a.startswith("--title="):
                    title = a.split("=", 1)[1]
                elif a in ("--body", "-b"):
                    body = nxt
                elif a in ("--head", "-H"):
                    head = nxt
                elif a in ("--repo", "-R"):
                    slug = nxt
            return ("pr", title, body, head, slug)
        if len(argv) >= 3 and argv[0] == "git" and "push" in argv:
            tags = [a for a in argv[argv.index("push") + 1:] if TAG_RE.match(a)]
            if tags:
                return ("tag", tags[0], "", None, None)
    return None


def repo_name(project, slug=None):
    """The checkout's name, also from inside a worktree: `<main checkout>/.git` is the common dir."""
    if slug and "/" in slug:
        return slug.rsplit("/", 1)[1]
    common = _git(project, "rev-parse", "--git-common-dir").stdout.strip()
    if common:
        path = pathlib.Path(common)
        path = path if path.is_absolute() else (pathlib.Path(project) / path)
        return path.resolve().parent.name
    return pathlib.Path(project).name


def note(project, kind, subject, body, head=None, slug=None):
    project = pathlib.Path(project)
    repo = repo_name(project, slug)
    branch = head or _git(project, "branch", "--show-current").stdout.strip() or "detached"
    if kind == "tag":
        prev = _git(project, "describe", "--tags", "--abbrev=0", f"{subject}^").stdout.strip()
        rng = f"{prev}..{subject}" if prev else subject
        label = subject
    else:
        tip = branch if _git(project, "rev-parse", "-q", "--verify", branch).returncode == 0 else "HEAD"
        rng = f"origin/main..{tip}"
        label = branch
    log = _git(project, "log", "--no-merges", "--format=- %h %s%n%b", rng).stdout.strip()
    span = f"{rng.split('..')[0]}...{rng.split('..')[1]}" if ".." in rng else f"{subject}^..{subject}"
    stat = _git(project, "diff", "--stat", span).stdout.strip()
    today = datetime.date.today().isoformat()
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", f"{today}-{repo}-{label}") + ".md"
    text = "\n".join([
        f"# {repo}: {'PR' if kind == 'pr' else 'release'} {subject or label}", "",
        f"Written by `inbox-note.py` on {today}; repo `{repo}`, branch `{branch}`.", "",
        "## " + ("PR body" if kind == "pr" else "Tag"), "", body or subject, "",
        "## Commits", "", log or "(none beyond origin/main)", "",
        "## Diff stat", "", "```", stat or "(empty)", "```", "",
    ])
    return name, text[:NOTE_LIMIT]


def land(docs, name, text):
    """One commit adding inbox/<name> on te-docs' `inbox` branch, via a temporary index; pushed if possible."""
    docs = pathlib.Path(docs)
    _git(docs, "fetch", "-q", "origin", "inbox")
    base = None
    for ref in ("refs/remotes/origin/inbox", "refs/heads/inbox", "refs/remotes/origin/main", "HEAD"):
        if _git(docs, "rev-parse", "-q", "--verify", ref).returncode == 0:
            base = ref
            break
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        index = tmp.name
    os.unlink(index)
    env = dict(os.environ, GIT_INDEX_FILE=index)
    try:
        _git(docs, "read-tree", base, env=env)
        blob = _git(docs, "hash-object", "-w", "--stdin", input_=text).stdout.strip()
        _git(docs, "update-index", "--add", "--cacheinfo", f"100644,{blob},inbox/{name}", env=env)
        tree = _git(docs, "write-tree", env=env).stdout.strip()
    finally:
        if os.path.exists(index):
            os.unlink(index)
    commit = _git(docs, "commit-tree", tree, "-p", base, "-m", f"inbox: {name}").stdout.strip()
    _git(docs, "update-ref", "refs/heads/inbox", commit)
    pushed = _git(docs, "push", "-q", "origin", "inbox:inbox").returncode == 0
    return commit, pushed


def main(argv, stdin):
    if argv == ["--self-test"]:
        return self_test()
    me = pathlib.Path(__file__).resolve()
    original = me.parents[3] / "te-labkit-v2" / "scripts" / "hooks" / me.name
    if original.is_file() and original != me and original.read_bytes() != me.read_bytes():
        os.execv(sys.executable, [sys.executable, str(original), *argv])  # a copy runs the canonical file
    try:
        data = json.load(stdin)
        cmd = data.get("tool_input", {}).get("command", "")
    except (json.JSONDecodeError, AttributeError):
        return 0
    found = classify(cmd)
    if not found:
        return 0
    cwd = pathlib.Path(data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    moved = leading_cd(cmd)
    project = (cwd / moved).resolve() if moved else cwd  # a PR opened from a worktree is read there
    home = pathlib.Path(os.environ.get("CLAUDE_PROJECT_DIR") or cwd)
    docs = pathlib.Path(os.environ.get("TE_DOCS") or home.parent / "te-docs")
    if not (docs / ".git").exists():
        print(f"inbox-note: te-docs not checked out at {docs}; no note written", file=sys.stderr)
        return 0
    kind, subject, body, head, slug = found
    name, text = note(project, kind, subject, body, head, slug)
    commit, pushed = land(docs, name, text)
    print(f"inbox-note: te-docs inbox/{name} on branch inbox ({commit[:7]}{'' if pushed else ', not pushed'})")
    return 0


def self_test():
    import io
    today = datetime.date.today().isoformat()
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        origin, docs, src = d / "origin.git", d / "te-docs", d / "te-src"
        _run(["git", "init", "-q", "--bare", str(origin)])
        _run(["git", "clone", "-q", str(origin), str(docs)])
        (docs / "inbox").mkdir()
        (docs / "inbox" / ".gitkeep").touch()
        _git(docs, "add", "-A")
        _git(docs, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
        _git(docs, "push", "-q", "origin", "HEAD:main")
        _run(["git", "init", "-q", "-b", "main", str(src)])
        (src / "a.txt").write_text("a\n")
        _git(src, "add", "-A")
        _git(src, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "base")
        _git(src, "update-ref", "refs/remotes/origin/main", "HEAD")
        _git(src, "checkout", "-q", "-b", "feat/x")
        (src / "b.txt").write_text("b\n")
        _git(src, "add", "-A")
        _git(src, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "add b")
        _git(src, "tag", "v0.1.0")
        for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
            os.environ[k] = "t"
        for k in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
            os.environ[k] = "t@t"
        bad = []

        def fire(cmd):
            return main([], io.StringIO(json.dumps({"tool_input": {"command": cmd}, "cwd": str(src)})))

        fire("gh pr create --title \"add b\" --body-file - <<'EOF'\nWhy b matters.\nEOF")
        _git(docs, "fetch", "-q", "origin", "inbox")
        shown = _git(docs, "show", f"origin/inbox:inbox/{today}-te-src-feat-x.md").stdout
        for needle in ("# te-src: PR add b", "Why b matters.", "add b", "b.txt"):
            if needle not in shown:
                bad.append(f"PR note lacks {needle!r}")
        fire("git push origin v0.1.0")
        _git(docs, "fetch", "-q", "origin", "inbox")
        shown = _git(docs, "show", f"origin/inbox:inbox/{today}-te-src-v0.1.0.md").stdout
        if "release v0.1.0" not in shown:
            bad.append("tag note missing")
        if _git(docs, "status", "--porcelain").stdout.strip():
            bad.append("te-docs working tree touched")
        remote = _git(docs, "rev-parse", "--verify", "-q", "refs/remotes/origin/inbox").stdout.strip()
        if remote != _git(docs, "rev-parse", "refs/heads/inbox").stdout.strip():
            bad.append("local inbox and origin/inbox differ")
        if classify("git push origin feat/x") or classify("ls") or classify("gh pr view 3"):
            bad.append("non-event classified")
        if classify("cd x && gh pr create --repo o/r --title 'T' --body 'B'") != ("pr", "T", "B", None, "o/r"):
            bad.append("inline PR title/body")
        if leading_cd("cd /tmp/x && gh pr create") != "/tmp/x" or leading_cd("gh pr create"):
            bad.append("leading cd")
        # A PR opened from a worktree while the project checkout sits on main: the note reads the worktree.
        wt = d / "wt"
        _git(src, "checkout", "-q", "main")
        _git(src, "worktree", "add", "-q", "-b", "feat/wt", str(wt), "main")
        (wt / "c.txt").write_text("c\n")
        _git(wt, "add", "-A")
        _git(wt, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "add c from worktree")
        fire(f"cd {wt} && gh pr create --repo wiz-training/te-src --base main --head feat/wt "
             "--title 'wt pr' --body 'B'")
        _git(docs, "fetch", "-q", "origin", "inbox")
        shown = _git(docs, "show", f"origin/inbox:inbox/{today}-te-src-feat-wt.md").stdout
        for needle in ("branch `feat/wt`", "add c from worktree", "c.txt"):
            if needle not in shown:
                bad.append(f"worktree PR note lacks {needle!r}: {shown[:80]!r}")
    for line in bad:
        print(f"self-test FAIL: {line}", file=sys.stderr)
    print(f"self-test: {'ok' if not bad else 'FAIL'}")
    return 0 if not bad else 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], sys.stdin))
