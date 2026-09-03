"""Secondo stadio: dalle notizie analizzate ai contenuti del CMS.

Qui vivono le tre decisioni che contano: se l'appuntamento esiste gia'
(deduplica fra settimane diverse), se va pubblicato o lasciato in bozza, e se
merita anche un articolo. Il bot non cancella e non pubblica il sito: crea
contenuti e basta.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from enum import Enum

from .config import Config
from .mcp_client import ClientMCP, ErroreMCP, Risultato
from .models import AnalisiFoglietto, Esito as EsitoModello, Notizia
from .registro import Registro

log = logging.getLogger(__name__)


class Azione(str, Enum):
    pubblicato = "pubblicato"
    bozza = "bozza"
    duplicato = "duplicato"
    scartato = "scartato"
    attenzione_umana = "attenzione_umana"
    errore = "errore"


@dataclass
class Riga:
    """Che fine ha fatto una notizia. E' la materia prima del report."""

    notizia: Notizia
    azione: Azione
    motivo: str = ""
    dubbi: list[str] = field(default_factory=list)
    slug_evento: str = ""
    slug_articolo: str = ""
    duplicato_di: str = ""

    @property
    def titolo(self) -> str:
        return self.notizia.titolo


# --------------------------------------------------------------------------
# confronto fra titoli
# --------------------------------------------------------------------------

_PAROLE_VUOTE = {
    "il", "lo", "la", "i", "gli", "le", "un", "uno", "una", "di", "a", "da",
    "in", "con", "su", "per", "tra", "fra", "del", "della", "dei", "delle",
    "al", "alla", "ai", "alle", "dal", "dalla", "e", "ed",
}


def normalizza(testo: str) -> str:
    senza_accenti = unicodedata.normalize("NFKD", testo).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", senza_accenti.lower())).strip()


def somiglianza(primo: str, secondo: str) -> float:
    return SequenceMatcher(None, normalizza(primo), normalizza(secondo)).ratio()


def parole_chiave(titolo: str, quante: int = 2) -> list[str]:
    parole = [p for p in normalizza(titolo).split() if len(p) > 3 and p not in _PAROLE_VUOTE]
    return sorted(parole, key=len, reverse=True)[:quante]


# --------------------------------------------------------------------------
# date
# --------------------------------------------------------------------------


def _data_di(notizia: Notizia) -> date | None:
    if not notizia.inizio:
        return None
    grezza = notizia.inizio.strip().replace(" ", "T")
    for formato in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(grezza, formato).date()
        except ValueError:
            continue
    log.warning("Data non interpretabile: %r", notizia.inizio)
    return None


# --------------------------------------------------------------------------
# deduplica
# --------------------------------------------------------------------------


def trova_duplicato(
    client: ClientMCP, notizia: Notizia, cfg: Config
) -> Risultato | None:
    """Cerca nel CMS un evento che sia sostanzialmente questo stesso.

    Prima si fida dello slug che il modello ha riconosciuto fra quelli gia' in
    calendario; poi cerca per finestra di date, e in mancanza di data per
    parola chiave del titolo.
    """
    if notizia.possibile_duplicato_di:
        try:
            scheda = client.leggi("evento", notizia.possibile_duplicato_di)
            if scheda.get("titolo"):
                return Risultato(
                    tipo="evento",
                    titolo=scheda.get("titolo", ""),
                    slug=scheda.get("slug", notizia.possibile_duplicato_di),
                    attivita=scheda.get("attivita", ""),
                )
        except ErroreMCP:
            log.info("Lo slug segnalato «%s» non esiste piu'", notizia.possibile_duplicato_di)

    quando = _data_di(notizia)
    candidati: list[Risultato] = []
    if quando:
        finestra = timedelta(days=cfg.giorni_dedup)
        candidati = client.cerca(
            tipo="evento",
            dal=(quando - finestra).isoformat(),
            al=(quando + finestra).isoformat(),
            limite=100,
        )
    else:
        for parola in parole_chiave(notizia.titolo):
            candidati.extend(client.cerca(testo=parola, tipo="evento", limite=30))

    migliore: Risultato | None = None
    punteggio_migliore = 0.0
    for candidato in candidati:
        punteggio = somiglianza(notizia.titolo, candidato.titolo)
        # Stesso giorno e stessa realta': basta molta meno somiglianza di titolo.
        stesso_giorno = bool(quando) and candidato.data_iso == quando.isoformat()
        stessa_attivita = bool(notizia.attivita) and candidato.attivita == notizia.attivita
        soglia = cfg.soglia_duplicato
        if stesso_giorno and stessa_attivita:
            soglia = min(soglia, 0.55)
        elif stesso_giorno:
            soglia = min(soglia, 0.70)
        if punteggio >= soglia and punteggio > punteggio_migliore:
            migliore, punteggio_migliore = candidato, punteggio
    if migliore:
        log.info(
            "«%s» somiglia a «%s» (%.2f): non lo ricreo",
            notizia.titolo, migliore.titolo, punteggio_migliore,
        )
    return migliore


# --------------------------------------------------------------------------
# scrittura
# --------------------------------------------------------------------------


def _campi_evento(notizia: Notizia, pubblicato: bool, inizio: str) -> dict:
    descrizione = notizia.descrizione.strip()
    if notizia.come_partecipare.strip():
        # Nel CMS non c'e' un campo «come partecipare»: finisce in coda al testo.
        descrizione = (descrizione + "\n\n" + notizia.come_partecipare.strip()).strip()
    return {
        "attivita": notizia.attivita,
        "titolo": notizia.titolo.strip(),
        "inizio": inizio,
        "fine": notizia.fine,
        "tutto_il_giorno": notizia.tutto_il_giorno,
        "etichetta_data": notizia.etichetta_data,
        "sommario": notizia.sommario,
        "descrizione": descrizione,
        "categoria": notizia.categoria,
        "icona": notizia.icona,
        "luogo": notizia.luogo,
        "luogo_libero": notizia.luogo_libero,
        "ingresso": notizia.ingresso,
        "prenotazione_entro": notizia.prenotazione_entro,
        "pubblicato": pubblicato,
    }


