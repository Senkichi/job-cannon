"""Eval harness package — runs scoring variants against the gold set,
computes literature-informed metrics, persists run history, writes reports.

Public surface:
    job_finder.eval.metrics — pure metric functions (mae, bias, icc, ...)
    job_finder.eval.harness — orchestration entry point: run(...)
    job_finder.eval.report  — markdown report writer
    python -m job_finder.eval — CLI entry point
"""
