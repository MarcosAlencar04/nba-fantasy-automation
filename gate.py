#!/usr/bin/env python3
"""Portao de execucao para o GitHub Actions (so stdlib, roda em ~1s).

O workflow e disparado por dois crons: um fixo de manha e outro a cada 15 min.
A cada 15 min este script decide se e hora de rodar: libera quando **algum** jogo
do dia comeca dentro da janela `PREGAME_LEAD_MIN`. Assim cada jogo tem sua propria
avaliacao, mesmo quando varios comecam em sequencia curta.

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
    raw = (os.getenv("PREGAME_LEAD_MIN") or "15").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 15


def todays_games(day_et):
    """[(tip_utc, 'BOS x BKN'), ...] dos jogos de hoje ainda nao iniciados."""
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
    return sorted(games)


def main():
    mode = (os.getenv("MODE") or "gate").strip().lower()
    if mode == "morning":
        emit(True, "execucao da manha")
        return

    games = todays_games(datetime.now(ET).date())
    if games is None:
        emit(False, "placar do ESPN indisponivel")
        return
    if not games:
        emit(False, "sem jogos restantes hoje")
        return

    lead = timedelta(minutes=lead_minutes())
    now = datetime.now(timezone.utc)
    in_window = [g for g in games if timedelta(0) <= g[0] - now < lead]
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
