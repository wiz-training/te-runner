"""The command line: verbs, the flags each reads, the exit-code contract, `--check`."""
import argparse
import sys

from . import (
    codescan,
    connector,
    core,
    k8s,
    outpost,
    reap,
    role,
    sensor,
    serviceaccount,
    session,
    user,
    wiz,
    workflow,
)

VERBS = {
    ("session", "verify"): session.cmd_session_verify,
    ("connector", "inspect"): connector.cmd_connector_inspect,
    ("connector", "ensure"): connector.cmd_connector_ensure,
    ("connector", "delete"): connector.cmd_connector_delete,
    ("instance", "inspect"): connector.cmd_instance_inspect,
    ("k8sconnector", "ensure"): k8s.cmd_k8sconnector_ensure,
    ("k8sconnector", "inspect"): k8s.cmd_k8sconnector_inspect,
    ("k8sconnector", "delete"): k8s.cmd_k8sconnector_delete,
    ("container", "inspect"): k8s.cmd_container_inspect,
    ("sensor", "ensure"): sensor.cmd_sensor_ensure,
    ("sensor", "delete"): sensor.cmd_sensor_delete,
    ("sensor", "inspect"): sensor.cmd_sensor_inspect,
    ("serviceaccount", "ensure"): serviceaccount.cmd_serviceaccount_ensure,
    ("serviceaccount", "inspect"): serviceaccount.cmd_serviceaccount_inspect,
    ("serviceaccount", "delete"): serviceaccount.cmd_serviceaccount_delete,
    ("code-scan", "inspect"): codescan.cmd_codescan_inspect,
    ("policy", "ensure"): codescan.cmd_policy_ensure,
    ("policy", "inspect"): codescan.cmd_policy_inspect,
    ("policy", "delete"): codescan.cmd_policy_delete,
    ("detection", "inspect"): sensor.cmd_detection_inspect,
    ("workflow", "inspect"): workflow.cmd_workflow_inspect,
    ("workflow", "ensure"): workflow.cmd_workflow_ensure,
    ("workflow-run", "inspect"): workflow.cmd_workflowrun_inspect,
    ("workflow-run", "ensure"): workflow.cmd_workflowrun_ensure,
    ("outpost", "inspect"): outpost.cmd_outpost_inspect,
    ("outpost", "ensure"): outpost.cmd_outpost_ensure,
    ("outpost", "delete"): outpost.cmd_outpost_delete,
    ("role", "inspect"): role.cmd_role_inspect,
    ("role", "ensure"): role.cmd_role_ensure,
    ("user", "ensure"): user.cmd_user_ensure,
    ("user", "inspect"): user.cmd_user_inspect,
    ("user", "delete"): user.cmd_user_delete,
    ("user", "login-url"): user.cmd_user_login_url,
    ("wiz", "tenant"): wiz.cmd_wiz_tenant,
    ("user", "reap"): reap.cmd_reap,
}


# Every flag a verb reads, as the spec of its parser: bare `{}` is a string defaulting to None; the
# rest carry the constraint the handler used to re-check by hand. main() refuses anything else with
# exit 2: a misspelled flag used to be ignored, so a check graded on the default it meant to override.
def _one_of(*choices):
    """A value from a fixed set; the first is the default."""
    return {"choices": choices, "default": choices[0]}


def _int(default=None):
    return {"type": int, "default": default}


_SWITCH = {"action": "store_true"}


_S = {"--session": {}}


_U = {**_S, "--domain": {"default": "titra-labs.ai"}}


# aws first so every pre-GCP lab keeps working unchanged. The default is also the trap it replaced:
# find_connector(cloud="aws") silently returned nothing for a live CONNECTED GCP project, i.e. a check
# that graded "learner did nothing" when the learner was done.
_CLOUD = {"--cloud": _one_of(*core.CLOUDS)}


