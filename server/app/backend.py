"""Accès agenda / notifications.

- N8nBackend : tout passe par des webhooks n8n (Google Agenda, SMS, mail...). C'est la prod.
- MockBackend : agenda en fichier JSON local, pour tester sans rien brancher.

Contrat des webhooks n8n (POST JSON, header X-Voice-Secret) :
  {base}/voice-events-list    {tenant_id, time_min, time_max}      -> {"events": [{"start": iso, "end": iso}]}
  {base}/voice-events-create  {tenant_id, appointment: {...}}       -> {"ok": true, "event_id": "..."}
  {base}/voice-message        {tenant_id, message: {...}}           -> {"ok": true}
  {base}/voice-call-report    {tenant_id, report: {...}}            -> {"ok": true}
  {base}/voice-booking        {tenant_id, appointment: {...}}       -> {"ok": true}   (mail "nouveau RDV" au garage)
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
from loguru import logger

from .slots import Busy
from .tenants import Tenant


class Backend:
    async def list_busy(self, tenant: Tenant, time_min: datetime, time_max: datetime) -> Busy: ...
    async def create_appointment(self, tenant: Tenant, appointment: dict) -> dict: ...
    async def send_message(self, tenant: Tenant, message: dict) -> dict: ...
    async def call_report(self, tenant: Tenant, report: dict) -> dict: ...
    async def notify_booking(self, tenant: Tenant, appointment: dict) -> dict: ...


def _to_busy(events: list[dict], tz: str = "Europe/Paris", capacity: int = 1) -> Busy:
    """Accepte des dates ISO avec ou sans fuseau.

    Un événement "journée entière" (start = "2026-10-05", sans heure) = garage fermé : il bloque
    toute la capacité de l'atelier ce jour-là (congés, jour férié...).
    """
    zone = ZoneInfo(tz)

    def parse(v: str) -> datetime:
        dt = datetime.fromisoformat(v)
        return dt if dt.tzinfo else dt.replace(tzinfo=zone)

    out = []
    for e in events:
        try:
            interval = (parse(e["start"]), parse(e["end"]))
            full_day = len(str(e["start"])) == 10
            out.extend([interval] * (capacity if full_day else 1))
        except (KeyError, ValueError, TypeError):
            logger.warning(f"Événement ignoré (format invalide) : {e}")
    return out


class N8nBackend(Backend):
    def __init__(self, base_url: str, secret: str, timeout_s: float = 8.0):
        self.base_url = base_url.rstrip("/")
        self.secret = secret
        self.timeout = aiohttp.ClientTimeout(total=timeout_s)

    async def _post(self, path: str, payload: dict) -> dict:
        url = f"{self.base_url}/{path}"
        # destinataire des mails (messages, nouveaux RDV, rapports) : réglable dans .env, sans toucher à n8n
        if os.getenv("NOTIFY_EMAIL"):
            payload = {**payload, "notify_email": os.getenv("NOTIFY_EMAIL")}
        async with aiohttp.ClientSession(timeout=self.timeout) as s:
            async with s.post(url, json=payload, headers={"X-Voice-Secret": self.secret}) as r:
                text = await r.text()
                if r.status >= 400:
                    raise RuntimeError(f"n8n {path} -> HTTP {r.status}: {text[:300]}")
                return json.loads(text) if text else {}

    async def list_busy(self, tenant, time_min, time_max):
        data = await self._post(
            "voice-events-list",
            {"tenant_id": tenant.id, "time_min": time_min.isoformat(), "time_max": time_max.isoformat()},
        )
        return _to_busy(data.get("events", []), tenant.timezone, tenant.booking.capacity)

    async def create_appointment(self, tenant, appointment):
        return await self._post("voice-events-create", {"tenant_id": tenant.id, "appointment": appointment})

    async def send_message(self, tenant, message):
        return await self._post("voice-message", {"tenant_id": tenant.id, "message": message})

    async def call_report(self, tenant, report):
        return await self._post("voice-call-report", {"tenant_id": tenant.id, "report": report})

    async def notify_booking(self, tenant, appointment):
        return await self._post("voice-booking", {"tenant_id": tenant.id, "appointment": appointment})


class MockBackend(Backend):
    def __init__(self, data_dir: str | Path):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _file(self, tenant: Tenant) -> Path:
        return self.dir / f"mock_{tenant.id}.json"

    def _load(self, tenant: Tenant) -> dict:
        f = self._file(tenant)
        if f.exists():
            return json.loads(f.read_text(encoding="utf-8"))
        return {"appointments": [], "messages": [], "reports": []}

    def _save(self, tenant: Tenant, data: dict) -> None:
        self._file(tenant).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    async def list_busy(self, tenant, time_min, time_max):
        return _to_busy(self._load(tenant)["appointments"], tenant.timezone, tenant.booking.capacity)

    async def create_appointment(self, tenant, appointment):
        data = self._load(tenant)
        event_id = uuid.uuid4().hex[:10]
        data["appointments"].append({"id": event_id, **appointment})
        self._save(tenant, data)
        logger.info(f"[mock] RDV créé : {appointment}")
        return {"ok": True, "event_id": event_id}

    async def send_message(self, tenant, message):
        data = self._load(tenant)
        data["messages"].append(message)
        self._save(tenant, data)
        logger.info(f"[mock] Message : {message}")
        return {"ok": True}

    async def notify_booking(self, tenant, appointment):
        logger.info(f"[mock] Mail 'nouveau RDV' : {appointment.get('label')}")
        return {"ok": True}

    async def call_report(self, tenant, report):
        data = self._load(tenant)
        data["reports"].append(report)
        self._save(tenant, data)
        return {"ok": True}


def make_backend() -> Backend:
    base = os.getenv("N8N_WEBHOOK_BASE", "").strip()
    if base:
        return N8nBackend(base, os.getenv("N8N_WEBHOOK_SECRET", ""))
    logger.warning("N8N_WEBHOOK_BASE vide : agenda MOCK (fichier local), rien n'est envoyé")
    return MockBackend(os.getenv("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
