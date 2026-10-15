"""Tests sans clé API : créneaux, outils (agenda mock), sécurité du serveur.

Lancer :  python -m pytest -q
"""

import asyncio
import base64
import hashlib
import hmac
import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

os.environ.setdefault("PUBLIC_HOST", "voice.example.com")
os.environ.setdefault("STREAM_SECRET", "test-secret")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ["N8N_WEBHOOK_BASE"] = ""

from app.backend import MockBackend  # noqa: E402
from app.slots import find_slots, fr_datetime, is_slot_free  # noqa: E402
from app.tenants import load_tenants, tenant_for_number  # noqa: E402
from app import tools  # noqa: E402

TZ = ZoneInfo("Europe/Paris")
T = load_tenants()["garage-demo"]
# lundi 28 septembre 2026, 7h
NOW = datetime(2026, 9, 28, 7, 0, tzinfo=TZ)


def test_tenant_lookup():
    assert tenant_for_number(load_tenants(), T.phone_numbers[0]).id == "garage-demo"
    assert tenant_for_number(load_tenants(), "+33999") is None


def test_three_spread_slots_on_requested_day():
    res = find_slots(T, 60, [], NOW, date(2026, 9, 29))
    assert res["requested_date_available"] is True
    assert len(res["slots"]) == 3
    hours = [datetime.fromisoformat(s["start"]).hour for s in res["slots"]]
    assert hours[0] == 8 and hours[-1] >= 16  # étalés sur la journée
    assert res["slots"][0]["label"] == "mardi 29 septembre à 8 heures"


def test_min_notice_and_closed_sunday():
    # dimanche fermé -> bascule sur lundi
    res = find_slots(T, 60, [], NOW, date(2026, 10, 4))
    assert res["requested_date_available"] is False
    assert res["date"] == "2026-10-05"
    # aujourd'hui 7h + 3h de délai -> pas avant 10h
    res = find_slots(T, 30, [], NOW, date(2026, 9, 28))
    assert datetime.fromisoformat(res["slots"][0]["start"]) >= NOW + timedelta(hours=3)


def test_period_and_lunch_break():
    res = find_slots(T, 90, [], NOW, date(2026, 9, 29), "apres-midi")
    for s in res["slots"]:
        st = datetime.fromisoformat(s["start"])
        assert st.hour >= 14 and st + timedelta(minutes=90) <= st.replace(hour=18, minute=0)
    res = find_slots(T, 90, [], NOW, date(2026, 9, 29), "matin")
    for s in res["slots"]:
        st = datetime.fromisoformat(s["start"])
        assert st + timedelta(minutes=90) <= st.replace(hour=12, minute=0)  # pas à cheval sur midi


def test_capacity():
    start = datetime(2026, 9, 29, 9, 0, tzinfo=TZ)
    one = [(start, start + timedelta(hours=1))]
    assert is_slot_free(T, start, 60, one, NOW)  # capacité 2 -> encore une place
    assert not is_slot_free(T, start, 60, one * 2, NOW)
    assert not is_slot_free(T, start.replace(hour=13), 60, [], NOW)  # pause déjeuner


class FakeParams:
    def __init__(self, session):
        self.app_resources = session
        self.result = None

    async def result_callback(self, result, properties=None):
        self.result = result


def test_tools_book_flow(tmp_path, monkeypatch):
    backend = MockBackend(tmp_path)
    s = tools.CallSession(tenant=T, backend=backend, caller_number="+33611111111")
    monkeypatch.setattr(tools.CallSession, "now", lambda self: NOW)

    p = FakeParams(s)
    asyncio.run(tools.check_availability(p, "freinage", "2026-09-29", ""))
    slot = p.result["slots"][0]
    assert p.result["service_id"] == "freinage"

    p2 = FakeParams(s)
    asyncio.run(tools.book_appointment(p2, slot["start"], "freinage", "Jean Dupont", "Peugeot 308 2018", "plaquettes"))
    assert p2.result["ok"] is True and s.appointments[0]["customer_phone"] == "+33611111111"

    # relance identique -> pas de doublon
    p3 = FakeParams(s)
    asyncio.run(tools.book_appointment(p3, slot["start"], "freinage", "Jean Dupont", "Peugeot 308 2018", "plaquettes"))
    assert p3.result.get("already_booked") is True

    # 2e client même créneau (capacité 2) ok, 3e refusé
    for i, expected in [(2, True), (3, False)]:
        other = tools.CallSession(tenant=T, backend=backend, caller_number=f"+3361000000{i}")
        p = FakeParams(other)
        asyncio.run(tools.book_appointment(p, slot["start"], "freinage", f"Client {i}", "Clio", "freins"))
        assert p.result["ok"] is expected


