"""Configurable logging for AI Overlay Helper.

Logging is driven by the ``logging:`` block in ``settings.yaml``::

    logging:
      enabled: true        # set false to silence ALL log output
      level: DEBUG         # DEBUG | INFO | WARNING | ERROR | CRITICAL

Modules should grab a logger via :func:`get_logger` at import time, then
log freely. If ``enabled: false``, the configured level is forced above
CRITICAL so nothing is emitted.

Main entry points (startup, hotkey, send, settings save) log at ``INFO``
with a bracketed tag like ``[hotkey] Summon overlay`` — visible at the
default level, silenced when ``enabled: false``.
"""
from __future__ import annotations
import logging
from typing import Optional

_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

DEFAULT_CONFIG = {"enabled": True, "level": "INFO"}

_OFF_LEVEL = logging.CRITICAL + 10  # higher than any real log call
_ROOT_NAME = "aioverlay"
_configured = False


def configure_logging(config: Optional[dict] = None) -> None:
    """Configure the ``aioverlay`` logger from a settings dict.

    Safe to call multiple times — reconfigures level on each call so a
    settings-window save can take effect without a restart.
    """
    global _configured
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    enabled = bool(cfg.get("enabled", True))
    level_name = str(cfg.get("level", "INFO")).upper()
    level = _LEVELS.get(level_name, logging.INFO)

    root = logging.getLogger(_ROOT_NAME)
    root.setLevel(level if enabled else _OFF_LEVEL)
    root.propagate = False

    if not _configured:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                datefmt="%H:%M:%S",
            )
        )
        root.addHandler(handler)
        _configured = True


def get_logger(name: str) -> logging.Logger:
    """Return a child of the ``aioverlay`` logger.

    Pass ``__name__`` from each module — the resulting logger name will
    be something like ``aioverlay.src.ai_client``, so you can see at a
    glance which module emitted each line.
    """
    return logging.getLogger(f"{_ROOT_NAME}.{name}")
