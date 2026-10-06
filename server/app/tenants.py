"""Chargement de la config des garages (un YAML par garage dans tenants/)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

TENANTS_DIR = Path(os.getenv("TENANTS_DIR", Path(__file__).resolve().parent.parent / "tenants"))

DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# Métier du client : choisit le prompt et le formulaire de réservation.
PROFILES = ("garage", "dentiste")


@dataclass
class Service:
    id: str
    label: str
    duration_min: int


@dataclass
class Booking:
    capacity: int = 1
    slot_step_min: int = 30
    min_notice_hours: int = 2
    horizon_days: int = 14
    max_slots_offered: int = 3


@dataclass
class Tenant:
    id: str
    name: str
    phone_numbers: list[str]
    timezone: str
    address: str
    agent_name: str
    agent_title: str
    goodbye: str
    sms_confirmation: bool
    voice_id: str
    greeting: str
    opening_hours: dict[str, list[str]]
    booking: Booking
    services: list[Service]
    transfer_number: str = ""
    faq: str = ""
    profile: str = "garage"
    extra: dict = field(default_factory=dict)

    def service(self, service_id: str) -> Service | None:
        return next((s for s in self.services if s.id == service_id), None)


def _parse(data: dict) -> Tenant:
    agent = data.get("agent", {})
    profile = data.get("profile", "garage") or "garage"
    if profile not in PROFILES:
        raise ValueError(f"{data.get('id')} : profile '{profile}' inconnu (valeurs possibles : {', '.join(PROFILES)})")
    hours = {d: list(data.get("opening_hours", {}).get(d, []) or []) for d in DAY_KEYS}
    return Tenant(
        id=data["id"],
        name=data["name"],
        phone_numbers=[str(n) for n in data.get("phone_numbers", [])],
        timezone=data.get("timezone", "Europe/Paris"),
        address=data.get("address", ""),
        agent_name=agent.get("name", "Léa"),
        agent_title=agent.get("title", "l'assistante virtuelle"),
        goodbye=agent.get("goodbye", "Merci pour votre appel, bonne journée et à bientôt au garage. Au revoir !"),
        sms_confirmation=bool(data.get("confirmations", {}).get("sms", False)),
        voice_id=agent.get("voice_id", "") or "",
        greeting=" ".join(agent["greeting"].split()),
        opening_hours=hours,
        booking=Booking(**data.get("booking", {})),
        services=[Service(**s) for s in data["services"]],
        transfer_number=data.get("transfer_number", "") or "",
        faq=data.get("faq", "") or "",
        profile=profile,
        extra=data.get("extra", {}) or {},
    )


def load_tenants(directory: Path = TENANTS_DIR) -> dict[str, Tenant]:
    tenants: dict[str, Tenant] = {}
    for path in sorted(Path(directory).glob("*.y*ml")):
        with open(path, encoding="utf-8") as f:
            t = _parse(yaml.safe_load(f))
        tenants[t.id] = t
    return tenants


def tenant_for_number(tenants: dict[str, Tenant], number: str | None) -> Tenant | None:
    if not number:
        return None
    for t in tenants.values():
        if number in t.phone_numbers:
            return t
    return None
