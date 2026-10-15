# Mise en ligne sur un VPS

Compte environ 1 h la première fois.

## 1. Les clés

| Service | Rôle | Où | Dans `server/.env` |
|---|---|---|---|
| Deepgram | transcription | [console.deepgram.com](https://console.deepgram.com) → API Keys | `DEEPGRAM_API_KEY` |
| Cartesia | voix | [play.cartesia.ai](https://play.cartesia.ai) → API Keys. Dans Voices, filtre **French**, copie l'ID d'une voix | `CARTESIA_API_KEY`, `CARTESIA_VOICE_ID` |
| LLM | cerveau | [Mistral](https://console.mistral.ai), [Gemini](https://aistudio.google.com/apikey) ou [Anthropic](https://console.anthropic.com) | `LLM_PROVIDER`, `LLM_MODEL` + la clé |
| Twilio (optionnel) | vrai numéro | [console.twilio.com](https://console.twilio.com). Un compte d'essai joue un message « trial » et n'appelle que des numéros vérifiés | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` |

Génère aussi `STREAM_SECRET` (`openssl rand -hex 32`) et choisis un `WEB_CALL_CODE` pour la page `/appel`.

## 2. DNS
Chez ton registrar : enregistrement **A** `voice` → IP du VPS. Tu obtiens par exemple `voice.tondomaine.com`. Mets-le dans `PUBLIC_HOST` (sans `https://`).

## 3. Installer
```bash
git clone <URL-de-ce-repo> ~/agent-vocal && cd ~/agent-vocal/server
cp .env.example .env && nano .env
cd .. && docker compose up -d --build
docker compose logs -f voice-agent      # doit afficher "Garages chargés : dentiste-demo, garage-demo, restaurant-demo"
curl http://127.0.0.1:8765/health
```
Laisse `N8N_WEBHOOK_BASE` vide au premier test : l'agenda est simulé dans `server/data/`.

## 4. HTTPS
- **Rien n'écoute sur les ports 80/443** : mets ton domaine dans `deploy/Caddyfile`, puis `docker compose --profile caddy up -d`.
- **Tu as déjà un Caddy** : ajoute `voice.tondomaine.com { reverse_proxy 127.0.0.1:8765 }` (ou `voice-agent:8765` si ton Caddy est dans Docker sur le même réseau), puis recharge-le.
- **Nginx** : `proxy_pass http://127.0.0.1:8765;` + `proxy_http_version 1.1; proxy_set_header Upgrade $http_upgrade; proxy_set_header Connection "upgrade";` (obligatoire pour le WebSocket), puis certbot.

Test : `https://voice.tondomaine.com/health` → `{"ok":true,...}`, puis `https://voice.tondomaine.com/appel?g=dentiste-demo`.

## 5. n8n (vrai agenda + mails)
1. n8n → Import from file : `n8n/agent-vocal-agenda.json` et `n8n/agent-vocal-notifications.json`
2. Credentials :
   - **Header Auth** : Name `X-Voice-Secret`, Value = un secret (le même que `N8N_WEBHOOK_SECRET`). Sélectionne-le dans tous les nœuds Webhook.
   - **Google Calendar OAuth2** → nœuds « Google Agenda – … »
   - **Gmail** → nœuds « Mail … »
   - **Twilio** → nœud « SMS de confirmation » (désactivé par défaut)
3. Dans les nœuds Code, édite le bloc `GARAGES` : un ID d'agenda Google par client (Paramètres de l'agenda → « Intégrer l'agenda »), et l'e-mail qui reçoit les notifications.
4. Active les 2 workflows.
5. Dans `.env` : `N8N_WEBHOOK_BASE=https://<ton-n8n>/webhook` (ou `http://n8n:5678/webhook` si n8n est sur le même réseau Docker), `N8N_WEBHOOK_SECRET=...`, `NOTIFY_EMAIL=...`, puis `docker compose up -d`.

Astuce : un événement « journée entière » dans l'agenda = fermé ce jour-là (congés, férié).

Diagnostic complet : `docker exec -i voice-agent python - < server/check.py`

## 6. Twilio (optionnel)
Phone Numbers → ton numéro → Voice Configuration → « A call comes in » : Webhook `https://voice.tondomaine.com/twilio/voice`, HTTP POST. Mets ce numéro dans le YAML du client (`phone_numbers`), puis `docker compose restart voice-agent`.

## Dépannage
- **La page /appel renvoie 404** : `WEB_CALL_CODE` est vide, ou `g=` ne correspond à aucun YAML.
- **Ça sonne mais silence** : le WebSocket ne passe pas le proxy (en-têtes Upgrade/Connection avec Nginx).
- **Twilio : « an application error has occurred »** : `docker compose logs voice-agent`. Un `403 Signature Twilio invalide` = `PUBLIC_HOST` ne correspond pas exactement à l'URL configurée dans Twilio.
- **Réponses lentes** : regarde les lignes `TTFB` dans les logs pour trouver la brique lente. Essaie un modèle plus rapide (`LLM_PROVIDER=google` avec un modèle Flash-Lite, par exemple).
