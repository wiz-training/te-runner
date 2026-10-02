"""Automation Workflows and their test runs."""
import sys
import time

from . import core

# --- Workflows (Automation > Workflows). A learner's artifact is a workflow DEFINITION, so the graded
# signal is either its lifecycle state (`published`) or a run's own `outboundEdge` — never a config
# field. Which steps and which switch cases a scenario wants lives in that lab's definition JSON,
# passed to `ensure`, because a `--require` per shape is a verb per assertion (SPEC.md §What may NOT). ---
WORKFLOWS_Q = """query Workflows($f: AutomationWorkflowFilters, $after: String) {
  automationWorkflows(first: 50, after: $after, filterBy: $f) {
    nodes { id name enabled project { name } steps { id name type } } pageInfo { hasNextPage endCursor }
  }
}"""


WORKFLOW_RUNS_Q = """query WorkflowRuns($f: AutomationWorkflowRunFilters) {
  automationWorkflowRuns(first: 20, filterBy: $f) {
    nodes { id status startedAt steps { status outboundEdge step { name type } } }
  }
}"""


CREATE_WORKFLOW = """mutation CreateWorkflow($input: CreateAutomationWorkflowInput!) {
  createAutomationWorkflow(input: $input) { workflow { id name enabled } }
}"""


DELETE_WORKFLOW = """mutation DeleteWorkflow($input: DeleteAutomationWorkflowInput!) {
  deleteAutomationWorkflow(input: $input) { _stub }
}"""


# The in-place edit the console's Save button sends. UpdateAutomationWorkflowPatchStrict is
# AutomationWorkflowDefinition minus `projectId`, so a definition converts by dropping that one key.
UPDATE_WORKFLOW = """mutation UpdateWorkflow($input: UpdateAutomationWorkflowInput!) {
  updateAutomationWorkflow(input: $input) { workflow { id name enabled } }
}"""


# The pre-flight that turns an opaque create failure into the actual reasons. A definition with
# `enabled: true` and any validation issue is refused as "an enabled workflow cannot have validation
# errors" — naming neither the step nor the expression. This query names both, costs no mutation, and
# runs on any tenant, so a definition is checkable before a lease exists.
VALIDATE_WORKFLOW = """query ValidateWorkflow($workflow: AutomationWorkflowDefinition!) {
  validateAutomationWorkflow(input: { workflow: $workflow }) {
    issues {
      message
      target {
        ... on AutomationWorkflowValidationTargetStep { stepId: id }
        ... on AutomationWorkflowValidationTargetTrigger { triggerId: id }
      }
    }
  }
}"""


RUN_WORKFLOW_TEST = """mutation RunWorkflowTest($input: RunAutomationWorkflowTestInput!) {
  runAutomationWorkflowTest(input: $input) { workflowRun { id status } }
}"""


def _workflow_stem(args):
    return core._named(args)


def _resolve_workflows(name, exact=False):
    """EVERY workflow on the session's stem, enabled first then by name so the order is stable. `search`
    is a case-insensitive substring match, so the stem also matches a suffixed name the guide told the
    learner to type (lab-<sid>-night-watch) — which is wanted: the suffix is not the lesson and the reap
    sweep keys on the same prefix. --exact-name pins it.

    Plural because a learner who attempts twice leaves two, and "the first enabled one in API order"
    grades an arbitrary attempt. Callers that assert state take any match; `ensure` deletes them all."""
    nodes = core._all_nodes(WORKFLOWS_Q, {"f": {"search": name}}, "automationWorkflows")
    hits = [n for n in nodes if (n.get("name") == name if exact else (n.get("name") or "").startswith(name))]
    return sorted(hits, key=lambda n: (not n.get("enabled"), n.get("name") or ""))


def _resolve_workflow(name, exact=False):
    hits = _resolve_workflows(name, exact=exact)
    return hits[0] if hits else None


