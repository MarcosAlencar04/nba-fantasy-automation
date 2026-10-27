#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NBA Fantasy Agent  |  ESPN  |  Head-to-Head Points
=====================================================================
Regras da liga embutidas (do print das configuracoes):
  * Pontuacao: PTS 1, FGM 2, FGA -1, FTM 1, FTA -1, 3PM 1, OREB 0.5, REB 1,
    AST 2, STL 4, BLK 4, TO -2, DD 5, TD 10, QD 100
  * Lineup: PG SG SF PF C G F UTIL UTIL UTIL (10) + 4 banco + 3 IR
  * Troca de lineup diaria, trava individual no horario do jogo
  * Matchup semanal (seg-dom), limite de 5 adds por matchup, waivers de 1 dia
  * Prazo de trocas: 22/03/2027

O que o agente faz:
  1. Lineup otimo de HOJE (problema de atribuicao jogador x slot).
  2. Diagnostico da SEMANA do confronto: quem rende acima/abaixo do esperado.
  3. Sinais "vender na alta" / "comprar na baixa" (regressao a media):
     sorte de arremesso, minutos inflados, uso (usage), amostra.
  4. Melhores add/drop (free agents) avaliados pelo valor SEMANAL do lineup
     (respeitando posicoes e jogos de cada dia), nao so pela media.
  5. Ideias de troca (com os elencos dos outros times vindos do ESPN).

Requer ESPN_LEAGUE_ID e ESPN_TEAM_ID no .env: o elenco vem da API do ESPN.

