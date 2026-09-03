"""Client per il server MCP del CMS.

Il server parla JSON-RPC 2.0 puro su POST, senza sessioni: ogni richiesta e'
autosufficiente e porta con se' l'header «Authorization: Bearer <chiave>».
Le risposte dei tool non sono JSON ma Markdown pensato per essere letto da un
agente, quindi qui sotto c'e' anche il minimo di parsing che serve al bot
(risultati di ricerca, schede di contenuto, slug di quanto appena creato).
"""

from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any

import requests

log = logging.getLogger(__name__)

# Il bot non cancella niente, mai. La chiave MCP dedicata non deve avere il
# permesso di farlo (si imposta in admin), questa e' la seconda serratura.
TOOL_VIETATI = frozenset({"elimina_contenuto"})

# Tool che scrivono: in modalita' di prova vengono simulati.
TOOL_DI_SCRITTURA = frozenset(
    {
        "crea_evento",
        "aggiorna_evento",
        "crea_articolo",
        "aggiorna_articolo",
        "crea_giornata",
        "aggiorna_giornata",
        "crea_foto",
        "aggiorna_foto",
        "crea_attivita",
        "aggiorna_attivita",
        "crea_edizione",
        "aggiorna_edizione",
        "crea_luogo",
        "aggiorna_luogo",
        "crea_album",
        "aggiorna_album",
        "imposta_dettagli_articolo",
        "aggiorna_impostazioni",
        "carica_immagine",
    }
)


class ErroreMCP(RuntimeError):
    pass


class ErroreAutenticazione(ErroreMCP):
    pass


class ToolVietato(ErroreMCP):
    pass


# --------------------------------------------------------------------------
# parsing del Markdown che il server restituisce
# --------------------------------------------------------------------------

_RIGA_RISULTATO = re.compile(
    r"^-\s+"
    r"(?:(?P<data>\d{2}/\d{2}/\d{4})(?:\s+(?P<ora>\d{1,2}:\d{2}))?\s*[—-]\s*)?"
    r"\*\*(?P<titolo>.+?)\*\*"
    r"(?:\s*\((?P<meta>[^)]*)\))?"
)
_INTESTAZIONE_SEZIONE = re.compile(r"^###\s+(?P<tipo>[a-zà-ù ]+?)\s*(?:\(\d+\))?\s*$", re.I)
_CAMPO_SCHEDA = re.compile(r"^-\s+\*\*(?P<campo>[a-z_0-9]+)\*\*:\s*(?P<valore>.*)$")
_BACKTICK = re.compile(r"`([^`]+)`")
_SLUG_VALIDO = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# Le sezioni dell'elenco sono al plurale, i tipi dei tool al singolare.
_TIPI_SEZIONE = {
    "eventi": "evento",
    "articoli": "articolo",
    "giornate": "giornata",
    "foto": "foto",
    "attivita": "attivita",
    "attività": "attivita",
    "edizioni": "edizione",
    "luoghi": "luogo",
    "album": "album",
    "dettagli": "dettaglio",
}


@dataclass
class Risultato:
    """Una riga dell'elenco restituito da «cerca_contenuti» o «panoramica»."""

    tipo: str
    titolo: str
    slug: str = ""
    attivita: str = ""
    data: str = ""
    ora: str = ""
    riga: str = ""

    @property
    def data_iso(self) -> str:
        """«05/09/2026» -> «2026-09-05». Vuoto se la riga non aveva data."""
        if not self.data:
            return ""
        giorno, mese, anno = self.data.split("/")
        return f"{anno}-{mese}-{giorno}"


def analizza_elenco(testo: str) -> list[Risultato]:
    """Legge le righe «- 05/09/2026 06:45 — **Titolo** (`slug`, attività `x`)»."""
    risultati: list[Risultato] = []
    tipo_corrente = ""
    for riga in testo.splitlines():
        riga = riga.rstrip()
        intestazione = _INTESTAZIONE_SEZIONE.match(riga.strip())
        if intestazione:
            tipo_corrente = intestazione.group("tipo").strip().lower()
            continue
        trovata = _RIGA_RISULTATO.match(riga.strip())
        if not trovata:
            continue
        meta = trovata.group("meta") or ""
        codici = _BACKTICK.findall(meta)
        slug = ""
        for codice in codici:
            if _SLUG_VALIDO.match(codice):
                slug = codice
                break
        attivita = ""
        fra_attivita = re.search(r"attivit[aà]\s+`([^`]+)`", meta)
        if fra_attivita:
            attivita = fra_attivita.group(1)
            if attivita == slug:
                # riga di un'attivita': lo slug e' quello, non un "di chi e'"
                attivita = ""
        risultati.append(
            Risultato(
                tipo=_TIPI_SEZIONE.get(tipo_corrente, tipo_corrente),
                titolo=trovata.group("titolo").strip(),
                slug=slug,
                attivita=attivita,
                data=trovata.group("data") or "",
                ora=trovata.group("ora") or "",
                riga=riga.strip(),
            )
        )
    return risultati