def cmd_workflow_inspect(args):
    """Assert the session's workflow exists (--require exists) or is live (--require published =
    `enabled`). Create leaves a workflow saved but inactive, so existence alone passes a learner who
    never clicked Publish. `enabled` is the ONLY signal for that: `activeVersion`, `draftVersion` and
    `versions` read null/0 on every workflow of a live tenant, so a version-based assertion fails
    everyone. `steps` distinguishes a bare trigger from a built flow if a lab needs it."""
    name = _workflow_stem(args)
    require = args.require
    node = _resolve_workflow(name, exact=args.exact_name)
    if not node:
        print(f"no workflow named {name}*")
        sys.exit(1)
    print(f"workflow {node['name']} ({node['id']}): enabled={node['enabled']} "
          f"project={(node.get('project') or {}).get('name')} steps={len(node.get('steps') or [])}")
    if require == "exists":
        return
    sys.exit(0 if node["enabled"] else 1)


def _workflow_definition(args):
    """The definition, with the fields wizlab owns injected over the file: `name` on the session stem so
    the reap sweep finds it, and `enabled` true because that IS published here."""
    definition = core._read_json(args.definition, "--definition")
    definition["name"] = _workflow_stem(args)
    project = args.project_id
    if project:
        definition["projectId"] = project
    definition.setdefault("enabled", True)
    definition.setdefault("tags", [])
    return definition


def _validate_definition(definition):
    data, _ = core.api(VALIDATE_WORKFLOW, {"workflow": definition})
    return (data.get("validateAutomationWorkflow") or {}).get("issues") or []


def _issue_line(issue):
    # `target` is a union of step/trigger/workflow; only the first two name anything, and the
    # workflow-level member carries `_stub`, so an unnamed target is the whole definition.
    t = issue.get("target")
    t = t if isinstance(t, dict) else {}
    where = t.get("stepId") or t.get("triggerId") or "workflow"
    return f"{where}: {issue.get('message')}"


def cmd_workflow_ensure(args):
    """Validate, then patch in place, create only when absent (contract: SPEC.md, workflow section).
    Validation issues exit 2: the definition is the caller's bug, not the environment's. The patch keeps
    the workflow id, so test runs an earlier activity graded survive a later activity's solve. No
    version-bearing mutation is sent: every one is refused live. `--dry-run` validates and stops, which
    is the only CEL gate there is — nothing local parses CEL."""
    definition = _workflow_definition(args)
    name = definition["name"]
    issues = _validate_definition(definition)
    for i in issues:
        print(_issue_line(i))
    if issues:
        core.die(2, f"definition has {len(issues)} validation issue(s); not submitted")
    if args.dry_run:
        print("0 validation issues; nothing submitted")
        return
    live = _resolve_workflows(name, exact=args.exact_name)
    for stale in live[1:]:
        core.api(DELETE_WORKFLOW, {"input": {"id": stale["id"]}})
        print(f"deleted duplicate workflow {stale['name']} ({stale['id']})")
    if live:
        patch = {k: v for k, v in definition.items() if k != "projectId"}
        mutation, verb = "updateAutomationWorkflow", "updated"
        data, _ = core.api(UPDATE_WORKFLOW, {"input": {"id": live[0]["id"], "patchStrict": patch}})
    else:
        mutation, verb = "createAutomationWorkflow", "created"
        data, _ = core.api(CREATE_WORKFLOW, {"input": {"workflow": definition}})
    wf = (data.get(mutation) or {}).get("workflow") or {}
    if not wf:
        core.die(3, f"{mutation} returned no workflow")
    print(f"{verb} workflow {wf['name']} ({wf['id']}) enabled={wf.get('enabled')}")
    sys.exit(0 if wf.get("enabled") else 3)


def _run_filter(args, wf_ids):
    # TEST by default: a lab fires test runs, and an AUTOMATIC run from a real event would grade a
    # learner on someone else's Threat. --run-type widens it.
    return {"workflowId": {"equals": wf_ids},
            "type": {"equals": [args.run_type]},
            "status": {"equals": ["COMPLETED"]}}