Uso:  python nba-fantasy-agent.py --notify
"""
from __future__ import annotations

import argparse
import difflib
import math
import os
import pickle
import re
import smtplib
import sys
import time
import traceback
import unicodedata
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from html import escape
from itertools import combinations
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
from scipy.optimize import linear_sum_assignment

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv e opcional
    pass

# --------------------------------------------------------------------------
# Constantes da liga
# --------------------------------------------------------------------------
ET = ZoneInfo("America/New_York")  # o "dia" do fantasy segue o fuso da NBA
CACHE_DIR = Path(".cache")
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124 Safari/537.36"}

LINEUP_SLOTS = ["PG", "SG", "SF", "PF", "C", "G", "F", "UTIL", "UTIL", "UTIL"]
BASE_POS = {"PG", "SG", "SF", "PF", "C"}
SLOT_ELIG = {
    "PG": {"PG"}, "SG": {"SG"}, "SF": {"SF"}, "PF": {"PF"}, "C": {"C"},
    "G": {"PG", "SG"}, "F": {"SF", "PF"}, "UTIL": BASE_POS,
}
MAX_CENTERS = 4
IR_SLOTS = 3
BENCH_SLOTS = 4
TRADE_DEADLINE = date(2027, 3, 22)
HORIZON_EXTRA_GAMES = 6      # peso do ganho "por jogo" (valor alem da semana) no ranking de adds
PEN = 1e4                    # penalidade p/ celula inviavel na atribuicao

# Peso (em fantasy points) de um arremesso convertido ALEM do esperado:
#   FGM vale 4 (2 de PTS + 2 de FGM); 3PM vale +2 (1 de PTS extra + 1 de 3PM); FTM vale 2.
LUCK_W = (4.0, 2.0, 2.0)

# Mapa de posicao da NBA (PlayerIndex) -> posicoes ESPN elegiveis
NBA_POS_MAP = {
    "G": {"PG", "SG"}, "F": {"SF", "PF"}, "C": {"C"},
    "G-F": {"SG", "SF"}, "F-G": {"SG", "SF"}, "F-C": {"PF", "C"}, "C-F": {"PF", "C"},
}


# --------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------
def cached(key, ttl_h, fn, refresh=False):
    CACHE_DIR.mkdir(exist_ok=True)
    f = CACHE_DIR / f"{key}.pkl"
    if f.exists() and not refresh and (time.time() - f.stat().st_mtime) < ttl_h * 3600:
        try:
            with open(f, "rb") as fh:
                return pickle.load(fh)
        except Exception:
            pass
    obj = fn()
    with open(f, "wb") as fh:
        pickle.dump(obj, fh)
    return obj


def _env(name):
    return (os.getenv(name) or "").strip()


def _env_first(name):
    """Primeira entrada de uma variavel em lista (separada por virgula).

    ESPN_TEAM_ID aceita lista posicional quando rodando na nuvem, onde cada time
    e uma execucao. Rodando local, vale o primeiro.
    """
    return _env(name).split(",")[0].strip()


def _chat_id_set() -> bool:
    """O chat id do Telegram e numerico (grupos usam negativo).

    Qualquer outra coisa ("XXX", vazio, lixo) significa nao configurado, para
    que um placeholder no .env nao vire uma tentativa de envio com erro.
    """
    raw = _env("TELEGRAM_CHAT_ID")
    return bool(raw) and (raw.lstrip("-").isdigit())


def missing_env(*names):
    return [n for n in names if not _env(n)]


def season_str(d: date) -> str:
    y = d.year if d.month >= 8 else d.year - 1
    return f"{y}-{(y + 1) % 100:02d}"


def prev_season_str(s: str) -> str:
    y = int(s[:4]) - 1
    return f"{y}-{(y + 1) % 100:02d}"


def f1(x, d=1):
    try:
        if x is None or (isinstance(x, float) and math.isnan(x)):
            return "-"
        return f"{x:.{d}f}"
    except Exception:
        return str(x)


def sgn(x, d=1):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "-"
    return f"{x:+.{d}f}"


def md_table(headers, rows):
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(x) for x in r) + " |")
    return out


_SUFFIX = {"jr", "sr", "ii", "iii", "iv", "v"}


def norm_name(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[.'`]", "", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return " ".join(t for t in s.split() if t not in _SUFFIX)


def parse_pos(s: str) -> set:
    out = set()
    for t in re.split(r"[/\-,\s]+", (s or "").upper().strip()):
        if t in BASE_POS:
            out.add(t)
        elif t == "G":
            out |= {"PG", "SG"}
        elif t == "F":
            out |= {"SF", "PF"}
    return out


def norm_status(s) -> str:
    s = (str(s) if s is not None else "").upper().strip()
    if s in ("", "ACTIVE", "ATIVO", "HEALTHY", "NONE", "NAN"):
        return "ACTIVE"
    if s == "IR" or s.startswith("INJURY"):
        return "IR"
    if "OUT" in s or "SUSP" in s:
        return "OUT"
    if s in ("Q", "GTD", "DTD") or any(k in s for k in ("DAY", "QUEST", "PROB", "DOUBT")):
        return "DTD"
    return "ACTIVE"


def avail_prob(status: str, offset_days: int) -> float:
    if status in ("OUT", "IR"):
        return 0.0
    if status == "DTD":
        return 0.6 if offset_days == 0 else 0.85
    return 1.0


# --------------------------------------------------------------------------
# Pontuacao da liga
# --------------------------------------------------------------------------
def fantasy_points(d):
    """d: DataFrame (ou dict) com colunas de box score + DD/TD/QD."""
    return (d["PTS"] + 2 * d["FGM"] - d["FGA"] + d["FTM"] - d["FTA"] + d["FG3M"]
            + 0.5 * d["OREB"] + d["REB"] + 2 * d["AST"] + 4 * d["STL"] + 4 * d["BLK"]
            - 2 * d["TOV"] + 5 * d["DD"] + 10 * d["TD"] + 100 * d["QD"])


STAT_COLS = ["MIN", "FGM", "FGA", "FG3M", "FG3A", "FTM", "FTA", "OREB", "REB", "AST", "STL", "BLK", "TOV", "PTS"]


def prep_logs(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    if df.empty:
        for c in STAT_COLS + ["FP", "DD", "TD", "QD", "PLAYER_ID"]:
            df[c] = pd.Series(dtype=float)
        df["GAME_DATE"] = pd.Series(dtype=object)
        df["PLAYER_NAME"] = pd.Series(dtype=object)
        df["TEAM_ABBREVIATION"] = pd.Series(dtype=object)
        return df
    df["GAME_DATE"] = pd.to_datetime(df["GAME_DATE"]).dt.date
    for c in STAT_COLS:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    df = df[df["MIN"] > 0].copy()
    n10 = (df[["PTS", "REB", "AST", "STL", "BLK"]] >= 10).sum(axis=1)
    # No ESPN um triple-double conta tambem como double-double (bonus cumulativos).
    df["DD"] = (n10 >= 2).astype(int)
    df["TD"] = (n10 >= 3).astype(int)
    df["QD"] = (n10 >= 4).astype(int)
    df["FP"] = fantasy_points(df)
    return df.sort_values(["PLAYER_ID", "GAME_DATE"]).reset_index(drop=True)


# --------------------------------------------------------------------------
# Coleta de dados (nba_api + calendario oficial)
# --------------------------------------------------------------------------
def fetch_gamelog(season):
    from nba_api.stats.endpoints import leaguegamelog
    return leaguegamelog.LeagueGameLog(
        season=season, player_or_team_abbreviation="P",
        season_type_all_star="Regular Season", timeout=90,
    ).get_data_frames()[0]


def fetch_team_stats(season):
    from nba_api.stats.endpoints import leaguedashteamstats
    from nba_api.stats.static import teams as st
    df = leaguedashteamstats.LeagueDashTeamStats(
        season=season, measure_type_detailed_defense="Advanced",
        per_mode_detailed="PerGame", timeout=60,
    ).get_data_frames()[0]
    ab = {t["id"]: t["abbreviation"] for t in st.get_teams()}
    df["ABBR"] = df["TEAM_ID"].map(ab)
    return df[["ABBR", "GP", "PACE", "DEF_RATING"]].dropna()


def fetch_positions(season):
    from nba_api.stats.endpoints import playerindex
    df = playerindex.PlayerIndex(season=season, timeout=60).get_data_frames()[0]
    return {int(a): b for a, b in zip(df["PERSON_ID"], df["POSITION"]) if isinstance(b, str)}


def fetch_schedule(season=None, source="nba"):
    """Retorna {date: [(home, away), ...]} so de jogos de temporada regular.

    `source="espn"` usa o placar do ESPN dia a dia (mesmas siglas do gamelog).
    Caso contrario tenta cdn.nba.com e depois o nba_api. Devolve {} (com aviso) se falhar.
    """
    if source == "espn":
        try:
            games = fetch_schedule_espn()
            if games:
                return games
            print("Aviso: placar do ESPN nao retornou jogos.", file=sys.stderr)
        except Exception as ex:
            print(f"Aviso: calendario via ESPN falhou ({ex}).", file=sys.stderr)

    def via_cdn(games):
        r = requests.get("https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json",
                         headers=UA, timeout=30)
        r.raise_for_status()
        for gd in r.json()["leagueSchedule"]["gameDates"]:
            d = datetime.strptime(gd["gameDate"].split(" ")[0], "%m/%d/%Y").date()
            for g in gd["games"]:
                if str(g.get("gameId", "")).startswith("002"):
                    games[d].append((g["homeTeam"]["teamTricode"], g["awayTeam"]["teamTricode"]))

    def via_nba(games):
        from nba_api.stats.endpoints import scheduleleaguev2
        data = scheduleleaguev2.ScheduleLeagueV2(timeout=90).get_dict()["leagueSchedule"]
        for gd in data["gameDates"]:
            d = datetime.strptime(gd["gameDate"].split(" ")[0], "%m/%d/%Y").date()
            for g in gd["games"]:
                if str(g.get("gameId", "")).startswith("002"):
                    games[d].append((g["homeTeam"]["teamTricode"], g["awayTeam"]["teamTricode"]))

    for source in (via_cdn, via_nba):
        games = defaultdict(list)
        try:
            source(games)
        except Exception as ex:
            print(f"Aviso: calendario via {source.__name__} falhou ({ex}).", file=sys.stderr)
            continue
        if games:
            return dict(games)

    print("Aviso: nenhuma fonte de calendario respondeu; o relatorio sai sem jogos do dia.",
          file=sys.stderr)
    return {}


# --------------------------------------------------------------------------
# Perfil estatistico dos jogadores (o "cerebro")
# --------------------------------------------------------------------------
def reg(made, att, base, k):
    """Regressao de um percentual a uma base, com 'k' arremessos de peso."""
    return (made + base * k) / (att + k)


def league_baselines(prev: pd.DataFrame, cur: pd.DataFrame) -> dict:
    ref = prev if len(prev) > 500 else pd.concat([prev, cur])
    rot = ref[ref["MIN"] >= 15]
    safe = lambda a, b, d: (a / b) if b else d
    return dict(
        fg=safe(ref.FGM.sum(), ref.FGA.sum(), 0.46),
        fg3=safe(ref.FG3M.sum(), ref.FG3A.sum(), 0.36),
        ft=safe(ref.FTM.sum(), ref.FTA.sum(), 0.78),
        fpm=safe(rot.FP.sum(), rot.MIN.sum(), 0.95),
    )


def S(g, col):
    return float(g[col].sum()) if g is not None and len(g) else 0.0


def _usg(g):
    m = S(g, "MIN")
    return (S(g, "FGA") + 0.44 * S(g, "FTA") + S(g, "TOV")) / m * 36 if m else float("nan")


def build_profile(pid, gc, gp, lg, recent_n):
    n = len(gc) if gc is not None else 0
    pn = len(gp) if gp is not None else 0
    if n == 0 and S(gp, "MIN") < 300:
        return None
    ref = gc if n else gp
    name, team = ref.PLAYER_NAME.iloc[-1], ref.TEAM_ABBREVIATION.iloc[-1]

    # ---- baseline (temporada anterior) --------------------------------
    p_min, p_fp = S(gp, "MIN"), S(gp, "FP")
    prior_fpm = (p_fp + lg["fpm"] * 200) / (p_min + 200)
    prior_mpg = p_min / pn if pn else float("nan")
    base_fg = reg(S(gp, "FGM"), S(gp, "FGA"), lg["fg"], 200)
    base_3 = reg(S(gp, "FG3M"), S(gp, "FG3A"), lg["fg3"], 250)
    base_ft = reg(S(gp, "FTM"), S(gp, "FTA"), lg["ft"], 100)

    # ---- temporada atual ---------------------------------------------
    c_min = S(gc, "MIN")
    t_fg = reg(S(gc, "FGM"), S(gc, "FGA"), base_fg, 150)    # "talento" regredido
    t_3 = reg(S(gc, "FG3M"), S(gc, "FG3A"), base_3, 200)
    t_ft = reg(S(gc, "FTM"), S(gc, "FTA"), base_ft, 80)

    def luck(g):  # FP "de sorte" no periodo (acima/abaixo do que o talento regredido prediz)
        if g is None or not len(g):
            return 0.0
        return (LUCK_W[0] * (S(g, "FGM") - t_fg * S(g, "FGA"))
                + LUCK_W[1] * (S(g, "FG3M") - t_3 * S(g, "FG3A"))
                + LUCK_W[2] * (S(g, "FTM") - t_ft * S(g, "FTA")))

    # ---- janelas recentes --------------------------------------------
    g5 = gc.tail(recent_n) if n else None
    g10 = gc.tail(10) if n else None
    fp_all = S(gc, "FP") / n if n else float("nan")
    fp5 = S(g5, "FP") / len(g5) if n else float("nan")
    fp10 = S(g10, "FP") / len(g10) if n else float("nan")
    min5 = S(g5, "MIN") / len(g5) if n else float("nan")
    luck5 = luck(g5) / len(g5) if n else 0.0

    # ---- projecao de minutos -----------------------------------------
    cur_mpg = c_min / n if n else float("nan")
    if n == 0:
        base_min = prior_mpg
    elif pn:
        base_min = (c_min + prior_mpg * 6) / (n + 6)
    else:
        base_min = cur_mpg
    proj_min = base_min if n < 3 else 0.5 * base_min + 0.5 * min5
    proj_min = float(min(proj_min, 40.0))

    # ---- projecao de FP por minuto (ajustada por sorte) ---------------
    if n == 0 or c_min == 0:
        proj_fpm = prior_fpm
    else:
        cur_fpm_adj = (S(gc, "FP") - luck(gc)) / c_min
        w = c_min / (c_min + 250.0)
        proj_fpm = w * cur_fpm_adj + (1 - w) * prior_fpm
    proj_fppg = proj_fpm * proj_min

    # ---- dispersao / uso ----------------------------------------------
    if n >= 6:
        sd = float(gc.FP.std())
    elif pn >= 10:
        sd = float(gp.FP.tail(30).std())
    else:
        sd = 12.0
    usg, usg5 = (_usg(gc), _usg(g5)) if n else (float("nan"), float("nan"))
    dd_src = gc if n else gp

    return dict(
        pid=int(pid), name=name, team=team, gp=n, prev_gp=pn, stale=(n == 0),
        tot_min=c_min + p_min * 0.01,
        mpg=cur_mpg, fppg=fp_all, fp5=fp5, fp10=fp10, min5=min5,
        base_min=base_min, proj_min=proj_min, proj_fppg=float(proj_fppg),
        luck5=luck5, delta_form=(fp5 - proj_fppg) if n else float("nan"),
        min_delta=(min5 - base_min) if n else float("nan"),
        usg=usg, usg_delta=(usg5 / usg - 1) if n and usg else float("nan"),
        fp_sd=sd, dd_rate=float(dd_src.DD.mean()) if len(dd_src) else 0.0,
        conf=min(1.0, n / 15.0),
        perceived=0.5 * fp_all + 0.5 * fp10 if n else prior_fpm * (prior_mpg if pn else 0),
    )


def build_profiles(cur, prev, recent_n=5):
    lg = league_baselines(prev, cur)
    cur_g = {k: g for k, g in cur.groupby("PLAYER_ID")}
    prev_g = {k: g for k, g in prev.groupby("PLAYER_ID")}
    out = {}
    for pid in set(cur_g) | set(prev_g):
        p = build_profile(pid, cur_g.get(pid), prev_g.get(pid), lg, recent_n)
        if p:
            out[int(pid)] = p
    return out


def form_tag(p, recent_n=5):
    """Classifica a fase do jogador (regressao a media)."""
    n = min(p["gp"], recent_n)
    if n < 3:
        return "AMOSTRA_PEQUENA", ["menos de 3 jogos na temporada"]
    thr = max(4.0, 0.12 * p["proj_fppg"])
    why = []
    if p["delta_form"] >= thr:
        if p["luck5"] >= 2.0:
            why.append(f"arremessos acima do esperado (~{p['luck5']:+.1f} FP/jogo de 'sorte')")
        if p["min_delta"] >= 3:
            why.append(f"minutos inflados ({p['min_delta']:+.1f} min vs. normal)")
        if why:
            return "VENDER_NA_ALTA", why
        return "ALTA_SUSTENTAVEL", ["sem sinal de sorte ou de minutos extras"]
    if p["delta_form"] <= -thr:
        if p["min_delta"] <= -4:
            return "PAPEL_ENCOLHENDO", [f"minutos cairam {p['min_delta']:+.1f}"]
        if p["luck5"] <= -1.5:
            why.append(f"azar de arremesso (~{p['luck5']:+.1f} FP/jogo)")
        why.append("minutos estaveis")
        return "COMPRAR_NA_BAIXA", why
    return "NEUTRO", []


# --------------------------------------------------------------------------
# Contexto: calendario, ritmo/defesa, valor esperado por dia
# --------------------------------------------------------------------------
class Ctx:
    def __init__(self, sched, tstats, today, done_today):
        self.today = today
        self.done_today = done_today
        self.opp = defaultdict(dict)
        for d, games in sched.items():
            for h, a in games:
                self.opp[d][h] = (a, True)
                self.opp[d][a] = (h, False)
        self.ts, self.lg_pace, self.lg_def = {}, 1.0, 1.0
        if tstats is not None and len(tstats):
            self.ts = {r.ABBR: (r.GP, r.PACE, r.DEF_RATING) for r in tstats.itertuples()}
            self.lg_pace, self.lg_def = tstats.PACE.mean(), tstats.DEF_RATING.mean()
        self.sched_days = sorted(sched)

    def game(self, team, d):
        return self.opp.get(d, {}).get(team)

    def env(self, team, opp):
        """Fator de ambiente (ritmo dos dois times + defesa do adversario), suavizado."""
        if team not in self.ts or opp not in self.ts:
            return 1.0
        gp = min(self.ts[team][0], self.ts[opp][0])
        pace = ((self.ts[team][1] + self.ts[opp][1]) / 2) / self.lg_pace
        dfac = self.ts[opp][2] / self.lg_def
        raw = (pace ** 0.7) * (dfac ** 0.5)
        k = 0.5 * min(1.0, gp / 20.0)
        return float(np.clip(1 + (raw - 1) * k, 0.94, 1.06))


def day_value(e, d, ctx: Ctx):
    g = ctx.game(e["team"], d)
    if not g:
        return 0.0
    if d == ctx.today and e["pid"] in ctx.done_today:
        return 0.0
    off = (d - ctx.today).days
    v = e["proj_fppg"] * ctx.env(e["team"], g[0]) * avail_prob(e["status"], off)
    if ctx.game(e["team"], d - timedelta(days=1)):  # back-to-back
        v *= 0.96
    return float(v)


# --------------------------------------------------------------------------
# Otimizador de lineup
# --------------------------------------------------------------------------
def optimize(elig_list, vals):
    """Atribui jogadores aos 10 slots maximizando o valor esperado.
    Retorna ([(slot_idx, player_idx)], total)."""
    n, m = len(vals), len(LINEUP_SLOTS)
    if n == 0:
        return [], 0.0
    cost = np.full((n, m), PEN)
    for i, (e, v) in enumerate(zip(elig_list, vals)):
        for j, s in enumerate(LINEUP_SLOTS):
            if s == "UTIL" or (e & SLOT_ELIG[s]):
                cost[i, j] = -v
    r, c = linear_sum_assignment(cost)
    assign, tot = [], 0.0
    for i, j in zip(r, c):
        if cost[i, j] < PEN / 2 and vals[i] > 0:
            assign.append((j, i))
            tot += vals[i]
    return sorted(assign), tot


def week_value(ids, pool, days):
    elig = [pool[i]["elig"] for i in ids]
    return sum(optimize(elig, [pool[i]["dv"][d] for i in ids])[1] for d in days)


# --------------------------------------------------------------------------
# Resolucao de nomes
# --------------------------------------------------------------------------
class NameIndex:
    def __init__(self, profiles):
        self.map = {}
        for pid, p in sorted(profiles.items(), key=lambda kv: kv[1]["tot_min"]):
            self.map[norm_name(p["name"])] = pid
        self.keys = list(self.map)

    def find(self, name):
        n = norm_name(name)
        if n in self.map:
            return self.map[n], None
        m = difflib.get_close_matches(n, self.keys, n=1, cutoff=0.75)
        if m:
            return self.map[m[0]], m[0]
        # tentativa por sobrenome
        last = n.split()[-1] if n else ""
        c = [k for k in self.keys if k.split()[-1:] == [last] and k[:1] == n[:1]]
        if len(c) == 1:
            return self.map[c[0]], c[0]
        return None, None


def read_names_csv(path):
    names = []
    with open(path, encoding="utf-8-sig") as fh:
        for ln in fh:
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                nm = re.split(r"[,;\t]", ln)[0].strip()
                if nm.lower() not in ("nome", "name", "jogador", "player"):
                    names.append(nm)
    return names


# --------------------------------------------------------------------------
# ESPN: elenco, free agents, status de lesao e elencos dos outros times
# --------------------------------------------------------------------------
def elig_from_espn(p):
    slots = set(getattr(p, "eligibleSlots", []) or [])
    base = slots & BASE_POS
    if not base:
        if "G" in slots:
            base |= {"PG", "SG"}
        if "F" in slots:
            base |= {"SF", "PF"}
    return base or parse_pos(getattr(p, "position", ""))


def owner_name(team):
    """Nome do dono do time. `owners` do ESPN e uma lista de dicts de membro."""
    owners = getattr(team, "owners", None) or []
    if not owners:
        return ""
    first = owners[0]
    if isinstance(first, dict):
        for key in ("displayName", "name", "firstName", "nickname"):
            val = first.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        return ""
    return str(first).strip()


def fetch_espn(year, roster_names=None, fa_size=150, team_id=None):
    from espn_api.basketball import League
    lg = League(league_id=int(_env("ESPN_LEAGUE_ID")), year=year,
                espn_s2=_env("ESPN_S2") or None, swid=_env("ESPN_SWID") or None)

    def conv(p):
        return dict(name=p.name, elig=elig_from_espn(p),
                    status=getattr(p, "injuryStatus", "ACTIVE"),
                    owned=getattr(p, "percent_owned", None),
                    espn_id=getattr(p, "playerId", None),
                    team=getattr(p, "proTeam", "") or "",
                    lineup_slot=getattr(p, "lineupSlot", "") or "",
                    expected_return=getattr(p, "expected_return_date", None))

    mine = {norm_name(n) for n in (roster_names or [])}
    teams, best, my_idx = [], -1, None
    for i, t in enumerate(lg.teams):
        pl = [conv(p) for p in t.roster]
        teams.append((t.team_name, pl, getattr(t, "team_id", i + 1),
                      getattr(t, "logo_url", "") or "", owner_name(t),
                      (getattr(t, "wins", 0), getattr(t, "losses", 0))))
        ov = len(mine & {norm_name(x["name"]) for x in pl})
        if ov > best:
            best, my_idx = ov, i

    my_status, my_players, my_name, my_team_id = {}, [], None, None
    my_logo, my_owner, my_record = "", "", (0, 0)
    if team_id is not None:
        hit = next((i for i, t in enumerate(teams) if t[2] == int(team_id)), None)
        if hit is None:
            print(f"Aviso: time {team_id} nao existe nesta liga (ids: "
                  f"{', '.join(str(t[2]) for t in teams)}).", file=sys.stderr)
        my_idx = hit
    elif roster_names and best >= 3:
        pass
    else:
        my_idx = None

    if my_idx is not None:
        my_name, my_players, my_team_id, my_logo, my_owner, my_record = teams.pop(my_idx)
        my_status = {norm_name(x["name"]): x["status"] for x in my_players}

    fa = [conv(p) for p in lg.free_agents(size=fa_size)]
    # (nome, jogadores, campanha) - ordem fixa consumida por build_universe_espn e run_analysis
    rivals = [(n, p, r) for n, p, _tid, _logo, _owner, r in teams]
    return dict(fa=fa, teams=rivals, my_status=my_status,
                my_team=my_name, my_players=my_players, my_team_id=my_team_id,
                my_logo=my_logo, my_owner=my_owner, my_record=my_record)


def roster_rows_from_espn(players):
    """Converte o elenco do ESPN no formato usado pela analise."""
    rows = []
    for p in players:
        status = norm_status(p.get("status"))
        slot = (p.get("lineup_slot") or "").upper()
        rows.append(dict(name=p["name"],
                         pos="/".join(sorted(p.get("elig") or [])),
                         status="" if status == "ACTIVE" else status,
                         in_ir=(slot == "IR"),
                         lineup_slot=slot,
                         expected_return=p.get("expected_return")))
    return rows


# --------------------------------------------------------------------------
# Fonte de dados alternativa: API publica do ESPN
# Usada quando o stats.nba.com esta bloqueado (GitHub Actions, IPs de cloud).
# --------------------------------------------------------------------------
ESPN_GAMELOG = ("https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba/"
                "athletes/{pid}/gamelog?season={year}")
ESPN_SEARCH = "https://site.web.api.espn.com/apis/search/v2"
ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
# O gamelog do ESPN nao separa rebotes ofensivos; ~24% dos rebotes da NBA sao ofensivos.
OREB_SHARE = 0.24


def _espn_get(url, timeout=30):
    r = requests.get(url, headers=UA, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _pair(v):
    """'8-18' -> (8, 18); '35' -> (35, 0)."""
    parts = str(v).split("-")
    try:
        made = float(parts[0]) if parts[0] else 0.0
        att = float(parts[1]) if len(parts) > 1 and parts[1] else 0.0
        return made, att
    except ValueError:
        return 0.0, 0.0


def espn_gamelog(pid, season, name="", team=""):
    """Converte o gamelog do ESPN nas linhas que prep_logs espera."""
    year = int(season[:4]) + 1
    try:
        d = _espn_get(ESPN_GAMELOG.format(pid=pid, year=year))
    except Exception as ex:
        print(f"Aviso: gamelog ESPN de {name or pid} falhou ({ex}).", file=sys.stderr)
        return []
    labels = d.get("labels") or []
    col = {lab: i for i, lab in enumerate(labels)}
    rows = []
    for stype in d.get("seasonTypes") or []:
        for cat in stype.get("categories") or []:
            for ev in cat.get("events") or []:
                info = (d.get("events") or {}).get(ev.get("eventId"), {})
                gdate = info.get("gameDate")
                stats = ev.get("stats") or []
                if not gdate or len(stats) != len(labels):
                    continue

                def val(lab):
                    i = col.get(lab)
                    return stats[i] if i is not None and i < len(stats) else "0"

                minutes = val("MIN")
                try:
                    if float(minutes or 0) <= 0:
                        continue
                except ValueError:
                    continue
                fgm, fga = _pair(val("FG"))
                fg3m, fg3a = _pair(val("3PT"))
                ftm, fta = _pair(val("FT"))
                reb = float(val("REB") or 0)
                team_abbr = ((info.get("team") or {}).get("abbreviation") or team or "")
                try:
                    gdt = datetime.fromisoformat(gdate.replace("Z", "+00:00"))
                    if gdt.tzinfo is None:
                        gdt = gdt.replace(tzinfo=timezone.utc)
                    game_day = gdt.astimezone(ET).date().isoformat()
                except ValueError:
                    game_day = gdate[:10]
                rows.append(dict(
                    PLAYER_ID=int(pid), PLAYER_NAME=name, TEAM_ABBREVIATION=team_abbr,
                    GAME_DATE=game_day, MIN=float(minutes),
                    FGM=fgm, FGA=fga, FG3M=fg3m, FG3A=fg3a, FTM=ftm, FTA=fta,
                    OREB=round(reb * OREB_SHARE, 2), REB=reb,
                    AST=float(val("AST") or 0), STL=float(val("STL") or 0),
                    BLK=float(val("BLK") or 0), TOV=float(val("TO") or 0),
                    PTS=float(val("PTS") or 0),
                ))
    return rows


def espn_search_id(name):
    """Resolve o ID do ESPN a partir do nome, quando o elenco veio sem id."""
    try:
        d = _espn_get(f"{ESPN_SEARCH}?query={requests.utils.quote(name)}&limit=10")
    except Exception:
        return None, None, None
    best = None
    for grp in d.get("results") or []:
        if grp.get("type") != "player":
            continue
        for item in grp.get("contents") or []:
            sub = (item.get("subtitle") or "")
            if "NBA" not in sub.upper() and item.get("sport") != "basketball":
                continue
            uid = (item.get("uid") or "")
            tail = uid.rsplit("~a:", 1)[-1]
            if tail.isdigit():
                best = best or dict(pid=int(tail), name=item.get("displayName"), team="")
    return (best["pid"], best["name"], best["team"]) if best else (None, None, None)


def fetch_logs_espn(season, players, workers=6):
    """players: [{'espn_id', 'name', 'team'}]. Baixa os game logs em paralelo."""
    from concurrent.futures import ThreadPoolExecutor

    def one(p):
        return espn_gamelog(p["espn_id"], season, p.get("name", ""), p.get("team", ""))

    rows = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for part in ex.map(one, players):
            rows.extend(part)
    return pd.DataFrame(rows, columns=["PLAYER_ID", "PLAYER_NAME", "TEAM_ABBREVIATION",
                                      "GAME_DATE", "MIN", "FGM", "FGA", "FG3M", "FG3A",
                                      "FTM", "FTA", "OREB", "REB", "AST", "STL", "BLK",
                                      "TOV", "PTS"])


def fetch_schedule_espn(days_back=1, days_ahead=15, workers=4, retries=2):
    """Calendario dos proximos dias pelo placar do ESPN (grade de jogos futuros).

    Cada dia e buscado isoladamente; uma falha pontual nao pode zerar o calendario
    inteiro, por isso cada dia tem retry e os que falharem sao avisados.
    """
    from concurrent.futures import ThreadPoolExecutor

    today = datetime.now(ET).date()
    dates = [today - timedelta(days=days_back)] + [today + timedelta(days=i)
                                                   for i in range(days_ahead)]

    def one(day):
        last = None
        for attempt in range(retries + 1):
            try:
                data = _espn_get(f"{ESPN_SCOREBOARD}?dates={day:%Y%m%d}", timeout=20)
                games = []
                for ev in data.get("events") or []:
                    comp = (ev.get("competitions") or [{}])[0]
                    side = {c.get("homeAway"): (c.get("team") or {}).get("abbreviation")
                            for c in comp.get("competitors") or []}
                    if side.get("home") and side.get("away"):
                        games.append((side["home"], side["away"]))
                return day, games, None
            except Exception as ex:
                last = ex
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
        return day, [], last

    out = defaultdict(list)
    failed = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for day, games, err in ex.map(one, dates):
            out[day] = games
            if err is not None:
                failed.append(f"{day:%d/%m} ({err})")
    if failed:
        print(f"Aviso: placar do ESPN falhou para {len(failed)} dia(s): "
              f"{'; '.join(failed[:3])}.", file=sys.stderr)
    if not out.get(today):
        print(f"Aviso: nenhum jogo encontrado para hoje ({today:%d/%m}) no placar do ESPN.",
              file=sys.stderr)
    return dict(out)


def build_universe_espn(roster_rows, fa_players=None, my_players=None, teams=None):
    """Monta a lista de jogadores a analisar: elenco, free agents e outros times.

    Cada item precisa de um espn_id; nomes sem id (sem --espn) sao resolvidos
    pela busca do ESPN.
    """
    universe, seen = [], set()

    def add(pid, name, team=""):
        if not pid or int(pid) in seen:
            return
        seen.add(int(pid))
        universe.append(dict(espn_id=int(pid), name=name or "", team=team or ""))

    def players_of(entry):
        return entry[1] if len(entry) > 1 else []

    for plist in [my_players or []] + [players_of(t) for t in (teams or [])] + [fa_players or []]:
        for p in plist:
            add(p.get("espn_id"), p.get("name"), p.get("team", ""))

    for r in roster_rows:
        nm = r["name"]
        target = norm_name(nm)
        if any(norm_name(u["name"]) == target for u in universe):
            continue
        pid, resolved, team = espn_search_id(nm)
        if pid is None:
            print(f"Aviso: nao achei '{nm}' na API do ESPN; ele fica fora da analise.",
                  file=sys.stderr)
            continue
        add(pid, resolved or nm, team)

    return universe


def nba_reachable(timeout=8):
    """O stats.nba.com bloqueia IPs de datacenter (GitHub Actions, cloud)."""
    try:
        r = requests.get("https://stats.nba.com/stats/commonplayerinfo", headers=UA, timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


# --------------------------------------------------------------------------
# Analise principal (funcao pura -> facilita testes)
# --------------------------------------------------------------------------
IR_ADVICE = {
    "MOVER_PARA_IR": ("Mover para IR", "#12233a", "#60a5fa"),
    "ATIVAR": ("Ativar do IR", "#0f2a1d", "#4ade80"),
    "MANTER_NO_IR": ("Manter no IR", "#232b39", "#9ca3af"),
    "MANTER": ("Manter", "#232b39", "#9ca3af"),
    "IR_CHEIO": ("IR lotado", "#2a2110", "#fbbf24"),
    "DROPAR": ("Dropar", "#2a1215", "#f87171"),
    "OK": ("Sem acao", "#232b39", "#9ca3af"),
}


def drop_options(candidate, roster, today, n=3):
    """Tres candidatos a sair para abrir vaga ao jogador que vai ser ativado.

    Ordena pelo valor efetivo mais baixo (projecao x chance de jogar), entao quem
    esta fora por lesao aparece primeiro. Nunca devolve uma opcao sozinha.
    """
    out = []
    for e in roster:
        if e["pid"] == candidate["pid"] or e.get("in_ir"):
            continue
        eff = e["proj_fppg"] * avail_prob(e["status"], 0)
        out.append((eff, e))
    out.sort(key=lambda x: (x[0], x[1]["dv"].get(today, 0.0)))

    picks = []
    for eff, e in out[:n]:
        if e["status"] in ("OUT", "IR"):
            why = f"ja esta fora por lesao (libera {e['proj_fppg']:.0f} FP de projecao)"
        elif e["status"] == "DTD":
            why = f"day-to-day e o de menor projecao disponivel ({e['proj_fppg']:.0f}/jogo)"
        else:
            why = f"menor projecao do elenco ativo ({e['proj_fppg']:.0f}/jogo)"
        picks.append(dict(name=e["name"], team=e["team"], proj=e["proj_fppg"],
                          status=e["status"], why=why))
    return picks


def ir_advice(roster, today, repl):
    """Avalia cada jogador contra as 3 vagas de IR.

    Regras praticas da liga (10 titulares + 4 banco + 3 IR):
      * Quem esta fora por lesao e volta tarde deve ocupar IR, liberando vaga ativa.
      * Quem esta no IR e ja pode jogar deve ser ativado - e para isso alguem sai,
        por isso a recomendacao sempre traz 3 opcoes de drop.
      * Day-to-day nao vale gastar IR (a vaga e escassa e ele pode jogar amanha).
      * Se o IR esta cheio, o pior ocupante precisa sair para outro entrar.
    """
    ir_used = sum(1 for e in roster if e.get("in_ir"))
    ir_free = max(0, IR_SLOTS - ir_used)
    out = []

    def days_until(ret):
        if not ret:
            return None
        try:
            return (ret - today).days
        except TypeError:
            return None

    for e in sorted(roster, key=lambda x: -x["proj_fppg"]):
        st = e["status"]
        in_ir = e.get("in_ir", False)
        du = days_until(e.get("expected_return"))
        ret_txt = f"retorno previsto {e['expected_return']:%d/%m}" if du is not None else ""
        plays_today = e["dv"][today] > 0
        advice, why, options = "OK", [], []

        if in_ir:
            if st in ("ACTIVE", "DTD"):
                # pode ser ativado: a pergunta e quem sai para abrir a vaga
                advice = "ATIVAR"
                why.append("esta no IR e pode jogar" if st == "ACTIVE"
                           else "esta no IR e o ESPN so marca day-to-day")
                if plays_today:
                    why.append(f"joga hoje e renderia {e['dv'][today]:.0f} FP")
                else:
                    why.append("nao joga hoje, mas nao ha motivo para ocupar vaga no IR")
                    if du is not None:
                        why.append(ret_txt)
                options = drop_options(e, roster, today)
                if options:
                    why.append(f"para ativar, drope um destes {len(options)}")
                else:
                    why.append("sem candidato claro a drop: o elenco esta no limite")
            else:
                advice = "MANTER_NO_IR"
                why.append("continua fora por lesao: nao ha como ativar")
                if du is not None:
                    why += [ret_txt]
                    if du > 14:
                        why.append("ausencia longa: avalie trocar por FA")
                else:
                    why.append("sem previsao de retorno informada pelo ESPN")
        elif st == "OUT":
            if du is not None and 0 <= du <= 3:
                advice = "MANTER"
                why += [ret_txt, "ausencia curta: nao vale gastar IR"]
            elif ir_free > 0:
                advice = "MOVER_PARA_IR"
                why.append("fora e ha vaga no IR (libera slot ativo)")
                if du is not None:
                    why.append(ret_txt)
            elif e["proj_fppg"] < repl:
                advice = "DROPAR"
                why += ["fora, IR lotado e projecao abaixo do nivel de reposicao"]
            else:
                advice = "IR_CHEIO"
                why += ["fora, mas as 3 vagas de IR estao ocupadas",
                        "tire do IR quem ja voltou ou drope um deles"]
        elif st == "DTD":
            advice = "MANTER"
            why.append("day-to-day: nao ocupe IR com duvida de 1 jogo")
        else:
            advice = "OK"
            why.append(f"disponivel ({e['dv'][today]:.0f} FP hoje)")

        out.append(dict(name=e["name"], team=e["team"], status=st, in_ir=in_ir,
                        advice=advice, why=why, proj=e["proj_fppg"],
                        value_today=e["dv"][today], expected_return=e.get("expected_return"),
                        days_out=du, options=options, plays_today=plays_today))
    return out, ir_used, ir_free


def _num(v, default=0.0):
    """Float seguro: NaN/invalidos viram `default`."""
    try:
        f = float(v)
        return default if math.isnan(f) else f
    except (TypeError, ValueError):
        return default


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def games_left(team, days_list, ctx):
    """Quantos jogos o time tem na janela informada."""
    return sum(1 for d in days_list if ctx.game(team, d))


def week_points(player, days_list):
    """FP esperado do jogador na janela (usa o valor por dia ja calculado)."""
    return sum(player["dv"].get(d, 0.0) for d in days_list)


def scenario_points(player, days_list, mode):
    """Pontos em tres cenarios: real, otimista e pessimista.

    A projecao ja remove a sorte de arremesso, entao:
      * otimista = sorte volta a favor + um quarto do desvio padrao;
      * pessimista = sorte volta contra + um quarto do desvio padrao.
    O cenario otimista responde "e se ele voltar ao normal?" para quem esta em
    baixa; o pessimista responde "e se a fase boa acabar?" para quem esta em alta.
    """
    base_pts = week_points(player, days_list)
    proj = _num(player["proj_fppg"])
    if proj <= 0:
        return base_pts, base_pts, base_pts

    luck = _num(player["luck5"])
    sd = abs(_num(player["fp_sd"], 10.0))
    up_fppg = proj + max(0.0, -luck) * 0.7 + 0.25 * sd
    down_fppg = max(0.0, proj - max(0.0, luck) * 0.7 - 0.25 * sd)

    ratio_up, ratio_down = up_fppg / proj, down_fppg / proj
    return base_pts, base_pts * ratio_up, base_pts * ratio_down


def injury_level(p, today):
    """('baixo'|'medio'|'alto', texto) para o risco de lesao do jogador."""
    st = p["status"]
    if st in ("OUT", "IR"):
        ret = p.get("expected_return")
        if ret:
            try:
                d = (ret - today).days
                return "alto", f"fora por lesao (retorno em ~{max(0, d)}d)"
            except TypeError:
                pass
        return "alto", "fora por lesao, sem previsao de retorno"
    if st == "DTD":
        return "medio", "day-to-day: pode ser cortado em cima da hora"
    return "baixo", "saudavel"


def roster_avg(players):
    vals = [_num(p["proj_fppg"]) for p in players if p["status"] not in ("OUT", "IR")]
    return sum(vals) / len(vals) if vals else 0.0


def _scenario_sum(players, days_list):
    """(real, otimista, pessimista) somando um pacote de jogadores."""
    real = up = down = 0.0
    for p in players:
        r, u, d = scenario_points(p, days_list, "real")
        real += r
        up += u
        down += d
    return real, up, down


def _games_sum(players, days_list, ctx):
    return sum(games_left(p["team"], days_list, ctx) for p in players)


def _injury_summary(players, today):
    """(nivel, texto) do pior risco de lesao dentro do pacote."""
    order = {"baixo": 0, "medio": 1, "alto": 2}
    level, hurt = "baixo", []
    for p in players:
        lvl, txt = injury_level(p, today)
        if order[lvl] > order[level]:
            level = lvl
        if lvl != "baixo":
            hurt.append(f"{p['name']} ({txt})")
    if not hurt:
        return level, "saudavel"
    return level, "; ".join(hurt)


def _proj_sum(players):
    return sum(_num(p["proj_fppg"]) for p in players)


def _perceived_sum(players):
    return sum(_num(p["perceived"]) for p in players)


def trade_legal(give, get, my_roster, their_roster, max_roster):
    """Limites duros do ESPN para a troca.

    O limite de pivos e bloqueante: o ESPN recusa a troca que estoura a cota.
    O tamanho do elenco entra como folga de 2 jogadores, porque o outro dono (ou
    voce) pode cortar alguem para fechar um 2x1 — esse custo aparece depois como
    penalidade na chance de aceite, nao como bloqueio.
    """
    give_pids = {p["pid"] for p in give}
    get_pids = {q["pid"] for q in get}
    mine = [x for x in my_roster if x["pid"] not in give_pids] + list(get)
    theirs = [x for x in their_roster if x["pid"] not in get_pids] + list(give)
    if len(mine) > max_roster + 2 or len(theirs) > max_roster + 2:
        return False
    if sum(1 for x in mine if "C" in x["elig"]) > MAX_CENTERS:
        return False
    if sum(1 for x in theirs if "C" in x["elig"]) > MAX_CENTERS:
        return False
    return True


def trade_eval(give, get, my_roster, their_roster, their_record, ctx, days_list,
               today, repl):
    """Avalia uma troca de N por M, dos dois lados.

    `give` = jogadores que eu entrego, `get` = jogadores que eu recebo. Tudo aqui
    e soma bruta de projecao; a variacao de lineup (que e o numero que decide) e
    calculada pelo chamador, que tem acesso ao pool e ao otimizador.
    """
    give, get = list(give), list(get)
    give_pids = {p["pid"] for p in give}
    get_pids = {q["pid"] for q in get}

    # ---- agenda: quantos jogos cada pacote tem na semana --------------
    g_out = _games_sum(give, days_list, ctx)
    g_in = _games_sum(get, days_list, ctx)

    # ---- pontos na semana (real, otimista, pessimista) ----------------
    p_real, p_up, p_down = _scenario_sum(give, days_list)
    q_real, q_up, q_down = _scenario_sum(get, days_list)

    # para mim: entrou q, saiu p
    mine = dict(
        real=q_real - p_real, up=q_up - p_up, down=q_down - p_down,
        games_in=g_in, games_out=g_out, delta_games=g_in - g_out,
        points_in=q_real, points_out=p_real,
    )
    # para ele: entrou p, saiu q
    theirs = dict(
        real=p_real - q_real, up=p_up - q_up, down=p_down - q_down,
        games_in=g_out, games_out=g_in, delta_games=g_out - g_in,
        points_in=p_real, points_out=q_real,
    )

    # ---- variacao da media do elenco ----------------------------------
    my_avg_before = roster_avg(my_roster)
    my_avg_after = roster_avg([x for x in my_roster if x["pid"] not in give_pids] + get)
    their_avg_before = roster_avg(their_roster)
    their_avg_after = roster_avg(
        [x for x in their_roster if x["pid"] not in get_pids] + give)
    mine["avg_delta"] = my_avg_after - my_avg_before
    theirs["avg_delta"] = their_avg_after - their_avg_before

    # ---- risco de lesao -----------------------------------------------
    mine["injury"], mine["injury_txt"] = _injury_summary(get, today)
    theirs["injury"], theirs["injury_txt"] = _injury_summary(give, today)
    mine["conf"] = min((_num(q["conf"], 0.5) for q in get), default=0.5)
    theirs["conf"] = min((_num(p["conf"], 0.5) for p in give), default=0.5)

    # ---- vagas abertas no elenco (2x1 libera espaco para streamer) ----
    mine["open_slots"] = len(give) - len(get)
    theirs["open_slots"] = len(get) - len(give)

    # ---- chance de aceite (lado dele) ---------------------------------
    risk = trade_accept(give, get, their_roster, their_record, theirs)

    return dict(mine=mine, theirs=theirs, risk=risk,
                proj_gain=_proj_sum(get) - _proj_sum(give),
                shape=f"{len(give)}x{len(get)}")


def trade_accept(give, get, their_roster, their_record, theirs):
    """Estima (0-100) a chance de o outro dono aceitar uma troca N x M.

    Combina o que ele ve no ESPN, o ganho real dele na semana, a variacao da media
    do elenco, necessidade posicional, lesao e situacao na tabela.
    """
    give, get = list(give), list(get)
    factors = []

    # 1) valor que ele enxerga (media visivel no ESPN)
    view = _perceived_sum(give) - _perceived_sum(get)
    score = 50 + clamp(view * 4.0, -35.0, 30.0)
    if view >= 1:
        factors.append(f"no ESPN ele ve {view:+.1f} FP/jogo a favor dele")
    elif view <= -1:
        factors.append(f"no ESPN ele ve {view:+.1f} FP/jogo contra ele")
    else:
        factors.append("no ESPN a troca parece equilibrada")

    # 2) ganho real dele na semana (lineup otimizado)
    wg = theirs["real"]
    score += clamp(wg * 0.7, -25.0, 20.0)
    if wg >= 5:
        factors.append(f"ele ganha {wg:+.0f} FP na semana")
    elif wg <= -5:
        factors.append(f"ele perde {wg:+.0f} FP na semana")

    # 3) agenda: mais jogos costuma agradar
    if theirs["delta_games"] > 0:
        score += min(8, theirs["delta_games"] * 3)
        factors.append(f"recebe {theirs['delta_games']:+d} jogo(s) a mais na semana")
    elif theirs["delta_games"] < 0:
        score -= min(8, -theirs["delta_games"] * 3)

    # 4) media do elenco dele
    ad = theirs["avg_delta"]
    score += clamp(ad * 2.5, -12.0, 12.0)
    if abs(ad) >= 0.5:
        factors.append(f"media do elenco dele varia {ad:+.1f} FP/jogo")

    # 5) necessidade posicional — vale para o pacote inteiro
    healthy = [x for x in their_roster if x["status"] not in ("OUT", "IR")]
    incoming_pos = set().union(*[set(p["elig"]) for p in give]) if give else set()
    outgoing_pos = set().union(*[set(q["elig"]) for q in get]) if get else set()
    need = sum(1 for pos in incoming_pos
               if sum(1 for x in healthy if pos in x["elig"]) <= 2)
    surplus = sum(1 for pos in outgoing_pos
                  if sum(1 for x in healthy if pos in x["elig"]) >= 5)
    if need:
        score += 8 * min(need, 2)
        factors.append("ele precisa da posicao que recebe")
    if surplus:
        score += 6 * min(surplus, 2)
        factors.append("voce pede uma posicao em que ele tem sobra")

    # 6) lesao de quem ele recebe: ninguem aceita jogador quebrado
    if theirs["injury"] == "alto":
        score = min(score - 40, 12)
        factors.append(f"voce esta oferecendo alguem lesionado ({theirs['injury_txt']})")
    elif theirs["injury"] == "medio":
        score = min(score - 12, 55)
        factors.append("voce esta oferecendo alguem day-to-day")

    # 7) ele fica com jogadores demais para o elenco: precisa cortar alguem
    if theirs["open_slots"] < 0:
        score -= 7 * min(3, -theirs["open_slots"])
        factors.append(f"ele recebe {len(give)} e manda {len(get)}: sobra jogador no elenco")

    # 8) situacao na tabela
    try:
        wins, losses = their_record
    except (TypeError, ValueError):
        wins, losses = 0, 0
    wins, losses = int(wins or 0), int(losses or 0)
    if wins + losses >= 10:
        rate = wins / (wins + losses)
        score -= (rate - 0.5) * 26
        if rate >= 0.6:
            factors.append(f"ele esta bem ({wins}-{losses}): resiste a mexer no time")
        elif rate <= 0.4:
            factors.append(f"ele esta mal ({wins}-{losses}): tende a aceitar mudancas")

    accept = int(clamp(round(score), 2, 97))
    reject = 100 - accept
    if reject <= 35:
        label, color = "Baixo", ("#10281c", "#4ade80")
    elif reject <= 60:
        label, color = "Medio", ("#2b2210", "#fbbf24")
    else:
        label, color = "Alto", ("#2a1417", "#f87171")

    # Em pacotes maiores a diferenca absoluta de projecao cresce, entao o limite
    # de veto acompanha o numero de jogadores envolvidos. `gain` e o que EU ganho.
    gain = _proj_sum(get) - _proj_sum(give)
    scale = max(1.0, (len(give) + len(get)) / 2.0)
    veto = ("alto" if gain > 8 * scale
            else ("medio" if gain > 5 * scale else "baixo"))
    return dict(accept=accept, reject=reject, label=label, color=color,
                factors=factors, veto=veto)


def find_trades(my_roster, give_pool, teams, pool, base_ids, base_week, days,
                wk_days, ctx, today, repl, max_roster):
    """Procura trocas em formatos 1x1, 2x1, 2x2, 1x2, 3x1 e 1x3.

    `teams` = [(nome, roster_deles, campanha)]. Faz um pre-filtro barato pela soma
    de projecao e depois reavalia os melhores candidatos com o otimizador de
    lineup, porque a soma bruta superestima pacotes (nem todo mundo entra no time).
    Devolve os candidatos ordenados por atratividade, ja com o ganho de lineup.
    """
    SHAPES = [(1, 1), (2, 1), (1, 2), (2, 2), (3, 1), (1, 3)]
    POOL = 8            # jogadores considerados de cada lado
    SHORTLIST = 40      # candidatos que passam para a checagem de lineup
    MAX_PER_TEAM = 2
    MAX_PER_SHAPE = 3
    MAX_TOTAL = 10

    def names_of(players):
        return " + ".join(p["name"] for p in players)

    give_pool = sorted(give_pool, key=lambda e: -_num(e["perceived"]))[:POOL]
    if not give_pool:
        return [], names_of
    give_sets = {k: list(combinations(give_pool, k)) for k in {n for n, _ in SHAPES}}
    my_give_pids = {e["pid"] for e in give_pool}

    rows = []
    for tname, their_roster, their_record in teams:
        their_roster = list(their_roster)
        if not their_roster:
            continue
        get_pool = sorted((x for x in their_roster
                           if x["status"] not in ("OUT", "IR")
                           and x["pid"] not in my_give_pids),
                          key=lambda e: -_num(e["perceived"]))[:POOL]
        if not get_pool:
            continue
        get_sets = {k: list(combinations(get_pool, k)) for k in {m for _, m in SHAPES}}
        their_base_ids = [x["pid"] for x in their_roster]
        their_base_week = week_value(their_base_ids, pool, days)

        cheap = []
        for n_give, n_get in SHAPES:
            for give in give_sets[n_give]:
                for get in get_sets[n_get]:
                    if not trade_legal(give, get, my_roster, their_roster, max_roster):
                        continue
                    # pre-filtro bruto: nao desistir de mais da metade do que sai.
                    # Um 2x1 valido costuma perder projecao bruta (dois jogadores
                    # de banco somam mais que uma estrela) mas ganha na escalacao,
                    # entao o corte tem de ser relativo, nao absoluto.
                    if _proj_sum(get) < 0.5 * _proj_sum(give):
                        continue
                    ev = trade_eval(give, get, my_roster, their_roster, their_record,
                                    ctx, wk_days, today, repl)
                    if ev["mine"]["real"] < -0.5 * max(1.0, ev["mine"]["points_out"]):
                        continue
                    if ev["theirs"]["real"] < -0.5 * max(1.0, ev["theirs"]["points_in"]):
                        continue
                    if ev["risk"]["reject"] > 70:
                        continue
                    cheap.append((ev, give, get))

        # a soma bruta superestima pacotes (nem todos entram no lineup), entao os
        # melhores passam pela checagem com o otimizador. Vaga aberta entra aqui
        # para uma consolidacao 2x1 nao ser descartada por projecao bruta.
        cheap.sort(key=lambda c: -(c[0]["mine"]["real"] + c[0]["mine"]["up"] * 0.5
                                  + c[0]["mine"]["open_slots"] * 25.0))
        for ev, give, get in cheap[:SHORTLIST]:
            give_pids = [p["pid"] for p in give]
            get_pids = [q["pid"] for q in get]
            wg_mine = week_value([i for i in base_ids if i not in give_pids] + get_pids,
                                 pool, days) - base_week
            wg_theirs = week_value([i for i in their_base_ids if i not in get_pids]
                                   + give_pids, pool, days) - their_base_week
            if wg_mine <= 0:
                continue
            if wg_theirs < -0.5 * max(1.0, ev["theirs"]["points_in"]):
                continue

            ev["mine"]["lineup"] = wg_mine
            ev["theirs"]["lineup"] = wg_theirs

            # ganho de lineup manda; cenario otimista e vaga aberta desempatam
            score = (wg_mine * 0.55 + ev["mine"]["up"] * 0.20
                     + ev["mine"]["real"] * 0.10
                     + ev["mine"]["open_slots"] * 6.0)
            score *= 0.55 + 0.45 * (ev["risk"]["accept"] / 100)
            rows.append(dict(team=tname, give=give, get=get, wg=wg_mine,
                             ev=ev, score=score, shape=ev["shape"]))

    rows.sort(key=lambda r: -r["score"])
    out, per_team, per_shape, used = [], defaultdict(int), defaultdict(int), set()

    def take(r, strict):
        """Aceita o candidato respeitando os limites. `strict` impede repetir jogador."""
        pids = {p["pid"] for p in r["give"]} | {q["pid"] for q in r["get"]}
        if per_team[r["team"]] >= MAX_PER_TEAM or per_shape[r["shape"]] >= MAX_PER_SHAPE:
            return False
        if strict and pids & used:
            return False
        out.append(r)
        per_team[r["team"]] += 1
        per_shape[r["shape"]] += 1
        used.update(pids)
        return True

    # 1) garante um exemplo de cada formato: e util ver "o mesmo jogador em 1x1 ou
    #    em 2x1" para comparar. Por isso aqui um jogador pode reaparecer.
    for shape in (f"{a}x{b}" for a, b in SHAPES):
        if len(out) >= MAX_TOTAL:
            break
        for r in rows:
            if r["shape"] == shape and take(r, strict=False):
                break

    # 2) completa com as melhores restantes, sem repetir jogador
    taken = {id(r) for r in out}
    for r in rows:
        if len(out) >= MAX_TOTAL:
            break
        if id(r) in taken:
            continue
        take(r, strict=True)

    out.sort(key=lambda r: -r["score"])
    return out, names_of


def plan_lineup_changes(current, recom, entering, by_name, today):
    """Mudancas de escalacao, pareando cada reserva apto com UMA saida.

    Cada jogador do banco abre uma vaga so. Com 5 titulares sem jogo e 2 reservas
    aptos, apenas 2 precisam sair: listar os 5 nao muda o resultado final e deixa
    o relatorio repetitivo. Retorna (changes, leftover), onde `leftover` e quantos
    titulares sem jogo seguem escalados por falta de banco para entrar.
    """
    changes = []
    for e in entering:
        changes.append(dict(kind="entrar", name=e["name"],
                            slot=recom[norm_name(e["name"])][1],
                            fp=e["dv"][today], proj=e["proj_fppg"],
                            why="esta no banco do ESPN e joga hoje"))

    # Sem ninguem do banco apto a entrar, tirar o titular nao muda nada:
    # nao reportamos remocao nem lesao nesse caso.
    if not entering:
        return changes, 0

    outgoing = []
    for key, (name, slot) in current.items():
        if key in recom:
            continue
        p = by_name.get(key)
        st = p["status"] if p else "ACTIVE"
        fp = p["dv"][today] if p else 0.0
        if st in ("OUT", "IR"):
            sev, kind, why = 0, "lesionado", "fora por lesao e segue escalado"
        elif st == "DTD":
            sev, kind, why = 1, "risco", "day-to-day escalado"
        elif fp <= 0:
            sev, kind, why = 2, "sair", "nao joga hoje"
        else:
            # Titular joga: a troca por um reserva melhor ja aparece como
            # "adicionar" no outro lado, entao nao duplicamos o aviso aqui.
            continue
        outgoing.append(dict(kind=kind, name=name, slot=slot, fp=fp,
                             proj=(p["proj_fppg"] if p else 0.0),
                             why=why, sev=sev))

    # Sai primeiro quem nao pontua de jeito nenhum: fora por lesao e quem nao
    # joga hoje (0 FP garantido). Um DTD que joga hoje fica por ultimo, porque
    # tirar ele pode custar pontos. Empate em FP desempata pela gravidade.
    outgoing.sort(key=lambda c: (c["fp"], c["sev"]))
    subs = sorted(entering, key=lambda x: -x["dv"][today])
    for sub, out in zip(subs, outgoing):
        sub_txt = f"{sub['name']} joga hoje e renderia {sub['dv'][today]:.0f} FP"
        changes.append(dict(kind=out["kind"], name=out["name"], slot=out["slot"],
                            fp=out["fp"], proj=out["proj"],
                            why=f"{out['why']}; {sub_txt}",
                            sub=sub["name"], sub_fp=sub["dv"][today]))
    return changes, max(0, len(outgoing) - len(subs))


def run_analysis(cur, prev, sched, tstats, positions, roster_rows, today, opts,
                 fa_players=None, other_teams=None, espn_status=None):
    notes = []
    summary = []
    L = []  # linhas do relatorio
    digest = dict(date=today, lineup=[], bench=[], alerts=[], adds=[], trades=[], ir=[],
                  total=0.0, week="", repl=0.0, empty_slots=0,
                  ir_used=0, ir_free=IR_SLOTS, tight=[], lineup_choice=False,
                  sells=[], buys=[], changes=[], changes_known=False,
                  changes_leftover=0,
                  lineup_only=bool(getattr(opts, "lineup_only", False)))

    def finalize():
        low = [e["name"] for e in roster if e["conf"] < 0.3]
        if low:
            notes.append("Amostra pequena na temporada para: " + ", ".join(low) +
                         ". A projecao pesa mais a temporada passada ate ~15 jogos.")
        stale = [e["name"] for e in roster if e["stale"]]
        if stale:
            notes.append("Sem jogos nesta temporada (time pode estar desatualizado): " + ", ".join(stale))
        if not any(e["status"] in ("DTD", "OUT", "IR") for e in roster) and not espn_status:
            notes.append("Status de lesao nao informado pelo ESPN; confira manualmente antes de agir.")
        if notes:
            L.extend(["## Avisos", ""] + [f"- {n}" for n in notes] + [""])
        return "\n".join(L), summary, digest

    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=6)
    horizon_end = week_end if (week_end - today).days >= 2 else week_end + timedelta(days=7)
    days = [today + timedelta(days=i) for i in range((horizon_end - today).days + 1)]
    wk_days = [d for d in days if d <= week_end]

    done_today = set(cur[cur.GAME_DATE == today].PLAYER_ID.astype(int)) if len(cur) else set()
    ctx = Ctx(sched, tstats, today, done_today)
    if sched and not sched.get(today):
        notes.append(f"O calendario nao trouxe jogos para hoje ({today:%d/%m}). Se a NBA joga hoje, "
                     "a consulta ao placar pode ter falhado: rode com --refresh.")

    profiles = build_profiles(cur, prev, opts.recent)
    idx = NameIndex(profiles)

    def mk(prof, elig, status, in_ir=False, expected_return=None):
        e = dict(prof)
        e["elig"] = elig
        e["status"] = norm_status(status)
        e["in_ir"] = bool(in_ir)
        e["expected_return"] = expected_return
        e["tag"], e["why"] = form_tag(e, opts.recent)
        e["dv"] = {d: day_value(e, d, ctx) for d in days}
        return e

    def pos_of(pid):
        return NBA_POS_MAP.get(positions.get(pid, ""), set()) if positions else set()

    # ---- elenco -------------------------------------------------------
    roster, seen = [], set()
    for r in roster_rows:
        pid, fuzzy = idx.find(r["name"])
        if pid is None:
            notes.append(f"Jogador do roster NAO encontrado nos dados da NBA: '{r['name']}' (confira a grafia).")
            continue
        if pid in seen:
            continue
        seen.add(pid)
        if fuzzy:
            notes.append(f"'{r['name']}' foi associado a '{profiles[pid]['name']}' (confira se esta certo).")
        elig = parse_pos(r["pos"]) or pos_of(pid)
        status = r["status"] or (espn_status or {}).get(norm_name(r["name"]), "")
        roster.append(mk(profiles[pid], elig, status, r.get("in_ir", False),
                         r.get("expected_return")))
    if not roster:
        return "Nenhum jogador do roster foi reconhecido.", [], digest
    pool = {e["pid"]: e for e in roster}
    roster_ids = [e["pid"] for e in roster]

    # ---- candidatos (free agents) ------------------------------------
    cands = []
    if fa_players:
        fa_label = "free agents reais da sua liga (ESPN)"
        for fp in fa_players:
            pid, _ = idx.find(fp["name"])
            if pid is None or pid in pool:
                continue
            elig = fp.get("elig") or pos_of(pid)
            cands.append(mk(profiles[pid], elig, fp.get("status", "")))
    else:
        fa_label = "jogadores da NBA em geral (VERIFIQUE se estao livres na sua liga)"
        top = sorted((p for p in profiles.values() if p["pid"] not in pool and not p["stale"]
                      and p["conf"] >= 0.2 and p["proj_min"] >= 14),
                     key=lambda p: -p["proj_fppg"])[:120]
        for p in top:
            cands.append(mk(p, pos_of(p["pid"]), ""))
        if positions is None or not positions:
            notes.append("Posicoes dos candidatos indisponiveis (PlayerIndex falhou): so UTIL foi considerado.")
    cands = [c for c in cands if c["status"] not in ("OUT", "IR")]
    # jogador de time rival nao e free agent: so pode chegar por troca
    if other_teams:
        owned_pids = set()
        for entry in other_teams:
            for x in (entry[1] if len(entry) > 1 else []):
                pid_, _ = idx.find(x["name"])
                if pid_ is not None:
                    owned_pids.add(pid_)
        cands = [c for c in cands if c["pid"] not in owned_pids]
    for c in cands:
        pool[c["pid"]] = c

    # nivel de reposicao
    healthy = sorted((c["proj_fppg"] for c in cands), reverse=True)
    repl = healthy[4] if (fa_players and len(healthy) >= 5) else opts.replacement

    # Free agent so entra na lista se estiver pontuando bem: projecao acima do nivel
    # de reposicao e sem sinal de queda. Quem esta em baixa nao ajuda agora.
    cands = [c for c in cands
             if c["proj_fppg"] >= repl
             and c["tag"] not in ("PAPEL_ENCOLHENDO", "AMOSTRA_PEQUENA")
             and c["conf"] >= 0.2
             and c["fp5"] >= repl * 0.85]
    for c in cands:
        pool[c["pid"]] = c

    # ---- cabecalho ---------------------------------------------------
    digest.update(week=f"{week_start:%d/%m} a {week_end:%d/%m}",
                  repl=repl,
                  trade_days=(TRADE_DEADLINE - today).days)
    L += [f"# Relatorio NBA Fantasy - {today.strftime('%d/%m/%Y')} ({today.strftime('%A')})",
          f"Semana do confronto: {week_start.strftime('%d/%m')} a {week_end.strftime('%d/%m')} | "
          f"Prazo de trocas: {(TRADE_DEADLINE - today).days} dias ({TRADE_DEADLINE.strftime('%d/%m/%Y')})",
          f"Nivel de reposicao usado: ~{repl:.0f} FP/jogo | Candidatos: {fa_label}",
          ""]

    # ========== 1) LINEUP DE HOJE ======================================
    active_ids = [e["pid"] for e in roster if e["status"] != "IR" and not e.get("in_ir")]
    elig = [pool[i]["elig"] for i in active_ids]
    vals = [pool[i]["dv"][today] for i in active_ids]
    assign, total = optimize(elig, vals)
    starters = {pi: sj for sj, pi in assign}
    L += ["## 1. Lineup otimo de hoje", f"Valor esperado total: **{total:.1f} FP**", ""]
    digest["total"] = total
    digest["empty_slots"] = n_empty = len(LINEUP_SLOTS) - len(assign)
    rows = []
    for sj, pi in assign:
        e = pool[active_ids[pi]]
        gm = ctx.game(e["team"], today)
        opp = f"{'vs' if gm[1] else '@'} {gm[0]}" if gm else "-"
        flag = " (DTD)" if e["status"] == "DTD" else ""
        digest["lineup"].append(dict(
            slot=LINEUP_SLOTS[sj], name=e["name"], pid=e["pid"], team=e["team"],
            opp=opp, fp=vals[pi], minutes=e["proj_min"], recent=e["fp5"],
            tag=e["tag"], status=e["status"]))
        rows.append((LINEUP_SLOTS[sj], e["name"] + flag, opp, f1(vals[pi]), f1(e["proj_min"], 0),
                     f1(e["fp5"]), e["tag"].replace("_", " ").title() if e["tag"] != "NEUTRO" else ""))
    L += md_table(["Slot", "Jogador", "Jogo", "FP esperado", "Min proj", "FP ult.%d" % opts.recent, "Fase"], rows)
    filled = {LINEUP_SLOTS[sj] for sj, _ in assign}
    n_empty = len(LINEUP_SLOTS) - len(assign)
    if n_empty:
        L += ["", f"Atencao: {n_empty} slot(s) ficaram sem jogador elegivel com jogo hoje."]
    L += ["", "**Fora do lineup:**"]
    for e in sorted(roster, key=lambda x: -x["dv"][today]):
        pi = active_ids.index(e["pid"]) if e["pid"] in active_ids else None
        if pi is not None and pi in starters:
            continue
        if e["status"] in ("IR", "OUT"):
            why = "fora por lesao" + (" (no IR)" if e.get("in_ir") else "")
        elif not ctx.game(e["team"], today):
            why = "sem jogo hoje" + (" (no IR)" if e.get("in_ir") else "")
        elif e["pid"] in done_today:
            why = "jogo de hoje ja terminou"
        elif e.get("in_ir"):
            why = "esta no IR: ative para escalar"
        else:
            why = f"banco (valor {e['dv'][today]:.1f} perde para os titulares)"
        digest["bench"].append(dict(name=e["name"], team=e["team"], why=why,
                                    status=e["status"]))
        L.append(f"- {e['name']} ({e['team']}): {why}")

    # ========== 1b) QUEM ENTRA E QUEM SAI ==============================
    # So faz sentido quando ha mais jogadores elegiveis do que slots escalaveis.
    tight = []
    if len(active_ids) > len(LINEUP_SLOTS):
        for sj, pi in assign:
            e = pool[active_ids[pi]]
            slot = LINEUP_SLOTS[sj]
            alts = [pool[active_ids[k]] for k in range(len(active_ids))
                    if k not in starters and pool[active_ids[k]]["dv"][today] > 0
                    and (slot == "UTIL" or pool[active_ids[k]]["elig"] & SLOT_ELIG[slot])]
            if not alts:
                continue
            b = max(alts, key=lambda x: x["dv"][today])
            tight.append(dict(slot=slot, starter=e["name"],
                              starter_fp=e["dv"][today], challenger=b["name"],
                              challenger_fp=b["dv"][today],
                              margin=e["dv"][today] - b["dv"][today]))
        tight.sort(key=lambda x: x["margin"])
        digest["tight"] = tight[:3]
        digest["lineup_choice"] = True
        if tight:
            L += ["", f"**Disputa por vaga** ({len(active_ids)} jogadores elegiveis para "
                      f"{len(LINEUP_SLOTS)} slots):"]
            for t in tight[:3]:
                if t["margin"] <= 3:
                    L.append(f"- Slot {t['slot']}: **{t['challenger']}** ({t['challenger_fp']:.1f} FP) "
                             f"esta a {t['margin']:.1f} FP de tomar o lugar de "
                             f"**{t['starter']}** ({t['starter_fp']:.1f} FP).")
                else:
                    L.append(f"- Slot {t['slot']}: **{t['starter']}** ({t['starter_fp']:.1f} FP) "
                             f"seguro; {t['challenger']} precisaria de "
                             f"+{t['margin']:.1f} FP.")
    else:
        digest["tight"] = []
        digest["lineup_choice"] = False
    # backups para DTD
    for sj, pi in assign:
        e = pool[active_ids[pi]]
        if e["status"] == "DTD":
            slot = LINEUP_SLOTS[sj]
            alts = [pool[active_ids[k]] for k in range(len(active_ids))
                    if k not in starters and pool[active_ids[k]]["dv"][today] > 0
                    and (slot == "UTIL" or pool[active_ids[k]]["elig"] & SLOT_ELIG[slot])]
            if alts:
                b = max(alts, key=lambda x: x["dv"][today])
                L.append(f"- Se {e['name']} (DTD) nao jogar, entra **{b['name']}** no slot {slot}.")
    summary.append(f"LINEUP HOJE ({total:.0f} FP): " + ", ".join(
        f"{LINEUP_SLOTS[sj]} {pool[active_ids[pi]]['name'].split()[-1]}" for sj, pi in assign))
    L.append("")

    # ========== 2) IR E STATUS =========================================
    ir_rows, ir_used, ir_free = ir_advice(roster, today, repl)
    digest["ir"] = ir_rows
    digest["ir_used"], digest["ir_free"] = ir_used, ir_free
    L += ["## 2. IR e status do elenco",
          f"IR em uso: **{ir_used}/{IR_SLOTS}** ({ir_free} vaga(s) livre(s)) | "
          f"Banco: {BENCH_SLOTS} | Titulares: {len(LINEUP_SLOTS)}", ""]
    pending = [r for r in ir_rows if r["advice"] not in ("OK", "MANTER_NO_IR", "MANTER")]
    if pending:
        for r in pending:
            L.append(f"- **{r['name']}** ({r['team']}, {r['status']}): "
                     f"**{IR_ADVICE[r['advice']][0]}** - " + "; ".join(r["why"]))
            for i, o in enumerate(r.get("options") or [], start=1):
                L.append(f"  {i}. dropar **{o['name']}** ({o['team']}, {o['proj']:.0f} FP/jogo) "
                         f"- {o['why']}")
            summary.append(f"IR: {IR_ADVICE[r['advice']][0]} {r['name']}")
    else:
        L.append("Nenhuma acao de IR pendente: elenco e IR coerentes com o status do ESPN.")
    L.append("")
    rows = []
    for r in ir_rows:
        rows.append((r["name"], r["team"], r["status"], "sim" if r["in_ir"] else "nao",
                     f"{r['proj']:.1f}", f"{r['value_today']:.1f}",
                     (f"{r['expected_return']:%d/%m}" if r["expected_return"] else "-"),
                     IR_ADVICE[r["advice"]][0]))
    L += md_table(["Jogador", "Time", "Status", "No IR", "Proj/jogo", "Valor hoje",
                   "Retorno", "Acao"], rows)
    L += ["", "Regras: fora por lesao e com retorno distante ocupa IR; day-to-day nao gasta vaga; "
          "quem esta no IR sem lesao precisa ser ativado para voltar a pontuar.", ""]

    # ========== 2b) MUDANCAS NECESSARIAS NA ESCALACAO ==================
    # Compara o lineup recomendado com o que esta realmente escalado no ESPN
    # (campo lineupSlot). Sem estado entre execucoes: o ESPN e a fonte da verdade.
    current = {}
    for r in roster_rows:
        slot = (r.get("lineup_slot") or "").upper()
        slot = "UTIL" if slot == "UT" else slot
        if slot in LINEUP_SLOTS:
            current[norm_name(r["name"])] = (r["name"], slot)

    recom = {norm_name(pool[active_ids[pi]]["name"]): (pool[active_ids[pi]]["name"],
                                                      LINEUP_SLOTS[sj])
             for sj, pi in assign}
    by_name = {norm_name(e["name"]): e for e in roster}

    # Quem o otimizador colocaria em campo e nao esta escalado no ESPN agora.
    # Como o lineup e uma atribuicao global (o reserva pode entrar em SF e o titular
    # deslizar para UTIL), a existencia de troca e verificada no conjunto, nao por slot.
    entering = [by_name[k] for k in recom if k not in current and k in by_name]
    entering = [e for e in entering if e["dv"][today] > 0]

    changes = []
    leftover_out = 0
    if current:
        changes, leftover_out = plan_lineup_changes(current, recom, entering,
                                                    by_name, today)

    digest["changes"] = changes
    digest["changes_leftover"] = leftover_out
    digest["changes_known"] = bool(current)

    if changes:
        n_swap = sum(1 for c in changes if c.get("sub"))
        n_add = len(changes) - n_swap
        parts = [f"{n_swap} troca(s)"] if n_swap else []
        if n_add:
            parts.append(f"{n_add} entrada(s) sem vaga livre" if n_swap else f"{n_add} entrada(s)")
        L += [f"**Mudancas necessarias na escalacao** — {', '.join(parts)}:"]
        for c in changes:
            L.append(f"- [{c['slot']}] **{c['name']}**: {c['why']} ({c['fp']:.1f} FP hoje)")
        if leftover_out:
            L.append(f"- Os outros {leftover_out} titular(es) sem jogo hoje seguem escalados: "
                     f"nao ha mais ninguem no banco para entrar, entao nao ha o que trocar.")
        L.append("")
        # O resumo prioriza as saidas: sao elas que o usuario executa.
        prio = [c for c in changes if c["kind"] != "entrar"] + \
               [c for c in changes if c["kind"] == "entrar"]
        for c in prio[:3]:
            summary.append(f"ESCALACAO: {c['why'].split(';')[0]} {c['name']} ({c['slot']})")
    elif current:
        L += ["Escalacao ja esta igual a recomendada: nenhuma mudanca necessaria.", ""]

    # Modo enxuto (pre-jogo): so lineup + IR.
    if digest["lineup_only"]:
        return finalize()

    # ========== 3) SEMANA / FORMA ======================================
    cw = cur[(cur.GAME_DATE >= week_start) & (cur.GAME_DATE <= today)] if len(cur) else cur
    wk = {k: g for k, g in cw.groupby("PLAYER_ID")} if len(cw) else {}
    L += ["## 3. Semana do confronto e fase de cada jogador", ""]
    rows, flags = [], []
    for e in sorted(roster, key=lambda x: -x["proj_fppg"]):
        g = wk.get(e["pid"])
        nj = len(g) if g is not None else 0
        fps = float(g.FP.sum()) if nj else 0.0
        exp = e["proj_fppg"] * nj
        z = (fps - exp) / (e["fp_sd"] * math.sqrt(nj)) if nj and e["fp_sd"] else 0.0
        start = today + timedelta(days=1) if e["pid"] in done_today else today
        rem = sum(1 for d in wk_days if d >= start and ctx.game(e["team"], d))
        if e["status"] in ("OUT", "IR"):
            ver = "FORA (lesao)" + (" - candidato a drop se for longa" if e["proj_fppg"] < repl else "")
        elif e["tag"] == "VENDER_NA_ALTA":
            ver = "VENDER NA ALTA"
        elif e["tag"] == "COMPRAR_NA_BAIXA":
            ver = "SEGURAR (fase ruim passageira - nao vender agora)"
        elif e["tag"] == "PAPEL_ENCOLHENDO":
            ver = "ALERTA: papel encolhendo"
        elif e["proj_fppg"] < repl - 1:
            ver = "CANDIDATO A DROP"
        else:
            ver = "Manter"
        # No proprio elenco so importa o risco de queda: comprar na baixa nao e acao.
        if e["tag"] in ("VENDER_NA_ALTA", "PAPEL_ENCOLHENDO"):
            flags.append(e)
        rows.append((e["name"], e["team"], f"{nj}J - {fps:.0f} (esp. {exp:.0f}; z={z:+.1f})" if nj else "0J",
                     f1(e["fp5"]), f1(e["proj_fppg"]), sgn(e["delta_form"]), sgn(e["min_delta"]),
                     rem, ver))
    L += md_table(["Jogador", "Time", "Semana: FP real vs esperado", "FP ult.%d" % opts.recent, "Proj/jogo",
                   "Forma vs proj", "Min delta", "Jogos rest. sem.", "Veredito"], rows)
    L += ["", "**Sinais detalhados:**"]
    if not flags:
        L.append("- Nenhum sinal forte de regressao no momento (amostra ainda pequena? veja 'conf' abaixo).")
    for e in flags:
        lab = {"VENDER_NA_ALTA": "VENDER NA ALTA", "COMPRAR_NA_BAIXA": "COMPRAR NA BAIXA / SEGURAR",
               "PAPEL_ENCOLHENDO": "PAPEL ENCOLHENDO", "ALTA_SUSTENTAVEL": "Alta sustentavel"}[e["tag"]]
        conf = "" if e["conf"] >= 0.5 else " [amostra pequena: confianca baixa]"
        digest["alerts"].append(dict(name=e["name"], team=e["team"], tag=e["tag"], label=lab,
                                     why=e["why"], conf=e["conf"],
                                     delta_form=e["delta_form"]))
        L.append(f"- **{e['name']}** - {lab}: " + "; ".join(e["why"]) + conf)
        if e["tag"] == "VENDER_NA_ALTA":
            digest["sells"].append(dict(name=e["name"], team=e["team"], proj=e["proj_fppg"],
                                        why=e["why"]))
            summary.append(f"VENDER NA ALTA: {e['name']} ({'; '.join(e['why'])})")
        if e["tag"] == "PAPEL_ENCOLHENDO":
            digest["sells"].append(dict(name=e["name"], team=e["team"], proj=e["proj_fppg"],
                                        why=e["why"]))
            summary.append(f"ALERTA papel: {e['name']} ({'; '.join(e['why'])})")
    L.append("")

    # ---- alvos de compra nos elencos rivais ---------------------------
    if other_teams:
        my_pids = {e["pid"] for e in roster}
        seen_buy = {s["name"] for s in digest["sells"]}
        for entry in other_teams:
            tname, plist = entry[0], entry[1]
            for x in plist:
                pid, _ = idx.find(x["name"])
                if pid is None or pid in my_pids:
                    continue
                prof = profiles.get(pid)
                if not prof or prof["stale"] or prof["proj_fppg"] < repl:
                    continue
                tag, why = form_tag(prof, opts.recent)
                if tag != "COMPRAR_NA_BAIXA":
                    continue
                if prof["name"] in seen_buy:
                    continue
                seen_buy.add(prof["name"])
                digest["buys"].append(dict(
                    name=prof["name"], team=prof["team"], owner=tname,
                    proj=prof["proj_fppg"], recent=prof["fp5"], why=why,
                    status=norm_status(x.get("status"))))
        digest["buys"].sort(key=lambda b: -b["proj"])
        digest["buys"] = digest["buys"][:6]
        if digest["buys"]:
            L.append("**Alvos de compra nos elencos rivais** (fase de baixa: o dono pode vender barato):")
            for b in digest["buys"]:
                L.append(f"- {b['name']} ({b['team']}, de {b['owner']}): proj {b['proj']:.1f}/jogo, "
                         f"FP ult.{opts.recent} {b['recent']:.1f} - " + "; ".join(b["why"]))
            L.append("")
    L.append("")

    # ficha avancada
    L += ["**Metricas avancadas do elenco** (proj = projecao por jogo ajustada por sorte e regredida a temporada passada; "
          "sorte = FP/jogo acima do esperado nos ultimos jogos; Usg36 = FGA+0,44FTA+TO por 36 min; "
          "DD% = taxa de double-double; SD = variacao de FP por jogo):", ""]
    rows = []
    for e in sorted(roster, key=lambda x: -x["proj_fppg"]):
        rows.append((e["name"], e["gp"], f1(e["mpg"]), f1(e["fppg"]), f1(e["fp10"]), f1(e["fp5"]),
                     f1(e["proj_fppg"]), sgn(e["luck5"]), f1(e["usg"]), f"{e['dd_rate']*100:.0f}%",
                     f1(e["fp_sd"]), f"{e['conf']*100:.0f}%"))
    L += md_table(["Jogador", "GP", "MPG", "FP/j", "FP L10", "FP L%d" % opts.recent, "Proj", "Sorte L%d" % opts.recent,
                   "Usg36", "DD%", "SD", "Conf"], rows)
    L.append("")

    # ========== 3) ADD / DROP ==========================================
    L += ["## 4. Contratar / dropar (free agents)", ""]
    # Quem pode ir para o IR nao precisa ser dropado: o IR garante o jogador de graca.
    parkable = {r["name"] for r in ir_rows if r["advice"] == "MOVER_PARA_IR"}
    droppable = [e for e in roster
                 if e["status"] != "IR" and not e.get("in_ir") and e["name"] not in parkable]
    n_centers = sum(1 for e in roster if "C" in e["elig"])
    base_ids = [e["pid"] for e in roster if not e.get("in_ir")]
    base_week = week_value(base_ids, pool, days)

    def rank_pre(c):
        gms = sum(1 for d in days if ctx.game(c["team"], d))
        return c["proj_fppg"] * (1 + 0.12 * gms)

    # A agenda da semana pesa: um FA com 4 jogos vale mais que outro com 2,
    # mesmo tendo projecao parecida.
    def week_games(c):
        return games_left(c["team"], wk_days, ctx)

    pre = sorted(cands, key=lambda c: -c["proj_fppg"])[:50]
    pre += [c for c in sorted(cands, key=lambda c: -rank_pre(c))[:30] if c not in pre]
    results = []
    for c in pre:
        best = None
        for d in droppable:
            if "C" in c["elig"] and "C" not in d["elig"] and n_centers >= MAX_CENTERS:
                continue
            ids = [i for i in base_ids if i != d["pid"]] + [c["pid"]]
            wg = week_value(ids, pool, days) - base_week
            d_eff = d["proj_fppg"] * (0.5 if d["status"] == "OUT" else 1.0)
            pg = c["proj_fppg"] * avail_prob(c["status"], 1) - d_eff
            score = wg + HORIZON_EXTRA_GAMES * pg
            if best is None or score > best["score"]:
                best = dict(add=c, drop=d, wg=wg, pg=pg, score=score)
        if best:
            results.append(best)
    results.sort(key=lambda r: -r["score"])
    picks, used_add, used_drop = [], set(), set()
    for r in results:
        if r["score"] < 8 or r["add"]["pid"] in used_add or r["drop"]["pid"] in used_drop:
            continue
        picks.append(r)
        used_add.add(r["add"]["pid"])
        used_drop.add(r["drop"]["pid"])
        if len(picks) >= 6:
            break
    if not picks:
        L.append("Nenhum add/drop passa do corte de ganho (score >= 8). Seu elenco esta competitivo frente aos candidatos analisados.")
    else:
        L.append(f"Ordenado por ganho = FP extra no lineup ate {horizon_end.strftime('%d/%m')} + {HORIZON_EXTRA_GAMES} x ganho por jogo "
                 f"(valor de medio prazo).")
        L.append("")
        rows = []
        for r in picks:
            a, d = r["add"], r["drop"]
            gj = sum(1 for x in days if ctx.game(a["team"], x))
            wgames = week_games(a)
            _, a_up, a_down = scenario_points(a, wk_days, "real")
            a_real = week_points(a, wk_days)
            conf = "" if a["conf"] >= 0.5 else " *amostra pequena*"
            digest["adds"].append(dict(add=a["name"], add_team=a["team"], drop=d["name"],
                                       drop_team=d["team"], add_fp=a["proj_fppg"],
                                       drop_fp=d["proj_fppg"], games=gj, wg=r["wg"],
                                       pg=r["pg"], owned=a.get("owned"), tag=a["tag"],
                                       week_games=wgames, week_points=a_real,
                                       week_up=a_up, week_down=a_down))
            rows.append((f"**{a['name']}** ({a['team']}, {'/'.join(sorted(a['elig'])) or '?'})", d["name"],
                         f"{a['proj_fppg']:.1f} vs {d['proj_fppg']:.1f}", wgames,
                         f"{a_real:.0f} (+{a_up:.0f} / {a_down:+.0f})",
                         sgn(r["wg"], 0), sgn(r["pg"]),
                         (f"{a['owned']:.0f}%" if a.get("owned") else "-"),
                         (a["tag"].replace("_", " ").title() if a["tag"] != "NEUTRO" else "-"), conf.strip() or "-"))
        L += md_table(["Contratar", "Dropar", "Proj/jogo (add vs drop)", "Jogos sem.",
                       "FP na semana (otim/pess)", "Ganho semana (FP)",
                       "Ganho/jogo", "% dono ESPN", "Fase", "Obs"], rows)
        for r in picks:
            summary.append(f"ADD {r['add']['name']} / DROP {r['drop']['name']} (ganho semana {r['wg']:+.0f} FP, {r['pg']:+.1f}/jogo)")
        L += ["", "Waivers duram 1 dia: o claim processa no dia seguinte. Se o candidato for DTD, confira o status antes."]
    L.append("")

    # ========== 4) TROCAS ==============================================
    L += ["## 5. Trocas", ""]
    sells = sorted((e for e in roster if e["tag"] == "VENDER_NA_ALTA"), key=lambda e: -e["delta_form"])
    if other_teams:
        MAX_ROSTER = max([len(roster)] + [len(e[1]) for e in other_teams])
        my_give_pids = {e["pid"] for e in droppable}

        teams = []
        for entry in other_teams:
            tname, plist = entry[0], entry[1]
            their_record = entry[2] if len(entry) > 2 else (0, 0)
            tpool = []
            for x in plist:
                pid, _ = idx.find(x["name"])
                if pid is None or pid in my_give_pids:
                    continue
                if pid not in pool:
                    pool[pid] = mk(profiles[pid], x.get("elig") or pos_of(pid),
                                   x.get("status", ""))
                tpool.append(pool[pid])
            if tpool:
                teams.append((tname, tpool, their_record))

        out, names_of = find_trades(roster, droppable, teams, pool, base_ids,
                                    base_week, days, wk_days, ctx, today, repl,
                                    MAX_ROSTER)

        if not out:
            L.append("Nenhuma troca passou nos filtros: precisa ganhar pontos no lineup "
                     "titular, nao custar ao outro dono mais da metade do valor que ele recebe "
                     "na semana e ter recusa estimada <= 70%.")
        else:
            L.append("Trocas avaliadas **dos dois lados**, em pacotes de **1x1, 2x1, 2x2, "
                     "1x2, 3x1 e 1x3**. Entram apenas as que melhoram o seu lineup titular e "
                     "que o outro dono tem chance real de aceitar.")
            L.append("")
            rows = []
            for r in out:
                ev, risk = r["ev"], r["ev"]["risk"]
                mine, theirs = ev["mine"], ev["theirs"]
                risco_lesao = {"baixo": "-", "medio": "medio", "alto": "ALTO"}[mine["injury"]]
                vagas = (f"+{mine['open_slots']}" if mine["open_slots"] > 0
                         else (str(mine["open_slots"]) if mine["open_slots"] < 0 else "0"))
                rows.append((
                    r["team"], r["shape"], names_of(r["give"]), names_of(r["get"]),
                    f"{_proj_sum(r['give']):.1f} -> {_proj_sum(r['get']):.1f}",
                    f"{mine['games_out']} -> {mine['games_in']}",
                    sgn(mine["lineup"], 0),
                    sgn(theirs["lineup"], 0),
                    f"{mine['avg_delta']:+.1f} / {theirs['avg_delta']:+.1f}",
                    f"+{mine['up']:.0f} / {mine['down']:+.0f}",
                    vagas,
                    risco_lesao,
                    f"{risk['label']} ({risk['reject']}%)"))
                digest["trades"].append(dict(
                    team=r["team"], shape=r["shape"],
                    give=names_of(r["give"]), get=names_of(r["get"]),
                    give_list=[dict(name=p["name"], team=p["team"],
                                    fp=_num(p["proj_fppg"])) for p in r["give"]],
                    get_list=[dict(name=q["name"], team=q["team"],
                                   fp=_num(q["proj_fppg"])) for q in r["get"]],
                    give_fp=_proj_sum(r["give"]), get_fp=_proj_sum(r["get"]),
                    gain=ev["proj_gain"], wg=mine["lineup"],
                    open_slots=mine["open_slots"],
                    give_games=mine["games_out"], get_games=mine["games_in"],
                    mine_real=mine["lineup"], theirs_real=theirs["lineup"],
                    mine_raw=mine["real"], theirs_raw=theirs["real"],
                    mine_up=mine["up"], mine_down=mine["down"],
                    mine_avg=mine["avg_delta"], theirs_avg=theirs["avg_delta"],
                    mine_injury=mine["injury"], mine_injury_txt=mine["injury_txt"],
                    theirs_injury=theirs["injury"],
                    theirs_injury_txt=theirs["injury_txt"],
                    accept=risk["accept"], reject=risk["reject"],
                    reject_label=risk["label"],
                    reject_bg=risk["color"][0], reject_fg=risk["color"][1],
                    veto=risk["veto"], factors=risk["factors"]))
            L += md_table(
                ["Time", "Formato", "Voce da", "Voce recebe", "Proj/jogo", "Jogos sem.",
                 "Lineup (voce)", "Lineup (ele)", "Media elenco (voce/ele)",
                 "Otimista/Pessimista", "Vagas", "Lesao", "Recusa"], rows)
            L.append("")
            L.append("**Leitura de cada coluna:**")
            L.append("- **Formato**: quantos voce entrega x quantos recebe. `2x1` consolida "
                     "dois medios em um bom; `1x2` abre o elenco para dois jogadores.")
            L.append("- **Lineup (voce/ele)**: pontos ganhos na **escalacao titular** de cada "
                     "lado na semana — e o numero que decide, porque banco nao pontua.")
            L.append("- **Vagas**: quanto o seu elenco cresce (+) ou encolhe (-). `+1` significa "
                     "uma vaga livre para streamer, mas voce gasta um add para preencher.")
            L.append("- **Media elenco**: quanto a media de projecao do elenco varia para cada lado.")
            L.append("- **Otimista/Pessimista**: e se a sorte de arremesso voltar ao normal.")
            L.append("")
            L.append("**Por que cada recusa:**")
            for r in out[:5]:
                L.append(f"- **{names_of(r['get'])}** de {r['team']} "
                         f"(recusa {r['ev']['risk']['reject']}%): "
                         + "; ".join(r["ev"]["risk"]["factors"]) + ".")
            for r in out[:3]:
                ev = r["ev"]
                summary.append(
                    f"TROCA {r['shape']} ({r['team']}): dar {names_of(r['give'])} "
                    f"por {names_of(r['get'])} "
                    f"({ev['mine']['lineup']:+.0f} FP no lineup, "
                    f"recusa {ev['risk']['label'].lower()})")

        L += ["", "A aceitacao depende do outro dono; a liga tem 2 dias de revisao e 4 votos "
              "vetam. Priorize as trocas com recusa baixa e lesao baixa: sao as que "
              "realmente acontecem."]
    else:
        L.append("Sem acesso aos elencos dos outros times (confira as credenciais do ESPN). Mesmo assim:")
        L.append("")
        L.append("**Seus candidatos a venda (alta insustentavel):** " +
                 (", ".join(f"{e['name']} ({'; '.join(e['why'])})" for e in sells) if sells else "nenhum no momento."))
        buys = sorted((p for p in profiles.values() if p["pid"] not in {e['pid'] for e in roster} and not p["stale"]),
                      key=lambda p: -p["proj_fppg"])
        targets = []
        for p in buys:
            t, why = form_tag(p, opts.recent)
            if t == "COMPRAR_NA_BAIXA" and p["proj_fppg"] >= repl + 6:
                targets.append((p, why))
            if len(targets) >= 8:
                break
        L.append("")
        L.append("**Alvos 'comprar na baixa' na NBA inteira** (procure o dono na sua liga):")
        for p, why in targets:
            L.append(f"- {p['name']} ({p['team']}): proj {p['proj_fppg']:.1f}/jogo, FP ult.{opts.recent} {p['fp5']:.1f} - " + "; ".join(why))
        if not targets:
            L.append("- Nenhum alvo claro agora (amostra pequena no inicio da temporada).")
    L.append("")

    # ========== NOTAS ===================================================
    return finalize()


# --------------------------------------------------------------------------
# Notificacoes (Telegram / email)
# --------------------------------------------------------------------------
def send_telegram(text):
    miss = missing_env("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID")
    if not miss and not _chat_id_set():
        miss.append("TELEGRAM_CHAT_ID (nao numerico)")
    if miss:
        print(f"Telegram: ignorado (faltando no .env: {', '.join(miss)})", file=sys.stderr)
        return None
    try:
        r = requests.post(f"https://api.telegram.org/bot{_env('TELEGRAM_TOKEN')}/sendMessage",
                          data={"chat_id": _env("TELEGRAM_CHAT_ID"), "text": text[:3900]}, timeout=20)
    except Exception as ex:
        print(f"Telegram: erro ({ex})", file=sys.stderr)
        return False
    print("Telegram:", "enviado" if r.ok else f"erro {r.status_code}")
    return r.ok


def normalize_password(pwd):
    """O Google mostra a app password em 4 blocos ('abcd efgh ijkl mnop').

    A senha real tem 16 caracteres, sem espacos. Alguns clientes SMTP aceitam os
    dois formatos, outros recusam - por isso normalizamos quando o formato e
    claramente o de app password.
    """
    if not pwd:
        return pwd
    parts = pwd.split()
    if len(parts) == 4 and all(len(p) == 4 for p in parts) and "".join(parts).isalpha():
        return "".join(parts)
    return pwd


def sender_header(user, to):
    """Monta o cabecalho From respeitando o que o servidor SMTP aceita.

    O Gmail (e a maioria dos provedores) so aceita um From igual ao usuario
    autenticado ou a um alias verificado na conta. Se o dominio de EMAIL_FROM
    nao for o mesmo do SMTP_USER, o servidor **reescreve** o cabecalho - por isso
    o destinatario ve o endereco da conta. Nesse caso avisamos e usamos o
    endereco real, com um nome amigavel para nao perder a identidade visual.
    """
    frm = _env("EMAIL_FROM")
    name = _env("EMAIL_FROM_NAME")
    base = frm or user or to[0]

    def domain(addr):
        return addr.rsplit("@", 1)[-1].lower() if "@" in addr else ""

    mismatch = bool(frm and user and domain(frm) != domain(user))
    if mismatch:
        print(f"Email: EMAIL_FROM ({frm}) usa dominio diferente do SMTP_USER "
              f"({user}). O provedor vai reescrever o remetente - usando "
              f"'{name or 'NBA Fantasy'} <{user}>'. Para manter {frm}, verifique o "
              "dominio no provedor (SPF/DKIM) ou use um servico transacional.",
              file=sys.stderr)
        base = user
        name = name or "NBA Fantasy"

    if name:
        return f'"{name}" <{base}>'
    return base


def send_email(subject, text, html=None):
    host = _env("SMTP_HOST")
    user, pwd = _env("SMTP_USER"), normalize_password(_env("SMTP_PASS"))
    # Aceita varios destinatarios. Na nuvem o workflow ja recortou a posicao deste
    # time, entao aqui sobra so quem deve receber este relatorio. O ';' permite
    # mais de um destinatario dentro do mesmo time.
    to = [a.strip() for a in re.split(r"[,;]", _env("EMAIL_TO")) if a.strip()]
    miss = []
    if not host:
        miss.append("SMTP_HOST")
    if not to:
        miss.append("EMAIL_TO")
    if bool(user) != bool(pwd):
        miss.append("SMTP_USER/SMTP_PASS (preencha os dois ou nenhum)")
    if miss:
        print(f"Email: ignorado (faltando no .env: {', '.join(miss)})", file=sys.stderr)
        return None
    try:
        port = int(_env("SMTP_PORT") or 587)
    except ValueError:
        print(f"Email: SMTP_PORT invalido ({_env('SMTP_PORT')}); usando 587.", file=sys.stderr)
        port = 587
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = sender_header(user, to)
        msg["To"] = ", ".join(to)
        msg.set_content(text)
        if html:
            msg.add_alternative(html, subtype="html")
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=30) as s:
                if user:
                    s.login(user, pwd)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=30) as s:
                s.starttls()
                if user:
                    s.login(user, pwd)
                s.send_message(msg)
        print("Email:", f"enviado para {', '.join(to)}")
        return True
    except Exception as ex:
        print(f"Email: erro ({ex})", file=sys.stderr)
        return False


# --------------------------------------------------------------------------
# Email em HTML (tema escuro)
# --------------------------------------------------------------------------
BG = "#0a0d13"        # fundo da pagina
CARD = "#141a24"      # cartao principal
SOFT = "#1b2230"      # blocos internos
LINE = "#242e3d"      # bordas
INK = "#eef2f8"       # texto principal
MUTED = "#8b96a8"     # texto secundario
ORANGE = "#f97316"    # destaque
GREEN_BG, GREEN_FG = "#10281c", "#4ade80"
RED_BG, RED_FG = "#2a1417", "#f87171"
AMBER_BG, AMBER_FG = "#2b2210", "#fbbf24"
BLUE_BG, BLUE_FG = "#12243a", "#60a5fa"
GREY_BG, GREY_FG = "#232d3c", "#9aa5b6"

TAG_STYLE = {
    "VENDER_NA_ALTA": ("Vender na alta", RED_BG, RED_FG),
    "COMPRAR_NA_BAIXA": ("Comprar na baixa", GREEN_BG, GREEN_FG),
    "PAPEL_ENCOLHENDO": ("Papel encolhendo", AMBER_BG, AMBER_FG),
    "ALTA_SUSTENTAVEL": ("Alta sustentável", BLUE_BG, BLUE_FG),
}


def _pill(tag):
    if tag not in TAG_STYLE:
        return ""
    label, bg, fg = TAG_STYLE[tag]
    return (f'<span style="display:inline-block;padding:3px 9px;border-radius:999px;'
            f'background:{bg};color:{fg};font-size:11px;font-weight:600;'
            f'white-space:nowrap">{label}</span>')


def _section(title, subtitle=""):
    sub = (f'<div style="color:{MUTED};font-size:12px;margin-top:2px">{subtitle}</div>'
           if subtitle else "")
    return (f'<tr><td style="padding:26px 28px 10px 28px">'
            f'<div style="color:{INK};font-size:15px;font-weight:700;letter-spacing:.2px">{title}</div>'
            f'{sub}</td></tr>')


def _stat(label, value, color=INK):
    return (f'<td align="center" style="padding:14px 8px">'
            f'<div style="color:{color};font-size:22px;font-weight:700;line-height:1.1">{value}</div>'
            f'<div style="color:{MUTED};font-size:11px;text-transform:uppercase;'
            f'letter-spacing:.6px;margin-top:4px">{label}</div></td>')


def render_email_html(d):
    """Monta o corpo HTML do email a partir do digest da analise."""

    def esc(value):
        return escape("" if value is None else str(value))

    day = d.get("date") or datetime.now(ET).date()
    team = esc(d.get("team_name") or "Meu time")
    owner = esc(d.get("owner") or "")
    logo = d.get("team_logo") or ""
    photos = bool(d.get("photos"))

    logo_html = ""
    if logo:
        logo_html = (f'<img src="{esc(logo)}" width="54" height="54" alt="" '
                     f'style="display:block;border-radius:12px;border:0;background:{SOFT}">')

    meta = f"{day:%d/%m/%Y} · Semana {esc(d.get('week',''))}"
    if owner:
        meta += f" · {owner}"

    rows = []

    lineup_only = bool(d.get("lineup_only"))

    # ---------- cabecalho ----------
    kicker = "NBA Fantasy · Pre-jogo" if lineup_only else "NBA Fantasy"
    rows.append(
        f'<tr><td style="background:{SOFT};padding:26px 28px;'
        f'border-bottom:1px solid {LINE}">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
        f'<td width="54" valign="middle">{logo_html}</td>'
        f'<td valign="middle" style="padding-left:14px">'
        f'<div style="color:{ORANGE};font-size:10px;letter-spacing:2.4px;'
        f'text-transform:uppercase;font-weight:700">{esc(kicker)}</div>'
        f'<div style="color:#ffffff;font-size:21px;font-weight:700;margin-top:4px">{team}</div>'
        f'<div style="color:{MUTED};font-size:12px;margin-top:4px">{esc(meta)}</div>'
        f'</td></tr></table></td></tr>')

    # ---------- faixa de numeros ----------
    if lineup_only:
        ir_used = d.get("ir_used", 0)
        stats = (_stat("FP projetado hoje", f"{d.get('total', 0):.0f}", ORANGE)
                 + _stat("Titulares", f"{len(d.get('lineup') or [])}")
                 + _stat("IR em uso", f"{ir_used}/{IR_SLOTS}"))
    else:
        stats = (_stat("FP projetado hoje", f"{d.get('total', 0):.0f}", ORANGE)
                 + _stat("Titulares", f"{len(d.get('lineup') or [])}")
                 + _stat("Nivel de reposicao", f"{d.get('repl', 0):.0f}"))
    rows.append(
        f'<tr><td style="padding:6px 20px 0 20px"><table role="presentation" width="100%" '
        f'cellpadding="0" cellspacing="0" border="0" style="border-bottom:1px solid {LINE}"><tr>'
        + stats + '</tr></table></td></tr>')

    # ---------- lineup ----------
    lineup = d.get("lineup") or []
    if lineup:
        rows.append(_section("Escalacao de hoje", "Slots na ordem da liga · FP esperado por jogo"))
        body = []
        for p in lineup:
            photo = ""
            if photos and p.get("pid"):
                photo = (f'<img src="https://a.espncdn.com/i/headshots/nba/players/full/'
                         f'{int(p["pid"])}.png" width="40" height="40" alt="" '
                         f'style="display:block;border-radius:50%;border:2px solid #eef0f3">')
            dtd = (f'<span style="color:{AMBER_FG};font-size:11px;font-weight:600"> · DTD</span>'
                   if p.get("status") == "DTD" else "")
            body.append(
                f'<tr>'
                f'<td width="52" style="padding:9px 0"><div style="background:{SOFT};color:{ORANGE};'
                f'border-radius:8px;padding:5px 0;text-align:center;font-size:11px;font-weight:700;'
                f'letter-spacing:.5px">{esc(p["slot"])}</div></td>'
                f'<td width="46" style="padding:9px 0 9px 8px">{photo}</td>'
                f'<td style="padding:9px 0 9px 10px">'
                f'<div style="color:{INK};font-size:14px;font-weight:600">{esc(p["name"])}{dtd}</div>'
                f'<div style="color:{MUTED};font-size:11px;margin-top:2px">{esc(p["team"])} · '
                f'{esc(p["opp"])} · {p["minutes"]:.0f} min</div></td>'
                f'<td align="right" style="padding:9px 0;white-space:nowrap">'
                f'<div style="color:{ORANGE};font-size:17px;font-weight:700">{p["fp"]:.1f}</div>'
                f'<div style="margin-top:3px">{_pill(p.get("tag"))}</div></td>'
                f'</tr>'
                f'<tr><td colspan="4" style="border-bottom:1px solid {LINE};font-size:0;line-height:0">&nbsp;</td></tr>')
        rows.append(f'<tr><td style="padding:0 28px"><table role="presentation" width="100%" '
                    f'cellpadding="0" cellspacing="0" border="0">{"".join(body)}</table></td></tr>')
        if d.get("empty_slots"):
            rows.append(
                f'<tr><td style="padding:10px 28px 0 28px"><div style="background:{AMBER_BG};'
                f'border-left:3px solid {AMBER_FG};padding:9px 12px;border-radius:6px;color:{AMBER_FG};'
                f'font-size:12px">{d["empty_slots"]} slot(s) sem jogador elegivel com jogo hoje.</div>'
                f'</td></tr>')
    else:
        rows.append(_section("Escalacao de hoje"))
        rows.append(f'<tr><td style="padding:0 28px"><div style="color:{MUTED};font-size:13px">'
                    f'Nenhum jogador com jogo hoje.</div></td></tr>')

    # ---------- mudancas necessarias ----------
    changes = d.get("changes") or []
    if changes:
        kind_style = {
            "entrar": ("Adicionar", GREEN_BG, GREEN_FG),
            "sair": ("Remover", AMBER_BG, AMBER_FG),
            "lesionado": ("Lesionado escalado", RED_BG, RED_FG),
            "risco": ("Risco (DTD)", AMBER_BG, AMBER_FG),
        }
        cards = []
        for c in changes:
            label, bg, fg = kind_style.get(c["kind"], ("Alterar", GREY_BG, GREY_FG))
            sub = ""
            if c.get("sub"):
                sub = (f'<div style="color:{GREEN_FG};font-size:12px;margin-top:5px">'
                       f'→ entra <span style="font-weight:700">{esc(c["sub"])}</span> '
                       f'({c["sub_fp"]:.1f} FP)</div>')
            cards.append(
                f'<div style="border-left:3px solid {fg};background:{SOFT};padding:11px 14px;'
                f'border-radius:6px;margin-bottom:8px">'
                f'<div style="font-size:13px"><span style="font-weight:700;color:{INK}">'
                f'{esc(c["name"])}</span> <span style="color:{MUTED}">slot {esc(c["slot"])}</span>'
                f'<span style="display:inline-block;margin-left:6px;padding:2px 8px;border-radius:999px;'
                f'background:{bg};color:{fg};font-size:10px;font-weight:700;text-transform:uppercase;'
                f'letter-spacing:.4px">{esc(label)}</span></div>'
                f'<div style="color:{MUTED};font-size:12px;margin-top:4px">{esc(c["why"])} '
                f'({c["fp"]:.1f} FP hoje)</div>{sub}</div>')
        n_swap = sum(1 for c in changes if c.get("sub"))
        rows.append(_section("Mudancas necessarias na escalacao",
                             f"{n_swap} troca(s) para hoje" if n_swap
                             else f"{len(changes)} ajuste(s) para hoje"))
        rows.append(f'<tr><td style="padding:0 28px">{"".join(cards)}</td></tr>')
        if d.get("changes_leftover"):
            rows.append(
                f'<tr><td style="padding:2px 28px 0 28px"><div style="color:{MUTED};'
                f'font-size:12px;line-height:1.5">Os outros {d["changes_leftover"]} '
                f'titular(es) sem jogo hoje seguem escalados: nao ha mais ninguem no banco '
                f'para entrar, entao nao ha o que trocar.</div></td></tr>')
    elif d.get("changes_known"):
        rows.append(_section("Mudancas necessarias na escalacao"))
        rows.append(f'<tr><td style="padding:0 28px"><div style="color:{GREEN_FG};font-size:13px">'
                    f'Escalacao ja esta igual a recomendada.</div></td></tr>')

    # ---------- disputa por vaga ----------
    tight = d.get("tight") or []
    if d.get("lineup_choice") and tight:
        cards = []
        for t in tight[:3]:
            risky = t["margin"] <= 3
            bg, fg = (AMBER_BG, AMBER_FG) if risky else (BLUE_BG, BLUE_FG)
            head = (f'<span style="color:{RED_FG};font-weight:700">{esc(t["challenger"])}</span> '
                    f'<span style="color:{MUTED}">pode entrar no lugar de</span> '
                    f'<span style="color:{INK};font-weight:700">{esc(t["starter"])}</span>'
                    if risky else
                    f'<span style="color:{GREEN_FG};font-weight:700">{esc(t["starter"])}</span> '
                    f'<span style="color:{MUTED}">esta seguro no slot</span>')
            cards.append(
                f'<div style="border-left:3px solid {fg};background:{SOFT};padding:10px 14px;'
                f'border-radius:6px;margin-bottom:8px">'
                f'<div style="font-size:13px">{head} '
                f'<span style="display:inline-block;margin-left:6px;padding:2px 8px;border-radius:999px;'
                f'background:{bg};color:{fg};font-size:10px;font-weight:700;letter-spacing:.4px">'
                f'{esc(t["slot"])}</span></div>'
                f'<div style="color:{MUTED};font-size:11px;margin-top:4px">'
                f'{t["starter_fp"]:.1f} vs {t["challenger_fp"]:.1f} FP · '
                f'diferenca {t["margin"]:.1f}</div></div>')
        rows.append(_section("Quem entra e quem sai",
                             "Slots apertados: margem de 3 FP ou menos pede atencao"))
        rows.append(f'<tr><td style="padding:0 28px">{"".join(cards)}</td></tr>')

    # ---------- banco ----------
    bench = d.get("bench") or []
    if bench:
        items = "".join(
            f'<div style="padding:6px 0;font-size:13px;color:{MUTED}">'
            f'<span style="font-weight:600;color:{INK}">{esc(b["name"])}</span>'
            f' — {esc(b["why"])}</div>'
            for b in bench[:12])
        rows.append(_section("Fora do lineup"))
        rows.append(f'<tr><td style="padding:0 28px">{items}</td></tr>')

    # ---------- sinais: vender e comprar ----------
    alerts = d.get("alerts") or []
    sells = d.get("sells") or []
    buys = d.get("buys") or []
    if alerts or sells or buys:
        cards = []
        for a in alerts:
            _, bg, fg = TAG_STYLE.get(a["tag"], ("", GREY_BG, GREY_FG))
            why = " · ".join(esc(w) for w in a["why"][:3])
            cards.append(
                f'<div style="border-left:3px solid {fg};background:{SOFT};padding:11px 14px;'
                f'border-radius:6px;margin-bottom:8px">'
                f'<div style="font-size:13px"><span style="font-weight:700;color:{INK}">'
                f'{esc(a["name"])}</span> <span style="color:{MUTED}">{esc(a["team"])}</span> '
                f'<span style="display:inline-block;padding:2px 8px;border-radius:999px;'
                f'background:{bg};color:{fg};font-size:10px;font-weight:700;text-transform:uppercase;'
                f'letter-spacing:.4px">{esc(a["label"])}</span></div>'
                f'<div style="color:{MUTED};font-size:12px;margin-top:4px">{why}</div></div>')
        for b in buys:
            cards.append(
                f'<div style="border-left:3px solid {GREEN_FG};background:{SOFT};padding:11px 14px;'
                f'border-radius:6px;margin-bottom:8px">'
                f'<div style="font-size:13px"><span style="font-weight:700;color:{INK}">'
                f'{esc(b["name"])}</span> <span style="color:{MUTED}">{esc(b["team"])}</span> '
                f'<span style="display:inline-block;padding:2px 8px;border-radius:999px;'
                f'background:{GREEN_BG};color:{GREEN_FG};font-size:10px;font-weight:700;'
                f'text-transform:uppercase;letter-spacing:.4px">Alvo de compra</span></div>'
                f'<div style="color:{MUTED};font-size:12px;margin-top:4px">'
                f'dono: {esc(b["owner"])} · proj {b["proj"]:.1f}/jogo · '
                f'FP ult. {b["recent"]:.1f}</div></div>')
        rows.append(_section("Sinais da semana", "Quem vender na sua equipe e quem tentar comprar"))
        rows.append(f'<tr><td style="padding:0 28px">{"".join(cards)}</td></tr>')

    # ---------- IR e status ----------
    ir_rows = d.get("ir") or []
    actions = [r for r in ir_rows if r.get("advice") not in (None, "OK", "MANTER_NO_IR", "MANTER")]
    if actions:
        sub = (f"IR {d.get('ir_used', 0)}/{IR_SLOTS} ocupado(s) · "
               f"{d.get('ir_free', 0)} vaga(s) livre(s)")
        cards = []
        for r in actions:
            label, bg, fg = IR_ADVICE.get(r["advice"], (r["advice"], GREY_BG, GREY_FG))
            why = " · ".join(esc(w) for w in r["why"][:3])
            ret = (f'<span style="color:{MUTED}"> · retorno {r["expected_return"]:%d/%m}</span>'
                   if r.get("expected_return") else "")
            opts = r.get("options") or []
            opts_html = ""
            if opts:
                lis = "".join(
                    f'<div style="color:{MUTED};font-size:11px;margin-top:3px">'
                    f'<span style="color:{RED_FG};font-weight:700">{i}.</span> dropar '
                    f'<span style="color:{INK};font-weight:600">{esc(o["name"])}</span> '
                    f'({esc(o["team"])}, {o["proj"]:.0f}/j)</div>'
                    for i, o in enumerate(opts, start=1))
                opts_html = (f'<div style="color:{MUTED};font-size:10px;text-transform:uppercase;'
                             f'letter-spacing:.5px;margin-top:7px">Opcoes para abrir vaga</div>'
                             f'{lis}')
            cards.append(
                f'<div style="border-left:3px solid {fg};background:{SOFT};padding:11px 14px;'
                f'border-radius:6px;margin-bottom:8px">'
                f'<div style="font-size:13px"><span style="font-weight:700;color:{INK}">'
                f'{esc(r["name"])}</span> <span style="color:{MUTED}">{esc(r["team"])} · '
                f'{esc(r["status"])}</span>'
                f'<span style="display:inline-block;margin-left:6px;padding:2px 8px;border-radius:999px;'
                f'background:{bg};color:{fg};font-size:10px;font-weight:700;text-transform:uppercase;'
                f'letter-spacing:.4px">{esc(label)}</span>{ret}</div>'
                f'<div style="color:{MUTED};font-size:12px;margin-top:4px">{why}</div>'
                f'{opts_html}</div>')
        rows.append(_section("IR e status", sub))
        rows.append(f'<tr><td style="padding:0 28px">{"".join(cards)}</td></tr>')

    # ---------- add/drop ----------
    adds = d.get("adds") or []
    if adds:
        body = []
        for a in adds:
            owned = f'{a["owned"]:.0f}% no ESPN' if a.get("owned") else ""
            wg_txt = f'{a["week_games"]} jogo(s) esta semana' if a.get("week_games") is not None else ""
            scen = (f' · <span style="color:{MUTED}">{a["week_up"]:.0f} no otimista / '
                    f'{a["week_down"]:.0f} no pessimista</span>'
                    if a.get("week_up") is not None else "")
            body.append(
                f'<tr><td style="padding:10px 0;border-bottom:1px solid {LINE}">'
                f'<div style="font-size:13px;color:{INK}">'
                f'<span style="color:{GREEN_FG};font-weight:700">+ {esc(a["add"])}</span>'
                f'<span style="color:{MUTED}"> ({esc(a["add_team"])}, {a["add_fp"]:.1f}/j)</span>'
                f'</div>'
                f'<div style="font-size:13px;color:{INK};margin-top:3px">'
                f'<span style="color:{RED_FG};font-weight:700">− {esc(a["drop"])}</span>'
                f'<span style="color:{MUTED}"> ({esc(a["drop_team"])}, {a["drop_fp"]:.1f}/j)</span>'
                f'</div>'
                f'<div style="color:{MUTED};font-size:11px;margin-top:4px">'
                f'{wg_txt} · {a["games"]} ate o fim · ganho na semana '
                f'<span style="color:{GREEN_FG};font-weight:700">{a["wg"]:+.0f} FP</span> '
                f'({a["pg"]:+.1f}/jogo){scen}{(" · " + owned) if owned else ""}</div>'
                f'</td></tr>')
        rows.append(_section("Contratar / dropar",
                             "Sugestoes de manutencao do elenco — confira seu limite de adds no ESPN"))
        rows.append(f'<tr><td style="padding:0 28px 8px 28px"><table role="presentation" width="100%" '
                    f'cellpadding="0" cellspacing="0" border="0">{"".join(body)}</table></td></tr>')

    # ---------- trocas ----------
    trades = d.get("trades") or []
    if trades:
        def chip(label, bg, fg, size=10):
            return (f'<span style="display:inline-block;padding:2px 8px;border-radius:999px;'
                    f'background:{bg};color:{fg};font-size:{size}px;font-weight:700;'
                    f'white-space:nowrap">{label}</span>')

        def side_panel(title, items, bg, fg, fallback):
            """Painel de um lado da troca: o pacote que sai ou o que chega."""
            if items:
                parts = []
                for p in items:
                    fp = p.get("fp")
                    tail = f" · {fp:.1f}/j" if isinstance(fp, (int, float)) else ""
                    parts.append(
                        f'<div style="margin-top:5px">'
                        f'<div style="font-size:12px;color:{INK};line-height:1.3">'
                        f'{esc(p.get("name"))}</div>'
                        f'<div style="color:{MUTED};font-size:10px;margin-top:1px">'
                        f'{esc(p.get("team"))}{tail}</div></div>')
                inner = "".join(parts)
            else:
                inner = (f'<div style="font-size:12px;color:{INK};margin-top:5px;'
                         f'line-height:1.3">{esc(fallback)}</div>')
            return (f'<td width="45%" valign="top" style="background:{bg};border-radius:8px;'
                    f'padding:9px 11px">'
                    f'<div style="color:{fg};font-size:9px;font-weight:700;'
                    f'letter-spacing:.8px;text-transform:uppercase">{title}</div>'
                    f'{inner}</td>')

        cards = []
        for idx, t in enumerate(trades, 1):
            reject = t.get("reject", 50)
            bg = t.get("reject_bg") or GREY_BG
            fg = t.get("reject_fg") or GREY_FG
            mine_gain = t.get("mine_real") or 0
            their_gain = t.get("theirs_real") or 0
            shape = t.get("shape") or ""

            # ---- cabecalho: posicao, time, formato e chance de aceite
            shape_html = (f'<span style="display:inline-block;margin-left:7px;padding:1px 7px;'
                          f'border-radius:5px;background:{SOFT};color:{BLUE_FG};font-size:10px;'
                          f'font-weight:700;letter-spacing:.3px">{esc(shape)}</span>'
                          if shape else "")
            veto_chip = (f'<span style="display:inline-block;margin-left:5px;'
                         f'color:{AMBER_FG};font-size:10px;font-weight:600">veto {esc(t.get("veto"))}</span>'
                         if t.get("veto") and t["veto"] != "baixo" else "")
            best_chip = (f'<span style="display:inline-block;margin-left:7px;padding:1px 7px;'
                         f'border-radius:5px;background:{GREEN_BG};color:{GREEN_FG};font-size:9px;'
                         f'font-weight:700;letter-spacing:.4px">MELHOR</span>'
                         if idx == 1 and len(trades) > 1 else "")
            head = (
                f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                f'border="0"><tr>'
                f'<td valign="middle">'
                f'<span style="display:inline-block;min-width:19px;padding:2px 0;'
                f'border-radius:5px;background:{SOFT};color:{MUTED};font-size:10px;'
                f'font-weight:700;text-align:center">{idx}</span>'
                f'<span style="color:{INK};font-size:13px;font-weight:700;margin-left:8px">'
                f'{esc(t["team"])}</span>{shape_html}{best_chip}</td>'
                f'<td align="right" valign="middle" style="white-space:nowrap">'
                f'{chip(f"recusa {t.get('reject_label', '-')} ({reject}%)", bg, fg)}'
                f'{veto_chip}</td></tr></table>')

            # ---- os dois lados, lado a lado
            swap = (
                f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                f'border="0" style="margin-top:9px"><tr>'
                f'{side_panel("Voce entrega", t.get("give_list"), RED_BG, RED_FG, t.get("give", ""))}'
                f'<td width="10%" align="center" valign="middle" '
                f'style="color:{MUTED};font-size:15px">&#8594;</td>'
                f'{side_panel("Voce recebe", t.get("get_list"), GREEN_BG, GREEN_FG, t.get("get", ""))}'
                f'</tr></table>')

            # ---- metrica principal: ganho no lineup titular
            cenarios = ""
            if isinstance(t.get("mine_up"), (int, float)):
                cenarios = (f'<span style="color:{MUTED};font-size:10px"> · cenarios '
                            f'<span style="color:{GREEN_FG}">{t["mine_up"]:+.0f}</span>'
                            f'<span style="color:{MUTED}">/</span>'
                            f'<span style="color:{AMBER_FG}">{t.get("mine_down", 0):+.0f}</span>'
                            f'</span>')
            hero = (
                f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                f'border="0" style="margin-top:10px"><tr>'
                f'<td valign="bottom" style="line-height:1.2">'
                f'<span style="color:{GREEN_FG if mine_gain >= 0 else RED_FG};font-size:20px;'
                f'font-weight:700">{mine_gain:+.0f}</span>'
                f'<span style="color:{MUTED};font-size:11px"> FP no seu lineup</span>'
                f'{cenarios}</td>'
                f'<td align="right" valign="bottom" style="white-space:nowrap">'
                f'<span style="color:{MUTED};font-size:10px">no dele </span>'
                f'<span style="color:{GREEN_FG if their_gain >= 0 else RED_FG};font-size:13px;'
                f'font-weight:700">{their_gain:+.0f}</span></td></tr></table>')

            # ---- faixa de contexto, tudo numa linha so
            meta = []
            if t.get("give_games") is not None and t.get("get_games") is not None:
                meta.append(f'agenda <span style="color:{INK}">{t["give_games"]}'
                            f'&#8594;{t["get_games"]}</span>')
            slots = t.get("open_slots")
            if slots:
                meta.append(f'<span style="color:{GREEN_FG if slots > 0 else AMBER_FG}">'
                            f'{slots:+d} vaga(s)</span>')
            if isinstance(t.get("mine_avg"), (int, float)):
                am = t["mine_avg"]
                at = t.get("theirs_avg") or 0
                meta.append(f'media <span style="color:{GREEN_FG if am >= 0 else RED_FG}">'
                            f'{am:+.1f}</span><span style="color:{MUTED}">/</span>'
                            f'<span style="color:{GREEN_FG if at >= 0 else RED_FG}">'
                            f'{at:+.1f}</span>')
            my_inj = t.get("mine_injury")
            if my_inj and my_inj != "baixo":
                meta.append(f'<span style="color:{AMBER_FG}">lesao no que voce recebe '
                            f'({"medio" if my_inj == "medio" else "alto"})</span>')
            their_inj = t.get("theirs_injury")
            if their_inj and their_inj != "baixo":
                meta.append(f'<span style="color:{BLUE_FG}">lesao no que ele recebe '
                            f'({"medio" if their_inj == "medio" else "alto"})</span>')
            meta_html = ""
            if meta:
                meta_html = (f'<div style="color:{MUTED};font-size:10px;margin-top:7px;'
                             f'line-height:1.5">'
                             + f' <span style="color:{MUTED}">·</span> '.join(meta)
                             + '</div>')

            # ---- uma linha de justificativa (o fator mais decisivo primeiro)
            top = (t.get("factors") or [])[:2]
            fac = ""
            if top:
                fac = (f'<div style="color:{MUTED};font-size:10px;margin-top:5px;'
                       f'line-height:1.45">&#8250; {esc(" · ".join(top))}</div>')

            cards.append(
                f'<div style="border-left:{4 if idx == 1 and len(trades) > 1 else 3}px solid {fg};'
                f'background:{SOFT};border-radius:8px;padding:12px 14px;margin-bottom:10px">'
                f'{head}{swap}{hero}{meta_html}{fac}</div>')

        best = max((t.get("mine_real") or 0) for t in trades)
        rows.append(_section("Trocas possiveis",
                             f"{len(trades)} opcao(oes) · melhor ganho +{best:.0f} FP no "
                             f"lineup titular da semana"))
        rows.append(f'<tr><td style="padding:0 28px">{"".join(cards)}'
                    f'<div style="color:{MUTED};font-size:10px;line-height:1.5;'
                    f'padding:2px 0 4px 0">'
                    f'Ganho = diferenca na <b>escalacao titular</b> da semana (banco nao '
                    f'pontua). Vaga + = espaco livre no elenco depois da troca.</div>'
                    f'</td></tr>')

    # ---------- rodape ----------
    footer = ("Projecao estatistica, nao garantia. Confirme o status dos jogadores no app "
              "antes do primeiro jogo.")
    if not lineup_only:
        footer += f"<br>Prazo de trocas em {d.get('trade_days', 0)} dias."
    rows.append(
        f'<tr><td style="padding:22px 28px 26px 28px;border-top:1px solid {LINE}">'
        f'<div style="color:{GREY_FG};font-size:11px;line-height:1.6">'
        f'{footer}<br>gerado pelo NBA Fantasy Agent'
        f'</div></td></tr>')

    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="dark only">'
        '<meta name="supported-color-schemes" content="dark"></head>'
        f'<body style="margin:0;padding:24px 12px;background:{BG};'
        f'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,Helvetica,Arial,sans-serif">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">'
        f'<tr><td align="center">'
        f'<table role="presentation" width="640" cellpadding="0" cellspacing="0" border="0" '
        f'style="width:640px;max-width:100%;background:{CARD};border-radius:14px;'
        f'border:1px solid {LINE};overflow:hidden">'
        + "".join(rows) +
        '</table></td></tr></table></body></html>'
    )


def notify(summary, today, telegram=False, email=False, digest=None):
    team = (digest or {}).get("team_name") or ""
    subject = f"NBA Fantasy {today.strftime('%d/%m')}" + (f" · {team}" if team else "")
    text = subject + "\n\n" + "\n".join(summary)
    results = {}
    if telegram:
        results["telegram"] = send_telegram(text)
    if email:
        html = render_email_html(digest) if digest else None
        results["email"] = send_email(subject, text, html=html)
    # None = canal nao configurado para este destinatario; so False e falha de fato.
    if results and all(v is None for v in results.values()):
        print("Notificacao: nenhum canal configurado para este destinatario.",
              file=sys.stderr)
        return True
    failed = [k for k, ok in results.items() if ok is False]
    if failed:
        print(f"Notificacao: falhou em {', '.join(failed)} (o relatorio foi gerado normalmente).",
              file=sys.stderr)
    return not failed


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Agente de NBA Fantasy (ESPN, H2H Points)")
    ap.add_argument("--team-id", type=int, default=None,
                    help="ID do seu time no ESPN (ou a variavel ESPN_TEAM_ID)")
    ap.add_argument("--source", choices=["auto", "nba", "espn"], default="auto",
                    help="fonte dos game logs: auto (detecta), nba_api ou API publica do ESPN")
    ap.add_argument("--recent", type=int, default=5, help="janela 'recente' em jogos")
    ap.add_argument("--replacement", type=float, default=24.0, help="FP/jogo de nivel de reposicao")
    ap.add_argument("--date", default=None, help="AAAA-MM-DD (padrao: hoje no fuso da NBA)")
    ap.add_argument("--telegram", action="store_true", help="envia o resumo pelo Telegram")
    ap.add_argument("--email", action="store_true", help="envia o resumo por email (SMTP)")
    ap.add_argument("--notify", action="store_true", help="envia o resumo pelo Telegram e por email")
    ap.add_argument("--lineup-only", action="store_true",
                    help="relatorio enxuto: so escalacao e IR (usado no pre-jogo)")
    ap.add_argument("--refresh", action="store_true", help="ignora o cache de dados")
    ap.add_argument("--out-dir", default="reports")
    opts = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    today = date.fromisoformat(opts.date) if opts.date else datetime.now(ET).date()
    season = season_str(today)
    prev_season = prev_season_str(season)

    team_id = opts.team_id
    if team_id is None and _env_first("ESPN_TEAM_ID").isdigit():
        team_id = int(_env_first("ESPN_TEAM_ID"))

    roster_rows = []
    print(f"Coletando dados da NBA ({season})...", file=sys.stderr)

    if not _env("ESPN_LEAGUE_ID"):
        sys.exit("Defina ESPN_LEAGUE_ID (e ESPN_TEAM_ID) no .env: o elenco vem da API do ESPN.")

    if missing_env("ESPN_SWID", "ESPN_S2"):
        print("Aviso: ESPN_SWID/ESPN_S2 ausentes no .env; so funciona em liga publica.", file=sys.stderr)

    fa = teams = status = my_players = None
    print("Lendo liga no ESPN...", file=sys.stderr)
    try:
        data = fetch_espn(int(season[:4]) + 1, team_id=team_id)
    except Exception as ex:
        sys.exit(f"Falha ao ler a liga no ESPN ({ex}). "
                 "Confira ESPN_LEAGUE_ID, ESPN_SWID e ESPN_S2 (os cookies expiram).")

    fa, teams, status = data["fa"], data["teams"], data["my_status"]
    my_players = data.get("my_players") or []
    if data.get("my_team"):
        print(f"Seu time: {data['my_team']} (id {data.get('my_team_id')}, "
              f"{len(my_players)} jogadores)", file=sys.stderr)
    if not my_players:
        sys.exit("Nao consegui identificar seu time no ESPN. "
                 "Defina ESPN_TEAM_ID (o numero teamId=... na URL do seu time).")

    roster_rows = roster_rows_from_espn(my_players)

    source = opts.source
    if source == "auto":
        source = "nba" if nba_reachable() else "espn"
    print(f"Fonte de dados: {'stats.nba.com' if source == 'nba' else 'API publica do ESPN'}",
          file=sys.stderr)

    if source == "espn":
        universe = build_universe_espn(roster_rows, fa, my_players, teams)
        cur = prep_logs(cached(f"espn_{season}", 3,
                               lambda: fetch_logs_espn(season, universe), opts.refresh))
        prev = prep_logs(cached(f"espn_{prev_season}", 168,
                                lambda: fetch_logs_espn(prev_season, universe), opts.refresh))
        tstats, positions = None, {}
        print(f"Jogadores analisados: {len(universe)}", file=sys.stderr)
    else:
        cur = prep_logs(cached(f"log_{season}", 3, lambda: fetch_gamelog(season), opts.refresh))
        prev = prep_logs(cached(f"log_{prev_season}", 168, lambda: fetch_gamelog(prev_season), opts.refresh))
        try:
            tstats = cached(f"team_{season}", 6, lambda: fetch_team_stats(season), opts.refresh)
        except Exception as ex:
            print(f"Aviso: ritmo/defesa dos times indisponivel ({ex}).", file=sys.stderr)
            tstats = None
        try:
            positions = cached(f"pos_{season}", 72, lambda: fetch_positions(season), opts.refresh)
        except Exception as ex:
            print(f"Aviso: posicoes (PlayerIndex) indisponiveis ({ex}).", file=sys.stderr)
            positions = {}

    sched = cached(f"sched_{season}_{source}", 6, lambda: fetch_schedule(season, source),
                   opts.refresh)
    if not sched:
        print("Aviso: calendario vazio; o relatorio sai sem jogos do dia.", file=sys.stderr)

    report, summary, digest = run_analysis(cur, prev, sched, tstats, positions, roster_rows, today, opts,
                                           fa_players=fa, other_teams=teams, espn_status=status)
    digest.update(team_name=data.get("my_team"), team_logo=data.get("my_logo"),
                  owner=data.get("my_owner"), photos=(source == "espn"))
    print(report)
    out = Path(opts.out_dir)
    out.mkdir(exist_ok=True)
    f = out / f"relatorio_{today.isoformat()}.md"
    f.write_text(report, encoding="utf-8")
    print(f"\n[salvo em {f}]", file=sys.stderr)
    want_telegram = opts.telegram or opts.notify
    want_email = opts.email or opts.notify

    # Pre-jogo: so notifica quando a escalacao precisa mudar. Se nao deu para ler o
    # lineup atual no ESPN (changes_known=False), envia mesmo assim (fail-open).
    # Cada jogo tem sua propria janela no gate, entao cada um gera seu aviso.
    skip_reason = None
    if digest.get("lineup_only") and digest.get("changes_known") and not digest.get("changes"):
        skip_reason = ("pre-jogo: escalacao ja esta correta, nenhuma mudanca necessaria; "
                       "notificacao nao enviada")

    if skip_reason:
        print(skip_reason, file=sys.stderr)
    elif summary and (want_telegram or want_email):
        try:
            notify(summary, today, telegram=want_telegram, email=want_email, digest=digest)
        except Exception as ex:
            traceback.print_exc()
            print(f"Aviso: falha ao notificar ({ex}); o relatorio foi salvo normalmente.", file=sys.stderr)


if __name__ == "__main__":
    main()