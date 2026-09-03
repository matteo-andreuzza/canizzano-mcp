"""Il report HTML di fine esecuzione, e il suo invio via SMTP.

Parte una mail per ogni foglietto elaborato, anche quando non c'e' stato nulla
da fare: il silenzio non distingue «niente da segnalare» da «il bot e' morto».
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, formatdate
from html import escape

from .config import Config
from .models import Pagina
from .scrittura import Azione, Riga

log = logging.getLogger(__name__)

# Palette del sito: crema, terracotta, salvia.
CREMA = "#faf6f0"
INCHIOSTRO = "#2f2a26"
TENUE = "#6b625a"
TERRACOTTA = "#b4553a"
SALVIA = "#6b7f66"
BORDO = "#e6ddd1"


@dataclass
class Riepilogo:
    nome_file: str
    righe: list[Riga] = field(default_factory=list)
    pagine: list[Pagina] = field(default_factory=list)
    data_foglietto: str = ""
    note_analisi: str = ""
    errore_grave: str = ""
    dry_run: bool = False
    iniziato: datetime = field(default_factory=datetime.now)
    durata: float = 0.0

    def con(self, *azioni: Azione) -> list[Riga]:
        return [r for r in self.righe if r.azione in azioni]

    @property
    def pubblicati(self) -> list[Riga]:
        return self.con(Azione.pubblicato)

    @property
    def bozze(self) -> list[Riga]:
        return self.con(Azione.bozza)

    @property
    def con_articolo(self) -> list[Riga]:
        return [r for r in self.righe if r.slug_articolo]

    @property
    def scartati(self) -> list[Riga]:
        return self.con(Azione.scartato, Azione.duplicato)

    @property
    def attenzione(self) -> list[Riga]:
        return self.con(Azione.attenzione_umana)

    @property
    def errori(self) -> list[Riga]:
        return self.con(Azione.errore)


# --------------------------------------------------------------------------
# costruzione dell'HTML
# --------------------------------------------------------------------------


def _quando(riga: Riga) -> str:
    notizia = riga.notizia
    if notizia.etichetta_data:
        return notizia.etichetta_data
    if not notizia.inizio:
        return "senza data"
    grezza = notizia.inizio.replace("T", " ")
    try:
        momento = datetime.fromisoformat(notizia.inizio)
    except ValueError:
        return grezza
    if notizia.tutto_il_giorno:
        return momento.strftime("%d/%m/%Y")
    return momento.strftime("%d/%m/%Y, ore %H:%M")


def _dove(riga: Riga) -> str:
    return riga.notizia.luogo or riga.notizia.luogo_libero or ""


def _sezione(titolo: str, sottotitolo: str, corpo: str, colore: str = INCHIOSTRO) -> str:
    if not corpo:
        return ""
    return f"""
      <tr><td style="padding:28px 28px 0 28px;">
        <h2 style="margin:0 0 2px 0;font:600 17px/1.3 Georgia,'Times New Roman',serif;color:{colore};">{escape(titolo)}</h2>
        <p style="margin:0 0 12px 0;font:400 13px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:{TENUE};">{escape(sottotitolo)}</p>
        {corpo}
      </td></tr>"""


def _voce(
    titolo: str,
    dettagli: list[str],
    note: list[str] | None = None,
    colore_nota: str = TENUE,
    bordo: str = BORDO,
) -> str:
    righe_dettaglio = "".join(
        f'<div style="font:400 13px/1.6 -apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,sans-serif;color:{TENUE};">{d}</div>'
        for d in dettagli
        if d
    )
    righe_nota = "".join(
        f'<div style="margin-top:6px;font:400 13px/1.5 -apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,sans-serif;color:{colore_nota};">{n}</div>'
        for n in (note or [])
        if n
    )
    return f"""
        <div style="margin:0 0 12px 0;padding:12px 14px;background:#ffffff;border:1px solid {bordo};border-left:3px solid {bordo};border-radius:4px;">
          <div style="font:600 15px/1.4 Georgia,'Times New Roman',serif;color:{INCHIOSTRO};">{escape(titolo)}</div>
          {righe_dettaglio}{righe_nota}
        </div>"""


def _elenco_eventi(righe: list[Riga], mostra_dubbi: bool = False) -> str:
    pezzi = []
    for riga in righe:
        dettagli = [escape(f"{_quando(riga)}{' · ' + _dove(riga) if _dove(riga) else ''}")]
        if riga.notizia.attivita:
            dettagli.append(f"attività <code>{escape(riga.notizia.attivita)}</code>")
        if riga.slug_evento:
            dettagli.append(f"<code>{escape(riga.slug_evento)}</code>")
        if riga.notizia.sommario:
            dettagli.append(escape(riga.notizia.sommario))
        note = []
        if mostra_dubbi and riga.dubbi:
            note.append(
                "<strong>Perché è in bozza:</strong> "
                + escape("; ".join(riga.dubbi))
            )
        if mostra_dubbi:
            note.append(f"confidenza dichiarata: {riga.notizia.confidenza:.2f}")
        pezzi.append(
            _voce(
                riga.titolo,
                dettagli,
                note,
                bordo="#e8cdbf" if mostra_dubbi else BORDO,
            )
        )
    return "".join(pezzi)


def _elenco_articoli(righe: list[Riga]) -> str:
    pezzi = []
    for riga in righe:
        dettagli = [
            f"collegato all'evento <code>{escape(riga.slug_evento)}</code>",
            escape(riga.notizia.sottotitolo or riga.notizia.sommario or ""),
        ]
        if riga.azione is Azione.bozza:
            dettagli.append("<em>anche l'articolo resta in bozza</em>")
        pezzi.append(_voce(riga.notizia.titolo, dettagli))
    return "".join(pezzi)


def _elenco_scartati(righe: list[Riga]) -> str:
    if not righe:
        return ""
    voci = []
    for riga in righe:
        motivo = riga.motivo or "senza motivo dichiarato"
        rimando = (
            f" (già sul sito: <code>{escape(riga.duplicato_di)}</code>)"
            if riga.duplicato_di
            else ""
        )
        voci.append(
            f'<li style="margin:0 0 6px 0;"><strong>{escape(riga.titolo)}</strong> — '
            f"{escape(motivo)}{rimando}</li>"
        )
    return (
        '<ul style="margin:0;padding-left:18px;font:400 13px/1.6 -apple-system,'
        "BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:" + TENUE + ';">'
        + "".join(voci)
        + "</ul>"
    )


def _elenco_attenzione(righe: list[Riga]) -> str:
    pezzi = []
    for riga in righe:
        dettagli = [escape(riga.motivo or "il modello lo ha giudicato da valutare a mano")]
        if riga.notizia.pagina:
            dettagli.append(f"pagina del foglietto: {escape(riga.notizia.pagina.value)}")
        if riga.notizia.descrizione:
            estratto = riga.notizia.descrizione[:400]
            dettagli.append(f"<em>{escape(estratto)}{'…' if len(riga.notizia.descrizione) > 400 else ''}</em>")
        pezzi.append(
            f"""
        <div style="margin:0 0 12px 0;padding:14px 16px;background:#fdf1ec;border:1px solid #e8c3b4;border-left:4px solid {TERRACOTTA};border-radius:4px;">
          <div style="font:600 15px/1.4 Georgia,'Times New Roman',serif;color:{TERRACOTTA};">{escape(riga.titolo)}</div>
          {''.join(f'<div style="font:400 13px/1.6 -apple-system,BlinkMacSystemFont,&#39;Segoe UI&#39;,Roboto,sans-serif;color:{INCHIOSTRO};margin-top:4px;">{d}</div>' for d in dettagli if d)}
          <div style="margin-top:8px;font:600 12px/1.4 -apple-system,sans-serif;color:{TERRACOTTA};">Non è stato creato nulla nel CMS.</div>
        </div>"""
        )
    return "".join(pezzi)


def _elenco_errori(righe: list[Riga]) -> str:
    if not righe:
        return ""
    voci = "".join(
        f'<li style="margin:0 0 6px 0;"><strong>{escape(r.titolo)}</strong> — {escape(r.motivo)}</li>'
        for r in righe
    )
    return f'<ul style="margin:0;padding-left:18px;font:400 13px/1.6 monospace;color:{TERRACOTTA};">{voci}</ul>'


def costruisci_html(riepilogo: Riepilogo) -> str:
    conteggio = (
        f"{len(riepilogo.pubblicati)} pubblicati · {len(riepilogo.bozze)} in bozza · "
        f"{len(riepilogo.scartati)} scartati"
    )
    if riepilogo.attenzione:
        conteggio += f" · {len(riepilogo.attenzione)} da guardare a mano"

    sezioni = []

    if riepilogo.errore_grave:
        sezioni.append(
            _sezione(
                "L'esecuzione si è interrotta",
                "Il foglietto non è stato elaborato fino in fondo: verrà ritentato.",
                f'<pre style="margin:0;padding:12px;background:#fdf1ec;border:1px solid #e8c3b4;'
                f'border-radius:4px;font:400 12px/1.5 monospace;color:{TERRACOTTA};white-space:pre-wrap;">'
                f"{escape(riepilogo.errore_grave)}</pre>",
                TERRACOTTA,
            )
        )

    sezioni.append(
        _sezione(
            "Richiede attenzione umana",
            "Materiale che il bot ha deliberatamente non toccato.",
            _elenco_attenzione(riepilogo.attenzione),
            TERRACOTTA,
        )
    )
    sezioni.append(
        _sezione(
            "Eventi pubblicati",
            "Creati e già visibili al prossimo build del sito.",
            _elenco_eventi(riepilogo.pubblicati),
            SALVIA,
        )
    )
    sezioni.append(
        _sezione(
            "Articoli collegati",
            "Gli eventi che hanno ricevuto anche una pagina di approfondimento.",
            _elenco_articoli(riepilogo.con_articolo),
            SALVIA,
        )
    )
    sezioni.append(
        _sezione(
            "In bozza, da rivedere",
            "Creati ma non pubblicati: il bot non era abbastanza sicuro.",
            _elenco_eventi(riepilogo.bozze, mostra_dubbi=True),
        )
    )
    sezioni.append(
        _sezione(
            "Scartati",
            "Valutati e volutamente esclusi. Serve a controllare che il bot ragioni bene.",
            _elenco_scartati(riepilogo.scartati),
        )
    )
    sezioni.append(
        _sezione(
            "Errori",
            "Il CMS ha rifiutato queste scritture.",
            _elenco_errori(riepilogo.errori),
            TERRACOTTA,
        )
    )

    if not any(sezioni):
        sezioni.append(
            _sezione(
                "Niente da fare",
                "Il foglietto è stato letto ma non conteneva appuntamenti da mettere sul sito.",
                f'<p style="margin:0;font:400 14px/1.6 -apple-system,BlinkMacSystemFont,'
                f'\'Segoe UI\',Roboto,sans-serif;color:{TENUE};">Nessun avviso ha superato i filtri.</p>',
            )
        )

    note = ""
    if riepilogo.note_analisi:
        note = (
            f'<p style="margin:12px 0 0 0;font:400 12px/1.5 -apple-system,sans-serif;'
            f'color:{TENUE};"><strong>Note sulla scansione:</strong> '
            f"{escape(riepilogo.note_analisi)}</p>"
        )

    avviso_prova = ""
    if riepilogo.dry_run:
        avviso_prova = (
            '<div style="margin:0 0 16px 0;padding:10px 14px;background:#fff8e6;'
            'border:1px solid #e8d9a8;border-radius:4px;font:600 13px/1.5 -apple-system,sans-serif;'
            'color:#7a6320;">Esecuzione di prova: nel CMS non è stato scritto nulla.</div>'
        )

    pagine = ", ".join(f"p.{p.indice_nel_pdf} {p.tipo.value}" for p in riepilogo.pagine)

    return f"""<!doctype html>
