"""Typed application settings — replaces nested cfg.get() chains.

Status: SKELETON ONLY in Session 5. No caller has been migrated. The
existing dict-based config flow (job_finder.config.load_config) is
still authoritative; this module is opt-in until Session 8 migrates
callers section by section.

Defaults are imported from job_finder.config so this module and
config.py stay in lock-step. Update DEFAULT_* there, not here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from job_finder.config import (
    DEFAULT_CANDIDATE_SCORE_THRESHOLD,
    DEFAULT_DAILY_BUDGET_USD,
    DEFAULT_DB_PATH,
    DEFAULT_LOOKBACK_DAYS,
    DEFAULT_MAX_RESULTS,
    DEFAULT_MIN_SCORE_THRESHOLD,
    DEFAULT_SERVER_DEBUG,
    DEFAULT_SERVER_HOST,
    DEFAULT_SERVER_PORT,
)

_EMPTY_RAW: Mapping[str, Any] = MappingProxyType({})


@dataclass(frozen=True)
class ServerSettings:
    host: str = DEFAULT_SERVER_HOST
    port: int = DEFAULT_SERVER_PORT
    debug: bool = DEFAULT_SERVER_DEBUG


@dataclass(frozen=True)
class ScoringSettings:
    candidate_score_threshold: int = DEFAULT_CANDIDATE_SCORE_THRESHOLD
    daily_budget_usd: float = DEFAULT_DAILY_BUDGET_USD
    min_score_threshold: int = DEFAULT_MIN_SCORE_THRESHOLD


@dataclass(frozen=True)
class IngestionSettings:
    lookback_days: int = DEFAULT_LOOKBACK_DAYS
    max_results: int = DEFAULT_MAX_RESULTS


@dataclass(frozen=True)
class Settings:
    """Top-level typed view of config.yaml.

    The `raw` field is a read-only Mapping proxy over the original
    config dict, exposed as an escape hatch for fields not yet typed.
    Callers should prefer the typed accessors (server / scoring /
    ingestion / db_path); `raw` is for migration intermediates only
    and should shrink as Session 8 progresses.
    """

    server: ServerSettings = field(default_factory=ServerSettings)
    scoring: ScoringSettings = field(default_factory=ScoringSettings)
    ingestion: IngestionSettings = field(default_factory=IngestionSettings)
    db_path: str = DEFAULT_DB_PATH
    raw: Mapping[str, Any] = field(default_factory=lambda: _EMPTY_RAW)

    @classmethod
    def load_from_yaml(cls, config_path: str) -> Settings:
        """Load settings from YAML file."""
        from job_finder.config import load_config

        cfg = load_config(config_path)
        return cls.from_dict(cfg)

    @classmethod
    def from_dict(cls, cfg: dict[str, Any]) -> Settings:
        srv = cfg.get("server") or {}
        scoring = cfg.get("scoring") or {}
        ingestion = cfg.get("ingestion") or {}
        db = cfg.get("db") or {}
        return cls(
            server=ServerSettings(
                host=srv.get("host", DEFAULT_SERVER_HOST),
                port=int(srv.get("port", DEFAULT_SERVER_PORT)),
                debug=bool(srv.get("debug", DEFAULT_SERVER_DEBUG)),
            ),
            scoring=ScoringSettings(
                candidate_score_threshold=int(
                    scoring.get("candidate_score_threshold", DEFAULT_CANDIDATE_SCORE_THRESHOLD)
                ),
                daily_budget_usd=float(scoring.get("daily_budget_usd", DEFAULT_DAILY_BUDGET_USD)),
                min_score_threshold=int(
                    scoring.get("min_score_threshold", DEFAULT_MIN_SCORE_THRESHOLD)
                ),
            ),
            ingestion=IngestionSettings(
                lookback_days=int(ingestion.get("lookback_days", DEFAULT_LOOKBACK_DAYS)),
                max_results=int(ingestion.get("max_results", DEFAULT_MAX_RESULTS)),
            ),
            db_path=db.get("path", DEFAULT_DB_PATH),
            raw=MappingProxyType(dict(cfg)),
        )

    def to_dict(self) -> dict[str, Any]:
        """Round-trip back to a config-shaped dict.

        WARNING: for the settings UI write-back path (_write_config),
        use ruamel.yaml round-trip mode instead of this method. This
        helper drops comments and any field not yet typed. The
        ruamel.yaml round-trip migration is Session 8.3.
        """
        return {
            "server": {
                "host": self.server.host,
                "port": self.server.port,
                "debug": self.server.debug,
            },
            "scoring": {
                "candidate_score_threshold": self.scoring.candidate_score_threshold,
                "daily_budget_usd": self.scoring.daily_budget_usd,
                "min_score_threshold": self.scoring.min_score_threshold,
            },
            "ingestion": {
                "lookback_days": self.ingestion.lookback_days,
                "max_results": self.ingestion.max_results,
            },
            "db": {"path": self.db_path},
        }

    def validate(self) -> None:
        """Raise ValueError if any field is out of contract."""
        if self.server.port < 1 or self.server.port > 65535:
            raise ValueError(f"Invalid server.port: {self.server.port}")
        if self.scoring.daily_budget_usd < 0:
            raise ValueError(f"Invalid scoring.daily_budget_usd: {self.scoring.daily_budget_usd}")
        if (
            self.scoring.candidate_score_threshold < 0
            or self.scoring.candidate_score_threshold > 100
        ):
            raise ValueError(
                f"Invalid scoring.candidate_score_threshold: {self.scoring.candidate_score_threshold}"
            )
        if self.scoring.min_score_threshold < 0 or self.scoring.min_score_threshold > 100:
            raise ValueError(
                f"Invalid scoring.min_score_threshold: {self.scoring.min_score_threshold}"
            )
        if self.ingestion.lookback_days < 0:
            raise ValueError(f"Invalid ingestion.lookback_days: {self.ingestion.lookback_days}")
        if self.ingestion.max_results < 1:
            raise ValueError(f"Invalid ingestion.max_results: {self.ingestion.max_results}")
