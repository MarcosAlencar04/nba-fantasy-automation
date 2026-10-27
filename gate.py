#!/usr/bin/env python3
"""Portao de execucao para o GitHub Actions (so stdlib, roda em ~1s).

O workflow e disparado por dois crons: um fixo de manha e outro a cada 30 min.
A cada 30 min este script decide se e hora de rodar: libera quando **algum** jogo
do dia comeca dentro da janela `PREGAME_LEAD_MIN`. Assim cada jogo tem sua propria
avaliacao, mesmo quando varios comecam em sequencia curta.

A janela pode atravessar a meia-noite: um jogo as 00:00 ET e consultado a partir
das 23:30 do dia anterior.

Saida: escreve run=true|false em $GITHUB_OUTPUT.
"""

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"


def emit(run, reason):
    print(f"{'executa' if run else 'ignora'}: {reason}", flush=True)
    out = os.getenv("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"run={'true' if run else 'false'}\n")
            fh.write(f"reason={reason}\n")


def lead_minutes():
    raw = (os.getenv("PREGAME_LEAD_MIN") or "30").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 30


def games_on(day_et):
    """[(tip_utc, 'BOS x BKN'), ...] dos jogos do dia ET informado ainda nao iniciados.

    Devolve None se o placar do ESPN nao responder, para o chamador distinguir
    "sem jogos" de "nao consegui consultar".
    """
    req = urllib.request.Request(
        f"{SCOREBOARD}?dates={day_et:%Y%m%d}",
        headers={"Accept": "application/json", "User-Agent": "nba-fantasy-agent"},
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.load(resp)
    except (urllib.error.URLError, TimeoutError, ValueError) as ex:
        print(f"aviso: placar do ESPN indisponivel ({ex})", file=sys.stderr, flush=True)
        return None

    now = datetime.now(timezone.utc)
    games = []
    for ev in data.get("events") or []:
        status = ((ev.get("status") or {}).get("type") or {}).get("name")
        if status not in ("STATUS_SCHEDULED", None):
            continue
        try:
            tip = datetime.fromisoformat(str(ev["date"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if tip <= now:
            continue
        games.append((tip, ev.get("shortName") or ev.get("name") or "jogo"))
    return games


def upcoming_games(now, lead):
    """Jogos que podem comecar dentro da janela, cruzando a virada do dia ET.

    Um jogo as 00:00 ET pertence ao dia seguinte, mas a janela abre as 23:30 do
    dia anterior. Consultamos os dois dias que a janela toca: normalmente e um
    so, e vira dois apenas na ultima `lead` da noite.
    """
    today_et = now.astimezone(ET).date()
    horizon_et = (now + lead).astimezone(ET).date()
    days = [today_et] if today_et == horizon_et else [today_et, horizon_et]

    games, ok = [], False
    for day in days:
        found = games_on(day)
        if found is None:
            continue
        ok = True
        games.extend(found)
    if not ok:
        return None
    # a segunda consulta pode repetir jogos do mesmo dia; dedup por horario+nome
    return sorted(set(games))


def main():
    mode = (os.getenv("MODE") or "gate").strip().lower()
    if mode == "morning":
        emit(True, "execucao da manha")
        return

    lead = timedelta(minutes=lead_minutes())
    now = datetime.now(timezone.utc)
    games = upcoming_games(now, lead)
    if games is None:
        emit(False, "placar do ESPN indisponivel")
        return
    if not games:
        emit(False, "sem jogos restantes hoje")
        return

    # Janela inclusiva nos dois extremos: um jogo que comeca exatamente `lead`
    # minutos depois entra aqui. Sem isso, jogos em horario redondo (:00/:30)
    # ficariam de fora quando o cron tem o mesmo intervalo do lead.
    in_window = [g for g in games if timedelta(0) < g[0] - now <= lead]
    if in_window:
        names = ", ".join(f"{name} {tip.astimezone(ET):%H:%M}" for tip, name in in_window[:3])
        emit(True, f"janela aberta: {names} ET")
        return

    tip, name = games[0]
    wait = int((tip - lead - now).total_seconds() // 60)
    if wait > 0:
        emit(False, f"proxima janela em {wait} min ({name} {tip.astimezone(ET):%H:%M} ET)")
    else:
        emit(False, f"janela de {name} ja passou; proximo jogo {tip.astimezone(ET):%H:%M} ET")


if __name__ == "__main__":
    main()
