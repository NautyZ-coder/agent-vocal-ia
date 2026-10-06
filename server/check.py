"""Check complet avant un appel. Lancer :  docker exec -i voice-agent python - < check.py
(ou coller le contenu via heredoc). Ne modifie rien, sauf le test 7 qui crée un événement TEST dans l'agenda.
"""
import base64, hashlib, hmac, json, os, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

OK, KO = "✅", "❌"
results = []


def check(name, fn):
    try:
        msg = fn()
        results.append((OK, name, msg or ""))
    except Exception as e:
        detail = e.read().decode()[:200] if isinstance(e, urllib.error.HTTPError) else str(e)[:200]
        code = f"HTTP {e.code} " if isinstance(e, urllib.error.HTTPError) else ""
        results.append((KO, name, code + detail))
    print(*results[-1])


def http(url, data=None, headers=None, method=None, timeout=20):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


E = os.environ.get

# 1. Config
def c_env():
    need = ["PUBLIC_HOST", "STREAM_SECRET", "DEEPGRAM_API_KEY", "CARTESIA_API_KEY", "LLM_PROVIDER", "LLM_MODEL",
            "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN"]
    if E("LLM_PROVIDER") == "openai":
        need += ["OPENAI_API_KEY", "OPENAI_BASE_URL"]
    missing = [k for k in need if not E(k)]
    if missing:
        raise RuntimeError("manquant dans .env : " + ", ".join(missing))
    return f"LLM = {E('LLM_PROVIDER')} / {E('LLM_MODEL')} · n8n = {E('N8N_WEBHOOK_BASE') or 'MOCK'}"


def c_tenant():
    from app.tenants import load_tenants
    t = load_tenants()[E("DEFAULT_TENANT", "garage-demo")]
    voice = t.voice_id or E("CARTESIA_VOICE_ID")
    if not voice:
        raise RuntimeError("aucune voix Cartesia (CARTESIA_VOICE_ID vide)")
    return f"{t.name} · fuseau {t.timezone} · numéros {t.phone_numbers}"


# 2. Cerveau : réponse + appel d'outil
def c_llm():
    p = E("LLM_PROVIDER")
    if p == "openai" and "generativelanguage.googleapis.com" in E("OPENAI_BASE_URL", ""):
        p = "google"  # même bascule automatique que bot.py
    tools = [{"type": "function", "function": {
        "name": "check_availability", "description": "Cherche les créneaux libres de l'atelier.",
        "parameters": {"type": "object", "properties": {"service_id": {"type": "string"}, "preferred_date": {"type": "string"}},
                       "required": ["service_id"]}}}]
    msgs = [{"role": "system", "content": "Tu es la secrétaire d'un garage. Utilise check_availability avant de proposer une heure."},
            {"role": "user", "content": "Bonjour, je voudrais un rendez-vous pour une vidange mardi prochain."}]
    t0 = time.time()
    if p == "openai":
        body = json.dumps({"model": E("LLM_MODEL"), "messages": msgs, "tools": tools}).encode()
        _, raw = http(E("OPENAI_BASE_URL").rstrip("/") + "/chat/completions", body,
                      {"Authorization": "Bearer " + E("OPENAI_API_KEY"), "Content-Type": "application/json"})
        m = json.loads(raw)["choices"][0]["message"]
        called = [c["function"]["name"] for c in (m.get("tool_calls") or [])]
    elif p == "anthropic":
        body = json.dumps({"model": E("LLM_MODEL"), "max_tokens": 200, "system": msgs[0]["content"], "messages": msgs[1:],
                           "tools": [{"name": "check_availability", "description": tools[0]["function"]["description"],
                                      "input_schema": tools[0]["function"]["parameters"]}]}).encode()
        _, raw = http("https://api.anthropic.com/v1/messages", body,
                      {"x-api-key": E("ANTHROPIC_API_KEY"), "anthropic-version": "2023-06-01", "content-type": "application/json"})
        called = [b["name"] for b in json.loads(raw)["content"] if b["type"] == "tool_use"]
    elif p == "google":
        model = E("LLM_MODEL")
        body = json.dumps({"systemInstruction": {"parts": [{"text": msgs[0]["content"]}]},
                           "contents": [{"role": "user", "parts": [{"text": msgs[1]["content"]}]}],
                           "tools": [{"functionDeclarations": [tools[0]["function"]]}]}).encode()
        key = E("GOOGLE_API_KEY") or E("OPENAI_API_KEY")
        _, raw = http(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}", body,
                      {"Content-Type": "application/json"})
        parts = json.loads(raw)["candidates"][0]["content"].get("parts", [])
        called = [pt["functionCall"]["name"] for pt in parts if "functionCall" in pt]
    else:
        return "fournisseur non testé ici"
    dt = time.time() - t0
    if "check_availability" not in called:
        raise RuntimeError("le modèle répond mais N'APPELLE PAS l'outil de dispos → prendre un modèle plus fort")
    return f"appelle bien l'outil de dispos ({dt:.1f} s)"


# 3. Oreilles
def c_deepgram():
    http("https://api.deepgram.com/v1/projects", headers={"Authorization": "Token " + E("DEEPGRAM_API_KEY")})
    return "clé valide"