<html lang="it"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Foglietto — {escape(riepilogo.nome_file)}</title></head>
<body style="margin:0;padding:0;background:{CREMA};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{CREMA};padding:24px 12px;">
<tr><td align="center">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:640px;background:{CREMA};border:1px solid {BORDO};border-radius:8px;overflow:hidden;">

  <tr><td style="padding:28px 28px 0 28px;">
    {avviso_prova}
    <div style="font:600 12px/1.4 -apple-system,sans-serif;letter-spacing:.08em;text-transform:uppercase;color:{TERRACOTTA};">canizzano.it · redazione automatica</div>
    <h1 style="margin:6px 0 4px 0;font:600 24px/1.25 Georgia,'Times New Roman',serif;color:{INCHIOSTRO};">Foglietto della settimana</h1>
    <p style="margin:0;font:400 13px/1.6 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:{TENUE};">
      {escape(riepilogo.nome_file)}{' · ' + escape(riepilogo.data_foglietto) if riepilogo.data_foglietto else ''}<br>
      elaborato il {riepilogo.iniziato.strftime('%d/%m/%Y alle %H:%M')} in {riepilogo.durata:.0f} secondi<br>
      {escape(conteggio)}{'<br>pagine riconosciute: ' + escape(pagine) if pagine else ''}
    </p>
    {note}
  </td></tr>
  {''.join(sezioni)}

  <tr><td style="padding:24px 28px 28px 28px;">
    <div style="border-top:1px solid {BORDO};padding-top:14px;font:400 12px/1.6 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:{TENUE};">
      <strong>Il sito non è ancora cambiato.</strong> Questi contenuti stanno nel CMS:
      diventano pagine solo dopo <code>./canizzano.sh tutto</code> dalla cartella del progetto.<br>
      Le bozze si rivedono nell'admin del CMS, sulla macchina che lo ospita.
    </div>
  </td></tr>

