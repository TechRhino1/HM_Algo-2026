from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional, Tuple, ClassVar
import zoneinfo

try:
    IST_TZ = zoneinfo.ZoneInfo("Asia/Kolkata")
except Exception:
    IST_TZ = timezone(timedelta(hours=5, minutes=30))

from jarvis.data.schemas import SessionContext

class SessionEngine:
    """Calculates active trading sessions, prime volume hours, killzones, and global market open/closed status."""
    
    # Killzone definitions (UTC hours) — institutional high-probability entry windows
    KILLZONES: ClassVar[Dict[str, Tuple[int, int]]] = {
        "LONDON_OPEN":  (7, 10),   # 07:00-10:00 UTC — first directional move of the day
        "NY_OPEN":      (12, 15),  # 12:00-15:00 UTC — highest volume, news reactions
        "LONDON_CLOSE": (15, 17),  # 15:00-17:00 UTC — mean reversion / position unwinding
    }
    ASIAN_RANGE = (0, 7)  # 00:00-07:00 UTC — defines the daily range box

    # Marks a 24/7 instrument. `jarvis.data.tradingview_provider._CRYPTO_SYMBOLS`
    # serves a longer list (XRP, ADA, DOGE, AVAX, DOT, BNB...); those were falling
    # through to the 24/5 Forex schedule below, so a crypto bar on a Saturday was
    # reported as a closed market.
    CRYPTO_TAGS = ("BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "AVAX", "DOT", "BNB", "CRYPTO")

    @staticmethod
    def _as_utc(dt: Optional[datetime]) -> datetime:
        """Normalise an input timestamp to UTC.

        Every window in this module is defined in UTC — the field is literally
        named `utc_hour` and the schedule is "closes 21:00 UTC" — so an aware
        datetime arriving in another zone has to be *converted*, not read as-is.
        `market_context` passes bar timestamps straight through when they already
        carry a tzinfo, and MT5-derived timestamps run on the broker's clock, so
        reading `.hour` off those shifts every boundary by the broker offset.
        """
        if dt is None:
            return datetime.now(timezone.utc)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    @staticmethod
    def get_current_session(dt: Optional[datetime] = None) -> SessionContext:
        dt = SessionEngine._as_utc(dt)

        hour = dt.hour
        weekday = dt.weekday()  # 0=Monday, 6=Sunday

        # The market is shut from Friday 21:00 UTC to Sunday 21:00 UTC, so there
        # is no session to name at the weekend. `is_prime_session` already
        # carried that, but `current_session` is also printed into analyst
        # narratives and the copilot context, where "LONDON_NY_OVERLAP" on a
        # Saturday afternoon is simply wrong.
        if weekday >= 5:
            return SessionContext(
                current_session="OFF_HOURS",
                is_prime_session=False,
                utc_hour=hour,
                day_of_week=weekday,
            )

        # Session intervals in UTC, checked most-specific first:
        # Asian:          00:00 - 07:00 UTC (Tokyo/Sydney) — 07:00 is the London open
        # London:         07:00 - 16:00 UTC
        # London/NY Overlap: 12:00 - 16:00 UTC
        # New York:       16:00 - 21:00 UTC — 12:00-16:00 is named as the overlap
        session_name = "OFF_HOURS"
        if 12 <= hour < 16:
            session_name = "LONDON_NY_OVERLAP"
        elif 7 <= hour < 16:
            session_name = "LONDON"
        elif 12 <= hour < 21:
            session_name = "NEW_YORK"
        elif 0 <= hour < 9:
            session_name = "ASIAN"
        elif 21 <= hour <= 23:
            session_name = "SYDNEY_PACIFIC"

        # Prime volume window is typically 07:00 - 20:59 UTC during weekdays (Monday to Friday)
        is_weekday = weekday < 5
        is_prime = is_weekday and (7 <= hour <= 20)

        return SessionContext(
            current_session=session_name,
            is_prime_session=is_prime,
            utc_hour=hour,
            day_of_week=weekday
        )

    @staticmethod
    def get_active_killzone(dt: Optional[datetime] = None) -> Dict[str, Any]:
        """Determine which killzone (if any) is currently active.
        
        Returns dict with:
        - 'active_killzone': str or None — 'LONDON_OPEN', 'NY_OPEN', 'LONDON_CLOSE', or None
        - 'is_in_killzone': bool — True if in any killzone
        - 'is_asian_range': bool — True if in Asian range accumulation window
        - 'killzone_minutes_remaining': int — minutes until current killzone ends

        Weekends return no killzone: these are institutional weekday windows, and
        `is_forex_killzone_active` is used as a hard Forex entry filter while
        `is_in_killzone` is written into the online-ML feature vector — a weekend
        bar would have trained `is_killzone=1` on a closed market.
        """
        dt = SessionEngine._as_utc(dt)

        hour = dt.hour
        active_kz = None
        minutes_remaining = 0

        if dt.weekday() < 5:
            for kz_name, (start_h, end_h) in SessionEngine.KILLZONES.items():
                if start_h <= hour < end_h:
                    active_kz = kz_name
                    minutes_remaining = (end_h - hour) * 60 - dt.minute
                    break

        is_asian = dt.weekday() < 5 and (
            SessionEngine.ASIAN_RANGE[0] <= hour < SessionEngine.ASIAN_RANGE[1]
        )

        return {
            "active_killzone": active_kz,
            "is_in_killzone": active_kz is not None,
            "is_asian_range": is_asian,
            "killzone_minutes_remaining": minutes_remaining
        }

    @staticmethod
    def is_forex_killzone_active(dt: Optional[datetime] = None) -> bool:
        """Quick check: is the current time within a Forex killzone window?
        Used as a hard filter for Forex entries — only trade during London/NY killzones."""
        kz = SessionEngine.get_active_killzone(dt)
        return kz["is_in_killzone"]

    @staticmethod
    def is_index_prime_session(dt: Optional[datetime] = None) -> bool:
        """Determines if the current time falls within US Equity Cash Market core liquidity hours (14:00 to 19:59 UTC).
        Captures institutional morning trend and afternoon continuation while avoiding opening/closing whipsaws."""
        dt = SessionEngine._as_utc(dt)
        return dt.weekday() < 5 and (14 <= dt.hour <= 19)

    @staticmethod
    def get_market_trading_status(symbol: str = "XAUUSD", dt: Optional[datetime] = None) -> Dict[str, Any]:
        """
        Determines exact market operational status, weekend closure, and opening schedules in IST & UTC.
        """
        dt = SessionEngine._as_utc(dt)

        sym_upper = (symbol or "XAUUSD").upper()

        # 1. Crypto assets operate 24/7 continuously
        if any(tag in sym_upper for tag in SessionEngine.CRYPTO_TAGS):
            return {
                "symbol": symbol,
                "is_open": True,
                "market_type": "CRYPTO_24_7",
                "status": "OPEN",
                "status_badge": "🟢 24/7 LIVE MARKET",
                "status_text": "Market is OPEN (Continuous 24/7 Crypto Trading)",
                "next_event": "Continuous 24/7 Trading",
                "next_open_ist": "Always Open",
                "next_open_utc": "",
                "countdown_seconds": 0,
                "countdown_formatted": "Live Now",
                "reason": "Crypto instruments trade continuously, including weekends.",
            }

        # 2. Forex & Spot Metals (Gold / Currencies / Indices)
        # Global market schedule, fixed in UTC so it does not move with anyone's DST:
        # Closes: Friday 21:00 UTC (Saturday 02:30 AM IST)
        # Opens:  Sunday 21:00 UTC (Monday 02:30 AM IST)
        weekday = dt.weekday()  # 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri, 5=Sat, 6=Sun
        hour = dt.hour

        is_weekend_closed = False
        # `minute >= 0` used to sit here; it is always true, so this is `hour >= 21`.
        if weekday == 4 and hour >= 21:
            is_weekend_closed = True
        elif weekday == 5:
            is_weekend_closed = True
        elif weekday == 6 and hour < 21:
            is_weekend_closed = True

        if is_weekend_closed:
            # Calculate next Sunday 21:00 UTC
            days_to_sunday = (6 - weekday) % 7
            if weekday == 6:
                target_date = dt.date()
            else:
                target_date = dt.date() + timedelta(days=days_to_sunday)

            next_open_utc = datetime(target_date.year, target_date.month, target_date.day, 21, 0, 0, tzinfo=timezone.utc)
            diff_sec = max(0, (next_open_utc - dt).total_seconds())

            hours = int(diff_sec // 3600)
            mins = int((diff_sec % 3600) // 60)
            days = hours // 24
            rem_hours = hours % 24

            countdown_str = f"{days}d {rem_hours}h {mins}m" if days > 0 else f"{hours}h {mins}m"
            next_open_ist_dt = next_open_utc.astimezone(IST_TZ)
            next_open_ist_str = next_open_ist_dt.strftime("%a %b %d, %I:%M %p IST")

            return {
                "symbol": symbol,
                "is_open": False,
                "market_type": "FOREX_METALS_24_5",
                "status": "CLOSED_WEEKEND",
                "status_badge": "🔴 MARKET CLOSED (WEEKEND)",
                "status_text": f"Market is CLOSED for the weekend. Re-opens {next_open_ist_str} (in {countdown_str}).",
                "next_event": f"Re-opens {next_open_ist_str}",
                "next_open_ist": next_open_ist_str,
                "next_open_utc": next_open_utc.isoformat(),
                "countdown_seconds": int(diff_sec),
                "countdown_formatted": countdown_str,
                "reason": "Global Forex & Spot Metals markets are closed on weekends (Friday 21:00 UTC to Sunday 21:00 UTC / Saturday 02:30 AM IST to Monday 02:30 AM IST)."
            }
        else:
            # Market is open on weekdays
            days_to_friday = (4 - weekday) % 7
            target_date = dt.date() + timedelta(days=days_to_friday)
            next_close_utc = datetime(target_date.year, target_date.month, target_date.day, 21, 0, 0, tzinfo=timezone.utc)
            diff_sec = max(0, (next_close_utc - dt).total_seconds())

            hours = int(diff_sec // 3600)
            mins = int((diff_sec % 3600) // 60)
            days = hours // 24
            rem_hours = hours % 24
            countdown_str = f"{days}d {rem_hours}h {mins}m" if days > 0 else f"{hours}h {mins}m"
            next_close_ist_dt = next_close_utc.astimezone(IST_TZ)
            next_close_ist_str = next_close_ist_dt.strftime("%a %b %d, %I:%M %p IST")

            return {
                "symbol": symbol,
                "is_open": True,
                "market_type": "FOREX_METALS_24_5",
                "status": "OPEN",
                "status_badge": "🟢 MARKET OPEN (24/5)",
                "status_text": f"Market is OPEN (Closes {next_close_ist_str} in {countdown_str})",
                "next_event": f"Closes {next_close_ist_str}",
                "next_open_ist": "Currently Open",
                "next_open_utc": "",
                "countdown_seconds": int(diff_sec),
                "countdown_formatted": countdown_str,
                "reason": "Global Forex & Spot Metals markets trade continuously from "
                          "Sunday 21:00 UTC to Friday 21:00 UTC.",
            }
