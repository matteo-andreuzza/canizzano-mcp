"""Servizio long-running per Docker: scheduler interno + endpoint HTTP /attiva.

Sostituisce i timer di systemd quando il bot gira in un container: un solo
processo fa sia da scheduler (APScheduler, per i giri regolari) sia da server
HTTP minimale (solo libreria standard, per il bottone «Elabora il foglietto»
della dashboard del sito). E' quello che parte con `docker compose up -d` e
resta acceso — non elabora niente lui stesso, lancia gli stessi comandi che
lanciava systemd:

    sabato 9:00      scarica + scansiona         (era foglietto-scraper.timer
                                                    + foglietto-bot.timer)
    ogni 30 minuti   solo scansiona               (era foglietto-bot.timer)
    lunedi' 9:00     scansiona --avvisa-se-vuoto  (era foglietto-bot-sentinella.timer)
    POST /attiva     scarica + scansiona, subito  (era foglietto-bot-manuale.path
                                                    + avvio-manuale.sh)

Ogni giro e' un sottoprocesso `python -m foglietto_bot ...` (o lo scraper):
stesso codice, stesso comportamento, sia lanciato da qui sia da un terminale.
Questo file non contiene logica di dominio, solo l'orologio e il centralino.
"""

from __future__ import annotations

import fcntl
import hmac
import json
import logging
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import config as configurazione

log = logging.getLogger("foglietto_bot.servizio")

PYTHON = sys.executable
# Copiato dalla radice del repo nell'immagine (vedi Dockerfile): li' sta
# perche' usa lo stesso venv del pacchetto ma non ne fa parte, come sul Pi.
SCRAPER = "/app/scraper.py"


def _prepara_log(livello: str) -> None:
    logging.basicConfig(
        level=getattr(logging, livello, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
        datefmt="%d/%m %H:%M:%S",
        stream=sys.stdout,
    )


def _esegui(comando: list[str], timeout: int = 2700) -> None:
    """Lancia un sottocomando e riversa il suo output nei log del container.

    Non alza mai: un giro programmato che fallisce non deve far morire lo
    scheduler, deve solo finire nei log (e, per la pipeline vera, nel report
    via mail e nel registro condiviso — quello lo fa gia' il comando stesso).
    """
    log.info("→ %s", " ".join(comando))
    try:
        esito = subprocess.run(comando, capture_output=True, text=True, timeout=timeout)
        if esito.stdout.strip():
            log.info(esito.stdout.rstrip())
        if esito.stderr.strip():
            (log.info if esito.returncode == 0 else log.warning)(esito.stderr.rstrip())
        if esito.returncode != 0:
            log.warning("«%s» uscito con codice %d", comando[-1], esito.returncode)
    except subprocess.TimeoutExpired:
        log.error("Interrotto dopo %ss senza finire: %s", timeout, comando)
    except OSError as errore:
        log.error("Non riesco a eseguire %s: %s", comando, errore)


def scarica_e_scansiona() -> None:
    """Scarica il foglietto e poi elabora quello che c'e': il giro completo."""
    _esegui([PYTHON, SCRAPER], timeout=300)
    _esegui([PYTHON, "-m", "foglietto_bot", "scansiona"])


def solo_scansiona() -> None:
    """Guarda se c'e' un PDF nuovo, senza riscaricare niente."""
    _esegui([PYTHON, "-m", "foglietto_bot", "scansiona"])


def sentinella() -> None:
    """Il controllo del lunedi': manda una mail se il foglietto non arriva da troppo."""
    _esegui([PYTHON, "-m", "foglietto_bot", "scansiona", "--avvisa-se-vuoto"])


# ── L'endpoint /attiva ───────────────────────────────────────────────────────


def _occupato(cfg: configurazione.Config) -> bool:
    """Prova il lucchetto del bot e lo rilascia subito: c'e' gia' un giro in corso?

    Stessa idea di «_libero()» nell'esecutore del sito: qui si guarda solo per
    rispondere subito alla dashboard. L'esclusione vera resta dove e' sempre
    stata, dentro a `pipeline.lucchetto()`, prova di nuovo quando il giro
    lanciato da questa richiesta parte davvero. Nella finestra fra i due controlli
    puo' infilarsi un giro programmato: non e' un problema, e' quello stesso
    lucchetto a fermare chi arriva secondo.
    """
    cfg.cartella_stato.mkdir(parents=True, exist_ok=True)
    percorso = cfg.cartella_stato / "bot.lock"
    fd = os.open(percorso, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


class GestoreAttiva(BaseHTTPRequestHandler):
    """Un solo percorso utile, `POST /attiva`, protetto da un token condiviso.

    Pensato per essere chiamato dall'esecutore del sito sulla rete Docker
    condivisa, mai esposto fuori da quella rete (nessuna porta pubblicata
    verso l'host in compose.yaml).
    """

    cfg: configurazione.Config  # impostati da avvia_server()
    token: str
    server_version = "foglietto-bot/1.0"

    def _rispondi(self, codice: int, corpo: dict) -> None:
        dati = json.dumps(corpo, ensure_ascii=False).encode("utf-8")
        self.send_response(codice)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(dati)))
        self.end_headers()
        self.wfile.write(dati)

    def do_POST(self) -> None:
        if self.path.rstrip("/") != "/attiva":
            self._rispondi(404, {"ok": False, "errore": "non trovato"})
            return

        intestazione = self.headers.get("Authorization", "")
        presentato = intestazione[7:] if intestazione.startswith("Bearer ") else ""
        # compare_digest anche quando il token non e' configurato: cosi' il
        # tempo di risposta non rivela se manca la chiave o se e' sbagliata.
        if not self.token or not hmac.compare_digest(presentato, self.token):
            self._rispondi(401, {"ok": False, "errore": "token mancante o sbagliato"})
            return

        if _occupato(self.cfg):
            self._rispondi(409, {"ok": False, "errore": "un'altra elaborazione e' gia' in corso"})
            return

        # Si torna subito: la richiesta e' «consegnata», non «elaborata». Il
        # giro vero (minuti) scrive il proprio esito in stato/report e nel
        # registro condiviso — non c'e' niente da aspettare qui.
        threading.Thread(target=scarica_e_scansiona, daemon=True).start()
        self._rispondi(202, {"ok": True, "avviato": True})

    def do_GET(self) -> None:
        # Per il proprio HEALTHCHECK del container, non per la dashboard del
        # sito: quella guarda solo se /attiva risponde, non chiama questa rotta.
        if self.path.rstrip("/") == "/salute":
            self._rispondi(200, {"ok": True})
            return
        self._rispondi(404, {"ok": False, "errore": "non trovato"})

    def log_message(self, formato: str, *args) -> None:  # noqa: A003 - firma di BaseHTTPRequestHandler
        log.debug(formato, *args)


