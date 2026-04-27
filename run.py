"""Flask entry point for job-finder web application."""

from job_finder.config import (
    DEFAULT_SERVER_DEBUG,
    DEFAULT_SERVER_HOST,
    DEFAULT_SERVER_PORT,
    load_config,
)
from job_finder.web import create_app

cfg = load_config()
app = create_app(config=cfg)

if __name__ == "__main__":
    server = cfg.get("server", {})
    app.run(
        host=server.get("host", DEFAULT_SERVER_HOST),
        port=server.get("port", DEFAULT_SERVER_PORT),
        debug=server.get("debug", DEFAULT_SERVER_DEBUG),
        use_reloader=False,
    )
