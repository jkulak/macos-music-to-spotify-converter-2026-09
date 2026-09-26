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

    @property
    def library_path(self) -> Path:
        return self.data_dir / "library.json"


def load_settings(env: Mapping[str, str] = os.environ) -> Settings:
    return Settings(
        data_dir=Path(env.get("DATA_DIR", "data")),
        reports_dir=Path(env.get("REPORTS_DIR", "reports")),
    )
