"""Outils que le LLM peut appeler pendant l'appel.

Chaque outil est une fonction async : sa signature + sa docstring deviennent le schéma
envoyé au LLM (Pipecat le fait tout seul). L'état de l'appel est dans params.app_resources.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp
from loguru import logger
from pipecat.frames.frames import EndWorkerFrame, FunctionCallResultProperties, TTSSpeakFrame
from pipecat.services.llm_service import FunctionCallParams

from .backend import Backend
from .slots import find_slots, fr_datetime, is_slot_free
from .tenants import Tenant


@dataclass
class CallSession:
    """État d'un appel, partagé entre les outils."""

    tenant: Tenant
    backend: Backend
    caller_number: str | None = None
    call_sid: str | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now().astimezone())
    appointments: list[dict] = field(default_factory=list)
    messages: list[dict] = field(default_factory=list)
    transferred: bool = False
    booking_in_progress: bool = False
    last_booking_at: float = 0.0
    transcript: list[dict] = field(default_factory=list)

    def now(self) -> datetime:
        return datetime.now(ZoneInfo(self.tenant.timezone))


def _session(params: FunctionCallParams) -> CallSession:
    return params.app_resources


def normalize_phone(raw: str) -> str:
    """'06 12 34 56 78' / '0033612...' / '+33 6 12...' -> '+33612345678'. Renvoie '' si inexploitable."""
    digits = re.sub(r"[^\d+]", "", raw or "")
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    if re.fullmatch(r"0[1-9]\d{8}", digits):
        digits = "+33" + digits[1:]
    return digits if re.fullmatch(r"\+[1-9]\d{6,14}", digits) else ""


_EMAIL_WORDS = [
    (r"\s*\b(arobase|arrobase|at)\b\s*", "@"),
    (r"\s*\b(point|dot)\b\s*", "."),
    (r"\s*\b(tiret du bas|underscore|tiret bas)\b\s*", "_"),
    (r"\s*\b(tiret|trait d'union)\b\s*", "-"),
]


