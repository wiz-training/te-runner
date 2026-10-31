#!/usr/bin/env python3
"""Sync the 16 Instruqt team secrets from AWS SSM Parameter Store.

Decision (eh24905-wiz, 2026-10-31): AWS SSM (`/te-labkit/*` plus `/gamekit/{admin,lab,super}_token`) is
the source of truth for the 16 team secrets fleet labs declare; sync is manual-dispatch only, with no cron.
"""
import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request

try:
    import boto3
except ModuleNotFoundError:  # pragma: no cover
    boto3 = None

TEAM = os.getenv("INSTRUQT_TEAM", "wiz")
SSM_PATH = "/te-labkit/"
EXTRA_MAP = {
    "/gamekit/admin_token": "GAMEKIT_ADMIN_TOKEN",
    "/gamekit/lab_token": "GAMEKIT_LAB_TOKEN",
    "/gamekit/super_token": "GAMEKIT_SUPER_TOKEN",
}
TEAM_SECRETS_Q = "query($t:String!){ teamSecrets(teamSlug:$t){ name } }"
UPSERT_SECRET_M = (
    "mutation($t:String!,$n:String!,$d:String!,$s:String!)"
    "{ upsertTeamSecret(teamSlug:$t,name:$n,description:$d,secret:$s){ name } }"
)


def _die(msg):
    print(f"secrets_sync: {msg}", file=sys.stderr)
    sys.exit(1)


class InstruqtError(Exception):
    """GraphQL transport or application error from play.instruqt.com."""


def _instruqt(query, variables):
    tok = os.getenv("INSTRUQT_TOKEN") or _die("INSTRUQT_TOKEN not set")
    req = urllib.request.Request(
        "https://play.instruqt.com/graphql",
        data=json.dumps({"query": query, "variables": variables}).encode(),
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raise InstruqtError(f"instruqt HTTP {e.code}: {e.read().decode(errors='replace')[:300]}") from e
    except OSError as e:
        raise InstruqtError(f"instruqt unreachable: {type(e).__name__}: {e}") from e
    try:
        res = json.loads(raw)
        errors, data = res.get("errors"), res.get("data")
    except (ValueError, AttributeError) as e:
        raise InstruqtError(f"instruqt body is not a GraphQL answer: {raw.decode(errors='replace')[:300]!r}") from e
    if errors:
        raise InstruqtError(f"instruqt: {errors}")
    if data is None:
        raise InstruqtError(f"instruqt answer has no data: {raw.decode(errors='replace')[:300]!r}")
    return data


def _read_descriptions(ssm):
    # GetParametersByPath and GetParameters return Parameter (no Description field); ParameterMetadata
    # from DescribeParameters is the only SSM read that carries Description.
    if not hasattr(ssm, "describe_parameters"):
        return {}
    out = {}
    for prefix in (SSM_PATH, "/gamekit/"):
        kwargs = {"ParameterFilters": [{"Key": "Name", "Option": "BeginsWith", "Values": [prefix]}]}
        while True:
            page = ssm.describe_parameters(**kwargs)
            for meta in page.get("Parameters") or []:
                if meta.get("Name") and meta.get("Description"):
                    out[meta["Name"]] = meta["Description"]
            tok = page.get("NextToken")
            if not tok:
                break
            kwargs["NextToken"] = tok
    return out


def _read_ssm(ssm):
    descs = _read_descriptions(ssm)
    raw_params = []
    kwargs = {"Path": SSM_PATH, "WithDecryption": True}
    while True:
        page = ssm.get_parameters_by_path(**kwargs)
        raw_params.extend(page.get("Parameters") or [])
        tok = page.get("NextToken")
        if not tok:
            break
        kwargs["NextToken"] = tok
    items = {}
    for p in sorted(raw_params, key=lambda x: x["Name"]):
        ssm_name = p["Name"]
        iq_name = EXTRA_MAP.get(ssm_name) or ssm_name.rsplit("/", 1)[-1]
        desc = descs.get(ssm_name) or p.get("Description") or None
        items[iq_name] = (p.get("Value") or "", desc)
    missing_extras = [k for k in EXTRA_MAP if EXTRA_MAP[k] not in items]
    if missing_extras:
        extra_rows = {}
        if hasattr(ssm, "get_parameters"):
            res = ssm.get_parameters(Names=missing_extras, WithDecryption=True)
            extra_rows = {p["Name"]: p for p in (res.get("Parameters") or [])}
        elif hasattr(ssm, "get_parameter"):
            for k in missing_extras:
                res = ssm.get_parameter(Name=k, WithDecryption=True)
                if "Parameter" in res:
                    extra_rows[k] = res["Parameter"]
        for ssm_name in missing_extras:
            iq_name = EXTRA_MAP[ssm_name]
            p = extra_rows.get(ssm_name, {})
            desc = descs.get(ssm_name) or p.get("Description") or None
            items[iq_name] = (p.get("Value") or "", desc)
    return items


def main(argv=None, *, ssm=None, send=None):
    parser = argparse.ArgumentParser(description="Sync team secrets from AWS SSM to Instruqt.")
    parser.add_argument("--dry-run", action="store_true", help="Print planned upserts without mutating Instruqt")
    parser.add_argument("--only", default="", help="Comma-separated Instruqt secret names to sync")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    if ssm is None:
        if boto3 is None:
            _die("boto3 is required")
        ssm = boto3.client("ssm")
    if send is None:
        send = _instruqt

    items = _read_ssm(ssm)
    ssm_names = set(items)

    data = send(TEAM_SECRETS_Q, {"t": TEAM})
    existing = {row["name"] for row in (data.get("teamSecrets") or []) if row and row.get("name")}
    unmanaged = sorted(existing - ssm_names)
    if unmanaged:
        print(f"# not managed by SSM: {', '.join(unmanaged)}")

    only = [n.strip() for n in args.only.split(",") if n.strip()]
    target_names = only if only else list(items)

    upserted = 0
    failed = 0
    for name in target_names:
        if name not in items:
            print(f"SKIP {name}: not in SSM", file=sys.stderr)
            failed += 1
            continue
        val, desc = items[name]
        # Empty writes overwrite working store entries with a blank payload (`gh secret set`-style).
        if not val:
            print(f"SKIP {name}: empty value in SSM")
            failed += 1
            continue
        if args.dry_run:
            print(f"would upsert {name} (desc: {'yes' if desc else 'no'})")
            continue
        # Instruqt stores `secret` verbatim and decodes base64 at sandbox build (`illegal base64 data` otherwise).
        encoded = base64.b64encode(val.encode()).decode()
        try:
            send(UPSERT_SECRET_M, {"t": TEAM, "n": name, "d": desc or "", "s": encoded})
        except InstruqtError as e:
            print(f"FAIL {name}: {e}", file=sys.stderr)
            failed += 1
            continue
        print(f"upserted {name}")
        upserted += 1

    if args.dry_run:
        return 1 if failed else 0
    print(f"# {upserted} upserted, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
