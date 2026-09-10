"""Orchestrazione: dal PDF appena arrivato al report spedito.

Ogni foglietto elaborato produce sempre una mail, anche quando non c'e' stato
niente da fare o qualcosa e' andato storto. Un PDF viene marcato come
«completato» solo a pipeline finita, cosi' un'interruzione lo fa ritentare
invece di perderlo.
"""

from __future__ import annotations

import fcntl
import logging
import time
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

from . import analisi as stadio_analisi
from . import attivita
from . import report as stadio_report
from .config import Config
from .mcp_client import ClientMCP
from .registro import Foglietto, Registro, impronta
from .report import Riepilogo
from .scrittura import scrivi

log = logging.getLogger(__name__)


class GiaInEsecuzione(RuntimeError):
    pass


@contextmanager
def lucchetto(cfg: Config):
    """Una sola esecuzione per volta.

    Il timer scatta ogni mezz'ora e la sentinella settimanale usa lo stesso
    comando: senza lucchetto due esecuzioni potrebbero leggere lo stesso PDF
    insieme e creare i contenuti due volte.
    """
    cfg.cartella_stato.mkdir(parents=True, exist_ok=True)
    percorso = cfg.cartella_stato / "bot.lock"
    with percorso.open("w") as file_lucchetto:
        try:
            fcntl.flock(file_lucchetto, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as errore:
            raise GiaInEsecuzione(
                f"un'altra esecuzione sta gia' lavorando ({percorso})"
            ) from errore
        try:
            yield
        finally:
            fcntl.flock(file_lucchetto, fcntl.LOCK_UN)


def crea_client(cfg: Config) -> ClientMCP:
    return ClientMCP(url=cfg.mcp_url, chiave=cfg.mcp_key, dry_run=cfg.dry_run)


def elabora(
    foglietto: Foglietto,
    cfg: Config,
    client: ClientMCP,
    registro: Registro,
    oggi: date | None = None,
) -> Riepilogo:
    """Legge un foglietto, scrive nel CMS quello che serve, prepara il report."""
    partenza = time.monotonic()
    riepilogo = Riepilogo(nome_file=foglietto.nome_file, dry_run=cfg.dry_run, iniziato=datetime.now())
    registro.inizia(foglietto)
    log.info("Elaboro %s (%s…)", foglietto.nome_file, foglietto.hash[:12])

    try:
        contesto = stadio_analisi.raccogli_contesto(client, oggi=oggi)
        analisi = stadio_analisi.analizza(foglietto.percorso, cfg, contesto)
        riepilogo.pagine = analisi.pagine
        riepilogo.data_foglietto = analisi.data_foglietto
        riepilogo.note_analisi = analisi.note
        log.info(
            "Analisi: %d pagine, %d notizie valutate", len(analisi.pagine), len(analisi.notizie)
        )

        riepilogo.righe = scrivi(
            analisi, client, cfg, registro=registro, hash_foglietto=foglietto.hash, oggi=oggi
        )
        registro.completa(foglietto.hash)
        log.info(
            "Fatto: %d pubblicati, %d in bozza, %d scartati, %d da vedere a mano",
            len(riepilogo.pubblicati),
            len(riepilogo.bozze),
            len(riepilogo.scartati),
            len(riepilogo.attenzione),
        )
    except Exception as errore:  # il report deve partire comunque
        log.exception("Pipeline interrotta su %s", foglietto.nome_file)
        riepilogo.errore_grave = f"{type(errore).__name__}: {errore}"
        registro.fallisci(foglietto.hash, riepilogo.errore_grave)

    riepilogo.durata = time.monotonic() - partenza
    _consegna(cfg, riepilogo)
    return riepilogo


def _riga_di_registro(riepilogo: Riepilogo) -> tuple[str, str]:
    """Riepilogo in due righe, per la dashboard della redazione.

    Chi guarda /riservata/ non vuole rileggersi il report: vuole sapere se e'
    andata bene e quanto e' stato scritto. Il dettaglio completo resta nella
    mail e nella copia HTML in stato/report.
    """
    if riepilogo.errore_grave:
        return "errore", f"{riepilogo.nome_file}: {riepilogo.errore_grave}"

    conto = (
        f"{len(riepilogo.pubblicati)} pubblicati, {len(riepilogo.bozze)} in bozza, "
        f"{len(riepilogo.scartati)} scartati, {len(riepilogo.attenzione)} da vedere"
    )
    dettaglio = f"{riepilogo.nome_file}: {conto}."
    if riepilogo.dry_run:
        dettaglio += "\n\nModalità di prova: nel CMS non è stato scritto niente."
    if riepilogo.attenzione:
        dettaglio += (
            "\n\nQualcosa è stato lasciato a una persona: guarda la mail di "
            "riepilogo."
        )
    dettaglio += "\n\nQuello che è stato creato resta in redazione: sul sito non "
    dettaglio += "compare finché non lo pubblichi tu."
    return "ok", dettaglio


def _consegna(cfg: Config, riepilogo: Riepilogo) -> None:
    stadio_report.salva_copia(cfg, riepilogo)
    try:
        stadio_report.invia(cfg, riepilogo)
    except Exception as errore:
        # Non far fallire l'elaborazione per colpa della posta: il report
        # resta comunque su disco in cartella_stato/report.
        log.error("Report non spedito (%s). La copia HTML resta su disco.", errore)


def scansiona(
    cfg: Config,
    forza: bool = False,
    solo: Path | None = None,
    oggi: date | None = None,
) -> list[Riepilogo]:
    """Elabora i PDF non ancora completati che stanno nella cartella dei foglietti."""
    cfg.prepara_cartelle()
    client = crea_client(cfg)
    riepiloghi: list[Riepilogo] = []

    with lucchetto(cfg), Registro(cfg.percorso_registro) as registro:
        if solo is not None:
            da_fare = [
                Foglietto(hash=impronta(solo), percorso=solo, nome_file=solo.name)
            ]
            riga = registro.stato(da_fare[0].hash)
            if riga and riga["stato"] == "completato" and not forza:
                log.info("%s risulta gia' elaborato: uso --forza per rifarlo.", solo.name)
                return []
        else:
            da_fare = registro.da_elaborare(
                cfg.cartella_foglietti,
                cfg.max_tentativi,
                forza=forza,
                eta_minima=cfg.eta_minima_pdf,
            )

        if not da_fare:
            log.info("Nessun foglietto nuovo in %s", cfg.cartella_foglietti)
            return []

        for foglietto in da_fare:
            riepilogo = elabora(foglietto, cfg, client, registro, oggi=oggi)
            riepiloghi.append(riepilogo)
            # Una riga per foglietto nel registro condiviso col CMS: e' quello
            # che la dashboard della redazione mostra sotto «Ultime attivita'».
            stato, dettaglio = _riga_di_registro(riepilogo)
            attivita.registra("ocr_redazione_foglietto", stato, dettaglio, cfg.file_attivita)

    return riepiloghi