def avvia_server(cfg: configurazione.Config) -> ThreadingHTTPServer:
    GestoreAttiva.cfg = cfg
    GestoreAttiva.token = os.environ.get("ATTIVA_TOKEN", "").strip()
    if not GestoreAttiva.token:
        log.warning(
            "ATTIVA_TOKEN non impostato: l'endpoint /attiva rifiuta sempre le "
            "richieste. Impostalo nel .env se vuoi usare il bottone «Elabora il "
            "foglietto» dalla dashboard del sito."
        )
    porta = int(os.environ.get("ATTIVA_PORTA", "8088"))
    server = ThreadingHTTPServer(("0.0.0.0", porta), GestoreAttiva)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("→ endpoint /attiva in ascolto sulla porta %d (solo rete Docker)", porta)
    return server


# ── Lo scheduler ─────────────────────────────────────────────────────────────


def avvia_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="Europe/Rome")
    scheduler.add_job(
        scarica_e_scansiona,
        CronTrigger(day_of_week="sat", hour=9, minute=0),
        id="settimanale",
        # Se il container era spento esattamente alle 9 di sabato, recupera il
        # giro entro un'ora dal riavvio invece di aspettare il sabato dopo.
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        solo_scansiona,
        CronTrigger(minute="*/30"),
        id="periodico",
        misfire_grace_time=600,
    )
    scheduler.add_job(
        sentinella,
        CronTrigger(day_of_week="mon", hour=9, minute=0),
        id="sentinella",
        misfire_grace_time=3600,
    )
    scheduler.start()
    log.info(
        "→ scheduler avviato: sabato 9:00 (scarica+scansiona), ogni 30 min "
        "(scansiona), lunedi' 9:00 (sentinella)."
    )
    return scheduler


def main() -> int:
    cfg = configurazione.carica()
    _prepara_log(cfg.log_level)
    cfg.prepara_cartelle()

    try:
        cfg.verifica()
    except configurazione.ErroreConfigurazione as errore:
        # Non usciamo: con «restart: always» un'uscita qui vorrebbe dire un
        # container in riavvio continuo finche' qualcuno non corregge il
        # .env. Meglio restare su, dirlo nei log, e riprovare al prossimo giro
        # programmato (a quel punto la config puo' essere gia' stata corretta
        # e il container riavviato).
        log.error("Configurazione incompleta: %s", errore)

    avvia_scheduler()
    avvia_server(cfg)

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