# 4. Voix : synthèse d'une vraie phrase en français avec TA voix
def c_cartesia():
    from app.tenants import load_tenants
    from pipecat.services.cartesia.tts import _CARTESIA_API_VERSION
    t = load_tenants()[E("DEFAULT_TENANT", "garage-demo")]
    body = json.dumps({"model_id": "sonic-3.6", "transcript": "Garage Démo Auto, bonjour !", "language": "fr",
                       "voice": {"mode": "id", "id": t.voice_id or E("CARTESIA_VOICE_ID")},
                       "output_format": {"container": "raw", "encoding": "pcm_s16le", "sample_rate": 16000}}).encode()
    _, raw = http("https://api.cartesia.ai/tts/bytes", body,
                  {"X-API-Key": E("CARTESIA_API_KEY"), "Cartesia-Version": _CARTESIA_API_VERSION, "Content-Type": "application/json"})
    return f"voix OK ({len(raw) / 32000:.1f} s d'audio générées)"


# 5-7. n8n
def n8n(path, payload):
    _, raw = http(E("N8N_WEBHOOK_BASE").rstrip("/") + "/" + path, json.dumps(payload).encode(),
                  {"Content-Type": "application/json", "X-Voice-Secret": E("N8N_WEBHOOK_SECRET", "")})
    return json.loads(raw or b"{}")


def c_n8n_list():
    if not E("N8N_WEBHOOK_BASE"):
        return "agenda MOCK (n8n pas branché)"
    now = datetime.now(timezone.utc)
    r = n8n("voice-events-list", {"tenant_id": E("DEFAULT_TENANT", "garage-demo"),
                                  "time_min": now.isoformat(), "time_max": (now + timedelta(days=7)).isoformat()})
    return f"lecture agenda OK ({len(r.get('events', []))} événements sur 7 jours)"


def c_n8n_create():
    if not E("N8N_WEBHOOK_BASE"):
        return "agenda MOCK (n8n pas branché)"
    from app.tenants import load_tenants
    from zoneinfo import ZoneInfo
    t = load_tenants()[E("DEFAULT_TENANT", "garage-demo")]
    start = (datetime.now(ZoneInfo(t.timezone)) + timedelta(days=10)).replace(hour=6, minute=0, second=0, microsecond=0)
    appt = {"start": start.isoformat(), "end": (start + timedelta(minutes=30)).isoformat(), "label": "TEST",
            "service_id": "vidange", "service_label": "TEST", "customer_name": "À SUPPRIMER", "customer_phone": "",
            "vehicle": "check agent vocal", "plate": "", "problem_description": "événement de test",
            "garage_name": t.name, "garage_address": t.address, "source": "check"}
    r = n8n("voice-events-create", {"tenant_id": t.id, "appointment": appt})
    if not r.get("ok"):
        raise RuntimeError(f"réponse inattendue : {r}")
    return f"écriture agenda OK → supprime l'événement 'TEST – À SUPPRIMER' du {start:%d/%m à %Hh}"


# 8-9. Twilio
def c_twilio_account():
    sid, tok = E("TWILIO_ACCOUNT_SID"), E("TWILIO_AUTH_TOKEN")
    auth = base64.b64encode(f"{sid}:{tok}".encode()).decode()
    _, raw = http(f"https://api.twilio.com/2010-04-01/Accounts/{sid}.json", headers={"Authorization": "Basic " + auth})
    return "compte " + json.loads(raw).get("status", "?")


def c_twilio_webhook():
    from app.tenants import load_tenants
    t = load_tenants()[E("DEFAULT_TENANT", "garage-demo")]
    url = f"https://{E('PUBLIC_HOST')}/twilio/voice"
    form = {"CallSid": "CAcheck", "From": "+33600000000", "To": t.phone_numbers[0]}
    payload = url + "".join(k + form[k] for k in sorted(form))
    sig = base64.b64encode(hmac.new(E("TWILIO_AUTH_TOKEN").encode(), payload.encode(), hashlib.sha1).digest()).decode()
    _, raw = http(url, urllib.parse.urlencode(form).encode(),
                  {"X-Twilio-Signature": sig, "Content-Type": "application/x-www-form-urlencoded"})
    txt = raw.decode()
    if "<Stream" not in txt:
        raise RuntimeError("pas de flux audio dans la réponse : " + txt[:150])
    return f"un appel vers {t.phone_numbers[0]} sera bien envoyé à l'agent"


check("1. Config .env", c_env)
check("2. Garage", c_tenant)
check("3. Cerveau (LLM + outils)", c_llm)
check("4. Oreilles (Deepgram)", c_deepgram)
check("5. Voix (Cartesia)", c_cartesia)
check("6. n8n lecture agenda", c_n8n_list)
check("7. n8n création RDV", c_n8n_create)
check("8. Twilio compte", c_twilio_account)
check("9. Twilio → agent (HTTPS + signature)", c_twilio_webhook)
ko = [r for r in results if r[0] == KO]
print("\n" + ("🎉 TOUT EST VERT, tu peux appeler." if not ko else f"⚠️  {len(ko)} point(s) à corriger avant d'appeler."))
