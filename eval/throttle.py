"""Request pacing for LLM calls.

The Gemini free tier allows 5 generate_content calls per minute. Pacing beats
retrying: a 429 costs a full minute of backoff, a 12-second wait costs 12
seconds. Both the pipeline sweep and the judge share this budget.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


class Throttle:
    def __init__(self, rpm: int) -> None:
        self._interval = 60.0 / rpm if rpm > 0 else 0.0
        self._last = 0.0

    async def wait(self) -> None:
        if self._interval <= 0:
            return
        delay = self._interval - (time.monotonic() - self._last)
        if delay > 0:
            await asyncio.sleep(delay)
        self._last = time.monotonic()


def is_rate_limited(exc: Exception) -> bool:
    text = str(exc)
    return "RESOURCE_EXHAUSTED" in text or "429" in text


async def call_with_retry(
    fn: Callable[[], Awaitable[T]],
    throttle: Throttle,
    *,
    tries: int = 4,
    backoff_s: float = 65.0,
) -> T:
    """Run ``fn`` paced by ``throttle``, backing off on provider rate limits."""
    for attempt in range(tries):
        await throttle.wait()
        try:
            return await fn()
        except Exception as exc:  # noqa: BLE001 - provider errors vary by SDK
            if not is_rate_limited(exc) or attempt == tries - 1:
                raise
            print(f"    rate limited; waiting {backoff_s:.0f}s (attempt {attempt + 1})")
            await asyncio.sleep(backoff_s)
    raise RuntimeError("unreachable")
