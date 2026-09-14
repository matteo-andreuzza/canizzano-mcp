"""Dashboard di monitoraggio del Raspberry Pi di canizzano.it, per lo sviluppatore.

Raccoglie in una pagina quello che altrimenti si guarda da quattro terminali
ssh: carico/temperatura/disco dell'host, i container Docker dei due stack
(canizzano e foglietto-bot), la salute di CMS e bot, il registro condiviso
attivita.log, il registro.db del bot e i log dei container. Qualche azione
(prova-report, verifica, riavvio di un container) con un bottone.

Solo libreria standard: parla con Docker via socket unix, legge l'host da
/proc e /sys montati in sola lettura. Protetto da HTTP Basic (MONITOR_TOKEN).
"""

from __future__ import annotations

import base64
import concurrent.futures as cf
import hmac
import http.client
import json
import os
import socket
import sqlite3
import struct
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORTA = int(os.environ.get("MONITOR_PORTA", "8090"))
TOKEN = os.environ.get("MONITOR_TOKEN", "")
PROC = Path(os.environ.get("HOST_PROC", "/host/proc"))
SYS = Path(os.environ.get("HOST_SYS", "/host/sys"))
ROOT = os.environ.get("HOST_ROOT", "/host/root")
SOCKET_DOCKER = "/var/run/docker.sock"
STATO_BOT = Path(os.environ.get("STATO_BOT", "/stato-bot"))
ATTIVITA = Path(os.environ.get("FILE_ATTIVITA", "/logs-sito/attivita.log"))
BOT = os.environ.get("CONTAINER_BOT", "foglietto-bot-bot-1")
UTENTE_BOT = os.environ.get("UTENTE_BOT", "1000:1000")
URL_CMS = os.environ.get("URL_CMS", "http://backend:8000/api/snapshot/")
URL_BOT = os.environ.get("URL_BOT", f"http://{BOT}:8088/salute")
PAGINA = Path(__file__).with_name("index.html")

# Solo comandi fissi: la pagina sceglie un nome, mai una riga di comando.
AZIONI_BOT = {
    "prova-report": ["python", "-m", "foglietto_bot", "prova-report"],
    "verifica": ["python", "-m", "foglietto_bot", "verifica"],
    "stato-bot": ["python", "-m", "foglietto_bot", "stato"],
}


# ── Docker via socket ────────────────────────────────────────────────────────


class _ConnUnix(http.client.HTTPConnection):
    def __init__(self, timeout: float = 30):
        super().__init__("localhost", timeout=timeout)

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(SOCKET_DOCKER)


def docker(metodo: str, percorso: str, corpo: dict | None = None, timeout: float = 30) -> tuple[int, bytes]:
    conn = _ConnUnix(timeout)
    try:
        dati = json.dumps(corpo).encode() if corpo is not None else None
        conn.request(metodo, percorso, body=dati, headers={"Content-Type": "application/json"})
        r = conn.getresponse()
        return r.status, r.read()
    finally:
        conn.close()


def _demux(flusso: bytes) -> str:
    """Toglie le intestazioni di 8 byte dei flussi stdout/stderr non-TTY."""
    out, i = [], 0
    while i + 8 <= len(flusso) and flusso[i] in (0, 1, 2):
        (lung,) = struct.unpack(">I", flusso[i + 4 : i + 8])
        out.append(flusso[i + 8 : i + 8 + lung])
        i += 8 + lung
    if i < len(flusso):  # non era multiplexato
        out.append(flusso[i:])
    return b"".join(out).decode("utf-8", "replace")


def _stats(cid: str) -> dict:
    try:
        st, raw = docker("GET", f"/containers/{cid}/stats?stream=false&one-shot=false", timeout=8)
        if st != 200:
            return {}
        s = json.loads(raw)
        cpu = s["cpu_stats"]["cpu_usage"]["total_usage"] - s["precpu_stats"]["cpu_usage"]["total_usage"]
        sistema = s["cpu_stats"].get("system_cpu_usage", 0) - s["precpu_stats"].get("system_cpu_usage", 0)
        ncpu = s["cpu_stats"].get("online_cpus") or 1
        mem = s.get("memory_stats", {})
        usata = mem.get("usage", 0) - mem.get("stats", {}).get("inactive_file", 0)
        return {
            "cpu": round(cpu / sistema * ncpu * 100, 1) if sistema > 0 else 0.0,
            "mem": usata,
            "mem_limite": mem.get("limit", 0),
        }
    except Exception:
        return {}


