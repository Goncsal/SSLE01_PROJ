"""Run the correlator: python -m evcorrelator"""

from __future__ import annotations

import sys


def main() -> int:
    from evcommon import config, jsonlog, metrics

    from . import __version__
    from .settings import Settings

    try:
        settings = Settings.from_env()
        log = jsonlog.setup_logging("correlator")
    except config.ConfigError as e:
        print(f"correlator: configuration error: {e}", file=sys.stderr)
        return 2

    metrics.init_exposition()          # before the metrics are created
    import uvicorn

    from .app import create_app        # noqa: PLC0415 (import after init_exposition)

    app = create_app(settings)
    log.event("service.start", "correlator starting", version=__version__, port=settings.port,
              bind=settings.bind_host)
    uvicorn.run(app, host=settings.bind_host, port=settings.port, log_config=None)
    log.event("service.stop", "correlator stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
