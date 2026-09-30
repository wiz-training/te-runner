#!/usr/bin/env python3
"""Claude Code PreToolUse hook: `main` takes PRs only (te-labkit-v2/CLAUDE.md).

Reads the Bash tool call from stdin and exits 2 (block) when any `git push` in
it could land on `main`: an explicit `main` refspec (with or without a leading
`+`), `HEAD` or a bare push while main is checked out, a refspec the hook cannot
read (a `$var`, `$(…)` or backtick), or a push inside `sh -c "…"`. Git's own
options before `push` (`-C dir`, `-c k=v`, `--git-dir=…`) are skipped, and a
`cd dir` earlier in the same command line moves the branch check to `dir`.
Branch pushes and tag pushes go through.

Reproducers, all exit 2:
  echo '{"tool_input":{"command":"git push origin main"}}' | python3 scripts/hooks/no-push-main.py
  python3 scripts/hooks/no-push-main.py --self-test
"""
import json
import os
import re
import shlex
import subprocess
import sys

MAIN = re.compile(r"^\+?([^:]*:)?(refs/heads/)?main$")
DYNAMIC = re.compile(r"[$`]")
# Global git options that take a value in the next argv slot.
GIT_VALUE_OPTS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"}
SHELLS = {"sh", "bash", "dash", "zsh"}


def on_main(cwd) -> bool:
    try:
        out = subprocess.run(["git", "branch", "--show-current"], capture_output=True, text=True,
                             cwd=cwd or None, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return True  # cannot tell: refuse rather than wave through
    return out.returncode != 0 or out.stdout.strip() == "main"


def _git_subcommand(argv):
    """(subcommand index, -C directory) after git's global options, or (None, None)."""
    i, cdir = 1, None
    while i < len(argv):
        a = argv[i]
        if a in GIT_VALUE_OPTS:
            if a == "-C" and i + 1 < len(argv):
                cdir = argv[i + 1]
            i += 2
        elif a.startswith("-"):
            i += 1
        else:
            return i, cdir
    return None, None


def lands_on_main(argv: list[str], cwd: str, is_main=on_main) -> bool:
    if not argv:
        return False
    if os.path.basename(argv[0]) in SHELLS:
        # `sh -c "git push origin main"`: inspect the script the shell would run.
        return any(command_lands_on_main(a, cwd, is_main) for a in argv[1:] if "git" in a)
    if argv[0] != "git":
        return False
    sub, cdir = _git_subcommand(argv)
    if sub is None or argv[sub] != "push":
        return False
    if cdir:
        cwd = cdir if os.path.isabs(cdir) or not cwd else os.path.join(cwd, cdir)
    positional = [a for a in argv[sub + 1:] if not a.startswith("-")]
    refs = positional[1:]  # positional[0] is the remote
    if not refs:
        return is_main(cwd)
    for r in refs:
        if DYNAMIC.search(r):
            return True  # `$(git branch --show-current)`: unreadable here, so treated as main
        if MAIN.search(r) or (r.lstrip("+") == "HEAD" and is_main(cwd)):
            return True
    return False


def command_lands_on_main(cmd: str, cwd: str, is_main=on_main) -> bool:
    for seg in re.split(r"[;&|]+|\n", cmd):
        try:
            argv = shlex.split(seg)
        except ValueError:
            argv = seg.split()
        if len(argv) == 2 and argv[0] == "cd":
            cwd = argv[1] if os.path.isabs(argv[1]) or not cwd else os.path.join(cwd, argv[1])
            continue
        if lands_on_main(argv, cwd, is_main):
            return True
    return False


# (command, branch checked out, blocked?). Branch is what `git branch --show-current` would answer.
CASES = (
    ("git push origin main", "feature", True),
    ("git push origin +main", "feature", True),
    ("git push --force-with-lease origin HEAD:main", "feature", True),
    ("git push origin main:main", "feature", True),
    ("git push origin refs/heads/main:refs/heads/main", "feature", True),
    ("git -C /x push origin main", "feature", True),
    ("git -c push.default=current push origin main", "feature", True),
    ("git push -u origin main", "feature", True),
    ("git push origin $(git branch --show-current)", "feature", True),
    ("git push origin $BRANCH", "feature", True),
    ("sh -c 'git push origin main'", "feature", True),
    ("cd /tmp && git push origin feature && git push origin main", "feature", True),
    ("git push", "main", True),
    ("git push origin HEAD", "main", True),
    ("git push", "feature", False),
    ("git push origin HEAD", "feature", False),
    ("git push origin feature", "main", False),
    ("git push origin v0.6.1", "main", False),
    ("git push origin main-fix", "main", False),
    ("git push origin feature:refs/heads/mainline", "main", False),
    ("gh pr merge --admin 1", "main", False),
    ("echo git push origin main", "main", False),
)


def self_test() -> int:
    bad = [(c, b) for c, b, want in CASES
           if command_lands_on_main(c, "", lambda cwd, b=b: b == "main") != want]
    for c, b in bad:
        print(f"self-test FAIL: {c!r} on {b}", file=sys.stderr)
    print(f"self-test: {'ok' if not bad else 'FAIL'} ({len(CASES)} cases)")
    return 0 if not bad else 3


def main() -> int:
    if sys.argv[1:] == ["--self-test"]:
        return self_test()
    try:
        data = json.load(sys.stdin)
        cmd = data.get("tool_input", {}).get("command", "")
        cwd = data.get("cwd", "")
    except (json.JSONDecodeError, AttributeError):
        return 0
    if command_lands_on_main(cmd, cwd):
        print("blocked: main takes PRs only. Push a branch and open a PR (te-labkit-v2/CLAUDE.md).",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
