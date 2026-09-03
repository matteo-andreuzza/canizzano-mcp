"""Primo stadio: Gemini legge il PDF e restituisce le notizie in forma strutturata.

Il PDF e' una scansione, quindi va dato al modello come documento e letto con
le sue capacita' di visione: niente OCR nostro, niente estrazione di testo.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, timedelta
from pathlib import Path

from google import genai
from google.genai import types

from .config import Config
from .mcp_client import ClientMCP
from .models import AnalisiFoglietto
from .prompt import SISTEMA, Contesto, costruisci_prompt, estrai_sezione

log = logging.getLogger(__name__)

# Oltre questa soglia il PDF non si manda piu' inline ma con la Files API.
LIMITE_INLINE = 15 * 1024 * 1024
# Quanto avanti guardare per sapere cos'e' gia' in calendario.
ORIZZONTE_CALENDARIO = 240


class ErroreAnalisi(RuntimeError):
    pass


def raccogli_contesto(client: ClientMCP, oggi: date | None = None) -> Contesto:
    """Legge dal CMS tutto cio' che il prompt deve sapere. Niente elenchi fissi."""
    oggi = oggi or date.today()
    try:
        regole = estrai_sezione(client.guida(), "Le dieci regole della redazione")
    except Exception as errore:  # la guida e' utile, non indispensabile
        log.warning("Non sono riuscito a leggere la guida del sito: %s", errore)
        regole = ""
    return Contesto(
        attivita=client.attivita(),
        luoghi=client.luoghi(),
        icone=client.valori_ammessi("crea_evento", "icona"),
        eventi_in_calendario=client.cerca(
            tipo="evento",
            dal=(oggi - timedelta(days=30)).isoformat(),
            al=(oggi + timedelta(days=ORIZZONTE_CALENDARIO)).isoformat(),
            limite=100,
        ),
        regole_redazione=regole,
        oggi=oggi,
    )


def _contenuto_pdf(client: genai.Client, pdf: Path):
    dimensione = pdf.stat().st_size
    if dimensione <= LIMITE_INLINE:
        return types.Part.from_bytes(data=pdf.read_bytes(), mime_type="application/pdf")
    log.info("PDF da %.1f MB: lo carico con la Files API", dimensione / 1e6)
    return client.files.upload(file=pdf, config={"mime_type": "application/pdf"})


def analizza(
    pdf: Path,
    cfg: Config,
    contesto: Contesto,
    tentativi: int = 3,
) -> AnalisiFoglietto:
    """Manda il foglietto a Gemini e riporta indietro l'analisi validata."""
    client = genai.Client(api_key=cfg.gemini_api_key)
    prompt = costruisci_prompt(contesto)
    configurazione = types.GenerateContentConfig(
        system_instruction=SISTEMA,
        temperature=cfg.gemini_temperatura,
        response_mime_type="application/json",
        response_schema=AnalisiFoglietto,
    )

    ultimo_errore: Exception | None = None
    for tentativo in range(1, tentativi + 1):
        try:
            risposta = client.models.generate_content(
                model=cfg.gemini_model,
                contents=[_contenuto_pdf(client, pdf), prompt],
                config=configurazione,
            )
        except Exception as errore:
            ultimo_errore = errore
            if tentativo == tentativi:
                break
            attesa = 5 * tentativo
            log.warning("Gemini ha risposto male (%s), riprovo fra %ss", errore, attesa)
            time.sleep(attesa)
            continue

        analisi = getattr(risposta, "parsed", None)
        if analisi is None:
            testo = (risposta.text or "").strip()
            if not testo:
                ultimo_errore = ErroreAnalisi("risposta vuota")
                continue
            try:
                analisi = AnalisiFoglietto.model_validate(json.loads(testo))
            except Exception as errore:
                ultimo_errore = errore
                log.warning("JSON non valido da Gemini: %s", errore)
                continue

        _salva_grezzo(cfg, pdf, analisi)
        return analisi

    raise ErroreAnalisi(f"analisi del foglietto fallita: {ultimo_errore}")


def _salva_grezzo(cfg: Config, pdf: Path, analisi: AnalisiFoglietto) -> None:
    """Copia dell'analisi accanto al registro: serve quando qualcosa stona."""
    try:
        cartella = cfg.cartella_stato / "analisi"
        cartella.mkdir(parents=True, exist_ok=True)
        destinazione = cartella / f"{pdf.stem}.json"
        destinazione.write_text(
            analisi.model_dump_json(indent=2, exclude_none=True), encoding="utf-8"
        )
    except OSError as errore:
        log.warning("Non sono riuscito a salvare l'analisi grezza: %s", errore)
