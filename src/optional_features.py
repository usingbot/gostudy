"""Small, dependency-free helpers for optional local services and modules."""

from collections.abc import Callable
from typing import Any


def feature_enabled(config, section: str) -> bool:
    """Return whether an optional feature is explicitly enabled."""
    return config.getboolean(section, "enabled", fallback=False)


def initial_extensions(config) -> list[str]:
    """Build the bot extension list without optional local services by default."""
    extensions = ["utils", "core"]
    if feature_enabled(config, "ANALYTICS"):
        extensions.append("analytics")
    extensions.extend(["modules", "babel", "tracking.voice", "tracking.text"])
    return extensions


def optional_webhook(
    value: str | None,
    factory: Callable[..., Any],
    *,
    session: Any,
    logger: Any,
) -> Any | None:
    """Create an optional webhook without leaking an invalid URL into logs."""
    url = (value or "").strip()
    if not url:
        return None

    try:
        return factory(url, session=session)
    except (TypeError, ValueError):
        logger.warning("Premium gem audit webhook is invalid; audit logging is disabled.")
        return None