def analizza_scheda(testo: str) -> dict[str, str]:
    """Legge le righe «- **campo**: valore» di «leggi_contenuto»."""
    scheda: dict[str, str] = {}
    for riga in testo.splitlines():
        trovata = _CAMPO_SCHEDA.match(riga.strip())
        if not trovata:
            continue
        valore = trovata.group("valore").strip()
        if valore in {"—", "-", "–"}:
            valore = ""
        scheda[trovata.group("campo")] = valore.strip("`")
    return scheda


def estrai_slug(testo: str) -> str:
    """Ricava lo slug dalla risposta di un «crea_*».

    La forma esatta della risposta non e' documentata, quindi si prova in
    ordine: la riga della scheda, un percorso /eventi/<slug>, il primo codice
    fra apici inversi che somigli a uno slug.
    """
    scheda = analizza_scheda(testo)
    if scheda.get("slug"):
        return scheda["slug"]
    percorso = re.search(r"/(?:eventi|articoli|attivita|luoghi)/([a-z0-9-]+)", testo)
    if percorso:
        return percorso.group(1)
    for codice in _BACKTICK.findall(testo):
        if _SLUG_VALIDO.match(codice) and not codice.endswith((".jpg", ".png")):
            return codice
    return ""


def slugifica(testo: str) -> str:
    """Solo per simulare uno slug in modalita' di prova: il CMS genera i suoi."""
    normale = unicodedata.normalize("NFKD", testo).encode("ascii", "ignore").decode()
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", normale.lower())).strip("-")


# --------------------------------------------------------------------------
# il client vero e proprio
# --------------------------------------------------------------------------


