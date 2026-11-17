"""Okta Workflows Router adapter behind `wizlab user`."""
import os

from .. import core


def _okta_env():
    def need(k):
        return os.getenv(k) or core.die(3, f"{k} not in environment (Okta Workflows secret; not wired?)")
    return need("OKTA_WF_INVOKE_URL"), need("OKTA_WF_CLIENT_TOKEN")


def _okta_call(invoke_url, token, body):
    """POST body to the Okta Workflows Router; returns the parsed response. `inspect` is a read and retries
    once (`attempts=2`, 61s worst-case at `_post`'s 30s timeout + 1s backoff so two `user inspect` calls fit
    inside `canary.sh`'s 300s window); mutations submit once because the router creates the user before
    answering."""
    headers = {"x-api-client-token": token, "Content-Type": "application/json", "Accept": "application/json"}
    attempts = 2 if body.get("action") == "inspect" else 1
    return core._post(invoke_url, body, headers, attempts=attempts)


class OktaAdapter:
    def check_env(self) -> None:
        _okta_env()

    def create(self, login: str, profile: str, password: str, participant_id: str) -> str:
        invoke_url, token = _okta_env()
        res = _okta_call(invoke_url, token, {
            "action": "create",
            "profile": profile,
            "login": login,
            "password": password,
            "participant_id": participant_id,
        })
        # The router appends the tenant's email domain; what it returns as `login` is what the learner
        # signs in with. A router that returns nothing leaves the stem, which is still the reap key.
        return res.get("login") or login

    def inspect(self, login: str) -> bool:
        invoke_url, token = _okta_env()
        res = _okta_call(invoke_url, token, {"action": "inspect", "login": login})
        return bool(res.get("okta_user_id"))