</table>
</td></tr></table>
</body></html>"""


def costruisci_testo(riepilogo: Riepilogo) -> str:
    """Versione in chiaro, per i client che non mostrano l'HTML."""
    parti = [
        f"Foglietto: {riepilogo.nome_file}",
        f"Elaborato il {riepilogo.iniziato.strftime('%d/%m/%Y alle %H:%M')}",
        "",
    ]
    for titolo, righe in (
        ("RICHIEDE ATTENZIONE UMANA", riepilogo.attenzione),
        ("EVENTI PUBBLICATI", riepilogo.pubblicati),
        ("IN BOZZA, DA RIVEDERE", riepilogo.bozze),
        ("SCARTATI", riepilogo.scartati),
        ("ERRORI", riepilogo.errori),
    ):
        if not righe:
            continue
        parti.append(titolo)
        for riga in righe:
            dettaglio = "; ".join(riga.dubbi) if riga.dubbi else riga.motivo
            parti.append(f"  - {riga.titolo}" + (f" — {dettaglio}" if dettaglio else ""))
        parti.append("")
    if riepilogo.errore_grave:
        parti += ["ESECUZIONE INTERROTTA", riepilogo.errore_grave, ""]
    parti.append("Il sito cambia solo dopo «./canizzano.sh tutto».")
    return "\n".join(parti)


