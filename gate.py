#!/usr/bin/env python3
"""Portao de execucao para o GitHub Actions (so stdlib, roda em ~1s).

O workflow e disparado por dois crons: um fixo de manha e outro a cada 30 min.
Nas execucoes de 30 min este script decide se e hora de rodar: so libera quando
o momento atual esta entre (primeiro jogo - PREGAME_LEAD_MIN) e o primeiro jogo.

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
        return max(0, int(raw))
    except ValueError:
        return 30


def first_tip(day_et):
    url = f"{SCOREBOARD}?dates={day_et:%Y%m%d}"
    req = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": "nba-fantasy-agent"}
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.load(resp)
    except (urllib.error.URLError, TimeoutError, ValueError) as ex:
        print(f"aviso: placar do ESPN indisponivel ({ex})", file=sys.stderr, flush=True)
        return None

    now = datetime.now(timezone.utc)
    times = []
    for ev in data.get("events") or []:
        status = ((ev.get("status") or {}).get("type") or {}).get("name")
        if status not in ("STATUS_SCHEDULED", None):
            continue
        try:
            start = datetime.fromisoformat(str(ev["date"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        if start > now:
            times.append(start)
    return min(times) if times else None


def main():
    mode = (os.getenv("MODE") or "gate").strip().lower()
    if mode == "morning":
        emit(True, "execucao da manha")
        return

    tip = first_tip(datetime.now(ET).date())
    if tip is None:
        emit(False, "sem jogos restantes hoje")
        return

    target = tip - timedelta(minutes=lead_minutes())
    now = datetime.now(timezone.utc)
    if now < target:
        emit(False, f"faltam {int((target - now).total_seconds() // 60)} min para a janela")
    elif now >= tip:
        emit(False, "o primeiro jogo ja comecou")
    else:
        emit(True, f"dentro da janela (jogo as {tip.astimezone(ET):%H:%M} ET)")


if __name__ == "__main__":
    main()
