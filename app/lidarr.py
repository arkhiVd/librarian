"""Lidarr v1 client.

Request shapes follow Lidarr API v1. Verify them against the version you deploy.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TrackFile:
    id: int
    path: str
    size: int
    album_id: int
    artist_id: int
    artist_name: str


class LidarrError(RuntimeError):
    pass


class LidarrClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 20.0) -> None:
        self._base = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=f"{self._base}/api/v1",
            headers={"X-Api-Key": api_key},
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str, **params: object) -> object:
        response = self._client.get(path, params=params or None)
        if response.status_code >= 400:
            raise LidarrError(f"GET {path} -> {response.status_code} {response.text[:200]}")
        return response.json()

    def artists(self) -> list[dict]:
        return self._get("/artist")  # type: ignore[return-value]

    def albums(self, artist_id: int) -> list[dict]:
        return self._get("/album", artistId=artist_id)  # type: ignore[return-value]

    def track_files(
        self, *, artist_id: int | None = None, album_id: int | None = None
    ) -> list[dict]:
        """Trackfiles for one artist or album.

        Lidarr returns 400 when neither parameter is supplied, so this never queries
        "everything" — callers iterate artists instead.
        """
        if artist_id is None and album_id is None:
            raise ValueError("track_files requires artist_id or album_id")
        params: dict[str, object] = {}
        if artist_id is not None:
            params["artistId"] = artist_id
        if album_id is not None:
            params["albumId"] = album_id
        return self._get("/trackfile", **params)  # type: ignore[return-value]

    def all_track_files(self) -> list[TrackFile]:
        """Every trackfile Lidarr knows, flattened, with its owning artist resolved.

        This performs one request per artist because Lidarr rejects an unfiltered
        track-file request. The caller caches the result for one operation.
        """
        files: list[TrackFile] = []
        for artist in self.artists():
            artist_id = artist["id"]
            artist_name = artist.get("artistName", "")
            for raw in self.track_files(artist_id=artist_id):
                path = raw.get("path")
                if not path:
                    continue
                files.append(
                    TrackFile(
                        id=raw["id"],
                        path=path,
                        size=raw.get("size", 0),
                        album_id=raw.get("albumId", 0),
                        artist_id=artist_id,
                        artist_name=artist_name,
                    )
                )
        return files

    # -- destructive ---------------------------------------------------------

    def delete_track_files(self, track_file_ids: list[int]) -> None:
        """``DELETE /trackFile/bulk`` — removes the files from disk *and* Lidarr's DB.

        Whether the file is recycled or unlinked depends on Lidarr's configuration.
        """
        if not track_file_ids:
            return
        response = self._client.request(
            "DELETE", "/trackFile/bulk", json={"trackFileIds": track_file_ids}
        )
        if response.status_code >= 400:
            raise LidarrError(
                f"DELETE /trackFile/bulk -> {response.status_code} {response.text[:200]}"
            )

    def artist(self, artist_id: int) -> dict:
        return self._get(f"/artist/{artist_id}")  # type: ignore[return-value]

    def retire_artist(self, artist_id: int) -> None:
        """Stop Lidarr wanting anything from this artist ever again.

        Deleting every file an artist has is not enough on its own: albums Lidarr has
        never acquired can stay monitored with zero files and be downloaded later.

        This unmonitors every album, then the artist, and sets ``monitorNewItems`` to
        none so a future release does not restart the cycle. It deliberately does **not**
        call ``DELETE /artist/{id}``: see SPEC.md § Artist-level delete.
        """
        album_ids = [a["id"] for a in self.albums(artist_id)]
        self.set_albums_monitored(album_ids, False)
        record = self.artist(artist_id)
        record["monitored"] = False
        record["monitorNewItems"] = "none"
        response = self._client.put(f"/artist/{artist_id}", json=record)
        if response.status_code >= 400:
            raise LidarrError(
                f"PUT /artist/{artist_id} -> {response.status_code} {response.text[:200]}"
            )

    def set_albums_monitored(self, album_ids: list[int], monitored: bool) -> None:
        """``PUT /album/monitor`` — the step that stops soularr re-grabbing a deletion."""
        if not album_ids:
            return
        response = self._client.put(
            "/album/monitor", json={"albumIds": album_ids, "monitored": monitored}
        )
        if response.status_code >= 400:
            raise LidarrError(f"PUT /album/monitor -> {response.status_code} {response.text[:200]}")