def containers() -> list[dict]:
    st, raw = docker("GET", "/containers/json?all=1")
    if st != 200:
        raise RuntimeError(raw.decode()[:200])
    lista = json.loads(raw)
    attivi = [c["Id"] for c in lista if c["State"] == "running"]
    with cf.ThreadPoolExecutor(8) as pool:
        stats = dict(zip(attivi, pool.map(_stats, attivi)))
    risultato = []
    for c in lista:
        stato = c.get("Status", "")
        salute = "healthy" if "(healthy)" in stato else "unhealthy" if "(unhealthy)" in stato else "starting" if "health: starting" in stato else None
        risultato.append({
            "nome": c["Names"][0].lstrip("/"),
            "progetto": c.get("Labels", {}).get("com.docker.compose.project", ""),
            "immagine": c["Image"],
            "stato": c["State"],
            "descrizione": stato,
            "salute": salute,
            "creato": c["Created"],
            **stats.get(c["Id"], {}),
        })
    risultato.sort(key=lambda c: (c["progetto"], c["nome"]))
    return risultato


def log_container(nome: str, righe: int) -> str:
    q = urllib.parse.urlencode({"stdout": 1, "stderr": 1, "tail": righe, "timestamps": 1})
    st, raw = docker("GET", f"/containers/{urllib.parse.quote(nome)}/logs?{q}")
    if st != 200:
        raise RuntimeError(raw.decode()[:200])
    return _demux(raw)


def exec_nel_bot(comando: list[str]) -> dict:
    st, raw = docker("POST", f"/containers/{BOT}/exec", {
        "Cmd": comando, "AttachStdout": True, "AttachStderr": True,
        "User": UTENTE_BOT, "WorkingDir": "/app",
    })
    if st != 201:
        return {"ok": False, "uscita": None, "output": raw.decode()[:500]}
    eid = json.loads(raw)["Id"]
    st, raw = docker("POST", f"/exec/{eid}/start", {"Detach": False, "Tty": False}, timeout=300)
    _, info = docker("GET", f"/exec/{eid}/json")
    codice = json.loads(info).get("ExitCode")
    return {"ok": codice == 0, "uscita": codice, "output": _demux(raw)}


# ── Host ─────────────────────────────────────────────────────────────────────


def _cpu_totali() -> tuple[int, int]:
    campi = [int(x) for x in (PROC / "stat").read_text().splitlines()[0].split()[1:]]
    return sum(campi), campi[3] + campi[4]


def host() -> dict:
    t1, i1 = _cpu_totali()
    time.sleep(0.25)
    t2, i2 = _cpu_totali()
    mem = {}
    for riga in (PROC / "meminfo").read_text().splitlines():
        k, v = riga.split(":", 1)
        mem[k] = int(v.split()[0]) * 1024
    disco = os.statvfs(ROOT)
    temp = None
    for zona in sorted(SYS.glob("class/thermal/thermal_zone*/temp")):
        try:
            temp = int(zona.read_text()) / 1000
            break
        except (OSError, ValueError):
            pass
    throttled = None
    f = SYS / "devices/platform/soc/soc:firmware/get_throttled"
    if f.exists():
        try:
            throttled = int(f.read_text().strip(), 16)
        except (OSError, ValueError):
            pass
    try:
        nome = (PROC / "sys/kernel/hostname").read_text().strip()
    except OSError:
        nome = socket.gethostname()
    return {
        "nome": nome,
        "cpu": round((1 - (i2 - i1) / max(t2 - t1, 1)) * 100, 1),
        "ncpu": os.cpu_count(),
        "carico": [float(x) for x in (PROC / "loadavg").read_text().split()[:3]],
        "uptime": float((PROC / "uptime").read_text().split()[0]),
        "mem_totale": mem["MemTotal"],
        "mem_usata": mem["MemTotal"] - mem["MemAvailable"],
        "swap_totale": mem.get("SwapTotal", 0),
        "swap_usata": mem.get("SwapTotal", 0) - mem.get("SwapFree", 0),
        "disco_totale": disco.f_blocks * disco.f_frsize,
        "disco_usato": (disco.f_blocks - disco.f_bfree) * disco.f_frsize,
        "temperatura": temp,
        "throttled": throttled,
    }


# ── Servizi, registri ────────────────────────────────────────────────────────


def sonda(url: str) -> dict:
    inizio = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return {"url": url, "ok": 200 <= r.status < 300, "codice": r.status, "ms": round((time.monotonic() - inizio) * 1000)}
    except Exception as e:
        return {"url": url, "ok": False, "codice": getattr(e, "code", None), "errore": str(e)[:160], "ms": round((time.monotonic() - inizio) * 1000)}


