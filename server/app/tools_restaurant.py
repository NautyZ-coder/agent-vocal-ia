"""Formulaire de réservation d'un restaurant.

La fonction s'appelle aussi `book_appointment` : c'est ce nom que voit le LLM (mêmes garde-fous,
même end_call), seuls les champs changent (nombre de couverts, allergies et remarques).
"""

from __future__ import annotations

from pipecat.services.llm_service import FunctionCallParams

from .tools import _book_entry, _session


async def book_appointment(
    params: FunctionCallParams,
    start: str,
    service_id: str,
    customer_name: str,
    party_size: int,
    notes: str = "",
    customer_phone: str = "",
    customer_email: str = "",
):
    """Réserve la table. À appeler seulement après confirmation explicite du client sur le récapitulatif.

    Args:
        start: Heure d'arrivée, exactement la valeur "start" renvoyée par check_availability.
        service_id: Identifiant du type de réservation.
        customer_name: Nom de la réservation (prénom et nom).
        party_size: Nombre de personnes, enfants compris.
        notes: Allergies, chaise haute, anniversaire... en une phrase courte, sinon vide.
        customer_phone: Numéro de portable si différent du numéro appelant (ex. "06 12 34 56 78"), sinon vide.
        customer_email: Adresse e-mail confirmée par le client pour recevoir la confirmation, sinon vide.
    """
    max_size = int(_session(params).tenant.extra.get("max_party_size", 8))
    try:
        size = int(party_size)
    except (TypeError, ValueError):
        size = 0
    if not 1 <= size <= max_size:
        await params.result_callback({
            "ok": False,
            "error": f"Réservation possible de 1 à {max_size} personnes. Au-delà, prends un message avec "
                     "take_message pour que le restaurant rappelle. Sinon, redemande le nombre de personnes.",
        })
        return
    summary = f"{size} personne{'s' if size > 1 else ''}" + (f". {notes.strip()}" if notes.strip() else "")
    await _book_entry(params, start, service_id, customer_name, "", summary, "", customer_phone, customer_email,
                      extra={"party_size": size})
