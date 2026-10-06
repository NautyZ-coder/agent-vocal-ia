"""Formulaire de RDV d'un cabinet dentaire.

La fonction s'appelle aussi `book_appointment` : c'est ce nom que voit le LLM (mêmes garde-fous, même end_call),
seuls les champs changent (pas de véhicule, mais "déjà patient ou non").
"""

from __future__ import annotations

from pipecat.services.llm_service import FunctionCallParams

from .tools import _book_entry


async def book_appointment(
    params: FunctionCallParams,
    start: str,
    service_id: str,
    patient_name: str,
    reason: str,
    new_patient: bool,
    customer_phone: str = "",
    customer_email: str = "",
):
    """Réserve le créneau. À appeler seulement après confirmation explicite du patient sur le récapitulatif.

    Args:
        start: Début du créneau, exactement la valeur "start" renvoyée par check_availability.
        service_id: Identifiant du type de rendez-vous.
        patient_name: Prénom et nom du patient.
        reason: Motif en une phrase courte, sans détail médical (ex. "douleur dent du bas depuis hier").
        new_patient: true si c'est un nouveau patient du cabinet, false s'il est déjà suivi.
        customer_phone: Numéro de portable si différent du numéro appelant (ex. "06 12 34 56 78"), sinon vide.
        customer_email: Adresse e-mail confirmée par le patient pour recevoir la confirmation, sinon vide.
    """
    await _book_entry(params, start, service_id, patient_name, "", reason, "", customer_phone, customer_email,
                      extra={"new_patient": bool(new_patient)})
