"""Per-tenant identity provider behind `wizlab user`."""
import typing

from . import okta


class IdpAdapter(typing.Protocol):
    # No delete method: `user delete` removes the Wiz user record only; the IdP expires its own logins.
    def check_env(self) -> None: ...
    def create(self, login: str, profile: str, password: str, participant_id: str) -> str: ...
    def inspect(self, login: str) -> bool: ...


_OKTA = okta.OktaAdapter()


def for_tenant(key: str) -> IdpAdapter:
    return _OKTA
