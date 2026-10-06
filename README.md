# Agent vocal IA – prise de rendez-vous (garage, cabinet dentaire…)

Une IA qui décroche le téléphone d'une petite entreprise, regarde le **vrai agenda**, propose des créneaux, prend le rendez-vous et prévient l'équipe par mail. Elle parle français et annonce dès le début qu'elle est une IA.

Deux métiers sont fournis en exemple :
- `garage-demo` : un garage (véhicule, immatriculation, travaux)
- `dentiste-demo` : un cabinet dentaire (urgences en priorité, jamais de conseil médical, renvoi vers le 15)

Un client = un fichier YAML. Tu en ajoutes autant que tu veux sur le même serveur.

```
Appel (téléphone Twilio OU page web /appel)
        │ audio en streaming (WebSocket)
        ▼
voice-agent (Python / Pipecat, Docker)
  Deepgram (oreilles) → LLM : Mistral / Gemini / Claude… (cerveau) → Cartesia (voix)
        │ outils : dispos, réservation, message, transfert, raccrocher
        ▼
n8n → Google Agenda + mails (nouveau RDV, confirmation client, rapport d'appel)
```

## Ce que fait l'agent
- Annonce qu'il est une IA dès l'accueil (AI Act, art. 50)
- Range la demande dans un type de RDV (chaque type a sa durée)
- Propose **3 créneaux max**, calculés depuis les horaires, la capacité (ponts, fauteuils…) et l'agenda réel
- Refuse une heure qui n'existe pas et repropose les vraies
- Récapitule et attend un « oui » avant de réserver. Revérifie le créneau au moment de réserver (pas de double réservation)
- Récupère l'e-mail dicté (« jean point dupont arobase gmail point com ») pour envoyer une confirmation
- Devis, réclamation, annulation… : prend un message, envoyé par mail
- Rapport de fin d'appel : issue, RDV, messages, transcription complète

## Prérequis

| Quoi | Pour quoi faire | Lien |
|---|---|---|
| Un VPS Linux avec Docker | héberger l'agent (et n8n) | n'importe quel hébergeur (Hostinger, Hetzner, OVH…) · [Docker](https://docs.docker.com/engine/install/) |
| Un sous-domaine | HTTPS obligatoire pour le micro et le téléphone | enregistrement DNS **A** → IP du VPS |
| n8n | agenda + mails | [n8n.io](https://n8n.io) (self-hosted ou cloud) |
| Deepgram | transcription (STT) | [console.deepgram.com](https://console.deepgram.com) |
| Cartesia | voix (TTS) | [play.cartesia.ai](https://play.cartesia.ai) |
| Un LLM, au choix | le cerveau | [Mistral](https://console.mistral.ai) · [Gemini](https://aistudio.google.com/apikey) · [Anthropic](https://console.anthropic.com) |
| Un compte Google | Google Agenda + Gmail (via n8n) | [Google Agenda](https://calendar.google.com) |
| Twilio (optionnel) | un vrai numéro de téléphone | [console.twilio.com](https://console.twilio.com) |

Sans Twilio, tu appelles l'agent depuis la page web `/appel` (écran d'appel type smartphone, protégé par un code). C'est parfait pour une démo.

## Démarrage rapide (sur ton ordinateur, 10 min, sans n8n)

```bash
git clone <URL-de-ce-repo> agent-vocal && cd agent-vocal/server
cp .env.example .env
# Dans .env, remplis au minimum :
#   PUBLIC_HOST=localhost   STREAM_SECRET=n_importe_quoi
#   DEEPGRAM_API_KEY, CARTESIA_API_KEY, CARTESIA_VOICE_ID (une voix "French")
#   LLM_PROVIDER + la clé correspondante
#   WEB_CALL_CODE=un-code-a-toi
#   N8N_WEBHOOK_BASE vide = agenda simulé dans server/data/
cd .. && docker compose up -d --build
```
Ouvre `http://localhost:8765/appel?g=dentiste-demo&code=un-code-a-toi`, autorise le micro, appelle.

## Mise en ligne sur un VPS
Le pas à pas complet est dans [docs/DEPLOY.md](docs/DEPLOY.md) : clés, HTTPS (Caddy fourni), import des 2 workflows n8n, Twilio (optionnel), tests.

## Ajouter un client
1. Copier `server/tenants/dentiste-demo.yaml` ou `garage-demo.yaml` → `server/tenants/<id>.yaml`. Adapter le nom, l'accueil, les horaires, les types de RDV, la FAQ et la voix.
2. Dans n8n, ajouter l'entrée `<id>` dans le bloc `GARAGES` des nœuds Code (ID d'agenda, e-mail de notification)
3. `docker compose restart voice-agent`
4. Tester : `https://<ton-domaine>/appel?g=<id>`

## Métiers (`profile` dans le YAML)
- `garage` (défaut) : véhicule, immatriculation, travaux
- `dentiste` : déjà patient ou non, motif, règles médicales

Un nouveau métier = un prompt dans `server/app/prompt.py` + (si les champs changent) un `book_appointment` dans `server/app/tools_<métier>.py`, branché dans `tools_for()`.

## Changer de modèle
Dans `.env` : `LLM_PROVIDER=mistral` (défaut, le moins cher), `google` (Gemini), `anthropic` (Claude), ou `openai` + `OPENAI_BASE_URL` pour tout fournisseur compatible OpenAI.

## Tests
```bash
cd server && pip install -r requirements-dev.txt
python -m pytest -q                                   # logique, sans clé API
DEFAULT_TENANT=dentiste-demo python bot.py -t eval    # terminal 1 (agenda simulé)
pipecat eval run evals/scenarios-dentiste.yaml -v     # terminal 2 : conversations en mode texte
```

## Structure
```
server/
  server.py            webhook Twilio, page /appel, WebSockets audio, sécurité
  bot.py               pipeline vocal STT → LLM → TTS
  check.py             diagnostic complet de la config (à lancer dans le conteneur)
  app/prompt.py        prompts (garage, dentiste) construits depuis le YAML
  app/tools.py         outils : check_availability, book_appointment, take_message, transfer_to_human, end_call
  app/tools_dentiste.py formulaire de RDV du cabinet
  app/slots.py         calcul des créneaux (pur, testé)
  app/backend.py       n8n (prod) ou agenda simulé (tests)
  app/webcall.py + app/static/appel.html   appel depuis le navigateur
  tenants/*.yaml       UN FICHIER PAR CLIENT
  evals/               conversations de test (mode texte)
  tests/               tests unitaires
n8n/                   2 workflows à importer
deploy/Caddyfile       HTTPS si tu n'as pas déjà de reverse proxy
docs/DEPLOY.md         mise en ligne pas à pas
```

## Légal (France / UE)
- L'agent annonce qu'il est une IA dès l'accueil. Garde cette phrase.
- Annonce l'enregistrement, fixe une durée de conservation des transcriptions, et signe un DPA avec tes fournisseurs (RGPD).
- Pour un cabinet de santé : ne collecte que le minimum (nom, contact, motif en une phrase). Le prompt l'impose déjà.

Ce code est un point de départ, pas un produit certifié. À toi de le tester sérieusement avant de le mettre devant de vrais clients.
