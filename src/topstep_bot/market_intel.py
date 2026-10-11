"""Market intelligence: what the bot knows about the market it trades, right now.

Built from the bars the bot already receives (no extra data subscription):
  * yesterday's regular-session high / low / close, today's overnight high / low and opening gap
  * the average daily range (ATR of the last 14 sessions) in points and in dollars per contract,
    and how much of it today has used up
  * where price sits relative to those levels
together with the instrument profile (market_knowledge.py), today's relevant calendar events and
recent headlines (news.py). This is the "market brief" on the dashboard's Market tab and in
Telegram /brief.

Every finished session is also appended to ``market_journal.jsonl`` (range, gap, events, the
bot's own result), so a record of market conditions builds up next to the trade journal.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from typing import TYPE_CHECKING

from topstep_bot.instruments import SPECS
from topstep_bot.market_knowledge import product_for

if TYPE_CHECKING:
    from topstep_bot.models import Bar, Contract
    from topstep_bot.news import HeadlineFeed, NewsCalendar
    from topstep_bot.sessions import SessionSchedule

log = logging.getLogger(__name__)
ATR_DAYS = 14


@dataclass
class Session:
    day: date
    high: float | None = None  # regular trading hours
    low: float | None = None
    open: float | None = None
    close: float | None = None
    on_high: float | None = None  # overnight (before the regular open)
    on_low: float | None = None
    last: float | None = None  # last price of any bar this trading day

    @property
    def range(self) -> float | None:
        return self.high - self.low if self.high is not None and self.low is not None else None


@dataclass
class DayRecord:
    """One line of the market journal."""

    day: str
    symbol: str
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    range: float | None
    gap: float | None
    atr: float | None
    overnight_high: float | None
    overnight_low: float | None
    events: list[str] = field(default_factory=list)
    bot_pnl: float | None = None
    bot_trades: int | None = None


def _parse(t: str) -> time:
    hh, mm = t.split(":")
    return time(int(hh), int(mm))


class MarketTracker:
    def __init__(self, symbol: str, contract: Contract, schedule: SessionSchedule, journal_path: Path | None = None):
        self.symbol = symbol.upper()
        self.contract = contract
        self.schedule = schedule
        self.journal_path = journal_path
        spec = SPECS.get(self.symbol)
        self.rth_open = _parse(spec.rth_open) if spec else time(8, 30)
        self.rth_close = _parse(spec.rth_close) if spec else time(15, 0)
        self.sessions: list[Session] = []  # oldest first, the last one is today
        self.news: NewsCalendar | None = None
        self.headlines: HeadlineFeed | None = None
        self.result_for_day = None  # callable(day) -> (pnl, trades) from the trade journal, set by the runner
        self.daily: list = []  # research.DailyBar: years of daily history for long-term context (research.py)

    # ---------------------------------------------------------------- feeding

    def update(self, bar: Bar, record: bool = True) -> None:
        day = self.schedule.trading_day(bar.ts)
        if not self.sessions or self.sessions[-1].day != day:
            if self.sessions and record:
                self._write_journal(self.sessions[-1])
            self.sessions.append(Session(day))
            del self.sessions[:-(ATR_DAYS + 3)]
        s = self.sessions[-1]
        s.last = bar.close
        if self.schedule.is_rth(bar.ts, self.rth_open, self.rth_close):
            if s.open is None:
                s.open = bar.open
            s.high = bar.high if s.high is None else max(s.high, bar.high)
            s.low = bar.low if s.low is None else min(s.low, bar.low)
            s.close = bar.close
        elif s.open is None:  # overnight, before today's regular session
            s.on_high = bar.high if s.on_high is None else max(s.on_high, bar.high)
            s.on_low = bar.low if s.on_low is None else min(s.on_low, bar.low)

    # ---------------------------------------------------------------- facts

    @property
    def today(self) -> Session | None:
        return self.sessions[-1] if self.sessions else None

    @property
    def prior(self) -> Session | None:
        """The last finished session that had regular-hours trading."""
        for s in reversed(self.sessions[:-1]):
            if s.close is not None:
                return s
        return None

    def atr(self) -> float | None:
        """Average true range of the last finished sessions (regular hours), in points."""
        done = [s for s in self.sessions[:-1] if s.range is not None]
        if len(done) < 3:
            return None
        trs, prev = [], None
        for s in done[-(ATR_DAYS + 1):]:
            tr = s.range if prev is None else max(s.high, prev.close) - min(s.low, prev.close)
            trs.append(tr)
            prev = s
        trs = trs[-ATR_DAYS:]
        return sum(trs) / len(trs)

    def facts(self) -> dict:
        t, p, atr = self.today, self.prior, self.atr()
        pv = self.contract.point_value
        price = t.last if t else None
        gap = round(t.open - p.close, 4) if t and p and t.open is not None else None
        used = t.range / atr if t and t.range is not None and atr else None
        levels = []
        for name, value in (("Prior high", p and p.high), ("Prior low", p and p.low), ("Prior close", p and p.close),
                            ("Overnight high", t and t.on_high), ("Overnight low", t and t.on_low),
                            ("Today's open", t and t.open), ("Today's high", t and t.high), ("Today's low", t and t.low)):
            if value is not None:
                levels.append({"name": name, "price": value,
                               "distance": round(price - value, 4) if price is not None else None})
        return {
            "price": price,
            "atr_points": round(atr, 2) if atr else None,
            "atr_usd": round(atr * pv, 2) if atr else None,
            "range_today": round(t.range, 2) if t and t.range is not None else None,
            "range_used": round(used, 2) if used is not None else None,
            "gap": gap,
            "gap_atr": round(gap / atr, 2) if gap is not None and atr else None,
            "levels": sorted(levels, key=lambda x: -x["price"]),
            "sessions": len([s for s in self.sessions if s.close is not None]),
            **self._long_term(t, atr),
        }

    def _long_term(self, today: Session | None, atr: float | None) -> dict:
        """Where today's range and the recent normal range sit among years of daily ranges."""
        from topstep_bot.research import range_percentile

        if len(self.daily) < 100:
            return {"history_days": len(self.daily), "range_pct": None, "atr_pct": None}
        first = self.daily[0].day
        range_pct = range_percentile(self.daily, today.range) if today and today.range else None
        atr_pct = range_percentile(self.daily, atr) if atr else None
        return {"history_days": len(self.daily), "history_from": first,
                "range_pct": round(range_pct, 3) if range_pct is not None else None,
                "atr_pct": round(atr_pct, 3) if atr_pct is not None else None}

    def notes(self, facts: dict) -> list[str]:
        """Plain-language observations a trader would make looking at the same numbers."""
        out = []
        if facts["gap_atr"] is not None and abs(facts["gap_atr"]) >= 0.3:
            out.append(f"Opened with a {'gap up' if facts['gap'] > 0 else 'gap down'} of {abs(facts['gap']):g} points "
                       f"({abs(facts['gap_atr']):.0%} of a normal day's range).")
        if facts.get("atr_pct") is not None:
            pct = facts["atr_pct"]
            if pct >= 0.8:
                out.append(f"Recent days have been wilder than {pct:.0%} of the last {facts['history_days'] // 252} years.")
            elif pct <= 0.2:
                out.append(f"Recent days have been quieter than {1 - pct:.0%} of the last {facts['history_days'] // 252} years.")
        if facts["range_used"] is not None:
            if facts["range_used"] >= 1.0:
                out.append(f"Today has already moved {facts['range_used']:.0%} of a normal day's range - extended; "
                           "chasing late moves is riskier.")
            elif facts["range_used"] <= 0.35:
                out.append(f"Quiet so far: only {facts['range_used']:.0%} of a normal day's range.")
        p, price = self.prior, facts["price"]
        if p and price is not None and p.high is not None:
            if price > p.high:
                out.append("Trading above yesterday's high.")
            elif price < p.low:
                out.append("Trading below yesterday's low.")
            else:
                out.append("Inside yesterday's range.")
        return out

    # ---------------------------------------------------------------- the brief

    def brief(self, now: datetime) -> dict:
        tz = self.schedule.tz
        product = product_for(self.symbol)
        facts = self.facts()
        today = self.schedule.trading_day(now)
        events, upcoming = [], []
        if self.news:
            events = [e.to_dict(tz) for e in self.news.on_day(today, tz)]
            upcoming = [e.to_dict(tz) for e in self.news.upcoming(now, hours=72)]
        return {
            "symbol": self.symbol,
            "contract": self.contract.name,
            "point_value": self.contract.point_value,
            "tick": {"size": self.contract.tick_size, "value": self.contract.tick_value},
            "rth": f"{self.rth_open:%H:%M}-{self.rth_close:%H:%M} CT",
            "profile": asdict(product) if product else None,
            "facts": facts,
            "notes": self.notes(facts),
            "events_today": events,
            "events_upcoming": upcoming,
            "news_day": bool(events),
            "headlines": [asdict(h) for h in self.headlines.items] if self.headlines else [],
            "headlines_at": self.headlines.fetched_at.isoformat(timespec="minutes") if self.headlines and
            self.headlines.fetched_at else None,
        }

    def brief_text(self, now: datetime) -> str:
        b = self.brief(now)
        f = b["facts"]
        lines = [f"📈 {b['contract']} - {b['profile']['name'] if b['profile'] else b['symbol']}"]
        if f["price"] is not None:
            lines.append(f"Last {f['price']:g}" + (f" | normal day ±{f['atr_points']:g} pts (${f['atr_usd']:,.0f}/contract)"
                                                     if f["atr_points"] else ""))
        if f["range_today"] is not None and f["range_used"] is not None:
            lines.append(f"Today's range {f['range_today']:g} pts = {f['range_used']:.0%} of normal")
        for lv in f["levels"]:
            lines.append(f"  {lv['name']}: {lv['price']:g}")
        lines += [f"• {n}" for n in b["notes"]]
        if b["events_today"]:
            lines.append("Today's reports (no new trades around them):")
            lines += [f"  {e['local']} CT {e['title']} ({e['impact']}"
                      + (f", forecast {e['forecast']}" if e["forecast"] else "") + ")" for e in b["events_today"]]
        else:
            lines.append("No market-moving reports today.")
        later = [e for e in b["events_upcoming"] if e not in b["events_today"]][:4]
        if later:
            lines.append("Coming up: " + "; ".join(f"{e['local']} {e['title']}" for e in later))
        if b["profile"]:
            lines.append("What moves it: " + "; ".join(b["profile"]["drivers"][:3]))
        if b["headlines"]:
            lines.append("Headlines:")
            lines += [f"  - {h['title']}" for h in b["headlines"][:5]]
        lines.append("Headlines are for your information - the bot doesn't trade on them.")
        return "\n".join(lines)

    # ---------------------------------------------------------------- journal

    def _write_journal(self, s: Session) -> None:
        if self.journal_path is None or s.close is None:
            return
        prior = next((x for x in reversed(self.sessions[:-1]) if x is not s and x.close is not None and x.day < s.day), None)
        pnl = trades = None
        if self.result_for_day:
            try:
                pnl, trades = self.result_for_day(s.day)
            except Exception:  # noqa: BLE001 - the journal line is a nice-to-have
                pnl = trades = None
        events = [e.title for e in self.news.on_day(s.day, self.schedule.tz)] if self.news else []
        rec = DayRecord(s.day.isoformat(), self.symbol, s.open, s.high, s.low, s.close, s.range,
                        round(s.open - prior.close, 4) if prior and s.open is not None else None,
                        round(self.atr(), 2) if self.atr() else None, s.on_high, s.on_low, events, pnl, trades)
        try:
            self.journal_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.journal_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(asdict(rec)) + "\n")
        except OSError as exc:
            log.debug("market journal write failed: %s", exc)
