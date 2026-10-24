#!/usr/bin/env python3
r"""Verify that a wizlab `(noun, verb)` pair conforms to the 7-part verb shape contract.

Usage:
  verb-shape.py [--repo <dir>] <noun> <verb>
  verb-shape.py [--repo <dir>] --all
  verb-shape.py --self-test

Checks:
  1. Registered in `wizlab.cli.VERBS`
  2. Fits `SPEC.md` §Grammar (one of the 6 kinds, or listed in Deliberate exceptions)
  3. Declares its flags in `wizlab.cli.FLAGS`
  4. Documented in `SPEC.md` §Nouns (header noun list + `- \`<noun> ...\`` bullet)
  5. Listed in `SPEC.md` §Verb floors (`<!-- verb-floors:begin -->`, `vX.Y.Z` or `vNEXT`)
  6. Covered by its contract table in `tests/test_wizlab.py` (`InspectContract` / `EnsureContract` / `DeleteContract`)
  7. When `ensure`: in `FOOTPRINT`, sweep type in `_SWEEP_TYPES`, and created fixture name starts with `lab-`
  8. Calling the handler in-process with a fixture `args` namespace exits in `{0, 1, 2, 3}`

Exit codes: 0 fits · 1 one or more shape checks failed · 2 usage error · 3 environment/repo error.
"""
import contextlib
import importlib
import io
import pathlib
import re
import sys
import tempfile
from unittest import mock


def _load_repo(repo):
    repo = pathlib.Path(repo).resolve()
    spec_file = repo / "wizlab" / "SPEC.md"
    test_file = repo / "tests" / "test_wizlab.py"
    if not (spec_file.is_file() and test_file.is_file()):
        raise OSError(f"{repo}: missing wizlab/SPEC.md or tests/test_wizlab.py")

    for mod_name in [k for k in sys.modules if k == "wizlab" or k.startswith("wizlab.") or k == "test_wizlab"]:
        sys.modules.pop(mod_name, None)

    sys_path_orig = list(sys.path)
    sys.path.insert(0, str(repo / "tests"))
    sys.path.insert(0, str(repo))
    try:
        wz = importlib.import_module("wizlab")
        tw = importlib.import_module("test_wizlab")
    finally:
        sys.path[:] = sys_path_orig

    spec_text = spec_file.read_text(encoding="utf-8")
    test_text = test_file.read_text(encoding="utf-8")

    grammar_m = re.search(r"^## Grammar\n(.*?)(?=\n### |\n## |\Z)", spec_text, re.MULTILINE | re.DOTALL)
    grammar_sec = grammar_m.group(1) if grammar_m else ""
    vt_m = re.search(r"\| verb \| means \|\n\|[-|]+\|\n((?:\|.*\|\n)+)", grammar_sec)
    kinds = {
        m.group(1)
        for m in re.finditer(r"^\|\s*`?([a-z-]+)`?\s*\|", vt_m.group(1) if vt_m else "", re.MULTILINE)
    }
    exc_m = re.search(r"\| pair \|[^\n]*\n\|[-|]+\|\n((?:\|.*\|\n?)+)", grammar_sec)
    exceptions = {
        (m.group(1), m.group(2))
        for m in re.finditer(
            r"^\|\s*`?([a-z0-9-]+)\s+([a-z0-9-]+)`?\s*\|", exc_m.group(1) if exc_m else "", re.MULTILINE
        )
    }

    nouns_m = re.search(r"^## Nouns\n(.*?)(?=\n## |\Z)", spec_text, re.MULTILINE | re.DOTALL)
    nouns_sec = nouns_m.group(1) if nouns_m else ""
    header_para, _, bullets_body = nouns_sec.partition("\n- ")
    header_nouns = set(re.findall(r"`([a-z0-9-]+)`", header_para))
    bullet_heads = [ln.split(" — ", 1)[0] for ln in ("- " + bullets_body).splitlines() if ln.startswith("- ")]
    bullet_nouns = {m.group(1) for hd in bullet_heads for m in re.finditer(r"`([a-z0-9-]+)\s+[a-z|-]+", hd)}

    fb_m = re.search(
        r"<!-- verb-floors:begin -->\n\| noun \| verb \| first tag \|\n\|[-|]+\|\n(.*?)\n<!-- verb-floors:end -->",
        spec_text,
        re.DOTALL,
    )
    floors_body = fb_m.group(1) if fb_m else ""
    floor_rows = {
        (m.group(1), m.group(2)): m.group(3)
        for m in re.finditer(
            r"^\|\s*([a-z0-9-]+)\s*\|\s*([a-z0-9-]+)\s*\|\s*(v\d+\.\d+\.\d+|vNEXT)\s*\|$",
            floors_body,
            re.MULTILINE,
        )
    }

    ec = tw.EnsureContract()
    ec.setUpClass()
    try:
        ensure_rows = ec.rows()
    finally:
        ec.tearDownClass()

    return {
        "repo": repo,
        "wz": wz,
        "tw": tw,
        "test_text": test_text,
        "kinds": kinds,
        "exceptions": exceptions,
        "header_nouns": header_nouns,
        "bullet_nouns": bullet_nouns,
        "floor_rows": floor_rows,
        "ensure_rows": ensure_rows,
    }


