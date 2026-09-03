"""Prova la catena di scrittura contro il CMS vero, senza scrivere niente.

    ./venv/bin/python tests/prova_integrazione.py

Serve il CMS acceso e una MCP_KEY valida nel .env. Gemini non viene chiamato:
al suo posto c'e' un'analisi finta che copre tutti i casi che il bot deve
saper gestire. Le letture (deduplica, elenco attivita') sono vere, le
scritture sono simulate perche' il client parte in modalita' di prova.
"""

from __future__ import annotations

import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from foglietto_bot import config, pipeline  # noqa: E402
from foglietto_bot.models import (  # noqa: E402
    AnalisiFoglietto, Dettaglio, Esito, Notizia, Pagina, TipoPagina,
)
from foglietto_bot.registro import Registro  # noqa: E402
from foglietto_bot.report import Riepilogo, costruisci_testo, salva_copia  # noqa: E402
from foglietto_bot.scrittura import scrivi  # noqa: E402


def analisi_finta(oggi: date) -> AnalisiFoglietto:
    return AnalisiFoglietto(
        data_foglietto=oggi.isoformat(),
        pagine=[Pagina(indice_nel_pdf=1, tipo=TipoPagina.avvisi, come_l_ho_riconosciuta="titolo")],
        notizie=[
            Notizia(  # dovrebbe risultare duplicato di quello gia' sul sito
                titolo="Gita sul Cansiglio", esito=Esito.crea, motivo="avviso con data",
                attivita="parrocchia", inizio="2026-09-05T06:45", confidenza=0.95,
            ),
            Notizia(  # dovrebbe essere pubblicato
                titolo="Castagnata in oratorio", esito=Esito.crea, motivo="si tiene a Canizzano",
                attivita="parrocchia", inizio="2026-10-25T15:00", icona="chiesa",
                luogo="Oratorio di Canizzano", sommario="Caldarroste nel cortile.",
                confidenza=0.93,
            ),
            Notizia(  # bassa confidenza -> bozza
                titolo="Incontro catechisti", esito=Esito.crea, motivo="forse solo interno",
                attivita="parrocchia", inizio="2026-10-10T20:30", confidenza=0.55,
                dubbi=["non e' chiaro se sia aperto a tutti"],
            ),
            Notizia(  # attivita' inesistente -> ripiego + bozza
                titolo="Torneo di calcetto", esito=Esito.crea, motivo="evento del quartiere",
                attivita="polisportiva-inventata", inizio="2026-10-11T14:00", confidenza=0.9,
            ),
            Notizia(  # senza data -> segnaposto + bozza
                titolo="Iscrizioni al coro", esito=Esito.crea, motivo="avviso senza data",
                attivita="coro-chierichetti-e-gli-altri", confidenza=0.88,
            ),
            Notizia(  # con articolo di approfondimento
                titolo="Pellegrinaggio a Motta di Livenza", esito=Esito.crea,
                motivo="parte dalla parrocchia", attivita="parrocchia",
                inizio="2026-11-08T06:30", icona="montagna", confidenza=0.91,
                merita_articolo=True,
                corpo_articolo="Partenza dal piazzale.\n\n## Il percorso\n- sosta a Oderzo",
                dettagli=[Dettaglio(etichetta="Ritrovo", valore="ore 6.30 in piazza")],
                come_partecipare="Iscrizioni in sacrestia entro il 2 novembre.",
            ),
            Notizia(
                titolo="Vangelo della domenica", esito=Esito.scarta,
                motivo="e' il testo del vangelo, non un appuntamento", confidenza=0.99,
            ),
            Notizia(
                titolo="Il bilancio della parrocchia", esito=Esito.attenzione_umana,
                motivo="tema economico delicato: lo lascio a una persona", confidenza=0.8,
            ),
        ],
    )


def principale() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    cfg = config.carica()
    cfg.dry_run = True  # non si scrive nel CMS, mai, da qui
    cfg.verifica(serve_gemini=False, serve_posta=False)
    cfg.prepara_cartelle()

    client = pipeline.crea_client(cfg)
    oggi = date.today()
    print(f"CMS: {len(client.schemi())} tool, {len(client.attivita())} attivita'\n")

    with Registro(cfg.percorso_registro) as registro:
        righe = scrivi(
            analisi_finta(oggi), client, cfg,
            registro=registro, hash_foglietto="prova-integrazione", oggi=oggi,
        )

    print("\n--- esiti ---")
    for riga in righe:
        print(f"{riga.azione.value:<18} {riga.titolo}")
        if riga.dubbi:
            print(f"{'':18} dubbi: {'; '.join(riga.dubbi)}")

    riepilogo = Riepilogo(nome_file="prova-integrazione.pdf", righe=righe, dry_run=True)
    salva_copia(cfg, riepilogo)
    print("\n" + costruisci_testo(riepilogo))
    print(f"Report HTML in {cfg.cartella_report}")

    attese = {
        "Gita sul Cansiglio": "duplicato",
        "Castagnata in oratorio": "pubblicato",
        "Incontro catechisti": "bozza",
        "Torneo di calcetto": "bozza",
        "Iscrizioni al coro": "bozza",
        "Pellegrinaggio a Motta di Livenza": "pubblicato",
        "Vangelo della domenica": "scartato",
        "Il bilancio della parrocchia": "attenzione_umana",
    }
    sbagliate = [
        f"{r.titolo}: atteso {attese[r.titolo]}, ottenuto {r.azione.value}"
        for r in righe
        if attese.get(r.titolo) and r.azione.value != attese[r.titolo]
    ]
    if sbagliate:
        print("\nESITI INATTESI:")
        for problema in sbagliate:
            print("  " + problema)
        return 1
    print("\nTutti gli esiti sono quelli attesi.")
    return 0


if __name__ == "__main__":
    raise SystemExit(principale())
