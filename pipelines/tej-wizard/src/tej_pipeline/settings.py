"""Typed configuration loaded from config/*.yml.

Single source of truth for every path and add-in parameter used by the pipeline.
Import ``get_settings()`` rather than reading os.environ or the YAML directly.

Unlike the FinMind pipeline there are no credentials here: TEJ authentication
lives inside the Smart Wizard add-in, which holds its own session. Nothing in
this package ever sees a password, so there is no .env to populate.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

# tej-wizard/src/tej_pipeline/settings.py -> tej-wizard/
PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"


@dataclass(frozen=True)
class DatasetConfig:
    """One entry from config/datasets.yml.

    ``layout`` says how the raw directory is organised, which decides both how
    workbooks are discovered and what a workbook's identity is:

    ``ticker``      flat, one file per security, named ``<stock_id>.xlsx``
    ``index-year``  one subdirectory per index, one file per calendar year,
                    named ``<YYYY>.xlsx``

    ``indices`` is only meaningful for ``index-year`` and maps the directory
    name onto the exact identity string TEJ writes into column A -- which is
    also what a seeded row has to carry for the add-in to resolve the query.
    """

    name: str
    tej_table: str
    frequency: str
    raw: str
    processed: str
    partition: str
    defined_name_prefix: str
    sheet_name: str
    layout: str = "ticker"
    indices: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AddinConfig:
    """How to drive the TEJ Smart Wizard Excel add-in."""

    xla_path: Path
    install_dir: Path
    refresh_macro: str
    refresh_sheet_macro: str
    timeout_seconds: int
    pause_seconds: float


@dataclass(frozen=True)
class Settings:
    # --- paths (absolute) ---
    db_root: Path
    raw_dir: Path
    processed_dir: Path
    logs_dir: Path
    state_dir: Path
    temp_dir: Path
    duckdb_path: Path
    trading_dates_path: Path

    # --- datasets & add-in ---
    datasets: dict[str, DatasetConfig]
    addin: AddinConfig

    def dataset(self, name: str) -> DatasetConfig:
        try:
            return self.datasets[name]
        except KeyError:
            known = ", ".join(sorted(self.datasets))
            raise KeyError(f"unknown dataset {name!r}; known: {known}") from None

    def raw_dir_for(self, name: str) -> Path:
        return self.db_root / self.dataset(name).raw

    def processed_dir_for(self, name: str) -> Path:
        return self.db_root / self.dataset(name).processed


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    paths = _load_yaml(CONFIG_DIR / "paths.yml")
    ds_cfg = _load_yaml(CONFIG_DIR / "datasets.yml")

    # TEJ_DB_ROOT wins over the YAML so a test run can point at a scratch tree
    # without editing tracked config.
    db_root = Path(os.environ.get("TEJ_DB_ROOT", paths["db_root"])).resolve()

    datasets = {
        name: DatasetConfig(name=name, **cfg) for name, cfg in ds_cfg["datasets"].items()
    }

    a = ds_cfg["addin"]
    addin = AddinConfig(
        xla_path=Path(a["xla_path"]),
        install_dir=Path(a["install_dir"]),
        refresh_macro=a["refresh_macro"],
        refresh_sheet_macro=a["refresh_sheet_macro"],
        timeout_seconds=int(a["timeout_seconds"]),
        pause_seconds=float(a["pause_seconds"]),
    )

    return Settings(
        db_root=db_root,
        raw_dir=db_root / paths["raw"],
        processed_dir=db_root / paths["processed"],
        logs_dir=db_root / paths["logs"],
        state_dir=db_root / paths["state"],
        temp_dir=db_root / paths["temp"],
        duckdb_path=db_root / paths["duckdb_file"],
        trading_dates_path=Path(paths["trading_dates_file"]).resolve(),
        datasets=datasets,
        addin=addin,
    )