def test_twilio_signature_and_stream_token():
    import server

    url = "https://voice.example.com/twilio/voice"
    form = {"CallSid": "CA1", "From": "+33611111111", "To": T.phone_numbers[0]}
    payload = url + "".join(k + form[k] for k in sorted(form))
    sig = base64.b64encode(hmac.new(b"test-token", payload.encode(), hashlib.sha1).digest()).decode()
    assert server.twilio_signature_ok(url, form, sig)
    assert not server.twilio_signature_ok(url, {**form, "To": "+33999"}, sig)

    tok = server.make_stream_token("CA1", "garage-demo")
    assert server.check_stream_token(tok, "CA1", "garage-demo")
    assert not server.check_stream_token(tok, "CA2", "garage-demo")
    assert not server.check_stream_token("garbage", "CA1", "garage-demo")


def test_twiml_endpoint():
    from fastapi.testclient import TestClient

    import server

    server.VERIFY_TWILIO = False
    c = TestClient(server.app)
    r = c.post("/twilio/voice", data={"CallSid": "CA1", "From": "+33611111111", "To": T.phone_numbers[0]})
    assert r.status_code == 200
    assert 'wss://voice.example.com/ws' in r.text and 'name="tenant_id" value="garage-demo"' in r.text
    r = c.post("/twilio/voice", data={"CallSid": "CA1", "From": "+336", "To": "+33999"})
    assert "pas encore configuré" in r.text
    assert c.get("/health").json()["ok"] is True


def test_busy_parsing_all_day_and_naive():
    from app.backend import _to_busy

    busy = _to_busy([{"start": "2026-10-05", "end": "2026-10-06"}, {"start": "bad"}], "Europe/Paris", 2)
    assert len(busy) == 2 and busy[0][0].tzinfo is not None
    # journée entière (congés) -> bloque toute la capacité, le lundi 5 n'a plus de créneaux
    res = find_slots(T, 60, busy, NOW, date(2026, 10, 5))
    assert res["requested_date_available"] is False


def test_normalize_phone_and_naive_start(tmp_path, monkeypatch):
    assert tools.normalize_phone("06 12 34 56 78") == "+33612345678"
    assert tools.normalize_phone("0033 6 12 34 56 78") == "+33612345678"
    assert tools.normalize_phone("+33 6-12-34-56-78") == "+33612345678"
    assert tools.normalize_phone("douze") == ""
    # heure sans fuseau = heure du garage, quel que soit le fuseau du serveur
    assert tools._parse_start("2026-09-29T09:00:00", "Europe/Paris").hour == 9
    assert tools._parse_start("2026-09-29T07:00:00+00:00", "Europe/Paris").hour == 9

    monkeypatch.setattr(tools.CallSession, "now", lambda self: NOW)
    s = tools.CallSession(tenant=T, backend=MockBackend(tmp_path), caller_number="+33611111111")
    p = FakeParams(s)
    asyncio.run(tools.book_appointment(p, "2026-09-29T09:00:00", "vidange", "A B", "Clio", "vidange", "", "07 00 00 00 01"))
    assert p.result["ok"] and p.result["label"] == "mardi 29 septembre à 9 heures"
    assert s.appointments[-1]["customer_phone"] == "+33700000001"
    p = FakeParams(s)
    asyncio.run(tools.book_appointment(p, "2026-09-29T10:00:00", "vidange", "A B", "Clio", "vidange", "", "",
                                       "a b arobase gmail point com"))
    assert p.result["email_confirmation"] == "ab@gmail.com"


