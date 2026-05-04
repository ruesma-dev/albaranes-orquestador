# main.py
from __future__ import annotations

from pathlib import Path

import uvicorn

from config.logging_config import configure_logging
from config.settings import Settings
from interface_adapters.api.app import build_app


def main() -> int:
    settings = Settings()
    configure_logging(
        log_dir=Path(settings.log_dir),
        log_level=settings.log_level,
        log_format=settings.log_format,
    )
    app = build_app(settings)
    uvicorn.run(
        app,
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
        # Importante: reload=False y workers=1 porque sv7 mantiene
        # tareas en memoria (BackgroundTasks de FastAPI + asyncio task
        # del FailedWorkflowRetrier). Multiproceso o reload romperían
        # esa asunción.
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
