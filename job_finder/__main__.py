"""Console-script entry point: ``python -m job_finder`` or ``job-cannon``.

Exposes :func:`main` for the ``[project.scripts]`` entry registered in
``pyproject.toml``. Equivalent to the legacy ``python run.py`` invocation
but resolves ``config.yaml`` via :func:`job_finder.config.resolve_config_path`
so the binary can be launched from any working directory once installed.
"""

from job_finder.config import (
    DEFAULT_SERVER_DEBUG,
    DEFAULT_SERVER_HOST,
    DEFAULT_SERVER_PORT,
    load_config,
)
from job_finder.web import create_app


def main() -> None:
    """Resolve config, build the Flask app, and start the dev server."""
    cfg = load_config(allow_missing=True)
    app = create_app(config=cfg)
    server = cfg.get("server", {})
    app.run(
        host=server.get("host", DEFAULT_SERVER_HOST),
        port=server.get("port", DEFAULT_SERVER_PORT),
        debug=server.get("debug", DEFAULT_SERVER_DEBUG),
        use_reloader=False,
    )


if __name__ == "__main__":
    main()