def _call_with_fixture_args(wz, tw, nv):
    fn = wz.VERBS[nv]
    args = wz.parse(nv, [])
    if hasattr(args, "timeout"):
        args.timeout = 0
    wiz = tw.FakeWiz()
    okta = tw.FakeOkta()
    gh = tw.FakeGitHub()
    env = {"INSTRUQT_SESSION_ID": "x", **tw.FakeWiz.ENV}
    wz._TOKENS.clear()
    wz._TOKEN.clear()
    with mock.patch.object(wz.time, "sleep", lambda *_: None), \
         mock.patch.object(tw._owner("_post"), "_post", wiz), \
         mock.patch.object(tw._owner("_okta_call"), "_okta_call", okta), \
         mock.patch.object(tw._owner("_gh_call"), "_gh_call", gh), \
         mock.patch.object(tw._owner("_cli"), "_cli", return_value=tw._proc(1, "", "error")), \
         mock.patch.dict(wz.os.environ, env, clear=True), \
         contextlib.redirect_stdout(io.StringIO()), \
         contextlib.redirect_stderr(io.StringIO()), \
         tw.exits() as ended:
        fn(args)
    return ended.code


def check_verb(ctx, noun, verb):
    """Return `(misses, exit_code_seen)` for `(noun, verb)`."""
    wz = ctx["wz"]
    tw = ctx["tw"]
    nv = (noun, verb)
    misses = []

    if nv not in wz.VERBS:
        return [f"{noun} {verb}: wizlab/cli.py: not in VERBS"], None

    if verb not in ctx["kinds"] and nv not in ctx["exceptions"]:
        misses.append(
            f"{noun} {verb}: wizlab/SPEC.md §Grammar: verb {verb!r} is neither in {sorted(ctx['kinds'])} "
            f"nor in Deliberate exceptions"
        )

    if nv not in wz.FLAGS or not isinstance(wz.FLAGS[nv], dict):
        misses.append(f"{noun} {verb}: wizlab/cli.py: missing FLAGS entry")

    if noun not in ctx["header_nouns"]:
        misses.append(f"{noun} {verb}: wizlab/SPEC.md §Nouns: `{noun}` missing from header noun list")
    if noun not in ctx["bullet_nouns"]:
        misses.append(f"{noun} {verb}: wizlab/SPEC.md §Nouns: missing `- \\`{noun} ...\\`` bullet")

    if nv not in ctx["floor_rows"]:
        misses.append(f"{noun} {verb}: wizlab/SPEC.md §Verb floors: missing row in <!-- verb-floors:begin --> block")

    if verb == "inspect":
        if nv not in tw.InspectContract.ROWS and nv not in tw.InspectContract.NOT_WIZ:
            misses.append(f"{noun} {verb}: tests/test_wizlab.py: missing InspectContract.ROWS / NOT_WIZ entry")
    elif verb == "ensure":
        if nv not in ctx["ensure_rows"] and nv not in tw.EnsureContract.NOT_WIZ:
            misses.append(f"{noun} {verb}: tests/test_wizlab.py: missing EnsureContract.rows() / NOT_WIZ entry")
    elif verb == "delete":
        if nv not in tw.DeleteContract.ROWS:
            misses.append(f"{noun} {verb}: tests/test_wizlab.py: missing DeleteContract.ROWS entry")
    else:
        fn_name = wz.VERBS[nv].__name__
        if fn_name not in ctx["test_text"]:
            misses.append(f"{noun} {verb}: tests/test_wizlab.py: handler `{fn_name}` is not referenced in tests")

    if verb == "ensure":
        fp = tw.ConnectorAndReaperSafety.FOOTPRINT
        if nv not in fp:
            misses.append(f"{noun} {verb}: tests/test_wizlab.py: missing ConnectorAndReaperSafety.FOOTPRINT entry")
        else:
            sweep_type = fp[nv]
            if sweep_type is not None:
                if sweep_type not in wz._SWEEP_TYPES:
                    misses.append(
                        f"{noun} {verb}: wizlab/reap.py: FOOTPRINT type {sweep_type!r} not in _SWEEP_TYPES"
                    )
                if nv in ctx["ensure_rows"]:
                    row = ctx["ensure_rows"][nv]
                    create_fn = row["creates"][0]
                    created_payload = row["absent"].get(create_fn, {})
                    created_name = wz._input_name(created_payload)
                    if not (isinstance(created_name, str) and created_name.startswith("lab-")):
                        misses.append(
                            f"{noun} {verb}: tests/test_wizlab.py: EnsureContract fixture creates "
                            f"{created_name!r}, must start with 'lab-'"
                        )

    code = _call_with_fixture_args(wz, tw, nv)
    if code not in (0, 1, 2, 3):
        misses.append(f"{noun} {verb}: fixture invocation exited with {code!r}, outside {{0, 1, 2, 3}}")

    return misses, code


