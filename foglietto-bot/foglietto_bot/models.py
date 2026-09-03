"""Lo schema dell'output strutturato di Gemini.

Questi modelli sono passati a Gemini come «response_schema»: il modello e'
costretto a rispondere in questa forma, cosi' lo stadio di scrittura non deve
interpretare prosa. I nomi dei campi ricalcano quelli dei tool MCP del CMS
(verificati su tools/list) per rendere la traduzione quasi diretta.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class TipoPagina(str, Enum):
    """Le quattro facciate del foglietto, riconosciute dal contenuto e non
    dalla posizione nel PDF: l'ordine delle pagine nella scansione non e'
    garantito."""

    copertina = "copertina"
    riflessione_evento = "riflessione_evento"
    intenzioni_messe = "intenzioni_messe"
    avvisi = "avvisi"
    altro = "altro"


class Esito(str, Enum):
    """Che cosa farne, deciso dal modello applicando la sequenza di filtro."""

    crea = "crea"
    scarta = "scarta"
    attenzione_umana = "attenzione_umana"


class Pagina(BaseModel):
    indice_nel_pdf: int = Field(description="Numero di pagina nel PDF, da 1.")
    tipo: TipoPagina
    come_l_ho_riconosciuta: str = Field(
        default="", description="L'indizio usato: intestazione, tabella, titolo."
    )


class Dettaglio(BaseModel):
    """Una riga della scheda pratica in testa all'articolo."""

    etichetta: str = Field(description="«Quando», «Ritrovo», «Quota»…")
    valore: str = Field(description="«ore 7.00 in piazza»")


class Notizia(BaseModel):
    # --- che cos'e' e che fine deve fare -----------------------------------
    titolo: str = Field(description="Titolo pulito, come apparirebbe sul sito.")
    esito: Esito
    motivo: str = Field(
        description=(
            "Perche' questo esito, in una riga. Se scartata il motivo finisce "
            "nel report: «non riguarda Canizzano», «duplicato interno», «e' la "
            "tabella delle messe»…"
        )
    )
    pagina: TipoPagina = Field(
        default=TipoPagina.altro, description="Da quale facciata arriva."
    )

    # --- campi che finiscono in crea_evento --------------------------------
    attivita: str = Field(
        default="",
        description=(
            "Slug della realta' del quartiere a cui appartiene, scelto fra "
            "quelle elencate nel prompt. Vuoto se non sei riuscito a decidere."
        ),
    )
    inizio: str = Field(
        default="",
        description="Data e ora italiane «2026-12-13T19:30». Vuoto se non c'e' una data.",
    )
    fine: str = Field(default="", description="Come «inizio», se il foglietto la dice.")
    tutto_il_giorno: bool = Field(
        default=False, description="Vero quando l'orario non e' indicato o non conta."
    )
    etichetta_data: str = Field(
        default="",
        description=(
            "Come si scrive la data quando non e' un giorno preciso: «meta' "
            "quaresima», «un sabato di maggio». Vuoto se la data e' precisa."
        ),
    )
    sommario: str = Field(default="", description="Una o due righe per le card.")
    descrizione: str = Field(
        default="", description="Il testo dell'evento, solo fatti presi dal foglietto."
    )
    categoria: str = Field(default="", description="Etichetta breve: «Musica», «Gita»…")
    icona: str = Field(
        default="", description="Una delle icone ammesse elencate nel prompt, o vuoto."
    )
    luogo: str = Field(
        default="", description="Nome esatto di un luogo gia' salvato nel CMS, se coincide."
    )
    luogo_libero: str = Field(
        default="", description="Sede occasionale, se non e' fra i luoghi salvati."
    )
    ingresso: str = Field(default="", description="«Ingresso libero», «Offerta libera», «5 €».")
    prenotazione_entro: str = Field(
        default="", description="Data «2026-12-13», se c'e' un termine per prenotarsi."
    )
    come_partecipare: str = Field(
        default="", description="Iscrizioni, contatti, ritrovo: i fatti pratici."
    )

    # --- eventuale articolo di approfondimento -----------------------------
    merita_articolo: bool = Field(
        default=False,
        description=(
            "Vero solo se c'e' davvero altro da dire oltre a data e luogo: un "
            "ritrovo, una quota, un percorso, una storia. L'articolo e' "
            "l'eccezione, non la regola."
        ),
    )
    occhiello: str = Field(default="", description="La riga sopra il titolo dell'articolo.")
    sottotitolo: str = Field(default="", description="Il sommario in apertura dell'articolo.")
    corpo_articolo: str = Field(
        default="",
        description=(
            "Il testo dell'articolo. Riga vuota = nuovo paragrafo; «## » = "
            "sottotitolo; «- » = voce di elenco; «> » = nota in evidenza."
        ),
    )
    dettagli: list[Dettaglio] = Field(
        default_factory=list, description="La scheda pratica in testa all'articolo."
    )

    # --- quanto ti fidi ----------------------------------------------------
    confidenza: float = Field(
        description=(
            "Da 0 a 1: quanto sei sicuro dell'insieme (che vada inserito, "
            "dell'attivita' scelta, della qualita' dell'estrazione)."
        )
    )
    dubbi: list[str] = Field(
        default_factory=list,
        description="I punti su cui non sei sicuro. Se ce n'e' uno il contenuto resta in bozza.",
    )
    possibile_duplicato_di: str = Field(
        default="",
        description=(
            "Slug dell'evento gia' presente nel CMS di cui questa notizia "
            "sembra la ripetizione, se ne riconosci uno fra quelli elencati."
        ),
    )


class AnalisiFoglietto(BaseModel):
    data_foglietto: str = Field(
        default="",
        description="La domenica a cui si riferisce il foglietto, «2026-09-06».",
    )
    pagine: list[Pagina] = Field(default_factory=list)
    notizie: list[Notizia] = Field(default_factory=list)
    note: str = Field(
        default="", description="Osservazioni sulla scansione: pagine illeggibili, storte…"
    )
