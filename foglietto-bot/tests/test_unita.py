"""Prove che non toccano ne' il CMS ne' Gemini: python -m unittest discover tests"""

from __future__ import annotations

import unittest
from datetime import date

from foglietto_bot.mcp_client import ClientMCP, analizza_elenco, analizza_scheda, estrai_slug
from foglietto_bot.models import Esito, Notizia
from foglietto_bot.report import Riepilogo, costruisci_html, oggetto
from foglietto_bot.scrittura import Azione, Riga, _data_di, normalizza, parole_chiave, somiglianza

# Risposte vere del server MCP, copiate da una chiamata reale.
ELENCO = """1 risultati.

### eventi (2)
- 05/09/2026 06:45 — **Gita sul Cansiglio** (`gita-sul-cansiglio-2026-09-05`, attività `parrocchia`) · in evidenza, articolo /eventi/gita-sul-cansiglio
- 12/09/2026 18:00 — **Ama il tuo quartiere — premiazione** (`ama-il-tuo-quartiere-premiazione-2026-09-12`, attività `pro-loco-cannetum`)

### attivita (1)
- **Pro Loco Cannetum** (`pro-loco-cannetum`)
"""

SCHEDA = """## evento: Gita sul Cansiglio — 05/09/2026

- **attivita**: `parrocchia`
- **titolo**: Gita sul Cansiglio
- **inizio**: 05/09/2026 06:45
- **sommario**: —
- **pubblicato**: sì
- **slug**: gita-sul-cansiglio-2026-09-05
- **id**: 35
"""


class ProveParsing(unittest.TestCase):
    def test_elenco_legge_data_slug_e_attivita(self):
        risultati = analizza_elenco(ELENCO)
        self.assertEqual(len(risultati), 3)
        primo = risultati[0]
        self.assertEqual(primo.tipo, "evento")
        self.assertEqual(primo.titolo, "Gita sul Cansiglio")
        self.assertEqual(primo.slug, "gita-sul-cansiglio-2026-09-05")
        self.assertEqual(primo.attivita, "parrocchia")
        self.assertEqual(primo.data_iso, "2026-09-05")

    def test_titolo_con_lineetta_non_confonde(self):
        secondo = analizza_elenco(ELENCO)[1]
        self.assertEqual(secondo.titolo, "Ama il tuo quartiere — premiazione")

    def test_riga_di_attivita_non_ha_attivita_propria(self):
        terzo = analizza_elenco(ELENCO)[2]
        self.assertEqual(terzo.tipo, "attivita")
        self.assertEqual(terzo.slug, "pro-loco-cannetum")
        self.assertEqual(terzo.attivita, "")

    def test_scheda_e_valori_vuoti(self):
        scheda = analizza_scheda(SCHEDA)
        self.assertEqual(scheda["attivita"], "parrocchia")
        self.assertEqual(scheda["sommario"], "")
        self.assertEqual(scheda["slug"], "gita-sul-cansiglio-2026-09-05")

    def test_slug_dalla_scheda_dal_percorso_e_dagli_apici(self):
        self.assertEqual(estrai_slug(SCHEDA), "gita-sul-cansiglio-2026-09-05")
        self.assertEqual(estrai_slug("creato: /eventi/castagnata-2026"), "castagnata-2026")
        self.assertEqual(estrai_slug("fatto `torneo-di-calcetto`"), "torneo-di-calcetto")
        self.assertEqual(estrai_slug("nessuno slug qui"), "")


class ProveCampi(unittest.TestCase):
    def setUp(self):
        self.client = ClientMCP(url="http://esempio", chiave="x")
        self.client._schemi = {
            "crea_evento": {
                "properties": {
                    "titolo": {"type": "string"},
                    "attivita": {"type": "string"},
                    "icona": {"type": "string", "enum": ["chiesa", "montagna", ""]},
                    "pubblicato": {"type": "boolean"},
                }
            }
        }

    def test_toglie_campi_ignoti_vuoti_ed_enum_sbagliati(self):
        puliti = self.client.filtra_campi(
            "crea_evento",
            {
                "titolo": "Prova",
                "attivita": "",
                "icona": "inesistente",
                "inventato": "x",
                "pubblicato": False,
            },
        )
        self.assertEqual(puliti, {"titolo": "Prova", "pubblicato": False})

    def test_pubblicato_falso_non_viene_scartato(self):
        self.assertIn("pubblicato", self.client.filtra_campi("crea_evento", {"pubblicato": False}))


class ProveConfronti(unittest.TestCase):
    def test_normalizza_toglie_accenti_e_punteggiatura(self):
        self.assertEqual(normalizza("«Sulle orme di San Francesco»"), "sulle orme di san francesco")

    def test_titoli_quasi_uguali_si_somigliano(self):
        self.assertGreater(somiglianza("Gita sul Cansiglio", "Gita sul Cansiglio 2026"), 0.85)
        self.assertLess(somiglianza("Castagnata in oratorio", "Concerto degli Onde Beat"), 0.5)

    def test_parole_chiave_scarta_gli_articoli(self):
        self.assertNotIn("della", parole_chiave("La festa della famiglia"))

    def test_data_di(self):
        self.assertEqual(
            _data_di(Notizia(titolo="x", esito=Esito.crea, motivo="", confidenza=1,
                             inizio="2026-10-25T15:00")),
            date(2026, 10, 25),
        )
        self.assertIsNone(
            _data_di(Notizia(titolo="x", esito=Esito.crea, motivo="", confidenza=1, inizio=""))
        )
        self.assertIsNone(
            _data_di(Notizia(titolo="x", esito=Esito.crea, motivo="", confidenza=1,
                             inizio="il 25 ottobre"))
        )


def _riga(azione: Azione, titolo: str = "Prova", **campi) -> Riga:
    notizia = Notizia(titolo=titolo, esito=Esito.crea, motivo="", confidenza=0.9, **campi)
    return Riga(notizia=notizia, azione=azione)


class ProveReport(unittest.TestCase):
    def test_sezioni_vuote_non_compaiono(self):
        html = costruisci_html(Riepilogo(nome_file="x.pdf", righe=[_riga(Azione.pubblicato)]))
        self.assertIn("Eventi pubblicati", html)
        self.assertNotIn("Scartati", html)
        self.assertNotIn("Richiede attenzione umana", html)

    def test_niente_da_fare_quando_non_c_e_nulla(self):
        riepilogo = Riepilogo(nome_file="x.pdf")
        self.assertIn("Niente da fare", costruisci_html(riepilogo))
        self.assertIn("niente da fare", oggetto(riepilogo))

    def test_attenzione_umana_dice_che_non_ha_creato_nulla(self):
        html = costruisci_html(
            Riepilogo(nome_file="x.pdf", righe=[_riga(Azione.attenzione_umana, "Bilancio")])
        )
        self.assertIn("Non è stato creato nulla nel CMS", html)

    def test_i_titoli_vengono_sempre_scappati(self):
        html = costruisci_html(
            Riepilogo(nome_file="x.pdf", righe=[_riga(Azione.pubblicato, "<script>brutto</script>")])
        )
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_oggetto_segnala_la_prova(self):
        self.assertTrue(oggetto(Riepilogo(nome_file="x.pdf", dry_run=True)).startswith("[prova]"))


if __name__ == "__main__":
    unittest.main()
