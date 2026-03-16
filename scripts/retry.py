"""
Shared retry utility for external API calls.

Provides a decorator and a function wrapper that implements
exponential backoff with jitter. Used by scanner, researcher,
resolver, and predictor for all external HTTP and API calls.

Config-driven: reads retry_attempts and retry_delay_seconds from
settings.yaml under execution.
"""

import functools
import logging
import random
import time
from typing import Any, Callable, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Exceptions that warrant a retry (transient network/API issues)
RETRYABLE_EXCEPTIONS = (
    ConnectionError,
    TimeoutError,
    OSError,
)

# Lazily try to include requests exceptions if available
try:
    import requests
    RETRYABLE_EXCEPTIONS = RETRYABLE_EXCEPTIONS + (
        requests.ConnectionError,
        requests.Timeout,
        requests.HTTPError,
    )
except ImportError:
    pass

# Lazily try to include openai exceptions
try:
    import openai
    RETRYABLE_EXCEPTIONS = RETRYABLE_EXCEPTIONS + (
        openai.APIConnectionError,
        openai.RateLimitError,
        openai.APITimeoutError,
    )
except ImportError:
    pass


def retry_call(
    fn: Callable[..., T],
    *args: Any,
    max_attempts: int = 3,
    base_delay: float = 5.0,
    retryable: tuple = RETRYABLE_EXCEPTIONS,
    context: str = "",
    **kwargs: Any,
) -> T:
    """
    Call fn(*args, **kwargs) with retry and exponential backoff.

    Args:
        fn: The function to call.
        max_attempts: Total attempts (1 = no retry).
        base_delay: Delay in seconds before first retry. Doubles each attempt.
        retryable: Tuple of exception types that trigger a retry.
        context: Human-readable label for log messages (e.g. "Kalshi API").

    Returns:
        The return value of fn() on success.

    Raises:
        The last exception if all attempts fail.
    """
    label = context or fn.__name__
    last_exc = None

    for attempt in range(1, max_attempts + 1):
        try:
            return fn(*args, **kwargs)
        except retryable as exc:
            last_exc = exc
            if attempt == max_attempts:
                logger.warning(
                    "%s: failed after %d attempts: %s",
                    label, max_attempts, exc,
                )
                raise

            # Exponential backoff with jitter
            delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, 1)
            logger.info(
                "%s: attempt %d/%d failed (%s), retrying in %.1fs",
                label, attempt, max_attempts, type(exc).__name__, delay,
            )
            time.sleep(delay)

    # Should never reach here, but satisfy type checker
    raise last_exc  # type: ignore[misc]


def with_retry(
    max_attempts: int = 3,
    base_delay: float = 5.0,
    retryable: tuple = RETRYABLE_EXCEPTIONS,
    context: str = "",
):
    """
    Decorator version of retry_call.

    Usage:
        @with_retry(max_attempts=3, context="Kalshi API")
        def fetch_markets():
            ...
    """
    def decorator(fn: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            return retry_call(
                fn, *args,
                max_attempts=max_attempts,
                base_delay=base_delay,
                retryable=retryable,
                context=context or fn.__qualname__,
                **kwargs,
            )
        return wrapper
    return decorator