def normalize_email(raw: str) -> str:
    """'Jean point Dupont arobase gmail point com' -> 'jean.dupont@gmail.com'. '' si invalide."""
    e = (raw or "").strip().lower()
    for pat, rep in _EMAIL_WORDS:
        e = re.sub(pat, rep, e)
    e = re.sub(r"\s+", "", e).strip(".")
    return e if re.fullmatch(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", e) else ""


def _parse_start(value: str, tz: str) -> datetime:
    """Un LLM oublie souvent le fuseau : une heure sans fuseau est l'heure locale du garage."""
    dt = datetime.fromisoformat(value)
    return dt.replace(tzinfo=ZoneInfo(tz)) if dt.tzinfo is None else dt.astimezone(ZoneInfo(tz))


async def check_availability(
    params: FunctionCallParams, service_id: str, preferred_date: str = "", period: str = ""
):
    """Cherche les prochains créneaux libres. À appeler avant de proposer une heure.

    Args:
        service_id: Identifiant du type de rendez-vous, pris dans la liste de tes consignes.
        preferred_date: Jour souhaité au format AAAA-MM-JJ. Vide si le client n'a pas de préférence.
        period: "matin", "apres-midi", ou vide si peu importe.
    """
    s = _session(params)
    t = s.tenant
    svc = t.service(service_id) or t.service("autre") or t.services[0]
    pref: date | None = None
    if preferred_date:
        try:
            pref = date.fromisoformat(preferred_date)
        except ValueError:
            pref = None
    now = s.now()
    try:
        busy = await s.backend.list_busy(t, now, now + timedelta(days=t.booking.horizon_days + 1))
    except Exception as e:
        logger.exception(f"list_busy a échoué : {e}")
        await params.result_callback(
            {"error": "Agenda indisponible. Propose de prendre un message pour qu'on rappelle le client."}
        )
        return
    res = find_slots(t, svc.duration_min, busy, now, pref, period or None)
    res["service_id"] = svc.id
    res["service_label"] = svc.label
    if not res["slots"] and not res.get("too_far"):
        res["note"] = "Aucun créneau dans les prochaines semaines : propose de prendre un message."
    elif pref and not res["requested_date_available"]:
        res["note"] = "Le jour demandé est complet ou fermé : annonce-le et propose ces créneaux à la place."
    await params.result_callback(res)


async def book_appointment(
    params: FunctionCallParams,
    start: str,
    service_id: str,
    customer_name: str,
    vehicle: str,
    problem_description: str,
    plate: str = "",
    customer_phone: str = "",
    customer_email: str = "",
):
    """Réserve le créneau. À appeler seulement après confirmation explicite du client sur le récapitulatif.

    Args:
        start: Début du créneau, exactement la valeur "start" renvoyée par check_availability.
        service_id: Identifiant du type d'intervention.
        customer_name: Prénom et nom du client.
        vehicle: Marque, modèle et année du véhicule (ex. "Peugeot 208 de 2019").
        problem_description: Travaux demandés et symptômes décrits par le client.
        plate: Immatriculation si le client l'a donnée, sinon vide.
        customer_phone: Numéro de portable pour le SMS si différent du numéro appelant (ex. "06 12 34 56 78"), sinon vide.
        customer_email: Adresse e-mail confirmée par le client pour recevoir la confirmation, sinon vide.
    """
    await _book_entry(params, start, service_id, customer_name, vehicle, problem_description, plate,
                      customer_phone, customer_email)


async def _book_entry(params, start, service_id, customer_name, vehicle, problem_description, plate="",
                      customer_phone="", customer_email="", extra: dict | None = None):
    """Point d'entrée commun à tous les métiers (le garage et le cabinet n'ont pas les mêmes champs)."""
    s = _session(params)
    t = s.tenant
    svc = t.service(service_id) or t.service("autre") or t.services[0]
    try:
        start_dt = _parse_start(start, t.timezone)
    except ValueError:
        await params.result_callback({"error": "Créneau invalide. Relance check_availability."})
        return

    s.booking_in_progress = True
    try:
        await _book(params, s, t, svc, start_dt, customer_name, vehicle, problem_description, plate, customer_phone,
                    customer_email, extra)
    finally:
        s.booking_in_progress = False
        s.last_booking_at = time.monotonic()


async def _book(params, s, t, svc, start_dt, customer_name, vehicle, problem_description, plate, customer_phone,
                customer_email="", extra=None):
    # Idempotence : si l'outil est relancé (interruption, doublon), on ne crée pas 2 RDV.
    for a in s.appointments:
        if a["start"] == start_dt.isoformat():
            await params.result_callback({"ok": True, "already_booked": True, "label": fr_datetime(start_dt)})
            return

    now = s.now()
    try:
        busy = await s.backend.list_busy(t, start_dt - timedelta(hours=12), start_dt + timedelta(hours=12))
        if not is_slot_free(t, start_dt, svc.duration_min, busy, now):
            await params.result_callback(
                {"ok": False, "error": "Ce créneau n'est plus libre. Excuse-toi et relance check_availability."}
            )
            return
        appt = {
            "start": start_dt.isoformat(),
            "end": (start_dt + timedelta(minutes=svc.duration_min)).isoformat(),
            "label": fr_datetime(start_dt),
            "service_id": svc.id,
            "service_label": svc.label,
            "customer_name": customer_name,
            "customer_phone": normalize_phone(customer_phone) or normalize_phone(s.caller_number or ""),
            "customer_email": normalize_email(customer_email),
            "vehicle": vehicle,
            "plate": plate,
            "problem_description": problem_description,
            "garage_name": t.name,
            "garage_address": t.address,
            "source": "agent-vocal",
            **(extra or {}),
        }

        async def create():
            res = await s.backend.create_appointment(t, appt)
            s.appointments.append(appt)
            asyncio.ensure_future(_notify_booking(s, appt))  # mail au garage, sans faire attendre le client
            return res

        # shield : même si l'outil est annulé, la création va au bout et est mémorisée (pas de doublon)
        await asyncio.shield(asyncio.ensure_future(create()))
    except Exception as e:
        logger.exception(f"create_appointment a échoué : {e}")
        await params.result_callback(
            {"ok": False, "error": "Réservation impossible pour raison technique. Prends un message avec take_message."}
        )
        return
    await params.result_callback(
        {
            "ok": True,
            "label": appt["label"],
            "email_confirmation": appt["customer_email"] or (
                "aucune (adresse non valide ou non donnée) : ne promets pas d'e-mail" if customer_email else "aucune"
            ),
        }
    )


async def take_message(params: FunctionCallParams, customer_name: str, reason: str, callback_number: str = ""):
    """Transmet un message à l'équipe (devis, suivi, rappel, annulation, réclamation...).

    Args:
        customer_name: Nom du client.
        reason: Objet du message, avec les détails utiles (question posée, rendez-vous concerné...).
        callback_number: Numéro de rappel si différent du numéro appelant, sinon vide.
    """
    s = _session(params)
    msg = {
        "customer_name": customer_name,
        "reason": reason,
        "callback_number": normalize_phone(callback_number) or callback_number or s.caller_number or "",
        "at": s.now().isoformat(),
    }
    try:
        await s.backend.send_message(s.tenant, msg)
    except Exception as e:
        logger.exception(f"send_message a échoué : {e}")
    s.messages.append(msg)  # gardé aussi dans le rapport de fin d'appel, même si l'envoi a raté
    await params.result_callback({"ok": True})


async def transfer_to_human(params: FunctionCallParams, reason: str):
    """Transfère l'appel vers un humain de l'équipe.

    Args:
        reason: Pourquoi le client veut parler à quelqu'un.
    """
    s = _session(params)
    number = s.tenant.transfer_number
    sid, tok = os.getenv("TWILIO_ACCOUNT_SID", ""), os.getenv("TWILIO_AUTH_TOKEN", "")
    if not (number and s.call_sid and sid and tok):
        await params.result_callback({"ok": False, "error": "Transfert impossible : prends un message."})
        return
    # La phrase est dite par Twilio juste avant de composer : rien ne peut couper le transfert.
    twiml = (
        '<Response><Say language="fr-FR">Je vous mets en relation, un instant s\'il vous plaît.</Say>'
        f'<Dial timeout="25">{number}</Dial>'
        '<Say language="fr-FR">Personne n\'est disponible pour le moment, merci de rappeler plus tard. Au revoir.</Say>'
        "</Response>"
    )
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls/{s.call_sid}.json"
    s.transferred = True  # avant l'appel REST : Twilio coupe le flux audio dès que ça marche
    try:
        async with aiohttp.ClientSession() as http:
            async with http.post(url, data={"Twiml": twiml}, auth=aiohttp.BasicAuth(sid, tok)) as r:
                if r.status >= 400:
                    raise RuntimeError(f"HTTP {r.status}: {await r.text()}")
        logger.info(f"Appel {s.call_sid} transféré vers {number} ({reason})")
        await params.result_callback({"ok": True}, properties=FunctionCallResultProperties(run_llm=False))
    except Exception as e:
        s.transferred = False
        logger.exception(f"Transfert échoué : {e}")
        await params.result_callback({"ok": False, "error": "Transfert impossible : prends un message."})


async def end_call(params: FunctionCallParams, reason: str = ""):
    """Raccroche. À appeler après avoir dit au revoir, quand le client n'a plus de demande.

    Args:
        reason: Motif de fin d'appel en quelques mots (ex. "rdv pris"), facultatif.
    """
    s = _session(params)
    # Garde-fou : certains modèles appellent end_call en même temps que book_appointment,
    # ce qui raccrochait avant que le client entende la confirmation.
    if s.booking_in_progress or (s.last_booking_at and time.monotonic() - s.last_booking_at < 8):
        await params.result_callback({
            "ok": False,
            "error": "Ne raccroche pas encore : annonce d'abord la confirmation du rendez-vous, "
                     "demande si le client a besoin d'autre chose, et attends sa réponse.",
        })
        return
    await params.result_callback({"ok": True}, properties=FunctionCallResultProperties(run_llm=False))
    # Certains modèles appellent end_call sans rien dire : on garantit un au revoir avant de raccrocher.
    if not _said_goodbye(params, s):
        await params.llm.push_frame(TTSSpeakFrame(s.tenant.goodbye))
    await params.llm.push_frame(EndWorkerFrame())


async def _notify_booking(s: CallSession, appt: dict) -> None:
    try:
        await s.backend.notify_booking(s.tenant, appt)
    except Exception as e:
        logger.warning(f"Mail 'nouveau RDV' non envoyé : {e}")


def _said_goodbye(params: FunctionCallParams, s: CallSession) -> bool:
    texts = []
    try:
        for m in reversed(params.context.get_messages()[-3:]):
            if m.get("role") == "assistant":
                c = m.get("content")
                texts.append(c if isinstance(c, str) else " ".join(p.get("text", "") for p in (c or []) if isinstance(p, dict)))
    except Exception:
        pass
    if s.transcript and s.transcript[-1]["role"] == "agent":
        texts.append(s.transcript[-1]["text"])
    return any("revoir" in t.lower() or "bonne journée" in t.lower() for t in texts)


ALL_TOOLS = [check_availability, book_appointment, take_message, transfer_to_human, end_call]


def tools_for(tenant: Tenant) -> list:
    """Outils envoyés au LLM selon le métier du client (le formulaire de RDV change)."""
    if tenant.profile == "dentiste":
        from .tools_dentiste import book_appointment as book_dentiste

        return [check_availability, book_dentiste, take_message, transfer_to_human, end_call]
    return ALL_TOOLS
