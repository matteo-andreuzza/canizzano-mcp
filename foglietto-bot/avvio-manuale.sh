#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  Un giro completo del foglietto, adesso: scarica e poi elabora.
#
#  Con Docker questo script non serve più: il bottone «Elabora il foglietto»
#  della dashboard del sito chiama direttamente l'endpoint HTTP /attiva del
#  container (vedi foglietto_bot/servizio.py), che fa la stessa cosa —
#  scraper, poi «scansiona» — internamente. Resta qui per l'installazione
#  «senza Docker» (venv + systemd, vedi README): lì il bottone della dashboard
#  non arriva fino a questa macchina, quindi un giro fuori programma si forza
#  lanciando questo script a mano.
#
#  Gli stessi timer che lo sostituiscono in automatico: lo scraper il sabato,
#  la scansione ogni mezz'ora, ciascuno per conto suo.
#
#  Si puo' lanciare anche a mano, dal terminale:  ./avvio-manuale.sh
# ─────────────────────────────────────────────────────────────────────────────
set -uo pipefail

CARTELLA="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$CARTELLA"

PYTHON="$CARTELLA/venv/bin/python"
[[ -x "$PYTHON" ]] || PYTHON="$(command -v python3)"

# Lo scraper e' del repository, non del pacchetto, e sta nella cartella sopra
# a questa — ma «sopra a questa» vuol dire due posti diversi. Nel checkout di
# sviluppo e' la radice del repo, e li' c'e'. Sul Pi no: la' «~/foglietto-bot»
# e' una copia della sola cartella del pacchetto (vedi «Installazione» nel
# README) e il repo resta in «~/foglietto-bot-src», quindi la cartella sopra e'
# «~», dove lo scraper non c'e'. Si guardano tutti e due, perche' sbagliare qui
# non da' errore: il giro prosegue senza scaricare niente, ed e' proprio lo
# scaricamento la ragione per cui si preme il bottone.
#
# SCRAPER_FOGLIETTO ha comunque l'ultima parola: e' la riga che
# foglietto-bot-manuale.service passa esplicita, per non dipendere da $HOME.
SCRAPER="${SCRAPER_FOGLIETTO:-}"
if [[ -z "$SCRAPER" ]]; then
    for candidato in "$CARTELLA/../scraper.py" "${HOME:-/home/pi}/foglietto-bot-src/scraper.py"; do
        if [[ -f "$candidato" ]]; then
            SCRAPER="$candidato"
            break
        fi
    done
    # Nessuno dei due: si tiene il primo, che e' quello da nominare nel
    # messaggio piu' avanti.
    SCRAPER="${SCRAPER:-$CARTELLA/../scraper.py}"
fi

# Il registro condiviso col CMS. Lo leggiamo dal .env come fa il bot, cosi' la
# configurazione resta in un posto solo.
da_env() { grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- || true; }

FILE_ATTIVITA="${FILE_ATTIVITA:-$(da_env FILE_ATTIVITA)}"
export FILE_ATTIVITA

# Lo scraper deposita i PDF dove il bot li cerca. Glielo diciamo esplicitamente
# invece di affidarci alla cartella corrente, che e' come funzionava prima e
# dipendeva da dove si veniva lanciati.
CARTELLA_FOGLIETTI="${CARTELLA_FOGLIETTI:-$(da_env CARTELLA_FOGLIETTI)}"
CARTELLA_FOGLIETTI="${CARTELLA_FOGLIETTI:-$CARTELLA/foglietti}"
export CARTELLA_FOGLIETTI

CASSETTA="$CARTELLA/stato/richieste"

# registra <processo> <ok|errore> <file col dettaglio>
# Delegato al modulo del bot: il formato del registro e' scritto in un posto
# solo, e da qui non serve saperlo.
registra() {
    [[ -n "$FILE_ATTIVITA" ]] || return 0
    "$PYTHON" -m foglietto_bot.attivita "$1" "$2" "$3" 2>/dev/null || true
}

# Le richieste si consumano subito, prima di lavorare: la .path unit di
# systemd guarda se la cartella e' vuota, e finche' non lo e' continuerebbe a
# far ripartire il servizio. Svuotarla adesso vuol dire anche che una seconda
# pressione del bottone, mentre siamo gia' in ballo, viene raccolta dal
# lucchetto del bot e non da un file rimasto li'.
mkdir -p "$CASSETTA"
rm -f "$CASSETTA"/*.json 2>/dev/null || true

traccia="$(mktemp)"
trap 'rm -f "$traccia"' EXIT

# ── 1. Lo scraper: scarica il foglietto della settimana ──────────────────────
#
# Puo' benissimo non trovare niente di nuovo, o fallire perche' il sito della
# parrocchia e' giu': non e' una ragione per non provare a elaborare quello
# che c'e' gia' nella cartella. Percio' si registra l'esito e si tira dritto.
if [[ -f "$SCRAPER" ]]; then
    echo "→ scarico il foglietto…"
    if "$PYTHON" "$SCRAPER" >"$traccia" 2>&1; then
        registra scraper_foglietto ok "$traccia"
    else
        echo "! lo scaricamento non è riuscito, provo lo stesso a elaborare." >&2
        {
            echo "Lo scaricamento del foglietto non è riuscito."
            echo
            echo "Il sito della parrocchia potrebbe essere irraggiungibile, o non"
            echo "aver ancora pubblicato il foglietto di questa settimana. Si"
            echo "prosegue comunque con i PDF già presenti in $CARTELLA_FOGLIETTI."
            echo
            cat "$traccia"
        } >"$traccia.esito"
        registra scraper_foglietto errore "$traccia.esito"
        rm -f "$traccia.esito"
    fi
else
    echo "! non trovo lo scraper in $SCRAPER: elaboro solo quello che c'è già." >&2
fi

# ── 2. Il bot: legge i PDF nuovi e scrive nel CMS ────────────────────────────
#
# La riga di registro per questo pezzo la scrive il bot stesso, per ogni
# foglietto elaborato («ocr_redazione_foglietto»): sa quanti contenuti ha
# creato, cosa ha lasciato in bozza e cosa va guardato a mano. Da qui si
# registra solo il caso in cui non e' riuscito nemmeno a partire.
echo "→ elaboro i foglietti nuovi…"
"$PYTHON" -m foglietto_bot scansiona >"$traccia" 2>&1
uscita=$?

cat "$traccia"
if (( uscita != 0 )); then
    registra ocr_redazione_foglietto errore "$traccia"
    exit "$uscita"
fi
