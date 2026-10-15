"""Serveur de prod : reçoit les appels Twilio et lance un bot par appel.

Routes
  POST /twilio/voice   webhook "A call comes in" de Twilio -> renvoie le TwiML qui ouvre le flux audio
  WS   /ws             flux audio Twilio (Media Streams)
  GET  /health         pour vérifier que le service tourne
  GET  /appel          page d'appel depuis le navigateur (sans numéro de téléphone), protégée par WEB_CALL_CODE
  WS   /ws-web         flux audio de cette page

Sécurité
  - La signature Twilio (X-Twilio-Signature) est vérifiée sur /twilio/voice.
  - Le TwiML contient un jeton signé (HMAC) valable 2 min, vérifié à l'ouverture du WebSocket :
    personne ne peut ouvrir /ws et consommer tes crédits IA sans passer par un vrai appel Twilio.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from html import escape
from pathlib import Path
from xml.sax.saxutils import quoteattr

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, Response
from loguru import logger
from pipecat.runner.utils import parse_telephony_websocket
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport

from app.backend import make_backend
from app.tenants import load_tenants, tenant_for_number
from app.webcall import WEB_SAMPLE_RATE, WebPCMSerializer
from bot import run_bot

load_dotenv(override=True)

PUBLIC_HOST = os.getenv("PUBLIC_HOST", "")  # ex. voice.example.com (sans https://)
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
STREAM_SECRET = os.getenv("STREAM_SECRET", "")
VERIFY_TWILIO = os.getenv("VERIFY_TWILIO_SIGNATURE", "true").lower() != "false"
MAX_CALL_SECONDS = int(os.getenv("MAX_CALL_SECONDS", "600"))
WEB_CALL_CODE = os.getenv("WEB_CALL_CODE", "")  # vide = page /appel désactivée
MAX_WEB_CALLS = int(os.getenv("MAX_WEB_CALLS", "2"))
DEFAULT_TENANT = os.getenv("DEFAULT_TENANT", "garage-demo")
APPEL_HTML = (Path(__file__).resolve().parent / "app" / "static" / "appel.html").read_text(encoding="utf-8")
_web_calls = 0

app = FastAPI(title="Agent vocal")
TENANTS = load_tenants()
BACKEND = make_backend()
logger.info(f"Garages chargés : {', '.join(TENANTS) or 'aucun'}")


def twilio_signature_ok(url: str, form: dict[str, str], signature: str) -> bool:
    """Algo officiel Twilio : HMAC-SHA1(url + params triés concaténés), en base64."""
    payload = url + "".join(k + form[k] for k in sorted(form))
    digest = hmac.new(TWILIO_AUTH_TOKEN.encode(), payload.encode(), hashlib.sha1).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), signature or "")


def make_stream_token(call_sid: str, tenant_id: str, ttl: int = 120) -> str:
    exp = str(int(time.time()) + ttl)
    msg = f"{call_sid}:{tenant_id}:{exp}"
    sig = hmac.new(STREAM_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def check_stream_token(token: str, call_sid: str, tenant_id: str) -> bool:
    try:
        exp, sig = token.split(".", 1)
        if int(exp) < time.time():
            return False
    except (ValueError, AttributeError):
        return False
    msg = f"{call_sid}:{tenant_id}:{exp}"
    expected = hmac.new(STREAM_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), sig.encode())


def xml(body: str) -> Response:
    return Response(content=f'<?xml version="1.0" encoding="UTF-8"?><Response>{body}</Response>',
                    media_type="application/xml")


@app.get("/health")
async def health():
    return {"ok": True, "tenants": list(TENANTS)}


@app.post("/twilio/voice")
async def twilio_voice(request: Request):
    form = {k: str(v) for k, v in (await request.form()).items()}
    if VERIFY_TWILIO:
        url = f"https://{PUBLIC_HOST}/twilio/voice"
        if not twilio_signature_ok(url, form, request.headers.get("X-Twilio-Signature", "")):
            logger.warning("Signature Twilio invalide : requête rejetée")
            return Response(status_code=403)

    to, frm, call_sid = form.get("To", ""), form.get("From", ""), form.get("CallSid", "")
    # Avec un renvoi d'appel, "To" est le numéro Twilio ; ForwardedFrom = numéro d'origine du garage.
    tenant = tenant_for_number(TENANTS, to) or tenant_for_number(TENANTS, form.get("ForwardedFrom"))
    if not tenant:
        logger.error(f"Aucun garage configuré pour le numéro {to}")
        return xml('<Say language="fr-FR">Désolé, ce numéro n\'est pas encore configuré.</Say>')

    token = make_stream_token(call_sid, tenant.id)
    params = {"tenant_id": tenant.id, "from_number": frm, "to_number": to, "token": token}
    params_xml = "".join(f"<Parameter name={quoteattr(k)} value={quoteattr(v)}/>" for k, v in params.items())
    return xml(f'<Connect><Stream url="wss://{PUBLIC_HOST}/ws">{params_xml}</Stream></Connect>')


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    try:
        transport_type, call_data = await parse_telephony_websocket(websocket)
    except ValueError:
        logger.warning("WebSocket fermé avant la poignée de main Twilio")
        return
    body = call_data.get("body", {}) or {}
    call_sid = call_data["call_id"]
    tenant = TENANTS.get(body.get("tenant_id", ""))

    if transport_type != "twilio" or not tenant or not check_stream_token(body.get("token", ""), call_sid, tenant.id):
        logger.warning(f"WebSocket refusé (type={transport_type}, tenant={body.get('tenant_id')})")
        await websocket.close(code=4003)
        return

    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            session_timeout=MAX_CALL_SECONDS,
            serializer=TwilioFrameSerializer(
                stream_sid=call_data["stream_id"],
                call_sid=call_sid,
                account_sid=TWILIO_ACCOUNT_SID,
                auth_token=TWILIO_AUTH_TOKEN,
                # raccroche l'appel Twilio quand le bot termine (end_call) ; nécessite les identifiants
                params=TwilioFrameSerializer.InputParams(
                    auto_hang_up=bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN)
                ),
            ),
        ),
    )
    await run_bot(transport, tenant, BACKEND, caller_number=body.get("from_number"), call_sid=call_sid)


@app.get("/appel")
async def appel_page(g: str = ""):
    tenant = TENANTS.get(g or DEFAULT_TENANT)
    if not WEB_CALL_CODE or not tenant:
        return Response(status_code=404)
    place = {"dentiste": "du cabinet", "restaurant": "du restaurant"}.get(tenant.profile, "du garage")
    return HTMLResponse(APPEL_HTML.replace("{{GARAGE_NAME}}", escape(tenant.name)).replace("{{PLACE}}", place))


@app.get("/appel-icon.png")
async def appel_icon():
    return FileResponse(Path(__file__).resolve().parent / "app" / "static" / "appel-icon.png", media_type="image/png")


@app.websocket("/ws-web")
async def ws_web(websocket: WebSocket, code: str = "", g: str = ""):
    global _web_calls
    await websocket.accept()
    tenant = TENANTS.get(g or DEFAULT_TENANT)
    if not WEB_CALL_CODE or not tenant or not hmac.compare_digest(code.encode(), WEB_CALL_CODE.encode()):
        logger.warning("Appel navigateur refusé (code ou garage invalide)")
        await websocket.close(code=4003)
        return
    if _web_calls >= MAX_WEB_CALLS:
        await websocket.close(code=4029)
        return
    _web_calls += 1
    try:
        transport = FastAPIWebsocketTransport(
            websocket=websocket,
            params=FastAPIWebsocketParams(
                audio_in_enabled=True,
                audio_out_enabled=True,
                add_wav_header=False,
                session_timeout=MAX_CALL_SECONDS,
                serializer=WebPCMSerializer(),
            ),
        )
        await run_bot(transport, tenant, BACKEND, caller_number=None, call_sid=None, sample_rate=WEB_SAMPLE_RATE)
    finally:
        _web_calls -= 1


if __name__ == "__main__":
    if not PUBLIC_HOST or not STREAM_SECRET:
        raise SystemExit("PUBLIC_HOST et STREAM_SECRET doivent être définis dans .env")
    if VERIFY_TWILIO and not TWILIO_AUTH_TOKEN:
        raise SystemExit("TWILIO_AUTH_TOKEN manquant : impossible de vérifier que les appels viennent de Twilio")
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8765")), proxy_headers=True)
