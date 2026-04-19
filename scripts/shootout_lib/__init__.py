"""Helpers for scripts/v3_shootout.py — extracted for testability.

Each module is independently importable and unit-tested. The orchestrator
(scripts/v3_shootout.py) wires them together into the full shootout pipeline
per Phase 33 Plan 2.

Modules:
  baseline          — Anthropic-filtered stratified baseline sampling
  gold_baseline     — Opus 4.6 gold ordinal-rubric generation with budget cap
  candidates        — VRAM reset, determinism probe, per-candidate runner
  metrics           — paired MAE, BCa bootstrap, retry gate, tiebreaker
  non_scoring_sites — homepage_backfill + candidate-vs-Opus agreement
  report            — matrix rendering + recommendation logic
"""
