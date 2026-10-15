#!/usr/bin/env bash
# Assistant de configuration : remplit server/.env en posant les questions une par une.
# Usage (depuis le dossier agent-vocal) :  bash server/configure.sh
# Relançable à volonté : appuie sur Entrée pour garder la valeur actuelle.
set -e
cd "$(dirname "$0")"
[ -f .env ] || cp .env.example .env

get() { grep -E "^$1=" .env | head -1 | cut -d= -f2-; }
set_env() { python3 - "$1" "$2" <<'PY'
import sys, re
k, v = sys.argv[1], sys.argv[2].strip()
s = open('.env', encoding='utf-8').read()
if re.search(rf'^{k}=', s, re.M):
    s = re.sub(rf'^{k}=.*$', lambda m: f'{k}={v}', s, flags=re.M)
else:
    s += f'\n{k}={v}\n'
open('.env', 'w', encoding='utf-8').write(s)
PY
}
ask() {  # ask CLE "Question" [secret]
  local cur; cur="$(get "$1")"
  local hint=""; [ -n "$cur" ] && hint=" [Entrée = garder ${cur:0:4}…]"
  read -rp "$2$hint : " v
  [ -n "$v" ] && set_env "$1" "$v"
  return 0
}

echo "=== Configuration de l'agent vocal ==="
echo
# Métier : choisit le client de démo (server/tenants/garage-demo.yaml, dentiste-demo.yaml ou restaurant-demo.yaml)
case "$(get DEFAULT_TENANT)" in dentiste-demo) def=d ;; restaurant-demo) def=r ;; *) def=g ;; esac
read -rp "Métier : g = garage, d = cabinet dentaire, r = restaurant [$def] : " metier
case "${metier:-$def}" in
  d) set_env DEFAULT_TENANT dentiste-demo ;;
  r) set_env DEFAULT_TENANT restaurant-demo ;;
  *) set_env DEFAULT_TENANT garage-demo ;;
esac
echo
ask PUBLIC_HOST "Sous-domaine de l'agent, sans https:// (ex. voice.mondomaine.com)"
ask DEEPGRAM_API_KEY "Clé Deepgram"
ask CARTESIA_API_KEY "Clé Cartesia"
ask CARTESIA_VOICE_ID "ID de la voix Cartesia (voix française)"

echo
read -rp "Modèle IA : g = Gemini (gratuit pour tester), c = Claude Haiku [g] : " choix
if [ "$choix" = "c" ]; then
  set_env LLM_PROVIDER anthropic; set_env LLM_MODEL claude-haiku-4-5
  ask ANTHROPIC_API_KEY "Clé API Anthropic (console.anthropic.com)"
else
  [ "$(get LLM_PROVIDER)" = "google" ] || set_env LLM_MODEL gemini-3.5-flash-lite
  set_env LLM_PROVIDER google
  ask LLM_MODEL "ID du modèle Gemini (Entrée = garder ${LLM_MODEL:-gemini-3.5-flash-lite})"
  ask GOOGLE_API_KEY "Clé Google AI Studio"
fi

echo
ask NOTIFY_EMAIL "E-mail qui reçoit les nouveaux RDV et rapports d'appel"
read -rp "URL de ton n8n (ex. https://n8n.mondomaine.com) — Entrée = agenda simulé pour l'instant : " n8n
if [ -n "$n8n" ]; then set_env N8N_WEBHOOK_BASE "${n8n%/}/webhook"; fi

echo
echo "Twilio (vrai numéro de téléphone) — Entrée pour passer, la page d'appel web marche sans."
ask TWILIO_ACCOUNT_SID "Twilio Account SID (AC...)"
ask TWILIO_AUTH_TOKEN "Twilio Auth Token"

# Secrets générés automatiquement (une seule fois)
[ -n "$(get STREAM_SECRET)" ] || set_env STREAM_SECRET "$(openssl rand -hex 32)"
[ -n "$(get N8N_WEBHOOK_SECRET)" ] || set_env N8N_WEBHOOK_SECRET "$(openssl rand -hex 8)"
[ -n "$(get WEB_CALL_CODE)" ] || set_env WEB_CALL_CODE "$(openssl rand -hex 4)"

echo
echo "=== Récapitulatif (clés masquées) ==="
grep -E '^(DEFAULT_TENANT|PUBLIC_HOST|LLM_PROVIDER|LLM_MODEL|N8N_WEBHOOK_BASE|NOTIFY_EMAIL|DEEPGRAM_API_KEY|CARTESIA_API_KEY|CARTESIA_VOICE_ID|GOOGLE_API_KEY|ANTHROPIC_API_KEY|TWILIO_ACCOUNT_SID|TWILIO_AUTH_TOKEN)=' .env \
  | sed -E 's/^([A-Z0-9_]*(KEY|TOKEN|VOICE_ID|SID))=(.{4}).+/\1=\3…(ok)/; s/=$/= (vide)/'
echo
echo "🔑 Secret à coller dans n8n (credential Header Auth, Name = X-Voice-Secret) :"
echo "   $(get N8N_WEBHOOK_SECRET)"
echo "📞 Ta page d'appel : https://$(get PUBLIC_HOST)/appel?g=$(get DEFAULT_TENANT)&code=$(get WEB_CALL_CODE)"
