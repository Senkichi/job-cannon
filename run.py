"""Flask entry point for job-finder web application."""

from job_finder.config import load_config, DEFAULT_SERVER_HOST, DEFAULT_SERVER_PORT, DEFAULT_SERVER_DEBUG
from job_finder.web import create_app

cfg = load_config()
app = create_app()

if __name__ == "__main__":
    server = cfg.get("server", {})
    app.run(
        host=server.get("host", DEFAULT_SERVER_HOST),
        port=server.get("port", DEFAULT_SERVER_PORT),
        debug=server.get("debug", DEFAULT_SERVER_DEBUG),
        use_reloader=False,
    )
