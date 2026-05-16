"""structlog configuration for the pipeline.

Two sinks: JSON-line records to ``<output_dir>/run.log`` and a human-readable
console renderer on stderr. Stages bind their name via :func:`bind_stage` so
every log entry from inside a stage carries ``stage=<name>``.

Idempotent: calling :func:`configure_logging` more than once with the same
``output_dir`` reopens the file handle cleanly.
"""

from __future__ import annotations

import logging as _stdlib_logging
import sys
from pathlib import Path

import structlog
from structlog.contextvars import bind_contextvars, clear_contextvars
from structlog.typing import EventDict


def _drop_color_message_key(_: object, __: str, event_dict: EventDict) -> EventDict:
    event_dict.pop("color_message", None)
    return event_dict


def configure_logging(output_dir: Path) -> None:
    """Wire structlog to write JSON to ``output_dir/run.log`` and pretty
    text to stderr. Safe to call repeatedly."""
    output_dir.mkdir(parents=True, exist_ok=True)
    run_log = output_dir / "run.log"

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _drop_color_message_key,
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    root = _stdlib_logging.getLogger()
    # Reset handlers so successive configure_logging() calls don't pile up.
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(_stdlib_logging.INFO)

    json_handler = _stdlib_logging.FileHandler(run_log, mode="w", encoding="utf-8")
    json_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=structlog.processors.JSONRenderer(),
            foreign_pre_chain=shared_processors,
        )
    )
    root.addHandler(json_handler)

    console_handler = _stdlib_logging.StreamHandler(sys.stderr)
    console_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=structlog.dev.ConsoleRenderer(colors=False),
            foreign_pre_chain=shared_processors,
        )
    )
    root.addHandler(console_handler)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)


def bind_stage(stage: str) -> None:
    """Bind ``stage=<name>`` to every subsequent log event in this context."""
    clear_contextvars()
    bind_contextvars(stage=stage)


def clear_stage() -> None:
    clear_contextvars()