def oggetto(riepilogo: Riepilogo) -> str:
    if riepilogo.errore_grave:
        return f"[canizzano.it] Foglietto non elaborato — {riepilogo.nome_file}"
    pezzi = []
    if riepilogo.pubblicati:
        pezzi.append(f"{len(riepilogo.pubblicati)} pubblicati")
    if riepilogo.bozze:
        pezzi.append(f"{len(riepilogo.bozze)} in bozza")
    if riepilogo.attenzione:
        pezzi.append(f"{len(riepilogo.attenzione)} da vedere")
    riassunto = ", ".join(pezzi) if pezzi else "niente da fare"
    prova = "[prova] " if riepilogo.dry_run else ""
    return f"{prova}[canizzano.it] Foglietto: {riassunto}"


# --------------------------------------------------------------------------
# invio
# --------------------------------------------------------------------------


def invia(cfg: Config, riepilogo: Riepilogo) -> None:
    messaggio = EmailMessage()
    messaggio["Subject"] = oggetto(riepilogo)
    messaggio["From"] = formataddr(("Redazione automatica canizzano.it", cfg.smtp_mittente))
    messaggio["To"] = cfg.email_destinatario
    messaggio["Date"] = formatdate(localtime=True)
    messaggio.set_content(costruisci_testo(riepilogo))
    messaggio.add_alternative(costruisci_html(riepilogo), subtype="html")

    contesto_ssl = ssl.create_default_context()
    if cfg.smtp_port == 465 or cfg.smtp_tls == "ssl":
        with smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, context=contesto_ssl, timeout=60) as posta:
            if cfg.smtp_user:
                posta.login(cfg.smtp_user, cfg.smtp_password)
            posta.send_message(messaggio)
    else:
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=60) as posta:
            posta.ehlo()
            if cfg.smtp_tls != "no":
                posta.starttls(context=contesto_ssl)
                posta.ehlo()
            if cfg.smtp_user:
                posta.login(cfg.smtp_user, cfg.smtp_password)
            posta.send_message(messaggio)
    log.info("Report inviato a %s", cfg.email_destinatario)


def salva_copia(cfg: Config, riepilogo: Riepilogo) -> None:
    """Una copia su disco: utile quando la posta non parte."""
    try:
        cfg.cartella_report.mkdir(parents=True, exist_ok=True)
        nome = f"{riepilogo.iniziato:%Y%m%d-%H%M}-{riepilogo.nome_file}.html"
        (cfg.cartella_report / nome).write_text(costruisci_html(riepilogo), encoding="utf-8")
    except OSError as errore:
        log.warning("Copia del report non salvata: %s", errore)
