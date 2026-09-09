"""In-process token-bucket rate limiting.

Short-window burst control, deliberately kept in memory: it is the cheap half
of the defense and needs no round trip. The durable half is the database-backed
quota in ``services/usage.py``.

**Scope caveat:** buckets live in this process. Behind N workers the effective
limit is N times the configured rate, and a restart clears them. Moving to a
shared store (Redis) is the fix if that ever matters; the quota is what
actually bounds spend.
"""

import threading
import time
from dataclasses import dataclass, field

from app.config import settings


@dataclass
class _Bucket:
    tokens: float
    updated_at: float


@dataclass
class TokenBucketLimiter:
    """Refills at ``rate_per_min`` tokens/minute, capped at that same burst."""

    rate_per_min: int
    _buckets: dict[str, _Bucket] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def allow(self, key: str, *, now: float | None = None) -> bool:
        """Consume one token for ``key``; False when the bucket is empty."""
        if self.rate_per_min <= 0:  # 0 or negative disables the limit
            return True
        now = time.monotonic() if now is None else now
        capacity = float(self.rate_per_min)
        per_second = capacity / 60.0

        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                self._buckets[key] = _Bucket(tokens=capacity - 1.0, updated_at=now)
                return True

            elapsed = max(0.0, now - bucket.updated_at)
            bucket.tokens = min(capacity, bucket.tokens + elapsed * per_second)
            bucket.updated_at = now

            if bucket.tokens < 1.0:
                return False
            bucket.tokens -= 1.0
            return True

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


# Shared limiters. Queries and uploads get separate budgets because their costs
# and abuse profiles differ (per the spec: "업로드와 질의 분리").
query_limiter = TokenBucketLimiter(settings.rate_limit_query_per_min)
upload_limiter = TokenBucketLimiter(settings.rate_limit_upload_per_min)
# 인증 시도는 비용이 아니라 추측을 막는 것이 목적이라 예산이 따로다.
auth_limiter = TokenBucketLimiter(settings.rate_limit_auth_per_min)
