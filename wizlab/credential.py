"""One record per credential noun: how it is found, minted, read back and deleted."""
import dataclasses
import pathlib
import sys
from collections.abc import Callable

from . import core

# The secret is readable only in the create payload, so every credential noun converges the same way:
# an existing object on the name is deleted and a fresh one minted on every run (SPEC.md §What `ensure`
# promises). A noun differs only in its documents and the path to (id, clientId, clientSecret).

# Tool visibility on the remote MCP server and every data query are filtered by the account's scopes;
# a lab that reads the tenant wants the one string that covers them all.
_DEFAULT_SCOPES = "read:all"


EXISTS = {"exists": lambda _node: True}


@dataclasses.dataclass(frozen=True)
class Credential:
    label: str                                  # print noun
    suffix: str                                 # on the session stem (core._named)
    find_doc: str
    find_field: str                             # the connection the find document answers with
    find_vars: Callable[[str], dict]
    create_doc: str
    create_input: Callable[[str, object], dict]  # (name, args) -> the create mutation's `input`
    creds: Callable[[dict], tuple]              # create payload -> (id, client_id, client_secret)
    delete_mutation: str
    emit_keys: tuple[str, str]                  # the env names the id and the secret are emitted under
    sweep_type: str                             # the _SWEEP_TYPES member the reaper reaches it through
    require: dict                               # --require choice -> predicate(node)
    delete_select: str = "_stub"
    status_field: str | None = None


def _cred_name(cred, args):
    return core._named(args, cred.suffix)


def find(cred, name):
    """The object named exactly `name`: every list filter is a substring match, so == is ours."""
    return core._exact(core._all_nodes(cred.find_doc, cred.find_vars(name), cred.find_field), name)


def _summary(cred, node):
    line = f"{cred.label} {node['name']} ({node['id']})"
    if cred.status_field:
        line += f": {cred.status_field}={node.get(cred.status_field)}"
    return line


def ensure(cred, args):
    """Converge to ONE fresh object on the session-stem name and emit its credentials to stdout and
    $EXEC_OUTPUT. Solve/setup only — never a learner check (it prints a secret)."""
    name = _cred_name(cred, args)
    inp = cred.create_input(name, args)  # a flag error is exit 2 before any request leaves
    existing = find(cred, name)
    if existing:
        core.api(core._delete_doc(cred.delete_mutation, cred.delete_select), {"id": existing["id"]})
        print(f"deleted {cred.label} {name} ({existing['id']}); re-minting", file=sys.stderr)
    data, _ = core.api(cred.create_doc, {"input": inp})
    oid, cid, sec = cred.creds(data)
    if not cid or not sec:
        core.die(3, f"{cred.label} create returned no client credentials")
    id_key, secret_key = cred.emit_keys
    lines = f"{id_key}={cid}\n{secret_key}={sec}\n"
    # --out-file is the only way a caller takes the pair without it reaching the play log: the
    # platform captures an exec's stdout, and a script that parses stdout for the secret publishes it
    # to anyone with access to the lab. With the flag the values go to the file and 0600 is the
    # caller's business (the grader is root-only); without it, behaviour is unchanged.
    out_file = getattr(args, "out_file", None)
    if out_file:
        pathlib.Path(out_file).write_text(lines)
        core._emit(lines, secret=True)
    else:
        core._emit(lines)
    print(f"created {cred.label} {name} ({oid})", file=sys.stderr)


def inspect(cred, args):
    """Exit 1 when nothing carries the exact name, or when the --require predicate is false of it."""
    name = _cred_name(cred, args)
    node = find(cred, name)
    if not node:
        print(f"no {cred.label} named {name}")
        sys.exit(1)
    print(_summary(cred, node))
    if not cred.require[args.require](node):
        sys.exit(1)


def delete(cred, args):
    """Delete by --id or (default) the exact session-stem name; absent is exit 0 and no mutation."""
    oid = args.id
    if not oid:
        name = _cred_name(cred, args)
        node = find(cred, name)
        if not node:
            print(f"no {cred.label} named {name}; nothing to delete")
            return
        oid = node["id"]
    core.api(core._delete_doc(cred.delete_mutation, cred.delete_select), {"id": oid})
    print(f"deleted {cred.label} {oid}")
