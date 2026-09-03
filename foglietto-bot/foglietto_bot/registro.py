"""Registro persistente dei foglietti processati (idempotenza) e di cosa hanno prodotto.

L'identita' di un foglietto e' l'hash del contenuto, non il nome del file:
lo scraper puo' riscaricare lo stesso PDF con un altro nome senza che venga
rielaborato, e un PDF davvero nuovo viene visto anche se il nome si ripete.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS foglietti (
    hash          TEXT PRIMARY KEY,
    nome_file     TEXT NOT NULL,
    percorso      TEXT NOT NULL,
    visto_il      TEXT NOT NULL,
    aggiornato_il TEXT NOT NULL,
    stato         TEXT NOT NULL,          -- in_corso | completato | errore
    tentativi     INTEGER NOT NULL DEFAULT 0,
    errore        TEXT
);
CREATE TABLE IF NOT EXISTS contenuti (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    hash_foglietto TEXT NOT NULL REFERENCES foglietti(hash),
    tipo           TEXT NOT NULL,         -- evento | articolo
    slug           TEXT,
    titolo         TEXT NOT NULL,
    pubblicato     INTEGER NOT NULL,
    creato_il      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_contenuti_foglietto ON contenuti(hash_foglietto);
"""


def impronta(pdf: Path) -> str:
    digestore = hashlib.sha256()
    with pdf.open("rb") as f:
        for blocco in iter(lambda: f.read(1 << 20), b""):
            digestore.update(blocco)
    return digestore.hexdigest()


def _adesso() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class Foglietto:
    hash: str
    percorso: Path
    nome_file: str
    stato: str = "nuovo"
    tentativi: int = 0


class Registro:
    def __init__(self, percorso_db: Path):
        percorso_db.parent.mkdir(parents=True, exist_ok=True)
        self.connessione = sqlite3.connect(percorso_db)
        self.connessione.row_factory = sqlite3.Row
        self.connessione.executescript(SCHEMA)
        self.connessione.commit()

    def chiudi(self) -> None:
        self.connessione.close()

    def __enter__(self) -> "Registro":
        return self

    def __exit__(self, *_: object) -> None:
        self.chiudi()

    # -- stato dei foglietti ----------------------------------------------

    def stato(self, hash_pdf: str) -> sqlite3.Row | None:
        with closing(self.connessione.execute(
            "SELECT * FROM foglietti WHERE hash = ?", (hash_pdf,)
        )) as cursore:
            return cursore.fetchone()

    def da_elaborare(
        self,
        cartella: Path,
        max_tentativi: int,
        forza: bool = False,
        eta_minima: int = 60,
    ) -> list[Foglietto]:
        """I PDF nella cartella che non sono ancora stati portati a termine.

        Un file appena toccato viene saltato: lo scraper potrebbe averlo ancora
        aperto in scrittura, e leggerlo a meta' darebbe un'analisi sbagliata e
        un hash che non tornera' mai piu'.
        """
        trovati: list[Foglietto] = []
        adesso = time.time()
        for pdf in sorted(cartella.glob("*.pdf")):
            if not pdf.is_file():
                continue
            if adesso - pdf.stat().st_mtime < eta_minima:
                log.info("%s e' appena arrivato: lo guardo al giro dopo", pdf.name)
                continue
            hash_pdf = impronta(pdf)
            riga = self.stato(hash_pdf)
            if riga and not forza:
                if riga["stato"] == "completato":
                    continue
                if riga["stato"] == "errore" and riga["tentativi"] >= max_tentativi:
                    continue
            trovati.append(
                Foglietto(
                    hash=hash_pdf,
                    percorso=pdf,
                    nome_file=pdf.name,
                    stato=riga["stato"] if riga else "nuovo",
                    tentativi=riga["tentativi"] if riga else 0,
                )
            )
        return trovati

    def inizia(self, foglietto: Foglietto) -> None:
        self.connessione.execute(
            """
            INSERT INTO foglietti (hash, nome_file, percorso, visto_il, aggiornato_il, stato, tentativi)
            VALUES (?, ?, ?, ?, ?, 'in_corso', 1)
            ON CONFLICT(hash) DO UPDATE SET
                stato = 'in_corso',
                aggiornato_il = excluded.aggiornato_il,
                nome_file = excluded.nome_file,
                percorso = excluded.percorso,
                tentativi = foglietti.tentativi + 1
            """,
            (foglietto.hash, foglietto.nome_file, str(foglietto.percorso), _adesso(), _adesso()),
        )
        self.connessione.commit()

    def completa(self, hash_pdf: str) -> None:
        self.connessione.execute(
            "UPDATE foglietti SET stato = 'completato', errore = NULL, aggiornato_il = ? WHERE hash = ?",
            (_adesso(), hash_pdf),
        )
        self.connessione.commit()

    def fallisci(self, hash_pdf: str, errore: str) -> None:
        self.connessione.execute(
            "UPDATE foglietti SET stato = 'errore', errore = ?, aggiornato_il = ? WHERE hash = ?",
            (errore[:2000], _adesso(), hash_pdf),
        )
        self.connessione.commit()

    def elenco(self, limite: int = 30) -> list[sqlite3.Row]:
        with closing(self.connessione.execute(
            """
            SELECT f.*, (SELECT COUNT(*) FROM contenuti c WHERE c.hash_foglietto = f.hash) AS creati
            FROM foglietti f ORDER BY f.visto_il DESC LIMIT ?
            """,
            (limite,),
        )) as cursore:
            return cursore.fetchall()

    def ultimo_completato(self) -> sqlite3.Row | None:
        with closing(self.connessione.execute(
            "SELECT * FROM foglietti WHERE stato = 'completato' ORDER BY aggiornato_il DESC LIMIT 1"
        )) as cursore:
            return cursore.fetchone()

    # -- cosa e' stato creato ---------------------------------------------

    def registra_contenuto(
        self, hash_pdf: str, tipo: str, slug: str, titolo: str, pubblicato: bool
    ) -> None:
        self.connessione.execute(
            """
            INSERT INTO contenuti (hash_foglietto, tipo, slug, titolo, pubblicato, creato_il)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (hash_pdf, tipo, slug, titolo, int(pubblicato), _adesso()),
        )
        self.connessione.commit()

    def contenuti_gia_creati(self, limite: int = 200) -> list[sqlite3.Row]:
        """Serve alla deduplica: quello che il bot ha inserito nelle settimane scorse."""
        with closing(self.connessione.execute(
            "SELECT tipo, slug, titolo, creato_il FROM contenuti ORDER BY id DESC LIMIT ?",
            (limite,),
        )) as cursore:
            return cursore.fetchall()