def attivita(n: int = 40) -> list[dict]:
    if not ATTIVITA.exists():
        return []
    with ATTIVITA.open("rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 400_000))
        righe = f.read().decode("utf-8", "replace").splitlines()[-n:]
    voci = []
    for r in reversed(righe):
        try:
            voci.append(json.loads(r))
        except json.JSONDecodeError:
            pass
    return voci


def registro_bot() -> dict:
    db = STATO_BOT / "registro.db"
    if not db.exists():
        return {"presente": False}
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
    conn.row_factory = sqlite3.Row
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='foglietti'").fetchone():
        conn.close()
        return {"presente": False}
    try:
        conteggi = {r["stato"]: r["n"] for r in conn.execute("SELECT stato, COUNT(*) n FROM foglietti GROUP BY stato")}
        foglietti = [dict(r) for r in conn.execute(
            "SELECT f.nome_file, f.stato, f.tentativi, f.errore, f.aggiornato_il,"
            " (SELECT COUNT(*) FROM contenuti c WHERE c.hash_foglietto = f.hash) contenuti"
            " FROM foglietti f ORDER BY f.aggiornato_il DESC LIMIT 12")]
        contenuti = [dict(r) for r in conn.execute(
            "SELECT tipo, titolo, slug, pubblicato, creato_il FROM contenuti ORDER BY id DESC LIMIT 10")]
    finally:
        conn.close()
    report = sorted((STATO_BOT / "report").glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)[:8]
    lock = STATO_BOT / "bot.lock"
    return {
        "presente": True,
        "conteggi": conteggi,
        "foglietti": foglietti,
        "contenuti": contenuti,
        "report": [{"nome": p.name, "quando": p.stat().st_mtime} for p in report],
        "lock_modificato": lock.stat().st_mtime if lock.exists() else None,
    }


def _prova(funzione, *args):
    try:
        return {"ok": True, "dati": funzione(*args)}
    except Exception as e:
        return {"ok": False, "errore": f"{type(e).__name__}: {e}"[:300]}


def stato_completo() -> dict:
    with cf.ThreadPoolExecutor(6) as pool:
        f = {
            "host": pool.submit(_prova, host),
            "container": pool.submit(_prova, containers),
            "cms": pool.submit(sonda, URL_CMS),
            "bot": pool.submit(sonda, URL_BOT),
            "attivita": pool.submit(_prova, attivita),
            "registro": pool.submit(_prova, registro_bot),
        }
        dati = {k: v.result() for k, v in f.items()}
    dati["ora"] = time.time()
    return dati


# ── HTTP ─────────────────────────────────────────────────────────────────────


class Gestore(BaseHTTPRequestHandler):
    server_version = "canizzano-monitor/1.0"

    def _autorizzato(self) -> bool:
        if not TOKEN:
            return False
        h = self.headers.get("Authorization", "")
        if not h.startswith("Basic "):
            return False
        try:
            _, _, password = base64.b64decode(h[6:]).decode().partition(":")
        except Exception:
            return False
        return hmac.compare_digest(password, TOKEN)

    def _json(self, codice: int, corpo) -> None:
        dati = json.dumps(corpo, ensure_ascii=False).encode()
        self.send_response(codice)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(dati)))
        self.end_headers()
        self.wfile.write(dati)

    def _controlla(self) -> bool:
        if self._autorizzato():
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="canizzano monitor"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def do_GET(self) -> None:
        url = urllib.parse.urlsplit(self.path)
        if url.path == "/salute":
            return self._json(200, {"ok": True})
        if not self._controlla():
            return
        q = urllib.parse.parse_qs(url.query)
        if url.path == "/":
            dati = PAGINA.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(dati)))
            self.end_headers()
            self.wfile.write(dati)
        elif url.path == "/api/stato":
            self._json(200, stato_completo())
        elif url.path == "/api/log":
            nome = q.get("container", [""])[0]
            righe = min(int(q.get("righe", ["200"])[0]), 2000)
            self._json(200, _prova(log_container, nome, righe))
        else:
            self._json(404, {"ok": False, "errore": "non trovato"})

    def do_POST(self) -> None:
        if not self._controlla():
            return
        lung = int(self.headers.get("Content-Length") or 0)
        try:
            corpo = json.loads(self.rfile.read(lung) or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"ok": False, "errore": "JSON non valido"})
        azione = corpo.get("azione")
        if azione in AZIONI_BOT:
            return self._json(200, _prova(exec_nel_bot, AZIONI_BOT[azione]))
        if azione == "riavvia":
            nome = str(corpo.get("container", ""))
            st, raw = docker("POST", f"/containers/{urllib.parse.quote(nome)}/restart?t=10", timeout=60)
            return self._json(200, {"ok": st == 204, "dati": {"output": raw.decode() or f"{nome} riavviato"}})
        self._json(400, {"ok": False, "errore": f"azione sconosciuta: {azione}"})

    def log_message(self, formato: str, *args) -> None:  # noqa: A003
        pass


def main() -> None:
    if not TOKEN:
        print("ATTENZIONE: MONITOR_TOKEN vuoto, ogni richiesta verra' rifiutata.", flush=True)
    print(f"monitor in ascolto su :{PORTA}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORTA), Gestore).serve_forever()


if __name__ == "__main__":
    main()
