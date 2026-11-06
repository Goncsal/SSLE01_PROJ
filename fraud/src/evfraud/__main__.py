"""Run the fraud service: python -m evfraud"""

from __future__ import annotations

import sys


def main() -> int:
    from evcommon import config, jsonlog, metrics

    from . import __version__
    from .settings import Settings

    try:
        settings = Settings.from_env()
        log = jsonlog.setup_logging("fraud")
    except config.ConfigError as e:
        print(f"fraud: configuration error: {e}", file=sys.stderr)
        return 2

    metrics.init_exposition()          # before the metrics are created
    from .app import create_app        # noqa: PLC0415 (import after init_exposition)
    import uvicorn

    app = create_app(settings)
    from .db import init_db            # noqa: PLC0415
    added = ", ".join(init_db.added)
    log.event("service.start", "fraud starting" + (f", added columns: {added}" if added else ""),
              version=__version__, port=settings.port, bind=settings.bind_host,
              dry_run=settings.dry_run)
    uvicorn.run(app, host=settings.bind_host, port=settings.port, log_config=None)
    log.event("service.stop", "fraud stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
