"""Settings come from the environment (see .env.example)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    reports_dir: Path
    client_id: str
    redirect_uri: str
    listen_host: str
    api_base: str
    accounts_base: str
    min_interval: float
    max_retry_wait: int

    @property
    def library_path(self) -> Path:
        return self.data_dir / "library.json"

    @property
    def token_path(self) -> Path:
        return self.data_dir / "spotify-token.json"

    @property
    def cache_path(self) -> Path:
        return self.data_dir / "spotify-cache.jsonl"

    @property
    def matches_path(self) -> Path:
        return self.data_dir / "matches.json"

    @property
    def overrides_path(self) -> Path:
        return self.data_dir / "overrides.csv"


def load_settings(env: Mapping[str, str] = os.environ) -> Settings:
    return Settings(
        data_dir=Path(env.get("DATA_DIR", "data")),
        reports_dir=Path(env.get("REPORTS_DIR", "reports")),
        client_id=env.get("SPOTIFY_CLIENT_ID", "").strip(),
        redirect_uri=env.get("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback"),
        # Inside Docker the callback server must listen on all interfaces; the
        # port is published on the host's 127.0.0.1 only (compose.yaml).
        listen_host=env.get("AUTH_LISTEN_HOST", "127.0.0.1"),
        api_base=env.get("SPOTIFY_API_BASE", "https://api.spotify.com/v1"),
        accounts_base=env.get("SPOTIFY_ACCOUNTS_BASE", "https://accounts.spotify.com"),
        min_interval=float(env.get("SPOTIFY_MIN_INTERVAL", "0.35")),
        max_retry_wait=int(env.get("SPOTIFY_MAX_RETRY_WAIT", "120")),
    )