@dataclass
class ClientMCP:
    url: str
    chiave: str
    timeout: int = 90
    tentativi: int = 3
    dry_run: bool = False
    _id: int = field(default=0, init=False)
    _schemi: dict[str, dict] | None = field(default=None, init=False, repr=False)
    _sessione: requests.Session | None = field(default=None, init=False, repr=False)

    # -- trasporto ---------------------------------------------------------

    @property
    def sessione(self) -> requests.Session:
        if self._sessione is None:
            self._sessione = requests.Session()
            self._sessione.headers.update(
                {
                    "Authorization": f"Bearer {self.chiave}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                }
            )
        return self._sessione

    def _rpc(self, metodo: str, parametri: dict[str, Any] | None = None) -> dict:
        self._id += 1
        corpo = {"jsonrpc": "2.0", "id": self._id, "method": metodo, "params": parametri or {}}
        ultimo_errore: Exception | None = None
        for tentativo in range(1, self.tentativi + 1):
            try:
                risposta = self.sessione.post(self.url, json=corpo, timeout=self.timeout)
                if risposta.status_code >= 500:
                    raise ErroreMCP(f"HTTP {risposta.status_code} dal server MCP")
                if risposta.status_code in (401, 403):
                    raise ErroreAutenticazione(
                        f"HTTP {risposta.status_code}: la chiave MCP non e' valida "
                        "o non ha i permessi per questa operazione."
                    )
                risposta.raise_for_status()
                dati = risposta.json()
            except (requests.RequestException, ValueError, ErroreMCP) as errore:
                if isinstance(errore, ErroreAutenticazione):
                    raise
                ultimo_errore = errore
                if tentativo == self.tentativi:
                    break
                attesa = 2**tentativo
                log.warning("MCP %s fallito (%s), riprovo fra %ss", metodo, errore, attesa)
                time.sleep(attesa)
                continue
            if "error" in dati:
                errore_rpc = dati["error"]
                raise ErroreMCP(
                    f"{metodo}: {errore_rpc.get('message', errore_rpc)} "
                    f"(codice {errore_rpc.get('code', '?')})"
                )
            return dati.get("result", {})
        raise ErroreMCP(f"{metodo}: nessuna risposta dal server MCP ({ultimo_errore})")

    # -- tool --------------------------------------------------------------

    def chiama(self, nome: str, argomenti: dict[str, Any] | None = None) -> str:
        """Esegue un tool e restituisce il testo della risposta."""
        if nome in TOOL_VIETATI:
            raise ToolVietato(
                f"«{nome}» non e' permesso a questo bot: non cancella mai contenuti."
            )
        argomenti = argomenti or {}
        if self.dry_run and nome in TOOL_DI_SCRITTURA:
            log.info("[prova] %s %s", nome, json.dumps(argomenti, ensure_ascii=False)[:400])
            finto = slugifica(
                str(
                    argomenti.get("titolo")
                    or argomenti.get("nome")
                    or argomenti.get("evento")
                    or "prova"
                )
            )
            return f"[modalita' di prova] nessuna scrittura eseguita\n- **slug**: {finto}"

        risultato = self._rpc("tools/call", {"name": nome, "arguments": argomenti})
        testo = "\n".join(
            parte.get("text", "")
            for parte in risultato.get("content", [])
            if parte.get("type") == "text"
        ).strip()
        if risultato.get("isError"):
            raise ErroreMCP(f"«{nome}» ha risposto con un errore: {testo}")
        return testo

    # -- schemi (per validare quello che mandiamo) -------------------------

    def schemi(self) -> dict[str, dict]:
        if self._schemi is None:
            elenco = self._rpc("tools/list").get("tools", [])
            self._schemi = {t["name"]: t.get("inputSchema", {}) for t in elenco}
        return self._schemi

    def valori_ammessi(self, tool: str, campo: str) -> list[str]:
        proprieta = self.schemi().get(tool, {}).get("properties", {}).get(campo, {})
        return [v for v in proprieta.get("enum", []) if v]

    def filtra_campi(self, tool: str, campi: dict[str, Any]) -> dict[str, Any]:
        """Tiene solo i campi che il tool conosce davvero.

        Lo schema arriva dal server, non da una copia locale: se il CMS cambia
        un campo il bot smette di mandarlo invece di far fallire la chiamata.
        """
        proprieta = self.schemi().get(tool, {}).get("properties", {})
        puliti: dict[str, Any] = {}
        for chiave, valore in campi.items():
            if chiave not in proprieta:
                log.debug("%s: campo «%s» ignorato, non e' nello schema", tool, chiave)
                continue
            if valore is None or valore == "" or valore == []:
                continue
            ammessi = [v for v in proprieta[chiave].get("enum", []) if v]
            if ammessi and isinstance(valore, str) and valore not in ammessi:
                log.debug("%s: valore «%s» fuori enum per «%s», lo tolgo", tool, valore, chiave)
                continue
            puliti[chiave] = valore
        return puliti

    # -- comodita' ---------------------------------------------------------

    def guida(self) -> str:
        return self.chiama("guida_del_sito")

    def panoramica(self) -> str:
        return self.chiama("panoramica")

    def cerca(self, **argomenti: Any) -> list[Risultato]:
        return analizza_elenco(self.chiama("cerca_contenuti", argomenti))

    def leggi(self, tipo: str, id_o_slug: str) -> dict[str, str]:
        return analizza_scheda(self.chiama("leggi_contenuto", {"tipo": tipo, "id_o_slug": id_o_slug}))

    def attivita(self) -> list[Risultato]:
        """Le realta' del quartiere davvero esistenti nel CMS."""
        return [r for r in self.cerca(tipo="attivita", limite=50) if r.slug]

    def luoghi(self) -> list[str]:
        return [r.titolo for r in self.cerca(tipo="luogo", limite=50)]

    def crea_evento(self, campi: dict[str, Any]) -> tuple[str, str]:
        """Crea l'evento e restituisce (slug, risposta testuale)."""
        risposta = self.chiama("crea_evento", self.filtra_campi("crea_evento", campi))
        return estrai_slug(risposta), risposta

    def crea_articolo(self, campi: dict[str, Any]) -> tuple[str, str]:
        risposta = self.chiama("crea_articolo", self.filtra_campi("crea_articolo", campi))
        return estrai_slug(risposta), risposta
