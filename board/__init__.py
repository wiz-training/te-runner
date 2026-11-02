"""Action board runtime (`wizlab-board serve | arm | run`)."""
from . import cli

MODULES = (cli,)
for _m in MODULES:
    globals().update({k: v for k, v in vars(_m).items() if not k.startswith("__")})
