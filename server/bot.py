"""Pipeline vocal : Twilio (audio) -> Deepgram (STT) -> LLM + outils -> Cartesia (TTS) -> Twilio.

- En prod, server.py appelle run_bot() pour chaque appel entrant.
- En test, `python bot.py -t eval` lance le bot via le runner Pipecat (voir evals/).
"""

from __future__ import annotations

import os
from datetime import datetime

from dotenv import load_dotenv
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    AssistantTurnStoppedMessage,
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
    UserTurnStoppedMessage,
)
from pipecat.frames.frames import EndWorkerFrame, TTSSpeakFrame
from pipecat.runner.types import RunnerArguments
from pipecat.services.cartesia.tts import CartesiaTTSService
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.transcriptions.language import Language
from pipecat.transports.base_transport import BaseTransport
from pipecat.turns.user_mute.function_call_user_mute_strategy import FunctionCallUserMuteStrategy
from pipecat.turns.user_mute.mute_until_first_bot_complete_user_mute_strategy import (
    MuteUntilFirstBotCompleteUserMuteStrategy,
)
from pipecat.workers.runner import WorkerRunner

from app.backend import Backend, make_backend
from app.prompt import build_system_prompt
from app.tenants import Tenant, load_tenants
from app.tools import CallSession, tools_for

load_dotenv(override=True)


def make_llm(system_prompt: str):
    """LLM choisi par LLM_PROVIDER : mistral | google (Gemini natif) | anthropic | openai (compatible OpenAI)."""
    provider = os.getenv("LLM_PROVIDER", "mistral").lower()
    # Gemini via l'URL "compatible OpenAI" casse les appels d'outils en streaming
    # (nom de fonction vide -> erreur 400). On passe automatiquement par l'API native de Google.
    if provider == "openai" and "generativelanguage.googleapis.com" in os.getenv("OPENAI_BASE_URL", ""):
        provider = "google"
    if provider == "google":
        from pipecat.services.google.llm import GoogleLLMService

        return GoogleLLMService(
            api_key=os.getenv("GOOGLE_API_KEY") or os.getenv("OPENAI_API_KEY"),
            settings=GoogleLLMService.Settings(
                model=os.getenv("LLM_MODEL", "gemini-3.5-flash-lite"),
                system_instruction=system_prompt,
                temperature=0.3,
            ),
        )
    if provider == "mistral":
        from pipecat.services.mistral.llm import MistralLLMService

        return MistralLLMService(
            api_key=os.getenv("MISTRAL_API_KEY"),
            settings=MistralLLMService.Settings(
                model=os.getenv("LLM_MODEL", "mistral-small-latest"),
                system_instruction=system_prompt,
                temperature=0.3,
            ),
        )
    if provider == "anthropic":
        from pipecat.services.anthropic.llm import AnthropicLLMService

        return AnthropicLLMService(
            api_key=os.getenv("ANTHROPIC_API_KEY"),
            settings=AnthropicLLMService.Settings(
                model=os.getenv("LLM_MODEL", "claude-haiku-4-5"),
                system_instruction=system_prompt,
            ),
        )
    if provider == "openai":  # tout fournisseur compatible OpenAI (Gemini, Groq, DeepSeek...)
        from pipecat.services.openai.llm import OpenAILLMService

        return OpenAILLMService(
            api_key=os.getenv("OPENAI_API_KEY"),
            base_url=os.getenv("OPENAI_BASE_URL") or None,
            settings=OpenAILLMService.Settings(
                model=os.getenv("LLM_MODEL", "gpt-4.1-mini"),
                system_instruction=system_prompt,
                temperature=0.3,
            ),
        )
    raise ValueError(f"LLM_PROVIDER inconnu : {provider}")


def build_call_report(session: CallSession) -> dict:
    ended = datetime.now().astimezone()
    return {
        "tenant_id": session.tenant.id,
        "garage_name": session.tenant.name,
        "call_sid": session.call_sid,
        "caller_number": session.caller_number,
        "started_at": session.started_at.isoformat(),
        "ended_at": ended.isoformat(),
        "duration_s": int((ended - session.started_at).total_seconds()),
        "outcome": (
            "rdv_pris" if session.appointments
            else "transfert" if session.transferred
            else "message" if session.messages
            else "sans_suite"
        ),
        "appointments": session.appointments,
        "messages": session.messages,
        "transcript": session.transcript,
    }