def _campi_articolo(notizia: Notizia, slug_evento: str, pubblicato: bool, oggi: date) -> dict:
    return {
        "evento": slug_evento,
        "occhiello": notizia.occhiello,
        "sottotitolo": notizia.sottotitolo or notizia.sommario,
        "corpo": notizia.corpo_articolo,
        "firma": "la redazione del foglietto",
        "data_pubblicazione": oggi.isoformat(),
        "pubblicato": pubblicato,
        "dettagli": [
            {"etichetta": d.etichetta, "valore": d.valore}
            for d in notizia.dettagli
            if d.etichetta and d.valore
        ],
    }


def scrivi(
    analisi: AnalisiFoglietto,
    client: ClientMCP,
    cfg: Config,
    registro: Registro | None = None,
    hash_foglietto: str = "",
    oggi: date | None = None,
) -> list[Riga]:
    """Porta nel CMS le notizie che hanno superato i filtri. Non cancella nulla."""
    oggi = oggi or date.today()
    attivita_valide = {a.slug for a in client.attivita()}
    righe: list[Riga] = []

    for notizia in analisi.notizie:
        if notizia.esito is EsitoModello.attenzione_umana:
            righe.append(
                Riga(notizia, Azione.attenzione_umana, motivo=notizia.motivo, dubbi=notizia.dubbi)
            )
            continue
        if notizia.esito is EsitoModello.scarta:
            azione = Azione.duplicato if notizia.possibile_duplicato_di else Azione.scartato
            righe.append(
                Riga(
                    notizia,
                    azione,
                    motivo=notizia.motivo,
                    duplicato_di=notizia.possibile_duplicato_di,
                )
            )
            continue

        try:
            righe.append(_crea(notizia, client, cfg, attivita_valide, registro, hash_foglietto, oggi))
        except ErroreMCP as errore:
            log.exception("Errore creando «%s»", notizia.titolo)
            righe.append(Riga(notizia, Azione.errore, motivo=str(errore)))

    return righe


def _crea(
    notizia: Notizia,
    client: ClientMCP,
    cfg: Config,
    attivita_valide: set[str],
    registro: Registro | None,
    hash_foglietto: str,
    oggi: date,
) -> Riga:
    dubbi = list(notizia.dubbi)

    doppione = trova_duplicato(client, notizia, cfg)
    if doppione:
        return Riga(
            notizia,
            Azione.duplicato,
            motivo=f"gia' sul sito come «{doppione.titolo}»",
            slug_evento=doppione.slug,
            duplicato_di=doppione.slug,
        )

    # L'attivita' deve esistere davvero. Se il modello non l'ha scelta o ha
    # scelto uno slug che non c'e', si ripiega sulla parrocchia (il foglietto
    # e' parrocchiale) ma il contenuto resta in bozza col dubbio scritto.
    attivita = notizia.attivita
    if attivita not in attivita_valide:
        if attivita:
            dubbi.append(f"attivita' «{attivita}» inesistente, assegnato a «{cfg.attivita_predefinita}»")
        else:
            dubbi.append(f"attivita' non decisa dal modello, assegnato a «{cfg.attivita_predefinita}»")
        attivita = cfg.attivita_predefinita
        if attivita not in attivita_valide:
            return Riga(
                notizia,
                Azione.errore,
                motivo=(
                    f"ne' l'attivita' proposta ne' quella predefinita "
                    f"«{cfg.attivita_predefinita}» esistono nel CMS"
                ),
            )
    notizia.attivita = attivita

    # «inizio» e' obbligatorio per crea_evento: senza data non si puo' ordinare.
    inizio = notizia.inizio.strip()
    if not _data_di(notizia):
        inizio = f"{oggi.isoformat()}T00:00"
        notizia.tutto_il_giorno = True
        dubbi.append("data non trovata nel foglietto: messa la data di oggi come segnaposto")

    pubblicato = notizia.confidenza >= cfg.soglia_pubblicazione and not dubbi
    slug_evento, _ = client.crea_evento(_campi_evento(notizia, pubblicato, inizio))
    if not slug_evento:
        dubbi.append("il CMS non ha restituito lo slug dell'evento")

    if registro and hash_foglietto:
        registro.registra_contenuto(
            hash_foglietto, "evento", slug_evento, notizia.titolo, pubblicato
        )

    slug_articolo = ""
    if notizia.merita_articolo and notizia.corpo_articolo.strip() and slug_evento:
        try:
            slug_articolo, _ = client.crea_articolo(
                _campi_articolo(notizia, slug_evento, pubblicato, oggi)
            )
            if registro and hash_foglietto:
                registro.registra_contenuto(
                    hash_foglietto, "articolo", slug_articolo, notizia.titolo, pubblicato
                )
        except ErroreMCP as errore:
            log.warning("Articolo per «%s» non creato: %s", notizia.titolo, errore)
            dubbi.append(f"articolo non creato: {errore}")

    return Riga(
        notizia,
        Azione.pubblicato if pubblicato else Azione.bozza,
        motivo=notizia.motivo,
        dubbi=dubbi,
        slug_evento=slug_evento,
        slug_articolo=slug_articolo,
    )
