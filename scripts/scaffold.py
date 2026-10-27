#!/usr/bin/env python3
"""Scaffold the shape of a new wizlab verb from `_template/verb/` into its noun module and contract files.

Usage:
  scaffold.py [--repo <dir>] verb <noun> <verb> [--exception "<why>"]
  scaffold.py --self-test

Exit codes: 0 scaffolded (or self-test pass) · 1 self-test failure · 2 usage / existing pair /
non-grammar verb without --exception · 3 missing repo or template files.
"""
import contextlib
import io
import keyword
import pathlib
import re
import shutil
import string
import sys
import tempfile

GRAMMAR_KINDS = ("inspect", "ensure", "delete", "verify", "reap", "tenant")
TOKEN_RE = re.compile(r"^[a-z][a-z0-9-]*$")
VERB_RE = re.compile(r'\(\s*"([a-z0-9-]+)"\s*,\s*"([a-z0-9-]+)"\s*\)\s*:')


def _cap(noun):
    return "".join(part.capitalize() for part in noun.split("-"))


def _plural(noun):
    parts = noun.split("-")
    head = parts[0] + "".join(p.capitalize() for p in parts[1:])
    if head.endswith("y") and len(head) > 1 and head[-2] not in "aeiou":
        return head[:-1] + "ies"
    return head + "s"


def _section(tmpl_text, name):
    marker = f"# --- {name} ---\n"
    if marker not in tmpl_text:
        return tmpl_text
    after = tmpl_text.split(marker, 1)[1]
    return after.split("\n# --- ", 1)[0]


def _render(text, ctx):
    return string.Template(text).safe_substitute(ctx).rstrip("\n")


def _dispatch_body(verb, ctx):
    plural = ctx["plural"]
    cap = ctx["cap"]
    noun_key = ctx["noun_key"]
    if verb == "inspect":
        return (
            f'    if args.require not in ("exists",):\n'
            f'        core.die(2, f"unknown --require {{args.require!r}}")\n'
            f'    qy = "query Q($after: String) {{ {plural}(first: 100, after: $after) {{ "\n'
            f'    qy += "nodes {{ id name }} pageInfo {{ hasNextPage endCursor }} }} }}"\n'
            f'    nodes = core._all_nodes(qy, {{}}, "{plural}")\n'
            f'    if not core._exact(nodes, args.name):\n'
            f'        sys.exit(1)'
        )
    if verb == "ensure":
        return (
            f'    qy = "query Q($after: String) {{ {plural}(first: 100, after: $after) {{ "\n'
            f'    qy += "nodes {{ id name }} pageInfo {{ hasNextPage endCursor }} }} }}"\n'
            f'    nodes = core._all_nodes(qy, {{}}, "{plural}")\n'
            f'    if not core._exact(nodes, args.name):\n'
            f'        doc = "mutation M($input: Create{cap}Input!) {{ "\n'
            f'        doc += "create{cap}(input: $input) {{ {noun_key} {{ id name }} }} }}"\n'
            f'        core.api(doc, {{"input": {{"name": args.name}}}})'
        )
    if verb == "delete":
        return (
            f'    qy = "query Q($after: String) {{ {plural}(first: 100, after: $after) {{ "\n'
            f'    qy += "nodes {{ id name }} pageInfo {{ hasNextPage endCursor }} }} }}"\n'
            f'    nodes = core._all_nodes(qy, {{}}, "{plural}")\n'
            f'    hit = core._exact(nodes, args.name)\n'
            f'    if hit:\n'
            f'        core.api(core._delete_doc("delete{cap}"), {{"id": hit["id"]}})'
        )
    return '    core.die(2, "TODO")'


def _add_rel_import(src, mod_name):
    m = re.search(r"^from \. import \(\n(.*?)\n\)", src, re.MULTILINE | re.DOTALL)
    if not m:
        raise OSError("missing `from . import (...)` block")
    names = {x.strip().rstrip(",") for x in m.group(1).splitlines() if x.strip()}
    if mod_name in names:
        return src, False
    names.add(mod_name)
    body = "\n".join(f"    {n}," for n in sorted(names))
    return src[:m.start()] + f"from . import (\n{body}\n)" + src[m.end():], True


