"""2026년 공휴일(대체공휴일 포함)과 '연휴 효율' 기반 연가 날짜 추천.
NOTE: 공휴일 목록은 데모용 하드코딩 — 인사혁신처 공고로 검증 필요."""
from datetime import date, timedelta

HOLIDAYS_2026 = {
    date(2026, 1, 1): "신정", date(2026, 2, 16): "설날 연휴", date(2026, 2, 17): "설날", date(2026, 2, 18): "설날 연휴",
    date(2026, 3, 2): "삼일절 대체공휴일", date(2026, 5, 5): "어린이날", date(2026, 5, 25): "부처님오신날 대체공휴일",
    date(2026, 6, 3): "지방선거", date(2026, 8, 17): "광복절 대체공휴일", date(2026, 9, 24): "추석 연휴",
    date(2026, 9, 25): "추석", date(2026, 9, 26): "추석 연휴", date(2026, 10, 5): "개천절 대체공휴일",
    date(2026, 10, 9): "한글날", date(2026, 12, 25): "성탄절",
}


def is_off(d: date) -> bool:
    return d.weekday() >= 5 or d in HOLIDAYS_2026


def holiday_name(d: date) -> str | None:
    return HOLIDAYS_2026.get(d)


def _block_len(d: date) -> int:
    """d를 쉰다고 가정했을 때 d를 포함해 연속으로 쉬는 날 수."""
    n, x = 1, d - timedelta(days=1)
    while is_off(x):
        n, x = n + 1, x - timedelta(days=1)
    x = d + timedelta(days=1)
    while is_off(x):
        n, x = n + 1, x + timedelta(days=1)
    return n


def suggest_dates(today: date, horizon_days: int = 35, top: int = 3, hire_date: date | None = None) -> list[dict]:
    """연가 1일로 가장 긴 연휴가 되는 평일을 추천. 이른 날짜 우선(동점 시)."""
    cands = []
    for i in range(1, horizon_days + 1):
        d = today + timedelta(days=i)
        if is_off(d) or (hire_date and d < hire_date):
            continue
        cands.append({"date": d, "연휴일수": _block_len(d)})
    cands.sort(key=lambda c: (-c["연휴일수"], c["date"]))
    out = []
    for c in cands[:top]:
        d = c["date"]
        around = [x for x in (d - timedelta(days=1), d + timedelta(days=1)) if holiday_name(x)]
        why = f"연가 1일로 {c['연휴일수']}일 연속 휴무"
        if around:
            why += f" ({holiday_name(around[0])} 연계)"
        out.append({"date": d.isoformat(), "요일": "월화수목금토일"[d.weekday()], "연휴일수": c["연휴일수"], "이유": why})
    return sorted(out, key=lambda c: c["date"])
