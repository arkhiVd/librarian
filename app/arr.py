"""Radarr / Sonarr v3 client.

Both use the Servarr API with different resource names, so the request shapes mirror
`lidarr.py`. Verify these endpoints against the versions you deploy:

Radarr:
  delete files  ``DELETE /api/v3/movieFile/bulk``  ``{"movieFileIds": [...]}``
  unmonitor     ``PUT /api/v3/movie/editor``       ``{"movieIds": [...], "monitored": false}``

Sonarr:
  delete files  ``DELETE /api/v3/episodeFile/bulk``  ``{"episodeFileIds": [...]}``
  unmonitor     ``PUT /api/v3/episode/monitor``      ``{"episodeIds": [...], "monitored": false}``

Librarian treats a non-success response from any endpoint as a failed plan step.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class MediaFile:
    """One file on disk, as the *arr knows it."""

    id: int
    path: str
    size: int
    parent_id: int  # movieId for Radarr, seriesId for Sonarr
    parent_title: str
    episode_ids: tuple[int, ...] = ()  # Sonarr only; empty for Radarr


class ArrError(RuntimeError):
    pass


class ArrClient:
    """Shared client. ``flavour`` selects the noun set."""

    def __init__(self, base_url: str, api_key: str, flavour: str, timeout: float = 20.0) -> None:
        if flavour not in ("radarr", "sonarr"):
            raise ValueError(f"unknown flavour: {flavour}")
        self.flavour = flavour
        self._client = httpx.Client(
            base_url=f"{base_url.rstrip('/')}/api/v3",
            headers={"X-Api-Key": api_key},
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str, **params: object) -> object:
        response = self._client.get(path, params=params or None)
        if response.status_code >= 400:
            raise ArrError(f"GET {path} -> {response.status_code} {response.text[:200]}")
        return response.json()

    # -- read ----------------------------------------------------------------

    def media_files(self) -> list[MediaFile]:
        """Every file this *arr manages, flattened.

        Radarr's ``/movieFile`` needs a ``movieId``; Sonarr's ``/episodeFile`` needs a
        ``seriesId``. Neither will return "everything", so both iterate their parents —
        the same constraint Lidarr's ``/trackfile`` imposes.
        """
        files: list[MediaFile] = []
        if self.flavour == "radarr":
            for movie in self._get("/movie"):  # type: ignore[union-attr]
                if not movie.get("hasFile"):
                    continue
                for raw in self._get("/movieFile", movieId=movie["id"]):  # type: ignore[union-attr]
                    if raw.get("path"):
                        files.append(
                            MediaFile(
                                id=raw["id"],
                                path=raw["path"],
                                size=raw.get("size", 0),
                                parent_id=movie["id"],
                                parent_title=movie.get("title", ""),
                            )
                        )
        else:
            for series in self._get("/series"):  # type: ignore[union-attr]
                for raw in self._get("/episodeFile", seriesId=series["id"]):  # type: ignore[union-attr]
                    if raw.get("path"):
                        files.append(
                            MediaFile(
                                id=raw["id"],
                                path=raw["path"],
                                size=raw.get("size", 0),
                                parent_id=series["id"],
                                parent_title=series.get("title", ""),
                                episode_ids=tuple(
                                    e["id"]
                                    for e in self._get("/episode", episodeFileId=raw["id"])  # type: ignore[union-attr]
                                ),
                            )
                        )
        return files

    # -- destructive ---------------------------------------------------------

    def delete_files(self, file_ids: list[int]) -> None:
        """Remove files from disk *and* the *arr's database, in one consistent call."""
        if not file_ids:
            return
        if self.flavour == "radarr":
            path, key = "/movieFile/bulk", "movieFileIds"
        else:
            path, key = "/episodeFile/bulk", "episodeFileIds"
        response = self._client.request("DELETE", path, json={key: file_ids})
        if response.status_code >= 400:
            raise ArrError(f"DELETE {path} -> {response.status_code} {response.text[:200]}")

    def unmonitor(self, ids: list[int]) -> None:
        """Stop the *arr re-downloading what was just deleted.

        Radarr unmonitors the movie; Sonarr unmonitors the individual episodes, because
        a series stays monitored for episodes that were never grabbed.
        """
        if not ids:
            return
        if self.flavour == "radarr":
            path, payload = "/movie/editor", {"movieIds": ids, "monitored": False}
        else:
            path, payload = "/episode/monitor", {"episodeIds": ids, "monitored": False}
        response = self._client.put(path, json=payload)
        if response.status_code >= 400:
            raise ArrError(f"PUT {path} -> {response.status_code} {response.text[:200]}")