def test_date_too_far_or_past():
    res = find_slots(T, 60, [], NOW, date(2027, 3, 1))
    assert res["too_far"] and res["slots"] == []
    res = find_slots(T, 60, [], NOW, date(2026, 1, 1))  # passée -> ignorée
    assert res["slots"] and res["requested_date_available"] is None


def test_web_serializer_and_page():
    from fastapi.testclient import TestClient
    from pipecat.frames.frames import EndFrame, InputAudioRawFrame, InterruptionFrame, OutputAudioRawFrame

    from app.webcall import WebPCMSerializer
    import server

    s = WebPCMSerializer()
    f = asyncio.run(s.deserialize(b"\x01\x00\x02\x00\x03"))
    assert isinstance(f, InputAudioRawFrame) and len(f.audio) == 4 and f.sample_rate == 16000
    assert asyncio.run(s.serialize(OutputAudioRawFrame(audio=b"\x00\x01", sample_rate=16000, num_channels=1))) == b"\x00\x01"
    assert asyncio.run(s.serialize(InterruptionFrame())) == '{"type": "clear"}'
    assert asyncio.run(s.serialize(EndFrame())) == '{"type": "end"}'

    c = TestClient(server.app)
    server.WEB_CALL_CODE = ""
    assert c.get("/appel").status_code == 404  # désactivée sans code
    server.WEB_CALL_CODE = "abc"
    r = c.get("/appel")
    assert r.status_code == 200 and "<title>Garage Démo Auto</title>" in r.text


def test_end_call_blocked_right_after_booking(tmp_path, monkeypatch):
    import time as _t

    monkeypatch.setattr(tools.CallSession, "now", lambda self: NOW)
    s = tools.CallSession(tenant=T, backend=MockBackend(tmp_path), caller_number="+33611111111")
    s.last_booking_at = _t.monotonic()
    p = FakeParams(s)
    asyncio.run(tools.end_call(p))
    assert p.result["ok"] is False and "confirmation" in p.result["error"]
    s.booking_in_progress = True
    s.last_booking_at = 0
    p = FakeParams(s)
    asyncio.run(tools.end_call(p))
    assert p.result["ok"] is False


def test_end_call_says_goodbye_when_silent(tmp_path, monkeypatch):
    frames = []

    class FakeLLM:
        async def push_frame(self, f):
            frames.append(f)

    class Ctx:
        def get_messages(self):
            return [{"role": "assistant", "content": "C'est noté."}]

    s = tools.CallSession(tenant=T, backend=MockBackend(tmp_path))
    p = FakeParams(s)
    p.llm, p.context = FakeLLM(), Ctx()
    asyncio.run(tools.end_call(p))
    from pipecat.frames.frames import EndWorkerFrame, TTSSpeakFrame
    assert isinstance(frames[0], TTSSpeakFrame) and "revoir" in frames[0].text.lower()
    assert isinstance(frames[-1], EndWorkerFrame)

    frames.clear()
    Ctx.get_messages = lambda self: [{"role": "assistant", "content": "Bonne journée, au revoir !"}]
    asyncio.run(tools.end_call(p))
    assert len(frames) == 1 and isinstance(frames[0], EndWorkerFrame)


def test_normalize_email():
    assert tools.normalize_email("Jean point Dupont arobase gmail point com") == "jean.dupont@gmail.com"
    assert tools.normalize_email("jean.dupont@gmail.com") == "jean.dupont@gmail.com"
    assert tools.normalize_email("jean tiret paul arobase orange point fr") == "jean-paul@orange.fr"
    assert tools.normalize_email("contact at garage dot fr") == "contact@garage.fr"
    assert tools.normalize_email("Jean Dupont arobase gmail") == ""
    assert tools.normalize_email("") == ""


