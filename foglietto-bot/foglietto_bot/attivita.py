"""Il registro condiviso con la dashboard della redazione.

Il CMS tiene un file JSON Lines — una riga per evento, sempre nella stessa
forma — che la dashboard su /riservata/ mostra come «Ultime attività»:

    {"timestamp": "...ISO8601...", "processo": "...", "stato": "ok"|"errore",
     "dettaglio": "..."}

Ci scrivono canizzano.sh e il suo esecutore. Da qui ci scriviamo anche noi, coi
nostri nomi di processo, cosi' chi guarda la dashboard vede in un elenco solo
sia i comandi premuti a mano sia quello che il bot ha fatto per conto suo.

    scraper_foglietto        lo scaricamento del PDF dal sito della parrocchia
    ocr_redazione_foglietto  la lettura del PDF e la scrittura nel CMS

Nessuno dei due processi importa l'altro: girano in ambienti diversi (host,
container, venv) e l'unica cosa che condividono e' il formato di questo file.
Percio' la scrittura e' ripetuta anche qui, in una trentina di righe senza
dipendenze — non e' un doppione da unificare, e' un accordo su un formato.

E' tutto facoltativo: se FILE_ATTIVITA non e' impostata, o il file non e'
scrivibile, non succede niente. Il registro e' un di piu' — il report vero
resta la mail, e il bot non deve fallire perche' la dashboard non c'e'.

Si puo' usare anche da riga di comando, per i pezzi che non sono Python:

    python -m foglietto_bot.attivita <processo> <ok|errore> [file-dettaglio]
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

# Il dettaglio finisce in una pagina HTML: un traceback lungo o l'output di
# curl non devono diventare un muro. La dashboard taglia a sua volta, ma
# meglio non scriverci dentro roba enorme.
TETTO_DETTAGLIO = 8000


def percorso_registro(esplicito: str | os.PathLike[str] | None = None) -> Path | None:
    """Dove scrivere, o None se nessuno l'ha detto."""
    grezzo = str(esplicito) if esplicito else os.environ.get("FILE_ATTIVITA", "")
    grezzo = grezzo.strip()
    return Path(grezzo).expanduser() if grezzo else None


def _accorcia(testo: str) -> str:
    testo = testo.strip()
    if len(testo) <= TETTO_DETTAGLIO:
        return testo
    meta = TETTO_DETTAGLIO // 2
    tagliati = len(testo) - TETTO_DETTAGLIO
    return f"{testo[:meta]}\n\n[… {tagliati} caratteri omessi …]\n\n{testo[-meta:]}"


def registra(
    processo: str,
    stato: str,
    dettaglio: str = "",
    destinazione: str | os.PathLike[str] | None = None,
) -> bool:
    """Appende una riga al registro. True se ci e' riuscita.

    Non alza mai: chi la chiama sta finendo un lavoro vero, e un registro che
    non si lascia scrivere non e' una buona ragione per farlo fallire.
    """
    percorso = percorso_registro(destinazione)
    if percorso is None:
        return False

    riga = {
        # Con il fuso: il bot e il CMS possono girare con TZ diversi, e senza
        # questo la stessa cronologia mostrerebbe due orari per lo stesso
        # momento.
        "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
        "processo": processo,
        "stato": "ok" if stato == "ok" else "errore",
        "dettaglio": _accorcia(dettaglio),
    }
    try:
        percorso.parent.mkdir(parents=True, exist_ok=True)
        with percorso.open("a", encoding="utf-8") as file:
            file.write(json.dumps(riga, ensure_ascii=False) + "\n")
    except OSError:
        return False
    return True


def _principale(argomenti: list[str]) -> int:
    if not 2 <= len(argomenti) <= 3:
        print(__doc__.strip().splitlines()[-1], file=sys.stderr)
        return 2
    processo, stato = argomenti[0], argomenti[1]
    dettaglio = ""
    if len(argomenti) == 3 and argomenti[2]:
        # Il dettaglio arriva da un file e non da un argomento: l'output di un
        # comando supera comodamente la lunghezza massima della riga.
        try:
            dettaglio = Path(argomenti[2]).read_text(encoding="utf-8", errors="replace")
        except OSError as errore:
            dettaglio = f"(dettaglio non leggibile: {errore})"
    return 0 if registra(processo, stato, dettaglio) else 1


if __name__ == "__main__":
    raise SystemExit(_principale(sys.argv[1:]))
