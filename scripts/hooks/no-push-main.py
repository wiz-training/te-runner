#!/usr/bin/env python3
"""Claude Code PreToolUse hook: `main` takes PRs only (te-labkit-v2/CLAUDE.md).

Reads the Bash tool call from stdin and exits 2 (block) when any `git push` in
it would land on `main`: an explicit `main` refspec, `HEAD` while on main, or a
bare push while main is checked out. Branch pushes and tag pushes go through.

Reproducer (exits 2):
  echo '{"tool_input":{"command":"git push origin main"}}' | python3 scripts/hooks/no-push-main.py
"""
import json
import re
import shlex
import subprocess
import sys

MAIN = re.compile(r"(^|:)(refs/heads/)?main$")


def on_main() -> bool:
    out = subprocess.run(["git", "branch", "--show-current"], capture_output=True, text=True)
    return out.stdout.strip() == "main"


def lands_on_main(argv: list[str]) -> bool:
    if len(argv) < 2 or argv[0] != "git" or argv[1] != "push":
        return False
    positional = [a for a in argv[2:] if not a.startswith("-")]
    refs = positional[1:]  # positional[0] is the remote
    if not refs:
        return on_main()
    return any(MAIN.search(r) or (r == "HEAD" and on_main()) for r in refs)


def main() -> int:
    try:
        cmd = json.load(sys.stdin).get("tool_input", {}).get("command", "")
    except (json.JSONDecodeError, AttributeError):
        return 0
    for seg in re.split(r"[;&|]+|\n", cmd):
        try:
            argv = shlex.split(seg)
        except ValueError:
            argv = seg.split()
        if lands_on_main(argv):
            msg = "blocked: main takes PRs only. Push a branch and open a PR (te-labkit-v2/CLAUDE.md)."
            print(msg, file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