# ── Cabinet dentaire (profil "dentiste") ────────────────────────────────────

D = load_tenants()["dentiste-demo"]


def test_dentiste_profile_prompt_and_tools():
    from app.prompt import build_system_prompt
    from app.tools_dentiste import book_appointment as book_d

    assert T.profile == "garage" and D.profile == "dentiste"
    p = build_system_prompt(D, NOW, "+33611111111")
    assert "Chloé" in p and "conseil médical" in p and "le 15" in p and "véhicule" not in p.lower()
    assert "SMS" not in p  # confirmations.sms: false
    assert "garage" not in p.lower()
    assert "véhicule" in build_system_prompt(T, NOW, None)
    names = [f.__name__ for f in tools.tools_for(D)]
    assert names == ["check_availability", "book_appointment", "take_message", "transfer_to_human", "end_call"]
    assert book_d in tools.tools_for(D) and tools.book_appointment in tools.tools_for(T)
    assert "cabinet" in D.goodbye


def test_dentiste_book_flow_capacity_one(tmp_path, monkeypatch):
    from app.tools_dentiste import book_appointment as book_d

    backend = MockBackend(tmp_path)
    s = tools.CallSession(tenant=D, backend=backend, caller_number="+33611111111")
    monkeypatch.setattr(tools.CallSession, "now", lambda self: NOW)

    p = FakeParams(s)
    asyncio.run(tools.check_availability(p, "urgence", "2026-09-28", ""))
    slot = p.result["slots"][0]
    assert p.result["service_id"] == "urgence"
    assert slot["start"].startswith("2026-09-28T09:00")  # 7h + 1h de délai, ouverture 9h

    p2 = FakeParams(s)
    asyncio.run(book_d(p2, slot["start"], "urgence", "Jean Dupont", "douleur dent du bas", False,
                       customer_email="jean point dupont arobase gmail point com"))
    assert p2.result["ok"] is True and p2.result["email_confirmation"] == "jean.dupont@gmail.com"
    appt = s.appointments[0]
    assert appt["new_patient"] is False and appt["vehicle"] == "" and appt["customer_name"] == "Jean Dupont"
    assert s.booking_in_progress is False and s.last_booking_at > 0  # garde-fou end_call actif

    # un seul fauteuil : un 2e patient sur le même créneau est refusé
    other = tools.CallSession(tenant=D, backend=backend, caller_number="+33622222222")
    p3 = FakeParams(other)
    asyncio.run(book_d(p3, slot["start"], "urgence", "Autre Patient", "douleur", True))
    assert p3.result["ok"] is False


def test_appel_page_dentiste():
    from fastapi.testclient import TestClient
    import server

    server.WEB_CALL_CODE = "abc"
    c = TestClient(server.app)
    r = c.get("/appel?g=dentiste-demo")
    assert r.status_code == 200 and "Cabinet dentaire du Docteur Martin" in r.text
    assert "accueil du cabinet est assuré par une IA" in r.text and "{{" not in r.text
    assert "accueil du garage" in c.get("/appel?g=garage-demo").text


# ── Restaurant (profil "restaurant") ────────────────────────────────────────

R = load_tenants()["restaurant-demo"]
# mardi 29 septembre 2026, 10h
NOW_R = datetime(2026, 9, 29, 10, 0, tzinfo=TZ)


def test_restaurant_profile_prompt_and_tools():
    from app.prompt import build_system_prompt
    from app.tools_restaurant import book_appointment as book_r

    assert R.profile == "restaurant"
    p = build_system_prompt(R, NOW_R, "+33611111111")
    assert "Camille" in p and "personnes" in p and "allergie" in p.lower() and "8 personnes" in p
    assert "garage" not in p.lower() and "véhicule" not in p.lower() and "cabinet" not in p.lower()
    assert "SMS" not in p and "90 minutes" in p
    names = [f.__name__ for f in tools.tools_for(R)]
    assert names == ["check_availability", "book_appointment", "take_message", "transfer_to_human", "end_call"]
    assert book_r in tools.tools_for(R)
    assert "restaurant" in R.goodbye and "assistante virtuelle" in R.greeting