def _add_to_modules_tuple(init_src, mod_name):
    m = re.search(r"^MODULES\s*=\s*\(([^)]*)\)", init_src, re.MULTILINE | re.DOTALL)
    if not m:
        raise OSError("wizlab/__init__.py: missing `MODULES = (...)` anchor")
    mods = [x.strip() for x in m.group(1).replace("\n", " ").split(",") if x.strip()]
    if mod_name in mods:
        return init_src, False
    if "cli" in mods:
        mods.insert(mods.index("cli"), mod_name)
    else:
        mods.append(mod_name)
    lines, cur = [], "MODULES = ("
    for idx, item in enumerate(mods):
        piece = item + (")" if idx == len(mods) - 1 else ", ")
        if len(cur) + len(piece) > 118 and cur != "MODULES = (":
            lines.append(cur.rstrip())
            cur = "           " + piece
        else:
            cur += piece
    lines.append(cur)
    return init_src[:m.start()] + "\n".join(lines) + init_src[m.end():], True


def _ensure_stdlib_import(mod_src, pkg):
    if re.search(rf"^import {re.escape(pkg)}\b", mod_src, re.MULTILINE):
        return mod_src
    m = re.search(r"(?:^import [^\n]+\n)+", mod_src, re.MULTILINE)
    if m:
        existing = [ln.strip() for ln in m.group(0).splitlines() if ln.strip()]
        existing.append(f"import {pkg}")
        block = "\n".join(sorted(set(existing))) + "\n"
        return mod_src[:m.start()] + block + mod_src[m.end():]
    m_from = re.search(r"^from ", mod_src, re.MULTILINE)
    if m_from:
        return mod_src[:m_from.start()] + f"import {pkg}\n\n" + mod_src[m_from.start():]
    return f"import {pkg}\n" + mod_src


def _resolve_noun_module(repo, cli_verbs_block, noun, fn_noun):
    m = re.search(
        r'\(\s*"' + re.escape(noun) + r'"\s*,\s*"[a-z0-9-]+"\s*\)\s*:\s*([a-z_][a-z0-9_]*)\.',
        cli_verbs_block,
    )
    if m:
        return m.group(1)
    mod_name = noun if (repo / "wizlab" / f"{noun}.py").is_file() else fn_noun
    if not mod_name.isidentifier() or keyword.iskeyword(mod_name):
        raise ValueError(f"noun {noun!r} does not map to a valid module identifier ({mod_name!r})")
    return mod_name


def _flags_spec(verb):
    if verb == "inspect":
        return '{**_S, "--name": {}, "--require": _one_of("exists")}'
    if verb == "delete":
        return '{**_S, "--id": {}, "--name": {}}'
    return '{**_S, "--name": {}}'


