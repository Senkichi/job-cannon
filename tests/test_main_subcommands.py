"""Regression guards for the ``job-cannon`` CLI subcommand surface (#434).

The ``healthcheck`` subparser must be purely additive: bare ``job-cannon`` (and
every existing top-level flag) must still resolve to the serve path, i.e.
``args.command is None``. Only an explicit ``healthcheck`` subcommand diverges.
"""

from __future__ import annotations

from job_finder.__main__ import _build_parser


def test_bare_invocation_resolves_to_serve():
    """No args -> command is None (the serve dispatch), not a subcommand."""
    args = _build_parser().parse_args([])
    assert getattr(args, "command", None) is None


def test_top_level_flags_still_parse_without_subcommand():
    """--port / --demo / --terminal keep working on the bare (serve) path."""
    parser = _build_parser()

    port_args = parser.parse_args(["--port", "5001"])
    assert port_args.port == 5001
    assert getattr(port_args, "command", None) is None

    demo_args = parser.parse_args(["--demo"])
    assert demo_args.demo is True
    assert getattr(demo_args, "command", None) is None

    term_args = parser.parse_args(["--terminal"])
    assert term_args.terminal is True
    assert getattr(term_args, "command", None) is None


def test_healthcheck_subcommand_parsed_with_defaults():
    args = _build_parser().parse_args(["healthcheck"])
    assert args.command == "healthcheck"
    assert args.heartbeat_max_age_hours == 26.0
    assert args.json is True
    assert args.user_data_dir is None


def test_healthcheck_subcommand_flags():
    args = _build_parser().parse_args(
        ["healthcheck", "--heartbeat-max-age-hours", "5", "--user-data-dir", "/tmp/jc"]
    )
    assert args.command == "healthcheck"
    assert args.heartbeat_max_age_hours == 5.0
    assert args.user_data_dir == "/tmp/jc"


def test_print_example_config_flag_survives_subparser_addition():
    """The pre-existing top-level short-circuit flag still parses on the bare path."""
    args = _build_parser().parse_args(["--print-example-config"])
    assert args.print_example_config is True
    assert getattr(args, "command", None) is None
