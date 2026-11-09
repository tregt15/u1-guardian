"""Email through Resend's HTTPS API: POST https://api.resend.com/emails (Bearer key).

Every user brings their own Resend key. Nothing here ships a key, and the key is never
put in a log, an error message, or an API answer.
"""

from __future__ import annotations

import httpx

from guardian import __version__
from guardian.config import EmailConfig

RESEND_URL = "https://api.resend.com/emails"


class NotifyError(Exception):
    """A send failed. `retryable` says whether trying again could help. Never holds the key."""

    def __init__(self, message: str, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class ResendEmailChannel:
    name = "email"

    def __init__(self, cfg: EmailConfig, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.cfg = cfg
        self._transport = transport  # tests pass a fake transport. No real network in tests.

    @property
    def configured(self) -> bool:
        return bool(self.cfg.api_key())

    async def send(self, subject: str, text: str) -> None:
        key = self.cfg.api_key()
        if not key:
            raise NotifyError(
                f"No Resend API key found. Set the {self.cfg.api_key_env} environment variable "
                "(or RESEND_API_KEY) with your own key.", retryable=False)
        subject = " ".join(subject.split())[:200]  # file names come from the printer: no newlines
        payload = {"from": self.cfg.from_addr, "to": self.cfg.to_addrs, "subject": subject, "text": text}
        headers = {"Authorization": f"Bearer {key}", "User-Agent": f"u1-guardian/{__version__}"}
        try:
            async with httpx.AsyncClient(timeout=15.0, transport=self._transport) as client:
                resp = await client.post(RESEND_URL, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            # Only the exception class, never its text (it could contain request details).
            raise NotifyError(f"could not reach Resend ({type(exc).__name__})", retryable=True) from None
        if resp.status_code < 300:
            return
        detail = ""
        try:
            body = resp.json()
            detail = str(body.get("message") or body.get("error") or "")[:200]
        except ValueError:
            pass
        retry = resp.status_code in (408, 425, 429) or resp.status_code >= 500
        hint = ""
        if resp.status_code in (401, 403):
            hint = " Check your API key."
        elif resp.status_code == 422:
            hint = " Check the from and to addresses (the test sender only reaches your own Resend account email)."
        detail = detail.replace(key, "***")  # in case the server echoes the key back
        raise NotifyError(f"Resend answered {resp.status_code}: {detail}{hint}".strip(), retryable=retry)