def scaffold_verb(repo, noun, verb, exception=None):
    repo = pathlib.Path(repo)
    tmpl_dir = repo / "_template" / "verb"
    if not tmpl_dir.is_dir():
        raise OSError(f"{tmpl_dir}: template directory missing")
    if not (TOKEN_RE.match(noun) and TOKEN_RE.match(verb)):
        raise ValueError(f"invalid noun/verb {noun!r} {verb!r}: must match [a-z][a-z0-9-]*")
    if verb not in GRAMMAR_KINDS and not exception:
        raise ValueError(
            f"verb {verb!r} is outside SPEC.md §Grammar {GRAMMAR_KINDS}; pass --exception \"<why>\""
        )

    init_file = repo / "wizlab" / "__init__.py"
    cli_file = repo / "wizlab" / "cli.py"
    test_file = repo / "tests" / "test_wizlab.py"
    spec_file = repo / "wizlab" / "SPEC.md"
    reap_file = repo / "wizlab" / "reap.py"
    meas_file = repo / "measurements.yaml"
    for f in (init_file, cli_file, test_file, spec_file, reap_file, meas_file):
        if not f.is_file():
            raise OSError(f"{f}: not found")

    cli_text = cli_file.read_text(encoding="utf-8")
    existing = {(m.group(1), m.group(2)) for m in VERB_RE.finditer(cli_text.split("\nFLAGS = {", 1)[0])}
    if (noun, verb) in existing:
        raise ValueError(f"`wizlab {noun} {verb}` already exists in wizlab/cli.py")

    fn_noun = noun.replace("-", "")
    fn_verb = verb.replace("-", "_")
    cap = _cap(noun)
    plural = _plural(noun)
    ctx = {
        "noun": noun,
        "verb": verb,
        "fn_noun": fn_noun,
        "fn_verb": fn_verb,
        "cap": cap,
        "plural": plural,
        "noun_key": fn_noun,
        "flags_spec": _flags_spec(verb),
        "exception": exception or "",
    }
    ctx["dispatch_body"] = _dispatch_body(verb, ctx)

    cmd_tmpl = (tmpl_dir / "cmd_stub.py.tmpl").read_text(encoding="utf-8")
    flags_tmpl = (tmpl_dir / "flags.tmpl").read_text(encoding="utf-8")
    test_tmpl = (tmpl_dir / "test_row.tmpl").read_text(encoding="utf-8")
    spec_tmpl = (tmpl_dir / "spec.tmpl").read_text(encoding="utf-8")
    sweep_tmpl = (tmpl_dir / "sweep.tmpl").read_text(encoding="utf-8")

    # 1. Noun module wizlab/<mod>.py (+ wizlab/__init__.py MODULES when new) and wizlab/cli.py VERBS/FLAGS
    verbs_section = cli_text.split("\nFLAGS = {", 1)[0]
    mod_name = _resolve_noun_module(repo, verbs_section, noun, fn_noun)
    mod_file = repo / "wizlab" / f"{mod_name}.py"
    stub_code = _render(cmd_tmpl, ctx)
    if mod_file.is_file():
        mod_text = mod_file.read_text(encoding="utf-8")
        if "sys." in stub_code:
            mod_text = _ensure_stdlib_import(mod_text, "sys")
        mod_text = mod_text.rstrip("\n") + f"\n\n\n{stub_code}\n"
    else:
        sys_imp = "import sys\n\n" if "sys." in stub_code else ""
        mod_text = f'"""`{noun}` verbs."""\n{sys_imp}from . import core\n\n\n{stub_code}\n'
    mod_file.write_text(mod_text, encoding="utf-8")

    init_text = init_file.read_text(encoding="utf-8")
    init_text, imp_added = _add_rel_import(init_text, mod_name)
    init_text, mod_added = _add_to_modules_tuple(init_text, mod_name)
    init_touched = imp_added or mod_added
    if init_touched:
        init_file.write_text(init_text, encoding="utf-8")

    cli_text, _ = _add_rel_import(cli_text, mod_name)
    flags_row = _render(flags_tmpl, ctx)
    verb_row = f'    ("{noun}", "{verb}"): {mod_name}.cmd_{fn_noun}_{fn_verb},'
    if "\nVERBS = {\n" not in cli_text:
        raise OSError("wizlab/cli.py: missing `VERBS = {` anchor")
    verbs_head, verbs_sep, verbs_rest = cli_text.partition("\nVERBS = {\n")
    v_body, v_close, v_tail = verbs_rest.partition("\n}\n")
    cli_text = verbs_head + verbs_sep + v_body + "\n" + verb_row + v_close + v_tail

    f_head, f_sep, f_rest = cli_text.partition("\nFLAGS = {\n")
    if not f_sep:
        raise OSError("wizlab/cli.py: missing `FLAGS = {` anchor")
    f_body, f_close, f_tail = f_rest.partition("\n}\n")
    cli_text = f_head + f_sep + f_body + "\n" + flags_row + f_close + f_tail
    cli_file.write_text(cli_text, encoding="utf-8")

    # 2. tests/test_wizlab.py: insert contract row (and FOOTPRINT if ensure)
    test_text = test_file.read_text(encoding="utf-8")
    if verb == "inspect":
        row_code = _render(_section(test_tmpl, "inspect (InspectContract.ROWS)"), ctx)
        anchor = (
            '        ("user", "inspect"): {"argv": ["--wiz"],\n'
            '                              "present": {"users": _conn(USER)},\n'
            '                              "absent": [{"users": _conn()}, '
            '{"users": _conn(dict(USER, status="DELETED"))}]},'
        )
        if anchor not in test_text:
            raise OSError("tests/test_wizlab.py: missing InspectContract.ROWS anchor")
        test_text = test_text.replace(anchor, anchor + "\n" + row_code, 1)
    elif verb == "ensure":
        row_code = _render(_section(test_tmpl, "ensure (EnsureContract.rows)"), ctx)
        anchor = (
            '            ("outpost", "ensure"): {\n'
            '                "argv": ["--name", "lab-x", "--role-arn", "a"],\n'
            '                "absent": {"outposts": _conn(), "createOutpost": {"outpost": self.OUTPOST}},\n'
            '                "present": {"outposts": _conn(self.OUTPOST)},\n'
            '                "creates": ["createOutpost"], "invalid": [["--name", "lab-x"]]},'
        )
        if anchor not in test_text:
            raise OSError("tests/test_wizlab.py: missing EnsureContract.rows() anchor")
        test_text = test_text.replace(anchor, anchor + "\n" + row_code, 1)
        fp_code = _render(_section(test_tmpl, "footprint (ConnectorAndReaperSafety.FOOTPRINT)"), ctx)
        fp_anchor = '        ("user", "ensure"): None,           # Okta; `user delete` is the inverse the reaper calls'
        if fp_anchor not in test_text:
            raise OSError("tests/test_wizlab.py: missing FOOTPRINT anchor")
        test_text = test_text.replace(fp_anchor, fp_anchor + "\n" + fp_code, 1)
    elif verb == "delete":
        row_code = _render(_section(test_tmpl, "delete (DeleteContract.ROWS)"), ctx)
        anchor = '        ("user", "delete"): {"argv": [], "field": "users", "node": USER, "deletes": ["deleteUser"]},'
        if anchor not in test_text:
            raise OSError("tests/test_wizlab.py: missing DeleteContract.ROWS anchor")
        test_text = test_text.replace(anchor, anchor + "\n" + row_code, 1)
    else:
        test_text += f"\n# TODO: add dedicated contract test referencing wz.cmd_{fn_noun}_{fn_verb}\n"
    test_file.write_text(test_text, encoding="utf-8")

    # 3. wizlab/SPEC.md: Nouns header + bullet + floor row (+ exception row if --exception)
    spec_text = spec_file.read_text(encoding="utf-8")
    if exception:
        exc_row = _render(_section(spec_tmpl, "exception_row"), ctx)
        exc_anchor = (
            "| `user login-url` | read-only author tool that prints a tenant's SAML sign-in URL without grading |"
        )
        if exc_anchor not in spec_text:
            raise OSError("wizlab/SPEC.md: missing Deliberate exceptions anchor")
        spec_text = spec_text.replace(exc_anchor, exc_anchor + "\n" + exc_row, 1)

    floor_row = _render(_section(spec_tmpl, "floor_row"), ctx)
    if "\n<!-- verb-floors:end -->" not in spec_text:
        raise OSError("wizlab/SPEC.md: missing <!-- verb-floors:end --> anchor")
    spec_text = spec_text.replace("\n<!-- verb-floors:end -->", f"\n{floor_row}\n<!-- verb-floors:end -->", 1)

    nouns_match = re.search(r"(^## Nouns\n)(.*?)(\n- )", spec_text, re.MULTILINE | re.DOTALL)
    if not nouns_match:
        raise OSError("wizlab/SPEC.md: missing `## Nouns` anchor")
    header_para = nouns_match.group(2)
    if f"`{noun}`" not in header_para and header_para.endswith(":"):
        new_header = header_para[:-1] + f", `{noun}`:"
        spec_text = spec_text[:nouns_match.start(2)] + new_header + spec_text[nouns_match.end(2):]

    bullet_line = _render(_section(spec_tmpl, "noun_bullet"), ctx)
    if "\n\n## What may be added" not in spec_text:
        raise OSError("wizlab/SPEC.md: missing `## What may be added` anchor")
    spec_text = spec_text.replace("\n\n## What may be added", f"\n{bullet_line}\n\n## What may be added", 1)
    spec_file.write_text(spec_text, encoding="utf-8")

    # 4. wizlab/reap.py: _SWEEP_TYPES entry when ensure (or TODO comment)
    reap_text = reap_file.read_text(encoding="utf-8")
    sweep_entry = _render(sweep_tmpl, ctx)
    if verb == "ensure":
        m_sweep = re.search(r"^(_SWEEP_TYPES\s*=\s*\[[^\]]*?)\]", reap_text, re.MULTILINE | re.DOTALL)
        if not m_sweep:
            raise OSError("wizlab/reap.py: missing _SWEEP_TYPES anchor")
        if sweep_entry not in m_sweep.group(1):
            prefix = m_sweep.group(1)
            sep = " " if prefix.rstrip().endswith(",") else ",\n                "
            reap_text = reap_text[:m_sweep.start()] + f"{prefix}{sep}{sweep_entry}]" + reap_text[m_sweep.end():]
        reap_text = reap_text.rstrip("\n") + f"\n# TODO: verify _SWEEP_TYPES handler for {noun}\n"
    else:
        reap_text = reap_text.rstrip("\n") + f"\n# TODO: verify reaper sweep coverage for {noun} {verb}\n"
    reap_file.write_text(reap_text, encoding="utf-8")

    # 5. measurements.yaml: plane TODO comment
    meas_text = meas_file.read_text(encoding="utf-8")
    meas_text += f"\n# TODO: record live timing row for wizlab {noun} {verb}\n"
    meas_file.write_text(meas_text, encoding="utf-8")

    touched = [f"wizlab/{mod_name}.py"]
    if init_touched:
        touched.append("wizlab/__init__.py")
    touched.extend([
        "wizlab/cli.py",
        "tests/test_wizlab.py",
        "wizlab/SPEC.md",
        "wizlab/reap.py",
        "measurements.yaml",
    ])
    print(f"scaffolded `wizlab {noun} {verb}` across {len(touched)} files:")
    for rel in touched:
        print(f"  touched: {rel}")
    for rel in touched:
        for idx, line in enumerate((repo / rel).read_text(encoding="utf-8").splitlines(), 1):
            if "TODO" in line and (noun in line or "die(2, \"TODO\")" in line):
                print(f"  TODO {rel}:{idx}: {line.strip()}")
    return 0