def run_all(repo):
    ctx = _load_repo(repo)
    wz = ctx["wz"]
    verbs = list(wz.VERBS)
    all_misses = []
    fit_count = 0
    for noun, verb in verbs:
        misses, _ = check_verb(ctx, noun, verb)
        if misses:
            all_misses.extend(misses)
        else:
            fit_count += 1
    for m in all_misses:
        print(m, file=sys.stderr)
    print(f"verb-shape: {fit_count}/{len(verbs)} fit ({'ok' if not all_misses else 'FAIL'})")
    return 0 if not all_misses else 1


def run_one(repo, noun, verb):
    ctx = _load_repo(repo)
    misses, code = check_verb(ctx, noun, verb)
    if misses:
        for m in misses:
            print(m, file=sys.stderr)
        return 1
    floor = ctx["floor_rows"][(noun, verb)]
    print(f"verb-shape: {noun} {verb}: ok (floor={floor}, fixture_exit={code})")
    return 0


def self_test():
    repo = pathlib.Path(__file__).resolve().parent.parent
    sys_path_orig = list(sys.path)
    sys.path.insert(0, str(repo / "scripts"))
    try:
        import scaffold
    finally:
        sys.path[:] = sys_path_orig

    with tempfile.TemporaryDirectory() as tmp:
        scaffold.copy_repo_tree(repo, tmp)
        with contextlib.redirect_stdout(io.StringIO()):
            scaffold.scaffold_verb(tmp, "selftest", "inspect")
            scaffold.scaffold_verb(tmp, "selftest", "ensure")
            scaffold.scaffold_verb(tmp, "selftest", "delete")
        ctx = _load_repo(tmp)
        for v in ("inspect", "ensure", "delete"):
            misses, code = check_verb(ctx, "selftest", v)
            if misses or code not in (0, 1, 2, 3):
                print(f"self-test FAIL: widget {v}: misses={misses} code={code}", file=sys.stderr)
                return 1
        misses, _ = check_verb(ctx, "nosuch", "verb")
        if not misses:
            print("self-test FAIL: expected miss for nosuch verb", file=sys.stderr)
            return 1

    print("self-test: ok (scaffolded inspect/ensure/delete fit, missing verb detected)")
    return 0


def main(argv):
    if argv == ["--self-test"]:
        return self_test()
    repo = pathlib.Path(__file__).resolve().parent.parent
    i = 0
    if argv[:1] == ["--repo"]:
        if len(argv) < 2:
            print(__doc__.strip().splitlines()[2], file=sys.stderr)
            return 2
        repo = pathlib.Path(argv[1])
        i = 2
    rest = argv[i:]
    try:
        if rest == ["--all"]:
            return run_all(repo)
        if len(rest) == 2 and not rest[0].startswith("-"):
            return run_one(repo, rest[0], rest[1])
    except OSError as e:
        print(f"verb-shape: {e}", file=sys.stderr)
        return 3
    print(__doc__.strip().splitlines()[2], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