FLAGS = {
    ("session", "verify"): {"--account-id": {}, "--cloud": {"choices": core.CLOUDS}, "--min-runner": {}},
    ("connector", "inspect"): {**_S, **_CLOUD, "--account-id": {}, "--outpost-id": {}, "--outpost-name": {},
                               "--require": _one_of("exists", "healthy", "outpost-bound")},
    ("connector", "ensure"): {**_S, **_CLOUD, "--account-id": {}, "--outpost-id": {}, "--outpost-name": {},
                              "--reauth": _SWITCH, "--role-arn": {}, "--scanner-role-arn": {}, "--tenant-id": {}},
    ("connector", "delete"): {**_S, **_CLOUD, "--account-id": {}},
    ("instance", "inspect"): {"--account-id": {}, "--type": {"default": "VIRTUAL_MACHINE"}},
    # --cluster is the EKS name (default: the session stem), resolved to its ARN by describe-cluster;
    # --cluster-arn skips the CLI. The ARN is the connector's external id.
    ("k8sconnector", "ensure"): {**_S, "--cluster": {}, "--cluster-arn": {}, "--enabled": _one_of("true", "false"),
                                 "--name": {}},
    ("k8sconnector", "inspect"): {**_S, "--cluster": {}, "--cluster-arn": {},
                                  "--require": _one_of("exists", "connected", "disabled")},
    ("k8sconnector", "delete"): {**_S, "--cluster": {}, "--cluster-arn": {}},
    ("container", "inspect"): {"--account-id": {}, "--image-contains": {}, "--require-image": _SWITCH},
    ("sensor", "ensure"): {**_S, "--name": {}},
    ("sensor", "inspect"): {**_S, "--name": {}, "--require": _one_of("exists", "active")},
    ("sensor", "delete"): {**_S, "--id": {}, "--name": {}},
    ("serviceaccount", "ensure"): {**_S, "--name": {}},
    ("serviceaccount", "inspect"): {**_S, "--name": {}, "--require": _one_of("exists")},
    ("serviceaccount", "delete"): {**_S, "--id": {}, "--name": {}},
    ("code-scan", "inspect"): {**_S, "--interval": _int(10), "--require": _one_of("published", "pass"),
                               "--tag-key": {"default": "session"}, "--tag-value": {}, "--timeout": _int(180)},
    ("policy", "ensure"): {"--control-search": {"default": "Last User Is"}, "--count-threshold": _int(),
                           "--name": {}, "--rule-id": {}, "--severity": {}},
    ("policy", "inspect"): {"--name": {}, "--require": _one_of("exists")},
    ("policy", "delete"): {"--name": {}},
    ("detection", "inspect"): {**_S, "--match-only": _SWITCH, "--name": {}, "--rule-name": {},
                               "--since-minutes": _int(120)},
    ("workflow", "inspect"): {**_S, "--exact-name": _SWITCH, "--name": {}, "--require": _one_of("exists", "published")},
    ("workflow", "ensure"): {**_S, "--definition": {}, "--dry-run": _SWITCH, "--exact-name": _SWITCH, "--name": {},
                             "--project-id": {}},
    ("workflow-run", "inspect"): {**_S, "--branch": {}, "--exact-name": _SWITCH, "--name": {},
                                  "--require": _one_of("completed", "branch", "error-path"),
                                  "--run-type": {"default": "TEST"}},
    ("workflow-run", "ensure"): {**_S, "--data": {}, "--exact-name": _SWITCH, "--initial-step": {},
                                 "--interval": {"type": float, "default": 2.0}, "--name": {}, "--timeout": _int(60),
                                 "--trigger-type": {"default": "EVENT"}},
    ("outpost", "inspect"): {**_S, "--lookback-days": _int(2), "--name": {},
                             "--require": _one_of("exists", "initialized", "connected", "scanned")},
    ("outpost", "ensure"): {**_S, "--name": {}, "--region": {}, "--role-arn": {}},
    ("outpost", "delete"): {**_S, "--id": {}, "--name": {}, "--timeout": _int(600)},
    ("role", "inspect"): {**_CLOUD, "--account-id": {}, "--role-name": {}, "--trusts-service": {}},
    ("role", "ensure"): {**_CLOUD, "--external-id": {}, "--role-name": {}},
    ("user", "ensure"): {**_U, "--group": {"default": "global-contributor"}},
    ("user", "inspect"): {**_U, "--group": {"default": "global-contributor"}},
    ("user", "delete"): _U,
    ("user", "login-url"): {},
    ("wiz", "tenant"): {},
    ("user", "reap"): {**_U, "--commit": _SWITCH, "--email": {}, "--last-min": _int(1440)},
}


class _Parser(argparse.ArgumentParser):
    def __init__(self, verb):
        super().__init__(prog=f"wizlab {' '.join(verb)}", add_help=False, allow_abbrev=False)
        self.verb = verb
        for flag, spec in FLAGS[verb].items():
            self.add_argument(flag, **spec)

    def error(self, message):
        # argparse would print its own usage and exit; the contract is one `wizlab:` line, exit 2.
        core.die(2, f"{message} for `{self.prog}`; known: {' '.join(sorted(FLAGS[self.verb])) or '(none)'}")


def parse(verb, argv):
    """The verb's flags as attributes: `--tag-key` reads as `args.tag_key`, a switch as True/False. An
    undeclared flag, a missing value or one outside its set is invocation error 2, never ignored."""
    return _Parser(verb).parse_args(argv)


def _run(argv):
    """Exit code for one invocation. One structural guard: any uncaught exception is a bug/invocation
    error (2), never a raw traceback exiting 1 (which a learner check would read as "you're wrong")."""
    try:
        if len(argv) < 2 or (argv[0], argv[1]) not in VERBS:
            core.die(2, f"usage: wizlab [--check] {{{' | '.join(' '.join(k) for k in VERBS)}}} [flags]")
        verb = (argv[0], argv[1])
        VERBS[verb](parse(verb, argv[2:]))
    except core.WizlabError as e:
        return core._fail(e)
    except SystemExit as e:
        return e.code or 0
    except Exception as e:
        return core._fail(core.WizlabError(2, f"internal error: {type(e).__name__}: {e}"))
    return 0


def main():
    argv = sys.argv[1:]
    if argv[:1] == ["--check"]:
        # The learner-check wrapper: anything but 0 is 1 (an out-of-list code puts the session in a
        # terminal `validating_error`), with the real code on stderr for the setup log.
        code = _run(argv[1:])
        if code:
            print(f"wizlab: exit {code}", file=sys.stderr)
            sys.exit(1)
        return
    code = _run(argv)
    if code:
        sys.exit(code)
