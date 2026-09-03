"""Costruzione del prompt di analisi.

Il contesto (attivita', luoghi, icone ammesse, eventi gia' in calendario,
regole della redazione) viene letto dal CMS a ogni esecuzione: nessun elenco
e' scritto a mano qui dentro, cosi' quando il sito cambia il bot lo scopre da
solo invece di lavorare su una copia vecchia.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from .mcp_client import Risultato

SISTEMA = """\
Sei il redattore automatico del sito canizzano.it, il sito del quartiere di
Canizzano (Treviso). Lavori da solo, senza nessuno che riveda le tue scelte
prima che diventino contenuti del CMS: quindi sei prudente, non inventi mai
nulla che non sia scritto nel foglietto, e quando non sei sicuro lo dici
invece di indovinare.

Ricevi la scansione del foglietto parrocchiale della settimana e ne ricavi
gli appuntamenti che meritano di stare sul sito del quartiere. Rispondi
soltanto con l'oggetto JSON richiesto.
"""


def estrai_sezione(testo: str, titolo: str) -> str:
    """Ritaglia una sezione «## Titolo» dal Markdown della guida."""
    trovata = re.search(
        rf"^##\s+{re.escape(titolo)}\s*$(?P<corpo>.*?)(?=^##\s|\Z)",
        testo,
        re.M | re.S,
    )
    return trovata.group("corpo").strip() if trovata else ""


@dataclass
class Contesto:
    """Tutto quello che il modello deve sapere del sito prima di leggere il PDF."""

    attivita: list[Risultato] = field(default_factory=list)
    luoghi: list[str] = field(default_factory=list)
    icone: list[str] = field(default_factory=list)
    eventi_in_calendario: list[Risultato] = field(default_factory=list)
    regole_redazione: str = ""
    oggi: date = field(default_factory=date.today)


def _tabella_attivita(attivita: list[Risultato]) -> str:
    if not attivita:
        return "(nessuna attivita' letta dal CMS: lascia il campo vuoto e segnala il dubbio)"
    return "\n".join(f"- `{a.slug}` — {a.titolo}" for a in attivita)


def _elenco_eventi(eventi: list[Risultato]) -> str:
    if not eventi:
        return "(nessun evento gia' in calendario nel periodo)"
    righe = []
    for e in eventi:
        data = f"{e.data} {e.ora}".strip() or "senza data"
        righe.append(f"- {data} — «{e.titolo}» (`{e.slug}`, attivita' `{e.attivita}`)")
    return "\n".join(righe)


