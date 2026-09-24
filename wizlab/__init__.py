"""wizlab: the TE lab runtime. Owns every Wiz fact; content decides which fact means success.

Exit codes are the contract:
  0 condition satisfied | 1 learner state not satisfied | 2 invocation error | 3 environment error

In learner-facing check scripts, never let 2/3 reach the platform: an exit code outside the declared
success/failure lists lands in a terminal validating_error state that bricks the session. Wrap with
`wizlab --check <noun> <verb> …`, which maps them to 1 and prints the real code on stderr, with the
env-health check as its own leading `check` block. In CI, consume 2/3 directly.

Every name of every module is re-exported here so a test or a shell one-liner reads `wizlab.X`; code
inside the package addresses another module's name as `module.X`, so patching `module.X` reaches
every caller.
"""
from . import (
    cli,
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

MODULES = (core, session, wiz, connector, k8s, sensor, serviceaccount, codescan, workflow, outpost, role, user,
           reap, cli)
for _m in MODULES:
    globals().update({k: v for k, v in vars(_m).items() if not k.startswith("__")})
