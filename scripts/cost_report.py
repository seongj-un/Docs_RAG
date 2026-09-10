"""Token spend over a window, and a non-zero exit when it is too high.

The alert channel is the exit code. A cron entry or CI step already knows how
to shout about a failing command, so this does not grow its own mail or Slack
integration — one less credential to hold and one less thing to break silently.

    python -m scripts.cost_report              # last 30 days
    python -m scripts.cost_report --days 1     # yesterday and today

**Rates are not built in.** COST_PER_MTOK_IN / COST_PER_MTOK_OUT default to 0,
so an unconfigured install reports tokens and says the rates are unset rather
than printing a number it invented. Providers change prices; a stale constant
compiled into a cost alert is worse than no alert, because it is believed.

Exit codes: 0 under the ceiling (or no ceiling set), 1 over it.
"""

import argparse
import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.config import settings
from app.db import SessionLocal, engine
from app.models import UsageEvent

MILLION = 1_000_000


async def collect(days: int) -> dict:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    async with SessionLocal() as session:
        row = (
            await session.execute(
                select(
                    func.count(),
                    func.count().filter(UsageEvent.cached.is_(True)),
                    func.coalesce(func.sum(UsageEvent.tokens_in), 0),
                    func.coalesce(func.sum(UsageEvent.tokens_out), 0),
                    func.coalesce(func.sum(UsageEvent.pages), 0),
                ).where(
                    UsageEvent.created_at >= since,
                    # 업로드 수락은 비용이 0 이다. 세면 "요청 N건"과
                    # 캐시 적중률이 함께 왜곡된다.
                    UsageEvent.kind != "upload",
                )
            )
        ).one()
        by_user = (
            await session.execute(
                select(
                    UsageEvent.user_id,
                    func.coalesce(func.sum(UsageEvent.tokens_in), 0),
                    func.coalesce(func.sum(UsageEvent.tokens_out), 0),
                )
                .where(UsageEvent.created_at >= since)
                .group_by(UsageEvent.user_id)
                .order_by(
                    (
                        func.coalesce(func.sum(UsageEvent.tokens_in), 0)
                        + func.coalesce(func.sum(UsageEvent.tokens_out), 0)
                    ).desc()
                )
                .limit(5)
            )
        ).all()
    events, cached, tokens_in, tokens_out, pages = row
    return {
        "events": events,
        "cached": cached,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "pages": pages,
        "by_user": by_user,
    }


def spend(tokens_in: int, tokens_out: int) -> float | None:
    """Cost, or None when the rates have not been configured."""
    if not settings.cost_per_mtok_in and not settings.cost_per_mtok_out:
        return None
    return (
        tokens_in / MILLION * settings.cost_per_mtok_in
        + tokens_out / MILLION * settings.cost_per_mtok_out
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args()

    try:
        data = await collect(args.days)
    finally:
        await engine.dispose()

    served = data["events"] - data["cached"]
    print(f"최근 {args.days}일")
    print(f"  요청        {data['events']:,}건 "
          f"(캐시 히트 {data['cached']:,}건 = LLM 호출 없음)")
    print(f"  토큰        입력 {data['tokens_in']:,} / 출력 {data['tokens_out']:,}")
    print(f"  업로드      {data['pages']:,}쪽")

    cost = spend(data["tokens_in"], data["tokens_out"])
    if cost is None:
        print("\n  단가 미설정 — COST_PER_MTOK_IN / COST_PER_MTOK_OUT 을 채우면"
              " 금액까지 계산합니다.")
        print("  (제공자 가격표에서 가져오세요. 여기에 기본값을 넣지 않는 이유는,"
              " 틀린 금액이 없는 금액보다 나쁘기 때문입니다.)")
        return 0

    unit = settings.cost_currency
    print(f"\n  비용        {cost:,.2f} {unit}")
    if served:
        print(f"  요청당      {cost / served:,.4f} {unit} (캐시 히트 제외 {served:,}건 기준)")

    if data["by_user"]:
        print("\n  사용자별 상위")
        for user_id, t_in, t_out in data["by_user"]:
            share = spend(t_in, t_out) or 0.0
            print(f"    {str(user_id)[:8]}  {share:,.2f} {unit}"
                  f"  (입력 {t_in:,} / 출력 {t_out:,})")

    if settings.cost_ceiling and cost > settings.cost_ceiling:
        print(f"\n초과: {cost:,.2f} > 상한 {settings.cost_ceiling:,.2f} {unit}")
        return 1
    if settings.cost_ceiling:
        left = settings.cost_ceiling - cost
        print(f"\n  상한        {settings.cost_ceiling:,.2f} {unit} "
              f"(남은 여유 {left:,.2f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
