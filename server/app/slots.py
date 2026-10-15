"""Calcul des créneaux libres à partir des horaires du garage et des RDV existants.

Pure logique, sans I/O : testable et identique quel que soit l'agenda derrière (n8n, mock...).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .tenants import DAY_KEYS, Tenant

JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS = [
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
]

Busy = list[tuple[datetime, datetime]]


def fr_date(d: date) -> str:
    return f"{JOURS[d.weekday()]} {d.day} {MOIS[d.month - 1]}"


def fr_time(t: datetime | time) -> str:
    """Format pensé pour être lu à voix haute : '11 heures 30', '9 heures'."""
    return f"{t.hour} heures {t.minute:02d}" if t.minute else f"{t.hour} heures"


def fr_datetime(dt: datetime) -> str:
    return f"{fr_date(dt.date())} à {fr_time(dt)}"


def _parse_range(r: str) -> tuple[time, time]:
    a, b = r.split("-")
    return time.fromisoformat(a.strip()), time.fromisoformat(b.strip())


def _overlaps(start: datetime, end: datetime, busy: Busy) -> int:
    return sum(1 for s, e in busy if s < end and e > start)


def day_slots(tenant: Tenant, day: date, duration_min: int, busy: Busy, now: datetime) -> list[datetime]:
    """Tous les débuts de créneau possibles pour ce jour."""
    tz = ZoneInfo(tenant.timezone)
    b = tenant.booking
    earliest = now + timedelta(hours=b.min_notice_hours)
    dur = timedelta(minutes=duration_min)
    step = timedelta(minutes=b.slot_step_min)
    out: list[datetime] = []
    for r in tenant.opening_hours.get(DAY_KEYS[day.weekday()], []):
        open_t, close_t = _parse_range(r)
        cur = datetime.combine(day, open_t, tzinfo=tz)
        close = datetime.combine(day, close_t, tzinfo=tz)
        while cur + dur <= close:
            if cur >= earliest and _overlaps(cur, cur + dur, busy) < b.capacity:
                out.append(cur)
            cur += step
    return out


def _period_ok(dt: datetime, period: str | None) -> bool:
    if period == "matin":
        return dt.hour < 12
    if period == "apres-midi":
        return dt.hour >= 12
    # Restaurant : service du midi / du soir
    if period == "midi":
        return dt.hour < 16
    if period == "soir":
        return dt.hour >= 16
    return True


def _spread(slots: list[datetime], n: int) -> list[datetime]:
    """Choisit n créneaux étalés dans la journée (ex. 9h, 11h30, 14h) plutôt que 3 d'affilée."""
    if len(slots) <= n:
        return slots
    if n == 1:
        return slots[:1]
    idx = sorted({round(i * (len(slots) - 1) / (n - 1)) for i in range(n)})
    return [slots[i] for i in idx]


def find_slots(
    tenant: Tenant,
    duration_min: int,
    busy: Busy,
    now: datetime,
    preferred_date: date | None = None,
    period: str | None = None,
    preferred_time: time | None = None,
) -> dict:
    """Renvoie les créneaux à proposer + si le jour (et l'heure) demandés étaient dispo."""
    tz = ZoneInfo(tenant.timezone)
    now = now.astimezone(tz)
    n = tenant.booking.max_slots_offered
    last_day = now.date() + timedelta(days=tenant.booking.horizon_days)
    if preferred_date and preferred_date < now.date():
        preferred_date = None  # date passée : on ignore la préférence
    if preferred_date and preferred_date > last_day:
        return {
            "requested_date_available": False,
            "too_far": True,
            "slots": [],
            "note": f"On ne prend pas de rendez-vous au-delà de {tenant.booking.horizon_days} jours. "
            "Propose de chercher une date plus proche.",
        }
    start_day = preferred_date or now.date()

    requested_available = None
    d = start_day
    while d <= last_day:
        slots = [s for s in day_slots(tenant, d, duration_min, busy, now) if _period_ok(s, period)]
        if d == preferred_date:
            requested_available = bool(slots)
        if slots:
            res = {"requested_date_available": requested_available}
            if preferred_time and d == preferred_date:
                # Heure précise demandée (ex. restaurant à 20 heures) : les créneaux les plus proches, pas étalés.
                res["requested_time_available"] = any(s.time() == preferred_time for s in slots)
                target = datetime.combine(d, preferred_time, tzinfo=tz)
                chosen = sorted(sorted(slots, key=lambda s: abs(s - target))[:n])
            else:
                chosen = _spread(slots, n)
            return {
                **res,
                "date": d.isoformat(),
                "date_label": fr_date(d),
                "slots": [{"start": s.isoformat(), "label": fr_datetime(s)} for s in chosen],
            }
        d += timedelta(days=1)
    return {"requested_date_available": False, "slots": []}


def is_slot_free(tenant: Tenant, start: datetime, duration_min: int, busy: Busy, now: datetime) -> bool:
    start = start.astimezone(ZoneInfo(tenant.timezone))
    return start in day_slots(tenant, start.date(), duration_min, busy, now)
