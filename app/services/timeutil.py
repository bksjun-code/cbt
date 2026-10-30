"""표시용 시간대. 한국은 DST 가 없으므로 고정 오프셋(UTC+9)을 사용한다(tzdata 의존 없음). DB 는 항상 UTC."""
from datetime import date, datetime, time, timedelta, timezone

KST = timezone(timedelta(hours=9))


def to_kst(dt: datetime | None) -> datetime | None:
    return dt.astimezone(KST) if dt else None


def fmt_kst(dt: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    return to_kst(dt).strftime(fmt) if dt else "-"


def kst_day_start_utc(d: date) -> datetime:
    """KST 기준 날짜의 00:00 을 UTC 로."""
    return datetime.combine(d, time.min, tzinfo=KST).astimezone(timezone.utc)


def parse_date(s: str | None) -> date | None:
    try:
        return date.fromisoformat(s) if s else None
    except ValueError:
        return None
