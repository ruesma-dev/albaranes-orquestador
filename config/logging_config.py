# config/logging_config.py
from __future__ import annotations

import json
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Literal

LogFormat = Literal["text", "json"]


# ============================================================== #
# Formato TEXTO — pensado para PyCharm/consola humano.
#
# 2026-05-03T08:14:33.485Z INFO  [sv7][wf:9f1e3a8c] state: ...
#
# El prefijo [wf:xxxxxxxx] solo aparece cuando el log lleva el
# atributo extra 'workflow_id'. Ese atributo lo pone el adapter
# WorkflowLogger (ver application/services/workflow_engine.py).
# ============================================================== #
class _TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts = self.formatTime(record, datefmt="%Y-%m-%dT%H:%M:%S")
        ms = int(record.msecs)
        wf = getattr(record, "workflow_id", None)
        wf_short = f"[wf:{str(wf)[:8]}]" if wf else ""
        component = getattr(record, "component", "sv7")
        component_part = f"[{component}]"
        return (
            f"{ts}.{ms:03d}Z {record.levelname:<5} "
            f"{component_part}{wf_short} {record.getMessage()}"
        )

    def formatTime(  # noqa: D401  (override)
        self,
        record: logging.LogRecord,
        datefmt: str | None = None,
    ) -> str:
        # ISO-8601 UTC.
        from datetime import datetime, timezone
        dt = datetime.fromtimestamp(record.created, tz=timezone.utc)
        return dt.strftime(datefmt or "%Y-%m-%dT%H:%M:%S")


# ============================================================== #
# Formato JSON — para Application Insights o cualquier sink
# estructurado. Activable con LOG_FORMAT=json en .env.
# ============================================================== #
class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        from datetime import datetime, timezone
        dt = datetime.fromtimestamp(record.created, tz=timezone.utc)
        payload = {
            "ts": dt.isoformat().replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for attr in ("workflow_id", "component", "step_name", "state"):
            value = getattr(record, attr, None)
            if value is not None:
                payload[attr] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(
    *,
    log_dir: Path,
    log_level: str,
    log_format: LogFormat = "text",
) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)

    if log_format == "json":
        formatter: logging.Formatter = _JsonFormatter()
    else:
        formatter = _TextFormatter()

    root = logging.getLogger()
    root.setLevel(log_level.upper())

    # Limpia handlers previos (uvicorn puede dejar alguno).
    for handler in list(root.handlers):
        root.removeHandler(handler)

    stream_handler = logging.StreamHandler(stream=sys.stdout)
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    file_handler = RotatingFileHandler(
        log_dir / "orchestrator.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # Bajamos el ruido de librerías terceras a WARN salvo errores.
    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