def test_restaurant_lunch_dinner_and_last_seating():
    # midi : 12h-15h avec des tables de 90 min -> arrivées 12h à 13h30 ; soir : 19h à 21h30
    midi = find_slots(R, 90, [], NOW_R, date(2026, 9, 29), "midi")
    soir = find_slots(R, 90, [], NOW_R, date(2026, 9, 29), "soir")
    assert all(datetime.fromisoformat(s["start"]).hour in (12, 13) for s in midi["slots"])
    hours = [datetime.fromisoformat(s["start"]).strftime("%H:%M") for s in soir["slots"]]
    assert hours[0] == "19:00" and hours[-1] == "21:30"
    # lundi fermé : on bascule sur le mardi suivant
    res = find_slots(R, 90, [], NOW_R, date(2026, 10, 5), "soir")
    assert res["requested_date_available"] is False and res["date"] == "2026-10-06"


def test_preferred_time_returns_nearest_slots():
    from datetime import time as dtime

    res = find_slots(R, 90, [], NOW_R, date(2026, 10, 3), "soir", dtime(20, 0))
    assert res["requested_time_available"] is True
    assert [s["start"][11:16] for s in res["slots"]] == ["19:30", "20:00", "20:30"]
    # 20h complet (10 tables) : on le dit et on propose autour
    full = [(datetime(2026, 10, 3, 20, 0, tzinfo=TZ), datetime(2026, 10, 3, 21, 30, tzinfo=TZ))] * 10
    res = find_slots(R, 90, full, NOW_R, date(2026, 10, 3), "soir", dtime(20, 0))
    assert res["requested_time_available"] is False
    assert "20:00" not in [s["start"][11:16] for s in res["slots"]]
    # sans heure précise : comportement inchangé (créneaux étalés), pas de clé en plus
    assert "requested_time_available" not in find_slots(R, 90, [], NOW_R, date(2026, 10, 3), "soir")


def test_restaurant_book_flow(tmp_path, monkeypatch):
    from app.tools_restaurant import book_appointment as book_r

    backend = MockBackend(tmp_path)
    s = tools.CallSession(tenant=R, backend=backend, caller_number="+33611111111")
    monkeypatch.setattr(tools.CallSession, "now", lambda self: NOW_R)

    p = FakeParams(s)
    asyncio.run(tools.check_availability(p, "table", "2026-10-03", "soir", "20:00"))
    assert p.result["requested_time_available"] is True and "note" not in p.result
    start = next(x["start"] for x in p.result["slots"] if x["start"][11:16] == "20:00")

    # groupe trop grand : refusé, l'agent doit prendre un message
    p_big = FakeParams(s)
    asyncio.run(book_r(p_big, start, "table", "Jean Dupont", 12))
    assert p_big.result["ok"] is False and "take_message" in p_big.result["error"] and not s.appointments

    p2 = FakeParams(s)
    asyncio.run(book_r(p2, start, "table", "Jean Dupont", 4, "une allergie aux arachides",
                       customer_email="jean point dupont arobase gmail point com"))
    assert p2.result["ok"] is True and p2.result["email_confirmation"] == "jean.dupont@gmail.com"
    appt = s.appointments[0]
    assert appt["party_size"] == 4 and appt["vehicle"] == ""
    assert appt["problem_description"] == "4 personnes. une allergie aux arachides"
    assert appt["end"].startswith("2026-10-03T21:30")
    assert s.booking_in_progress is False and s.last_booking_at > 0  # garde-fou end_call actif


def test_appel_page_restaurant():
    from fastapi.testclient import TestClient
    import server

    server.WEB_CALL_CODE = "abc"
    r = TestClient(server.app).get("/appel?g=restaurant-demo")
    assert r.status_code == 200 and "Le Comptoir des Halles" in r.text
    assert "accueil du restaurant est assuré par une IA" in r.text and "{{" not in r.text
