"""Riga di comando del bot. E' quello che systemd chiama ogni mezz'ora.

    python -m foglietto_bot verifica          controlla configurazione e CMS
    python -m foglietto_bot scansiona         elabora i foglietti nuovi
    python -m foglietto_bot elabora FILE.pdf  ne elabora uno preciso
    python -m foglietto_bot stato             cos'e' gia' passato
    python -m foglietto_bot prova-report      manda un report finto
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from . import config as configurazione
from . import pipeline, report
from .models import Esito, Notizia, Pagina, TipoPagina
from .registro import Registro
from .scrittura import Azione, Riga

log = logging.getLogger("foglietto_bot")


def _prepara_log(livello: str) -> None:
    logging.basicConfig(
        level=getattr(logging, livello, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
        datefmt="%d/%m %H:%M:%S",
        stream=sys.stdout,
    )


# --------------------------------------------------------------------------
# comandi
# --------------------------------------------------------------------------


def comando_verifica(cfg: configurazione.Config) -> int:
    """Controlla che ci sia tutto, senza scrivere niente da nessuna parte."""
    problemi: list[str] = []
    try:
        cfg.verifica()
        print("configurazione: completa")
    except configurazione.ErroreConfigurazione as errore:
        print(f"configurazione: {errore}")
        problemi.append("configurazione")

    cfg.prepara_cartelle()
    print(f"foglietti in:   {cfg.cartella_foglietti}")
    pdf = sorted(cfg.cartella_foglietti.glob('*.pdf'))
    print(f"                {len(pdf)} PDF presenti")
    print(f"stato in:       {cfg.cartella_stato}")

    try:
        client = pipeline.crea_client(cfg)
        tool = sorted(client.schemi())
        print(f"server MCP:     {len(tool)} tool su {cfg.mcp_url}")
        attivita = client.attivita()
        print("attività:       " + ", ".join(a.slug for a in attivita))
        print("luoghi:         " + ", ".join(client.luoghi()))
        print("icone:          " + ", ".join(client.valori_ammessi("crea_evento", "icona")[:8]) + "…")
        if "elimina_contenuto" in tool:
            print(
                "attenzione:     la chiave MCP vede «elimina_contenuto». Il bot non lo\n"
                "                chiamera' mai, ma è meglio togliere il permesso alla chiave\n"
                "                nell'admin (Assistente AI → la chiave del bot)."
            )
    except Exception as errore:
        print(f"server MCP:     NON raggiungibile — {errore}")
        problemi.append("MCP")

    with Registro(cfg.percorso_registro) as registro:
        ultimo = registro.ultimo_completato()
        if ultimo:
            print(f"ultimo lavoro:  {ultimo['nome_file']} il {ultimo['aggiornato_il']}")
        else:
            print("ultimo lavoro:  nessuno")

    print(f"report a:       {cfg.email_destinatario}")
    print(f"modalità:       {'PROVA (non scrive nel CMS)' if cfg.dry_run else 'normale'}")
    return 1 if problemi else 0


def comando_scansiona(cfg: configurazione.Config, forza: bool, avvisa_se_vuoto: bool) -> int:
    cfg.verifica()
    riepiloghi = pipeline.scansiona(cfg, forza=forza)
    if not riepiloghi and avvisa_se_vuoto:
        _avvisa_se_troppo_silenzio(cfg)
    for riepilogo in riepiloghi:
        print(
            f"{riepilogo.nome_file}: {len(riepilogo.pubblicati)} pubblicati, "
            f"{len(riepilogo.bozze)} in bozza, {len(riepilogo.scartati)} scartati, "
            f"{len(riepilogo.attenzione)} da vedere"
        )
    return 0


def comando_elabora(cfg: configurazione.Config, percorso: Path, forza: bool) -> int:
    cfg.verifica()
    if not percorso.is_file():
        print(f"Non trovo {percorso}", file=sys.stderr)
        return 2
    riepiloghi = pipeline.scansiona(cfg, forza=forza, solo=percorso)
    return 0 if riepiloghi else 1


def comando_stato(cfg: configurazione.Config) -> int:
    with Registro(cfg.percorso_registro) as registro:
        righe = registro.elenco()
        if not righe:
            print("Il registro è vuoto: nessun foglietto ancora elaborato.")
            return 0
        print(f"{'stato':<12}{'contenuti':>10}  {'quando':<20} file")
        for riga in righe:
            print(
                f"{riga['stato']:<12}{riga['creati']:>10}  "
                f"{riga['aggiornato_il']:<20} {riga['nome_file']}"
                + (f"  ({riga['errore'][:60]})" if riga["errore"] else "")
            )
    return 0


def _avvisa_se_troppo_silenzio(cfg: configurazione.Config, giorni: int = 9) -> None:
    """Se da troppo tempo non arriva un foglietto, probabilmente lo scraper è fermo."""
    with Registro(cfg.percorso_registro) as registro:
        ultimo = registro.ultimo_completato()
    if ultimo:
        quando = datetime.fromisoformat(ultimo["aggiornato_il"])
        if datetime.now() - quando < timedelta(days=giorni):
            return
        motivo = f"L'ultimo foglietto elaborato è «{ultimo['nome_file']}» del {quando:%d/%m/%Y}."
    else:
        motivo = "Nel registro non risulta ancora nessun foglietto elaborato."

    riepilogo = report.Riepilogo(nome_file="(nessun foglietto nuovo)", dry_run=cfg.dry_run)
    riepilogo.errore_grave = (
        f"Sono passati più di {giorni} giorni senza un foglietto nuovo da elaborare.\n"
        f"{motivo}\n"
        f"Controlla che lo scraper stia depositando i PDF in {cfg.cartella_foglietti}."
    )
    pipeline._consegna(cfg, riepilogo)


def comando_prova_report(cfg: configurazione.Config, solo_file: bool = False) -> int:
    """Manda un report finto: serve a provare l'SMTP e a guardare l'impaginazione."""
    cfg.verifica(serve_gemini=False, serve_posta=not solo_file)
    domani = date.today() + timedelta(days=6)
    riepilogo = report.Riepilogo(
        nome_file="foglietto-di-prova.pdf",
        data_foglietto=domani.isoformat(),
        dry_run=True,
        pagine=[
            Pagina(indice_nel_pdf=1, tipo=TipoPagina.avvisi, come_l_ho_riconosciuta="titolo AVVISI"),
            Pagina(indice_nel_pdf=2, tipo=TipoPagina.copertina, come_l_ho_riconosciuta="intestazione"),
        ],
        note_analisi="La terza pagina è leggermente storta ma leggibile.",
    )
    riepilogo.righe = [
        Riga(
            notizia=Notizia(
                titolo="Castagnata in oratorio",
                esito=Esito.crea,
                motivo="avviso con data e luogo, si tiene a Canizzano",
                attivita="parrocchia",
                inizio=f"{domani.isoformat()}T15:00",
                sommario="Pomeriggio con caldarroste e vin brulé nel cortile dell'oratorio.",
                luogo="Oratorio di Canizzano",
                confidenza=0.93,
            ),
            azione=Azione.pubblicato,
            slug_evento=f"castagnata-in-oratorio-{domani.isoformat()}",
        ),
        Riga(
            notizia=Notizia(
                titolo="Pellegrinaggio a Motta di Livenza",
                esito=Esito.crea,
                motivo="parte da Canizzano ma si svolge altrove: tenuto, con dubbi",
                attivita="parrocchia",
                inizio=f"{domani.isoformat()}T06:30",
                sommario="Partenza in pullman dal piazzale della chiesa.",
                merita_articolo=True,
                sottotitolo="Ritrovo alle 6.30, rientro in serata.",
                confidenza=0.62,
            ),
            azione=Azione.bozza,
            dubbi=["non è chiaro se il pullman sia solo per la parrocchia di Canizzano"],
            slug_evento="pellegrinaggio-a-motta-di-livenza",
            slug_articolo="pellegrinaggio-a-motta-di-livenza",
        ),
        Riga(
            notizia=Notizia(
                titolo="Gita sul Cansiglio",
                esito=Esito.scarta,
                motivo="già presente sul sito",
                confidenza=0.9,
            ),
            azione=Azione.duplicato,
            motivo="già sul sito come «Gita sul Cansiglio»",
            duplicato_di="gita-sul-cansiglio-2026-09-05",
        ),
        Riga(
            notizia=Notizia(
                titolo="Vangelo della domenica",
                esito=Esito.scarta,
                motivo="è il testo del vangelo, non un appuntamento",
                confidenza=0.99,
            ),
            azione=Azione.scartato,
            motivo="è il testo del vangelo, non un appuntamento",
        ),
        Riga(
            notizia=Notizia(
                titolo="Il bilancio della parrocchia e le opere da fare",
                esito=Esito.attenzione_umana,
                motivo="testo lungo su un tema economico delicato: lo lascio a una persona",
                descrizione="Due facciate sul rendiconto dell'anno e sulla raccolta fondi per il tetto.",
                confidenza=0.8,
            ),
            azione=Azione.attenzione_umana,
            motivo="testo lungo su un tema economico delicato: lo lascio a una persona",
        ),
    ]
    riepilogo.durata = 42.0
    report.salva_copia(cfg, riepilogo)
    print(f"Copia HTML in {cfg.cartella_report}")
    if solo_file:
        return 0
    report.invia(cfg, riepilogo)
    print(f"Report di prova spedito a {cfg.email_destinatario}")
    return 0


# --------------------------------------------------------------------------


def principale(argomenti: list[str] | None = None) -> int:
    analizzatore = argparse.ArgumentParser(
        prog="foglietto_bot", description="Redazione automatica del foglietto di Canizzano."
    )
    analizzatore.add_argument("--env", type=Path, help="File .env da usare.")
    analizzatore.add_argument("--dry-run", action="store_true", help="Non scrive nel CMS.")
    analizzatore.add_argument("--log-level", help="DEBUG, INFO, WARNING…")
    sotto = analizzatore.add_subparsers(dest="comando")

    sotto.add_parser("verifica", help="Controlla configurazione, CMS e cartelle.")
    scansiona = sotto.add_parser("scansiona", help="Elabora i foglietti nuovi.")
    scansiona.add_argument("--forza", action="store_true", help="Rielabora anche i già fatti.")
    scansiona.add_argument(
        "--avvisa-se-vuoto",
        action="store_true",
        help="Se non arrivano foglietti da troppo tempo, manda una mail di allarme.",
    )
    elabora = sotto.add_parser("elabora", help="Elabora un PDF preciso.")
    elabora.add_argument("pdf", type=Path)
    elabora.add_argument("--forza", action="store_true")
    sotto.add_parser("stato", help="Mostra il registro dei foglietti.")
    prova = sotto.add_parser("prova-report", help="Manda un report di esempio.")
    prova.add_argument(
        "--solo-file", action="store_true", help="Scrive solo l'HTML, senza spedire nulla."
    )

    opzioni = analizzatore.parse_args(argomenti)
    cfg = configurazione.carica(opzioni.env)
    if opzioni.dry_run:
        cfg.dry_run = True
    _prepara_log(opzioni.log_level.upper() if opzioni.log_level else cfg.log_level)

    try:
        if opzioni.comando == "verifica":
            return comando_verifica(cfg)
        if opzioni.comando == "elabora":
            return comando_elabora(cfg, opzioni.pdf, opzioni.forza)
        if opzioni.comando == "stato":
            return comando_stato(cfg)
        if opzioni.comando == "prova-report":
            return comando_prova_report(cfg, solo_file=opzioni.solo_file)
        return comando_scansiona(
            cfg,
            forza=getattr(opzioni, "forza", False),
            avvisa_se_vuoto=getattr(opzioni, "avvisa_se_vuoto", False),
        )
    except pipeline.GiaInEsecuzione as errore:
        log.info("Non faccio niente: %s", errore)
        return 0
    except configurazione.ErroreConfigurazione as errore:
        print(errore, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(principale())
