"""slskd v0 client.

Two API behaviors affect cleanup:

1. ``GET /api/v0/transfers/downloads`` hides completed transfers by default. Librarian
   requests ``?includeRemoved=true`` to include them.
2. ``DELETE /api/v0/transfers/downloads/{username}/{id}`` is idempotent. A 204 response
   does not prove that a matching record existed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Transfer:
    id: str
    username: str
    directory: str
    filename: str
    state: str


class SlskdError(RuntimeError):
    pass


class SlskdClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 20.0) -> None:
        self._client = httpx.Client(
            base_url=f"{base_url.rstrip('/')}/api/v0",
            headers={"X-API-Key": api_key},
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def downloads(self) -> list[Transfer]:
        """Every download record, including the completed ones slskd hides by default."""
        response = self._client.get("/transfers/downloads", params={"includeRemoved": "true"})
        if response.status_code >= 400:
            raise SlskdError(f"GET /transfers/downloads -> {response.status_code}")
        transfers: list[Transfer] = []
        for user in response.json():
            username = user.get("username", "")
            for directory in user.get("directories") or []:
                for entry in directory.get("files") or []:
                    transfers.append(
                        Transfer(
                            id=entry.get("id", ""),
                            username=username,
                            directory=directory.get("directory", ""),
                            filename=entry.get("filename", ""),
                            state=entry.get("state", ""),
                        )
                    )
        return transfers

    def remove(self, username: str, transfer_id: str) -> None:
        """Drop one transfer record. Returns 204 whether or not it existed."""
        response = self._client.delete(f"/transfers/downloads/{username}/{transfer_id}")
        if response.status_code >= 400 and response.status_code != 404:
            raise SlskdError(
                f"DELETE /transfers/downloads/{username}/{transfer_id} -> {response.status_code}"
            )