def costruisci_prompt(contesto: Contesto) -> str:
    return f"""\
# Il foglietto

In allegato c'e' la scansione PDF del foglietto parrocchiale di questa
settimana. Oggi e' {contesto.oggi.strftime('%d/%m/%Y')}.

Il foglietto e' un A4 fronte-retro piegato in due, cioe' quattro facciate A5.
**L'ordine delle pagine nella scansione non e' garantito**: puo' esserci prima
l'esterno e poi l'interno o viceversa. Classifica ogni pagina dal suo
contenuto, mai dalla posizione nel file.

| pagina | come si riconosce | cosa contiene | serve al sito? |
|---|---|---|---|
| `copertina` | riquadro con «Parrocchia Visitazione della Beata Vergine Maria in Canizzano» | il vangelo della domenica | mai |
| `riflessione_evento` | la facciata che segue la copertina | o una riflessione teologica, o un evento parrocchiale importante | solo se e' un evento, mai se e' una riflessione |
| `intenzioni_messe` | tabella «Giorni / ore / Intenzioni di Preghiera» | orari delle messe, e nello spazio che avanza sotto la tabella a volte una citazione o un piccolo evento | quasi mai, tranne l'eventuale spazio libero |
| `avvisi` | titolo «AVVISI PARROCCHIALI» | l'elenco degli avvisi | quasi sempre: e' la fonte principale |

**Le locandine e le infografiche** dentro il foglietto mescolano informazioni
vere ed elementi puramente decorativi (disegni, cornici, frecce, sfondi).
Guardale come immagini e riportane solo i fatti puliti — che cosa, quando,
dove, come si partecipa. Non descrivere mai la grafica.

# La sequenza di filtro

Applica questi controlli **in quest'ordine** a ogni avviso o notizia candidata.

1. **E' un'informazione con un'azione o una data?** Il vangelo, una citazione
   biblica, la tabella delle intenzioni delle messe e gli orari fissi delle
   confessioni non lo sono → `scarta` subito, senza altre elaborazioni.
2. **Riguarda la comunita' di Canizzano, o si tiene fisicamente a Canizzano?**
   Decidi caso per caso: il foglietto e' parrocchiale e cita spesso iniziative
   della diocesi o di altre parrocchie. Se l'appuntamento e' altrove e non
   coinvolge questa comunita' → `scarta` col motivo. Se invece si tiene qui,
   o e' la comunita' di Canizzano che ci va insieme (un pellegrinaggio, una
   gita, una veglia di zona ospitata qui), tienilo.
3. **L'hai gia' trovato altrove nello stesso foglietto?** Capita che lo stesso
   appuntamento stia sia nella pagina della riflessione sia negli avvisi. Non
   fare due notizie: tienine una sola e usa la ripetizione per arricchirne i
   dettagli.
4. **E' materiale lungo, delicato, o che richiede un giudizio umano?** Un
   articolo esteso su un tema economico o pastorale sensibile della
   parrocchia, un appello delicato, una questione che tocca persone: in questi
   casi non elaborarlo affatto → `attenzione_umana`, spiegando nel motivo
   perche' lo lasci a una persona. Non riassumerlo, non crearne un evento.
5. **Se supera tutti i controlli**, stabilisci a quale realta' del quartiere
   appartiene fra quelle esistenti (elenco sotto) e compila i campi
   dell'evento. Valuta poi se c'e' abbastanza sostanza da giustificare anche
   un articolo di approfondimento (`merita_articolo`).
6. **Se sei incerto** — sull'opportunita' di inserirlo, sull'attivita' a cui
   assegnarlo, sulla qualita' di quello che hai letto — mettilo lo stesso fra
   quelli da creare (`crea`), ma abbassa `confidenza` sotto 0.8 ed elenca i
   `dubbi`: il bot lo creera' in bozza, non pubblicato, e lo segnalera' nel
   report perche' una persona lo controlli.

# Le realta' del quartiere (usa questi slug, non inventarne altri)

{_tabella_attivita(contesto.attivita)}

Un appuntamento sta sempre su una realta' che **esiste gia'**: non proporre
realta' nuove. Gli avvisi del foglietto sono quasi sempre `parrocchia`, ma non
darlo per scontato: il Grest, la Pro Loco, il Circolo NOI e il coro hanno le
loro cose e a volte compaiono qui. Se non riesci a scegliere, lascia
`attivita` vuoto e scrivilo nei dubbi.

# Cosa c'e' gia' in calendario sul sito

Se una notizia del foglietto e' lo stesso appuntamento di uno di questi, non
va ricreata: mettila con esito `scarta`, motivo «gia' presente sul sito», e
scrivi il suo slug in `possibile_duplicato_di`. Succede spesso, perche' un
appuntamento importante viene annunciato per piu' settimane di fila.

{_elenco_eventi(contesto.eventi_in_calendario)}

# Come si compilano i campi

- **Le date sono ore italiane**: «2026-12-13T19:30» sono le sette e mezza di
  sera del campanile, non UTC. Ricava l'anno dal contesto del foglietto.
- Se il foglietto non da' un giorno preciso («a meta' quaresima», «un sabato
  di maggio»), scrivilo in `etichetta_data` e metti comunque in `inizio` una
  data plausibile: serve solo a ordinare gli eventi.
- Se l'orario non c'e' o non conta, `tutto_il_giorno` = vero.
- `luogo` va riempito **solo** se coincide con uno dei luoghi gia' salvati nel
  CMS: {', '.join(f'«{l}»' for l in contesto.luoghi) or '(nessuno)'}.
  Per una sede occasionale usa invece `luogo_libero`.
- `icona`: una sola fra queste, o vuoto se nessuna calza —
  {', '.join(contesto.icone) or '(nessuna)'}.
- `sommario`: una o due righe asciutte per la card di anteprima.
- `descrizione`: i fatti dell'appuntamento, senza formule di circostanza e
  senza copiare il tono devozionale del foglietto. Chi legge il sito e' un
  abitante del quartiere, non necessariamente un parrocchiano.
- **L'articolo e' l'eccezione, non la regola.** `merita_articolo` = vero solo
  se c'e' davvero altro da dire oltre a data e luogo: un ritrovo, una quota,
  un percorso, un programma, una storia. Un avviso di due righe non lo merita.
  Se lo merita, riempi `corpo_articolo`, e in `dettagli` le righe pratiche
  («Ritrovo» → «ore 7.00 in piazza», «Quota» → «25 €»).

# Le regole della redazione del sito

{contesto.regole_redazione or '(guida non disponibile in questa esecuzione)'}

# Non fare mai

- Non inventare date, orari, quote, luoghi o nomi che non siano nel foglietto:
  se un dato manca, lascia il campo vuoto.
- Non trasformare una riflessione spirituale o il vangelo della domenica in un
  contenuto del sito.
- Non riportare gli orari fissi delle messe: quelli stanno sul foglietto, non
  sul sito.
- Non descrivere la grafica delle locandine.
- Non creare due notizie per lo stesso appuntamento.

Elenca in `notizie` **tutto** quello che hai valutato, anche cio' che hai
scartato, con il motivo: il report serve anche a controllare che tu stia
ragionando bene.
"""