def cmd_workflowrun_inspect(args):
    """Assert the session has a COMPLETED run (--require completed), that one of those runs left a step
    by a named edge (--require branch --branch <branchName>), or that one of them carried a failed step
    whose error edge was followed (--require error-path). outboundEdge is the run's own signal: a
    SWITCH_CASE case name, a CONDITION's true/false, an unbranched step's main — so this grades the
    routing decision itself rather than the definition's shape. Two hops because
    AutomationWorkflowRunFilters has no name or search key — and the first hop takes EVERY workflow on the
    stem, because a learner who attempts twice leaves two and grading whichever the API returned first
    grades an arbitrary attempt."""
    require, branch = _run_require(args)
    stem = _workflow_stem(args)
    nodes = _resolve_workflows(stem, exact=args.exact_name)
    if not nodes:
        print(f"no workflow named {stem}*; cannot scope runs")
        sys.exit(1)
    data, _ = core.api(WORKFLOW_RUNS_Q, {"f": _run_filter(args, [n["id"] for n in nodes])})
    runs = (data.get("automationWorkflowRuns") or {}).get("nodes") or []
    if not runs:
        print(f"no completed run across {len(nodes)} workflow(s) named {stem}*")
        sys.exit(1)
    steps = [s for r in runs for s in (r.get("steps") or [])]
    print(f"{stem}*: {len(nodes)} workflow(s), {len(runs)} completed run(s), "
          f"edges taken: {[s.get('outboundEdge') for s in steps] or 'none'}")
    sys.exit(0 if _run_satisfies(require, branch, steps) else 1)


def _run_require(args):
    require = args.require
    branch = args.branch if require == "branch" else None
    if require == "branch" and not branch:
        core.die(2, "--require branch needs --branch <branchName>")
    return require, branch


def _run_satisfies(require, branch, steps):
    if require == "completed":
        return True
    if require == "branch":
        return any(s.get("outboundEdge") == branch for s in steps)
    # Every failed step reads outboundEdge "error", edge or no edge; what proves the edge was FOLLOWED is
    # that the step failed inside a run that still COMPLETED (the run filter admits COMPLETED only).
    return any(s.get("status") == "FAILED" and s.get("outboundEdge") == "error" for s in steps)


# AutomationWorkflowRunStatus spells CANCELED; a misspelling here is not a wrong exit code but a
# cancelled run that burns the whole --timeout before it dies.
_RUN_DEAD = ("FAILED", "CANCELED", "SKIPPED")


def _wait_for_run(run_id, timeout, interval):
    """Poll THIS run by id, never "a completed run of this workflow": the workflow's own earlier runs
    already satisfy the latter, so a second call would report success while its own run was in flight.
    Budget from measurements.yaml planes.workflow.test_run, which observes 46-82ms."""
    deadline = time.monotonic() + timeout
    while True:
        data, _ = core.api(WORKFLOW_RUNS_Q, {"f": {"id": {"equals": [run_id]}}})
        nodes = (data.get("automationWorkflowRuns") or {}).get("nodes") or []
        status = nodes[0].get("status") if nodes else None
        if status == "COMPLETED":
            return status
        if status in _RUN_DEAD:
            core.die(3, f"test run {run_id} ended {status}, not COMPLETED")
        if time.monotonic() >= deadline:
            core.die(3, f"test run {run_id} still {status or 'unseen'} after {timeout}s")
        time.sleep(interval)


def _initial_step(args, node):
    """customData.initialSteps is non-optional and step ids are minted per create, so a lab names the
    step and it resolves here."""
    step_name = args.initial_step or core.die(2, "needs --initial-step <step name>")
    step = next((s for s in (node.get("steps") or []) if s.get("name") == step_name), None)
    if not step:
        core.die(2, f"workflow {node['name']} has no step named {step_name!r}; "
               f"has {[s.get('name') for s in (node.get('steps') or [])]}")
    return step_name, step


def cmd_workflowrun_ensure(args):
    """Fire a test run of the session's workflow with the synthetic trigger payload in --data
    <file.json>, then wait for that run to reach COMPLETED."""
    node = _resolve_workflow(_workflow_stem(args), exact=args.exact_name)
    if not node:
        core.die(3, f"no workflow named {_workflow_stem(args)}*; nothing to test-run")
    step_name, step = _initial_step(args, node)
    payload = core._read_json(args.data, "--data")
    data, _ = core.api(RUN_WORKFLOW_TEST, {"input": {
        "workflowId": node["id"], "triggerType": args.trigger_type,
        "customData": {"initialSteps": [step["id"]], "data": payload}}})
    run = (data.get("runAutomationWorkflowTest") or {}).get("workflowRun") or {}
    if not run.get("id"):
        core.die(3, "runAutomationWorkflowTest returned no workflowRun")
    print(f"test run {run['id']} fired on {node['name']} entering at {step_name!r}")
    _wait_for_run(run["id"], args.timeout, args.interval)
    print(f"test run {run['id']} reached COMPLETED")
