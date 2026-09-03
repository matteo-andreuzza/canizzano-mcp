"""Configurazione: tutto da variabili d'ambiente, niente valori sparsi nel codice."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _stringa(nome: str, default: str = "") -> str:
    return os.environ.get(nome, default).strip()


def _intero(nome: str, default: int) -> int:
    grezzo = _stringa(nome)
    try:
        return int(grezzo) if grezzo else default
    except ValueError:
        return default


def _decimale(nome: str, default: float) -> float:
    grezzo = _stringa(nome)
    try:
        return float(grezzo.replace(",", ".")) if grezzo else default
    except ValueError:
        return default


def _booleano(nome: str, default: bool = False) -> bool:
    grezzo = _stringa(nome).lower()
    if not grezzo:
        return default
    return grezzo in {"1", "true", "si", "sì", "yes", "on"}


def _cartella(nome: str, default: str) -> Path:
    return Path(_stringa(nome, default)).expanduser()


class ErroreConfigurazione(RuntimeError):
    pass


@dataclass
class Config:
    # Gemini
    gemini_api_key: str
    gemini_model: str
    gemini_temperatura: float

    # CMS
    mcp_url: str
    mcp_key: str

    # Cartelle
    cartella_foglietti: Path
    cartella_stato: Path

    # Posta
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    smtp_mittente: str
    smtp_tls: str
    email_destinatario: str

    # Soglie e parametri
    attivita_predefinita: str
    soglia_pubblicazione: float
    soglia_duplicato: float
    giorni_dedup: int
    max_tentativi: int
    eta_minima_pdf: int
    dry_run: bool
    log_level: str

    @property
    def percorso_registro(self) -> Path:
        return self.cartella_stato / "registro.db"

    @property
    def cartella_report(self) -> Path:
        return self.cartella_stato / "report"

    def prepara_cartelle(self) -> None:
        self.cartella_foglietti.mkdir(parents=True, exist_ok=True)
        self.cartella_stato.mkdir(parents=True, exist_ok=True)
        self.cartella_report.mkdir(parents=True, exist_ok=True)

    def verifica(self, *, serve_gemini: bool = True, serve_posta: bool = True) -> None:
        """Alza un errore unico che elenca tutto quello che manca."""
        mancanti: list[str] = []
        if not self.mcp_key:
            mancanti.append("MCP_KEY")
        if not self.mcp_url:
            mancanti.append("MCP_URL")
        if serve_gemini and not self.gemini_api_key:
            mancanti.append("GEMINI_API_KEY")
        if serve_posta:
            if not self.smtp_host:
                mancanti.append("SMTP_HOST")
            if not self.smtp_mittente:
                mancanti.append("SMTP_MITTENTE")
            if not self.email_destinatario:
                mancanti.append("EMAIL_DESTINATARIO")
        if mancanti:
            raise ErroreConfigurazione(
                "Configurazione incompleta, mancano: " + ", ".join(mancanti)
                + ". Vedi .env.example."
            )


def carica(percorso_env: Path | None = None) -> Config:
    """Legge il .env accanto al pacchetto (o quello indicato) e costruisce la Config."""
    if percorso_env is None:
        percorso_env = Path(__file__).resolve().parent.parent / ".env"
    if percorso_env.exists():
        load_dotenv(percorso_env, override=False)

    return Config(
        gemini_api_key=_stringa("GEMINI_API_KEY"),
        gemini_model=_stringa("GEMINI_MODEL", "gemini-3.5-flash"),
        gemini_temperatura=_decimale("GEMINI_TEMPERATURA", 0.15),
        mcp_url=_stringa("MCP_URL", "http://localhost:8000/mcp/"),
        mcp_key=_stringa("MCP_KEY"),
        cartella_foglietti=_cartella("CARTELLA_FOGLIETTI", "~/foglietto-bot/foglietti"),
        cartella_stato=_cartella("CARTELLA_STATO", "~/foglietto-bot/stato"),
        smtp_host=_stringa("SMTP_HOST"),
        smtp_port=_intero("SMTP_PORT", 587),
        smtp_user=_stringa("SMTP_USER"),
        smtp_password=_stringa("SMTP_PASSWORD"),
        smtp_mittente=_stringa("SMTP_MITTENTE"),
        smtp_tls=_stringa("SMTP_TLS", "starttls").lower(),
        email_destinatario=_stringa("EMAIL_DESTINATARIO", "matteoandreuzza@outlook.it"),
        attivita_predefinita=_stringa("ATTIVITA_PREDEFINITA", "parrocchia"),
        soglia_pubblicazione=_decimale("SOGLIA_PUBBLICAZIONE", 0.80),
        soglia_duplicato=_decimale("SOGLIA_DUPLICATO", 0.82),
        giorni_dedup=_intero("GIORNI_DEDUP", 21),
        max_tentativi=_intero("MAX_TENTATIVI", 3),
        eta_minima_pdf=_intero("ETA_MINIMA_PDF", 60),
        dry_run=_booleano("DRY_RUN", False),
        log_level=_stringa("LOG_LEVEL", "INFO").upper(),
    )
