"""Structured logging for Jarvis Control Policy flow. Shows what the AI has done at each step."""

import logging

JARVIS_LOGGER = logging.getLogger("jarvis.flow")

# If no handler configured, add a simple one so logs appear
if not JARVIS_LOGGER.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("[Jarvis] %(message)s"))
    JARVIS_LOGGER.addHandler(_h)
    JARVIS_LOGGER.setLevel(logging.INFO)


def log_step(step: str, detail: str = "", extra: dict | None = None) -> None:
    """Log a Control Policy step for observability."""
    msg = f"{step} | {detail}" if detail else step
    if extra:
        extras = " | ".join(f"{k}={v}" for k, v in extra.items())
        msg = f"{msg} | {extras}"
    JARVIS_LOGGER.info(msg)