async def run_bot(
    transport: BaseTransport,
    tenant: Tenant,
    backend: Backend,
    caller_number: str | None = None,
    call_sid: str | None = None,
    sample_rate: int | None = 8000,  # 8000 = téléphone ; 16000 = appel navigateur ; None = défaut (evals)
    handle_sigint: bool = False,
) -> None:
    session = CallSession(tenant=tenant, backend=backend, caller_number=caller_number, call_sid=call_sid)
    logger.info(f"Appel {call_sid} de {caller_number} pour {tenant.id}")

    stt = DeepgramSTTService(
        api_key=os.getenv("DEEPGRAM_API_KEY"),
        settings=DeepgramSTTService.Settings(
            model=os.getenv("DEEPGRAM_MODEL", "nova-3-general"),
            language=Language.FR,
            smart_format=True,
        ),
    )
    tts = CartesiaTTSService(
        api_key=os.getenv("CARTESIA_API_KEY"),
        settings=CartesiaTTSService.Settings(
            voice=tenant.voice_id or os.getenv("CARTESIA_VOICE_ID"),
            language=Language.FR,
        ),
    )
    llm = make_llm(build_system_prompt(tenant, session.now(), caller_number))

    context = LLMContext(tools=tools_for(tenant))
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(),
            # tolère des trous de réseau jusqu'à 3 s sans couper la phrase du client (1 s par défaut)
            audio_idle_timeout=float(os.getenv("AUDIO_IDLE_TIMEOUT", "3.0")),
            user_mute_strategies=[
                # l'accueil (avec l'annonce "assistante virtuelle") est toujours dit en entier
                MuteUntilFirstBotCompleteUserMuteStrategy(),
                # un "oui" pendant une réservation ou un transfert n'annule pas l'outil en cours
                FunctionCallUserMuteStrategy(),
            ],
        ),
    )

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            user_aggregator,
            llm,
            tts,
            transport.output(),
            assistant_aggregator,
        ]
    )

    rates = {"audio_in_sample_rate": sample_rate, "audio_out_sample_rate": sample_rate} if sample_rate else {}
    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=True, **rates),
        app_resources=session,
    )
    runner = WorkerRunner(handle_sigint=handle_sigint)
    await runner.add_workers(worker)

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        # Accueil dit mot pour mot (avec l'annonce "assistante virtuelle"), puis le LLM prend le relais.
        await worker.queue_frames([TTSSpeakFrame(tenant.greeting)])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Client déconnecté")
        await runner.cancel()

    @transport.event_handler("on_session_timeout")
    async def on_session_timeout(transport, websocket):
        # garde-fou coûts : MAX_CALL_SECONDS atteint
        logger.warning("Durée max d'appel atteinte, on raccroche")
        await worker.queue_frames([
            TTSSpeakFrame("Je dois malheureusement écourter l'appel. N'hésitez pas à nous rappeler. Au revoir."),
            EndWorkerFrame(),
        ])

    @user_aggregator.event_handler("on_user_turn_stopped")
    async def on_user_turn_stopped(aggregator, strategy, message: UserTurnStoppedMessage):
        session.transcript.append({"role": "client", "text": message.content})
        logger.info(f"CLIENT : {message.content}")

    @assistant_aggregator.event_handler("on_assistant_turn_stopped")
    async def on_assistant_turn_stopped(aggregator, message: AssistantTurnStoppedMessage):
        session.transcript.append({"role": "agent", "text": message.content})
        logger.info(f"AGENT : {message.content}")

    try:
        await runner.run()
    finally:
        try:
            await backend.call_report(tenant, build_call_report(session))
        except Exception as e:
            # pas bloquant : le workflow n8n "rapports" est optionnel
            logger.warning(f"Rapport de fin d'appel non envoyé : {e}")


async def bot(runner_args: RunnerArguments):
    """Point d'entrée du runner Pipecat (tests : `python bot.py -t eval`)."""
    from pipecat.runner.utils import create_transport

    transport_params = {}
    try:
        from pipecat.evals.transport import EvalTransportParams

        transport_params["eval"] = lambda: EvalTransportParams(audio_in_enabled=True, audio_out_enabled=True)
    except ImportError:
        pass

    transport = await create_transport(runner_args, transport_params)
    tenants = load_tenants()
    tenant = tenants[os.getenv("DEFAULT_TENANT", "garage-demo")]
    await run_bot(
        transport,
        tenant,
        make_backend(),
        caller_number="+33600000000",
        sample_rate=None,
        handle_sigint=runner_args.handle_sigint,
    )


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()
