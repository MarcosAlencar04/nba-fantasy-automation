#!/usr/bin/env python3
"""Agendador do NBA Fantasy Agent.

Roda o agente duas vezes por dia:
  1. De manha (MORNING_TIME, no fuso da sua maquina).
  2. 30 minutos antes do primeiro jogo do dia (PREGAME_LEAD_MIN).

O horario do primeiro jogo vem da API publica do ESPN. O estado do dia fica em
.cache/scheduler_state.json, entao reiniciar o processo nao repete o envio.

Uso:
  python scheduler.py                 # loop continuo (deixe rodando)
  python scheduler.py --plan          # mostra os proximos horarios e sai
  python scheduler.py --once morning  # executa uma vez e sai (morning|pregame)
  python scheduler.py --dry-run       # nao chama o agente, so registra
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = timezone.utc
ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / ".cache" / "scheduler_state.json"
AGENT = HERE / "nba-fantasy-agent.py"

MORNING_GRACE = timedelta(hours=6)
MAX_FAILS = 3


def log(msg):
    print(f"[{datetime.now().astimezone():%Y-%m-%d %H:%M:%S}] {msg}", file=sys.stderr, flush=True)


def local_tz():
    return datetime.now().astimezone().tzinfo


def morning_time():
    raw = (os.getenv("MORNING_TIME") or "10:00").strip()
    try:
        hh, mm = (int(x) for x in raw.split(":")[:2])
        return hh, mm
    except ValueError:
        log(f"MORNING_TIME invalido ({raw}); usando 10:00.")
        return 10, 0


def pregame_lead():
    raw = (os.getenv("PREGAME_LEAD_MIN") or "30").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        log(f"PREGAME_LEAD_MIN invalido ({raw}); usando 30.")
        return 30


def load_state():
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state):
    STATE_FILE.parent.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def marked(state, day, kind):
    return bool(state.get(day, {}).get(kind))


def is_done(state, day, kind):
    """Executado com sucesso ou desistiu depois de MAX_FAILS tentativas."""
    entry = state.get(day, {})
    return bool(entry.get(kind)) or int(entry.get(f"{kind}_fails", 0)) >= MAX_FAILS


def mark(state, day, kind, when):
    state.setdefault(day, {})[kind] = when.isoformat()
    save_state(state)


def mark_failure(state, day, kind):
    entry = state.setdefault(day, {})
    fails = int(entry.get(f"{kind}_fails", 0)) + 1
    entry[f"{kind}_fails"] = fails
    save_state(state)
    return fails


def first_tipoff_utc(day_et):
    """Primeiro horario de inicio ainda nao ocorrido, no dia ET informado."""
    import requests

    url = f"{ESPN_SCOREBOARD}?dates={day_et:%Y%m%d}"
    try:
        r = requests.get(url, timeout=20, headers={"Accept": "application/json"})
        r.raise_for_status()
        events = r.json().get("events", [])
    except Exception as ex:
        log(f"ESPN scoreboard indisponivel ({ex}); sem checagem de pre-jogo hoje.")
        return None

    now = datetime.now(UTC)
    times = []
    for ev in events:
        if (ev.get("status", {}).get("type", {}) or {}).get("name") != "STATUS_SCHEDULED":
            continue
        try:
            start = datetime.fromisoformat(ev["date"].replace("Z", "+00:00"))
        except Exception:
            continue
        if start > now:
            times.append(start)
    return min(times) if times else None


def morning_at(day_local):
    hh, mm = morning_time()
    naive = datetime.combine(day_local, datetime.min.time()).replace(hour=hh, minute=mm)
    return naive.astimezone(local_tz())


def next_events(state, now=None):
    """Lista ordenada de (quando, kind) que ainda devem rodar."""
    now = now or datetime.now(UTC)
    today_local = now.astimezone(local_tz()).date()
    today_et = now.astimezone(ET).date()
    out = []

    for offset in (0, 1):
        day_local = today_local + timedelta(days=offset)
        day_key = day_local.isoformat()
        if is_done(state, day_key, "morning"):
            continue
        at = morning_at(day_local)
        if at >= now - MORNING_GRACE:
            out.append((max(at, now), "morning", day_key))

    day_key_et = today_et.isoformat()
    if not is_done(state, day_key_et, "pregame"):
        tip = first_tipoff_utc(today_et)
        if tip:
            at = tip - timedelta(minutes=pregame_lead())
            if at >= now and at < tip:
                out.append((at, "pregame", day_key_et))
            elif at < now < tip:
                out.append((now, "pregame", day_key_et))

    out.sort(key=lambda x: x[0])
    return out


def run_agent(kind, dry_run=False):
    args = [sys.executable, str(AGENT), "--notify"]
    adds = (os.getenv("ADDS_USED") or "").strip()
    if adds.isdigit():
        args += ["--adds-used", adds]
    if dry_run:
        log(f"[dry-run] {kind}: {' '.join(args)}")
        return 0
    log(f"executando {kind}...")
    proc = subprocess.run(args, cwd=HERE)
    if proc.returncode != 0:
        log(f"agente terminou com codigo {proc.returncode}")
    return proc.returncode


def main():
    ap = argparse.ArgumentParser(description="Agendador do NBA Fantasy Agent")
    ap.add_argument("--plan", action="store_true", help="mostra os proximos horarios e sai")
    ap.add_argument("--once", choices=["morning", "pregame"], help="executa uma vez e sai")
    ap.add_argument("--dry-run", action="store_true", help="nao executa o agente")
    ap.add_argument("--refresh", action="store_true", help="ignora o estado salvo do dia")
    opts = ap.parse_args()

    state = {} if opts.refresh else load_state()

    if opts.plan:
        now = datetime.now(UTC)
        events = next_events(state, now)
        if not events:
            print("Nada agendado (sem jogos pendentes hoje e a manha ja foi executada).")
        for at, kind, day in events:
            flag = "ja passou, roda agora" if at <= now + timedelta(seconds=1) else ""
            print(f"{at.astimezone(local_tz()):%d/%m %H:%M}  {kind:8s} ({day}) {flag}")
        return

    if opts.once:
        now = datetime.now(UTC)
        code = run_agent(opts.once, opts.dry_run)
        if not opts.dry_run:
            day = now.astimezone(ET).date().isoformat() if opts.once == "pregame" \
                else now.astimezone(local_tz()).date().isoformat()
            if code == 0:
                mark(state, day, opts.once, now)
            else:
                mark_failure(state, day, opts.once)
        sys.exit(code)

    log("agendador iniciado (Ctrl+C para parar)")
    while True:
        now = datetime.now(UTC)
        events = next_events(state, now)
        if not events:
            log("nada a fazer hoje; dormindo 10 min")
            time.sleep(600)
            continue

        at, kind, day = events[0]
        wait = (at - now).total_seconds()
        if wait > 0:
            log(f"proxima execucao: {kind} em {at.astimezone(local_tz()):%d/%m %H:%M} "
                f"({wait / 60:.0f} min)")
            time.sleep(min(wait, 600))
            continue

        code = run_agent(kind, opts.dry_run)
        if opts.dry_run:
            mark(state, day, kind, datetime.now(UTC))
            continue
        if code == 0:
            mark(state, day, kind, datetime.now(UTC))
        else:
            fails = mark_failure(state, day, kind)
            log(f"falhou ({fails}/{MAX_FAILS}); nova tentativa em 10 min")
            time.sleep(600)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("encerrado")
