"""Short-lived AWS credentials for a project, from the myinfra STS vendor.

The vendor is a Lambda Function URL with ``AWS_IAM`` auth (myinfra
``platform/sts-vendor``).  The deployer signs a request with its own long-lived
identity, the ``agent-deployer`` IAM user, whose only permission is calling
that URL.  The vendor checks that caller against its allow-list and returns
15-60 minute keys for role ``proj-<aws_project>``.

The temporary keys exist only in memory, for the deploy step.  They are never
put into task output, errors or logs.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Protocol

import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
from botocore.session import get_session

from common import TRANSIENT, RejectedError, TransientError

VENDOR_URL_ENV = "RELAY_STS_VENDOR_URL"
DEFAULT_DURATION = 900
# https://<id>.lambda-url.<region>.on.aws/
URL_REGION = re.compile(r"\.lambda-url\.([a-z0-9-]+)\.on\.aws")
HTTP_OK = 200
HTTP_UNAVAILABLE = 503


@dataclass(frozen=True)
class AwsCredentials:
    """Temporary keys for one project role. ``repr`` hides the secret parts."""

    access_key_id: str = field(repr=False)
    secret_access_key: str = field(repr=False)
    session_token: str = field(repr=False)
    expiration: str

    def env(self) -> dict[str, str]:
        """Environment for a deploy subprocess (aws, terraform, helm ...)."""
        return {
            "AWS_ACCESS_KEY_ID": self.access_key_id,
            "AWS_SECRET_ACCESS_KEY": self.secret_access_key,
            "AWS_SESSION_TOKEN": self.session_token,
        }


class CredentialSource(Protocol):
    """Anything that returns credentials for an ``aws_project``."""

    async def __call__(self, aws_project: str) -> AwsCredentials: ...


def url_region(url: str) -> str | None:
    """Region from a Lambda Function URL, or None if the URL has another form."""
    match = URL_REGION.search(url)
    return match.group(1) if match else None


class StsVendor:
    """Client for the vendor URL, signing with the worker's own AWS identity."""

    def __init__(
        self,
        url: str,
        signer: Credentials | None = None,
        region: str | None = None,
        duration: int = DEFAULT_DURATION,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """``signer`` defaults to the standard AWS chain (AWS_ACCESS_KEY_ID etc.)."""
        self.url = url
        self.signer = signer
        self.region = region or url_region(url) or os.getenv("AWS_REGION") or "ap-northeast-1"
        self.duration = duration
        self.transport = transport

    def _signer(self) -> Credentials:
        if self.signer is not None:
            return self.signer
        found = get_session().get_credentials()
        if found is None:
            raise RejectedError("aws_unavailable: the deployer has no AWS identity to sign with")
        return found

    def _signed_headers(self, body: str) -> dict[str, str]:
        request = AWSRequest(
            "POST", self.url, data=body, headers={"content-type": "application/json"}
        )
        SigV4Auth(self._signer(), "lambda", self.region).add_auth(request)
        return dict(request.headers.items())

    async def __call__(self, aws_project: str) -> AwsCredentials:
        """Ask the vendor for ``aws_project``'s keys.

        Refusals (not allowed, switched off, role missing) fail the task;
        throttling and network errors leave it for redelivery.
        """
        body = json.dumps({"project": aws_project, "duration": self.duration})
        headers = self._signed_headers(body)
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=15) as client:
                response = await client.post(self.url, content=body, headers=headers)
        except httpx.HTTPError as exc:
            raise TransientError(f"sts-vendor unreachable: {type(exc).__name__}") from exc
        if response.status_code == HTTP_OK:
            return parse_reply(response)
        reason = vendor_error(response)
        if response.status_code == HTTP_UNAVAILABLE and "switched off" in reason:
            raise RejectedError("aws_vendor_off: sts-vendor is switched off (make vendor-on)")
        if response.status_code in TRANSIENT:
            raise TransientError(f"sts-vendor returned {response.status_code}")
        raise RejectedError(f"aws_denied: sts-vendor returned {response.status_code}: {reason}")


def vendor_error(response: httpx.Response) -> str:
    """The vendor's ``error`` text (or AWS's ``Message``), trimmed."""
    try:
        data = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(data, dict):
        return str(data.get("error") or data.get("Message") or data.get("message") or "")[:200]
    return ""


def parse_reply(response: httpx.Response) -> AwsCredentials:
    """Turn a 200 reply into ``AwsCredentials``; never echo its content on error."""
    try:
        data = response.json()
        return AwsCredentials(
            access_key_id=str(data["AccessKeyId"]),
            secret_access_key=str(data["SecretAccessKey"]),
            session_token=str(data["SessionToken"]),
            expiration=str(data["Expiration"]),
        )
    except (ValueError, KeyError, TypeError):
        raise RejectedError("aws_denied: sts-vendor reply had no credentials") from None


def vendor_from_env() -> StsVendor | None:
    """Vendor client from ``RELAY_STS_VENDOR_URL``, or None when it is unset."""
    url = os.getenv(VENDOR_URL_ENV, "").strip()
    return StsVendor(url) if url else None


__all__ = ["AwsCredentials", "CredentialSource", "StsVendor", "vendor_from_env"]
