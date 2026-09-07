"""Token-bucket unit tests (no infra).

Time is injected rather than slept on, so refill behaviour is tested exactly
instead of approximately.
"""

from app.services.ratelimit import TokenBucketLimiter


def test_allows_up_to_capacity_then_denies():
    limiter = TokenBucketLimiter(rate_per_min=3)
    assert [limiter.allow("k", now=0.0) for _ in range(3)] == [True, True, True]
    assert limiter.allow("k", now=0.0) is False


def test_keys_have_independent_buckets():
    limiter = TokenBucketLimiter(rate_per_min=1)
    assert limiter.allow("user:a", now=0.0) is True
    assert limiter.allow("user:a", now=0.0) is False
    assert limiter.allow("user:b", now=0.0) is True  # different key, own budget


def test_refills_over_time():
    limiter = TokenBucketLimiter(rate_per_min=60)  # 1 token/second
    for _ in range(60):
        limiter.allow("k", now=0.0)
    assert limiter.allow("k", now=0.0) is False
    assert limiter.allow("k", now=1.0) is True  # one second -> one token


def test_refill_is_capped_at_capacity():
    limiter = TokenBucketLimiter(rate_per_min=5)
    limiter.allow("k", now=0.0)
    # An hour later the bucket is full, not overfull: exactly capacity allowed.
    assert [limiter.allow("k", now=3600.0) for _ in range(5)] == [True] * 5
    assert limiter.allow("k", now=3600.0) is False


def test_non_positive_rate_disables_limiting():
    limiter = TokenBucketLimiter(rate_per_min=0)
    assert all(limiter.allow("k", now=0.0) for _ in range(100))


def test_reset_clears_buckets():
    limiter = TokenBucketLimiter(rate_per_min=1)
    limiter.allow("k", now=0.0)
    assert limiter.allow("k", now=0.0) is False
    limiter.reset()
    assert limiter.allow("k", now=0.0) is True
