"""Typed configuration loaded from .env + config/*.yml.

Single source of truth for every path and API parameter used by the pipeline.
Import ``get_settings()`` rather than reading os.environ or the YAML directly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values

# finmind/src/finmind_pipeline/settings.py -> finmind/
PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"
ENV_FILE = PROJECT_ROOT / ".env"


@dataclass(frozen=True)
class DatasetConfig:
    """One entry from config/datasets.yml."""

    name: str
    finmind_dataset: str
    fetch_modes: tuple[str, ...]
    partition: str
    filter_to_universe: bool = False
    raw: str | None = None
    raw_market: str | None = None
    raw_ticker: str | None = None
    processed: str | None = None
    # Overrides the global history_start. Only the discovery sweep needs this:
    # whole-market history begins at 2004-02-11, well after the 1994-10-01
    # floor that per-ticker calls reach for still-listed securities.
    history_start: str | None = None
    # Which fetch unit wins when both hold the same (stock_id, date). Only the
    # price datasets read it; see processing/prices.py for why the adjusted
    # series cannot share the unadjusted one's answer.
    dedupe_prefer: str = "market"


@dataclass(frozen=True)
class Settings:
    # --- credentials ---
    token: str
    api_hour_limit: int
    subscription_plan: str

    # --- paths (absolute) ---
    db_root: Path
    raw_dir: Path
    processed_dir: Path
    logs_dir: Path
    state_dir: Path
    temp_dir: Path
    duckdb_path: Path
    rate_limit_state: Path
    backfill_checkpoint: Path

    # --- api ---
    base_url: str
    user_info_url: str
    timeout_seconds: int
    rate_budget_ratio: float
    max_retries: int
    quota_backoff_seconds: int
    quota_retries: int

    # --- datasets ---
    history_start: str
    datasets: dict[str, DatasetConfig]

    # --- industry normalisation ---
    industry_aliases: dict[str, str] = field(default_factory=dict)
    index_categories: frozenset[str] = frozenset()

    # ------------------------------------------------------------------
    @property
    def rate_budget_per_hour(self) -> int:
        """Requests per hour we allow ourselves, leaving headroom under the cap."""
        return int(self.api_hour_limit * self.rate_budget_ratio)

    def dataset(self, name: str) -> DatasetConfig:
        try:
            return self.datasets[name]
        except KeyError:
            raise KeyError(f"unknown dataset {name!r}; known: {sorted(self.datasets)}") from None

    def path(self, relative: str) -> Path:
        """Resolve a db_root-relative path from the config files."""
        return self.db_root / relative

    def normalise_industry(self, category: str | None) -> str | None:
        if category is None:
            return None
        return self.industry_aliases.get(category, category)

    def is_index_category(self, category: str | None) -> bool:
        return category in self.index_categories


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"missing config file: {path}")
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load and validate settings. Cached — call freely."""
    # dotenv_values strips the literal quotes around e.g. FINMIND_SUBSCRIPTION_PLAN.
    env: dict[str, str | None] = {}
    if ENV_FILE.exists():
        env.update(dotenv_values(ENV_FILE))
    # real environment variables win over the .env file
    env.update({k: v for k, v in os.environ.items() if k.startswith("FINMIND_")})

    token = (env.get("FINMIND_TOKEN") or "").strip()
    if not token:
        raise RuntimeError(
            f"FINMIND_TOKEN is not set. Copy {PROJECT_ROOT / '.env.example'} to .env "
            "and fill in your token."
        )

    try:
        api_hour_limit = int(str(env.get("FINMIND_API_HOUR_LIMIT") or 600).strip())
    except ValueError:
        raise RuntimeError("FINMIND_API_HOUR_LIMIT must be an integer") from None

    paths_cfg = _read_yaml(CONFIG_DIR / "paths.yml")
    ds_cfg = _read_yaml(CONFIG_DIR / "datasets.yml")
    alias_cfg = _read_yaml(CONFIG_DIR / "industry_alias.yml")

    db_root = Path(env.get("FINMIND_DB_ROOT") or paths_cfg["db_root"]).resolve()

    api = ds_cfg.get("api", {})
    datasets = {}
    for name, spec in (ds_cfg.get("datasets") or {}).items():
        spec = dict(spec)
        spec["fetch_modes"] = tuple(spec.get("fetch_modes") or ())
        datasets[name] = DatasetConfig(name=name, **spec)

    return Settings(
        token=token,
        api_hour_limit=api_hour_limit,
        subscription_plan=str(env.get("FINMIND_SUBSCRIPTION_PLAN") or "unknown").strip(),
        db_root=db_root,
        raw_dir=db_root / paths_cfg["raw"],
        processed_dir=db_root / paths_cfg["processed"],
        logs_dir=db_root / paths_cfg["logs"],
        state_dir=db_root / paths_cfg["state"],
        temp_dir=db_root / paths_cfg["temp"],
        duckdb_path=db_root / paths_cfg["duckdb_file"],
        rate_limit_state=db_root / paths_cfg["rate_limit_state"],
        backfill_checkpoint=db_root / paths_cfg["backfill_checkpoint"],
        base_url=api["base_url"],
        user_info_url=api["user_info_url"],
        timeout_seconds=int(api.get("timeout_seconds", 300)),
        rate_budget_ratio=float(api.get("rate_budget_ratio", 0.9)),
        max_retries=int(api.get("max_retries", 5)),
        quota_backoff_seconds=int(api.get("quota_backoff_seconds", 300)),
        quota_retries=int(api.get("quota_retries", 8)),
        history_start=str(ds_cfg["history_start"]),
        datasets=datasets,
        industry_aliases=dict(alias_cfg.get("aliases") or {}),
        index_categories=frozenset(alias_cfg.get("index_categories") or ()),
    )