def copy_repo_tree(src, dst):
    """Copy tracked working-tree files into `dst` (excluding .git and caches) for isolated testing."""
    src, dst = pathlib.Path(src), pathlib.Path(dst)
    ignore = shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", ".ruff_cache")
    for item in src.iterdir():
        if item.name in (".git", "__pycache__", ".pytest_cache", ".ruff_cache"):
            continue
        target = dst / item.name
        if item.is_dir():
            shutil.copytree(item, target, ignore=ignore)
        else:
            shutil.copy2(item, target)


def self_test():
    repo = pathlib.Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory() as tmp:
        copy_repo_tree(repo, tmp)
        # 1. Existing verb refuses with ValueError (exit 2)
        try:
            scaffold_verb(tmp, "sensor", "inspect")
            print("self-test FAIL: expected duplicate verb refusal", file=sys.stderr)
            return 1
        except ValueError:
            pass

        # 2. Verb outside grammar without --exception refuses with ValueError (exit 2)
        try:
            scaffold_verb(tmp, "selftest", "count")
            print("self-test FAIL: expected non-grammar verb refusal", file=sys.stderr)
            return 1
        except ValueError:
            pass

        # 3. Scaffold inspect, two consecutive ensures across nouns, delete, and an exception verb
        cases = (
            ("selftest", "inspect", None),
            ("selftest", "ensure", None),
            ("selftesttwo", "ensure", None),
            ("selftest", "delete", None),
            ("selftest", "rotate", "rotates widget key"),
        )
        for noun_i, verb, exc in cases:
            with contextlib.redirect_stdout(io.StringIO()) as buf:
                rc = scaffold_verb(tmp, noun_i, verb, exception=exc)
            out = buf.getvalue()
            if f"touched: wizlab/{noun_i}.py" not in out or "touched: wizlab/cli.py" not in out or "TODO " not in out:
                print(f"self-test FAIL: missing touched/TODO output for {noun_i} {verb}", file=sys.stderr)
                return 1
            if rc != 0:
                print(f"self-test FAIL: scaffold {noun_i} {verb} returned {rc}", file=sys.stderr)
                return 1
        cli_after = (pathlib.Path(tmp) / "wizlab" / "cli.py").read_text(encoding="utf-8")
        if "def cmd_selftest_" in cli_after or "def cmd_selftesttwo_" in cli_after:
            print("self-test FAIL: cmd_* stub leaked into wizlab/cli.py", file=sys.stderr)
            return 1

    print("self-test: ok (duplicate refusal, grammar refusal, inspect/ensure/delete/exception scaffold)")
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
    if len(rest) < 3 or rest[0] != "verb":
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        return 2
    noun, verb = rest[1], rest[2]
    tail = rest[3:]
    exception = None
    if tail:
        if len(tail) == 2 and tail[0] == "--exception" and tail[1].strip():
            exception = tail[1].strip()
        else:
            print(__doc__.strip().splitlines()[2], file=sys.stderr)
            return 2
    try:
        return scaffold_verb(repo, noun, verb, exception=exception)
    except ValueError as e:
        print(f"scaffold: {e}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"scaffold: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
