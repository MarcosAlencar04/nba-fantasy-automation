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
  5. Ideias de troca (precisa do modo --espn para ver os outros elencos).

Uso:  python nba_fantasy_agent.py            (so com roster.csv)
      python nba_fantasy_agent.py --espn     (le free agents/outros times)
"""
from __future__ import annotations

import argparse
import difflib
import math
import os
import pickle
import re
import sys
import time
import unicodedata
from collections import defaultdict
from datetime import date, datetime, timedelta
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
ADDS_LIMIT = 5
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


def fetch_schedule():
    """Retorna {date: [(home, away), ...]} so de jogos de temporada regular."""
    games = defaultdict(list)
    try:
        r = requests.get("https://cdn.nba.com/static/json/staticData/scheduleLeagueV2.json",
                         headers=UA, timeout=30)
        r.raise_for_status()
        data = r.json()["leagueSchedule"]
    except Exception:
        from nba_api.stats.endpoints import scheduleleaguev2  # fallback
        data = scheduleleaguev2.ScheduleLeagueV2(timeout=60).get_dict()["leagueSchedule"]
    for gd in data["gameDates"]:
        d = datetime.strptime(gd["gameDate"].split(" ")[0], "%m/%d/%Y").date()
        for g in gd["games"]:
            if not str(g.get("gameId", "")).startswith("002"):
                continue
            games[d].append((g["homeTeam"]["teamTricode"], g["awayTeam"]["teamTricode"]))
    return dict(games)


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


def read_roster_csv(path):
    rows = []
    with open(path, encoding="utf-8-sig") as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            parts = [x.strip() for x in re.split(r"[,;\t]", ln)]
            if len(parts) < 2:
                parts.append("")
            if parts[0].lower() in ("nome", "name", "jogador", "player", "nome jogador"):
                continue
            rows.append(dict(name=parts[0], pos=parts[1], status=parts[2] if len(parts) > 2 else ""))
    return rows


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
# ESPN (opcional): free agents, status de lesao e elencos dos outros times
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


def fetch_espn(year, roster_names, fa_size=150):
    from espn_api.basketball import League
    lg = League(league_id=int(os.environ["ESPN_LEAGUE_ID"]), year=year,
                espn_s2=os.getenv("ESPN_S2"), swid=os.getenv("ESPN_SWID"))

    def conv(p):
        return dict(name=p.name, elig=elig_from_espn(p),
                    status=getattr(p, "injuryStatus", "ACTIVE"),
                    owned=getattr(p, "percent_owned", None))

    mine = {norm_name(n) for n in roster_names}
    teams, best, my_idx = [], -1, None
    for i, t in enumerate(lg.teams):
        pl = [conv(p) for p in t.roster]
        ov = len(mine & {norm_name(x["name"]) for x in pl})
        teams.append((t.team_name, pl))
        if ov > best:
            best, my_idx = ov, i
    my_status = {}
    if my_idx is not None and best >= 3:
        my_status = {norm_name(x["name"]): x["status"] for x in teams[my_idx][1]}
        my_name = teams.pop(my_idx)[0]
    else:
        my_name = None
    fa = [conv(p) for p in lg.free_agents(size=fa_size)]
    return dict(fa=fa, teams=teams, my_status=my_status, my_team=my_name)


# --------------------------------------------------------------------------
# Analise principal (funcao pura -> facilita testes)
# --------------------------------------------------------------------------
def run_analysis(cur, prev, sched, tstats, positions, roster_rows, today, opts,
                 fa_players=None, other_teams=None, espn_status=None):
    notes = []
    summary = []
    L = []  # linhas do relatorio

    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=6)
    horizon_end = week_end if (week_end - today).days >= 2 else week_end + timedelta(days=7)
    days = [today + timedelta(days=i) for i in range((horizon_end - today).days + 1)]
    wk_days = [d for d in days if d <= week_end]

    done_today = set(cur[cur.GAME_DATE == today].PLAYER_ID.astype(int)) if len(cur) else set()
    ctx = Ctx(sched, tstats, today, done_today)

    profiles = build_profiles(cur, prev, opts.recent)
    idx = NameIndex(profiles)

    def mk(prof, elig, status):
        e = dict(prof)
        e["elig"] = elig
        e["status"] = norm_status(status)
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
        roster.append(mk(profiles[pid], elig, status))
    if not roster:
        return "Nenhum jogador do roster foi reconhecido.", []
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
    if opts.candidates:
        extra = []
        for nm in read_names_csv(opts.candidates):
            pid, _ = idx.find(nm)
            if pid is None:
                notes.append(f"Candidato '{nm}' nao encontrado.")
            elif pid not in pool and all(c["pid"] != pid for c in cands):
                extra.append(mk(profiles[pid], pos_of(pid), ""))
        if extra:
            fa_label = "sua lista de candidatos" + ("" if fa_players else " + top da NBA")
            cands = extra + cands
    cands = [c for c in cands if c["status"] not in ("OUT", "IR")]
    for c in cands:
        pool[c["pid"]] = c

    # nivel de reposicao
    healthy = sorted((c["proj_fppg"] for c in cands), reverse=True)
    repl = healthy[4] if (fa_players and len(healthy) >= 5) else opts.replacement

    # ---- cabecalho ---------------------------------------------------
    adds_left = max(0, ADDS_LIMIT - opts.adds_used)
    L += [f"# Relatorio NBA Fantasy - {today.strftime('%d/%m/%Y')} ({today.strftime('%A')})",
          f"Semana do confronto: {week_start.strftime('%d/%m')} a {week_end.strftime('%d/%m')} | "
          f"Adds restantes no matchup: **{adds_left}/{ADDS_LIMIT}** | "
          f"Prazo de trocas: {(TRADE_DEADLINE - today).days} dias ({TRADE_DEADLINE.strftime('%d/%m/%Y')})",
          f"Nivel de reposicao usado: ~{repl:.0f} FP/jogo | Candidatos: {fa_label}",
          ""]

    # ========== 1) LINEUP DE HOJE ======================================
    active_ids = [e["pid"] for e in roster if e["status"] != "IR"]
    elig = [pool[i]["elig"] for i in active_ids]
    vals = [pool[i]["dv"][today] for i in active_ids]
    assign, total = optimize(elig, vals)
    starters = {pi: sj for sj, pi in assign}
    L += ["## 1. Lineup otimo de hoje", f"Valor esperado total: **{total:.1f} FP**", ""]
    rows = []
    for sj, pi in assign:
        e = pool[active_ids[pi]]
        gm = ctx.game(e["team"], today)
        opp = f"{'vs' if gm[1] else '@'} {gm[0]}" if gm else "-"
        flag = " (DTD)" if e["status"] == "DTD" else ""
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
            why = "lesionado/IR"
        elif not ctx.game(e["team"], today):
            why = "sem jogo hoje"
        elif e["pid"] in done_today:
            why = "jogo de hoje ja terminou"
        else:
            why = f"banco (valor {e['dv'][today]:.1f} perde para os titulares)"
        L.append(f"- {e['name']} ({e['team']}): {why}")
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

    # ========== 2) SEMANA / FORMA ======================================
    cw = cur[(cur.GAME_DATE >= week_start) & (cur.GAME_DATE <= today)] if len(cur) else cur
    wk = {k: g for k, g in cw.groupby("PLAYER_ID")} if len(cw) else {}
    L += ["## 2. Semana do confronto e fase de cada jogador", ""]
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
            ver = "SEGURAR (fase ruim e passageira)"
        elif e["tag"] == "PAPEL_ENCOLHENDO":
            ver = "ALERTA: papel encolhendo"
        elif e["proj_fppg"] < repl - 1:
            ver = "CANDIDATO A DROP"
        else:
            ver = "Manter"
        if e["tag"] in ("VENDER_NA_ALTA", "COMPRAR_NA_BAIXA", "PAPEL_ENCOLHENDO", "ALTA_SUSTENTAVEL"):
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
        L.append(f"- **{e['name']}** - {lab}: " + "; ".join(e["why"]) + conf)
        if e["tag"] == "VENDER_NA_ALTA":
            summary.append(f"VENDER NA ALTA: {e['name']} ({'; '.join(e['why'])})")
        if e["tag"] == "PAPEL_ENCOLHENDO":
            summary.append(f"ALERTA papel: {e['name']} ({'; '.join(e['why'])})")
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
    L += ["## 3. Contratar / dropar (free agents)", ""]
    droppable = [e for e in roster if e["status"] != "IR"]
    n_centers = sum(1 for e in roster if "C" in e["elig"])
    base_ids = [e["pid"] for e in droppable]
    base_week = week_value(base_ids, pool, days)

    def rank_pre(c):
        gms = sum(1 for d in days if ctx.game(c["team"], d))
        return c["proj_fppg"] * (1 + 0.12 * gms)

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
                 f"(valor de medio prazo). Voce tem {adds_left} add(s) restante(s).")
        L.append("")
        rows = []
        for r in picks:
            a, d = r["add"], r["drop"]
            gj = sum(1 for x in days if ctx.game(a["team"], x))
            conf = "" if a["conf"] >= 0.5 else " *amostra pequena*"
            rows.append((f"**{a['name']}** ({a['team']}, {'/'.join(sorted(a['elig'])) or '?'})", d["name"],
                         f"{a['proj_fppg']:.1f} vs {d['proj_fppg']:.1f}", gj, sgn(r["wg"], 0), sgn(r["pg"]),
                         (f"{a['owned']:.0f}%" if a.get("owned") else "-"),
                         (a["tag"].replace("_", " ").title() if a["tag"] != "NEUTRO" else "-"), conf.strip() or "-"))
        L += md_table(["Contratar", "Dropar", "Proj/jogo (add vs drop)", "Jogos ate o fim", "Ganho semana (FP)",
                       "Ganho/jogo", "% dono ESPN", "Fase", "Obs"], rows)
        for r in picks[:adds_left or 0]:
            summary.append(f"ADD {r['add']['name']} / DROP {r['drop']['name']} (ganho semana {r['wg']:+.0f} FP, {r['pg']:+.1f}/jogo)")
        if adds_left == 0:
            L += ["", "Voce ja usou os 5 adds deste matchup: as sugestoes valem a partir do proximo."]
        L += ["", "Waivers duram 1 dia: o claim processa no dia seguinte. Se o candidato for DTD, confira o status antes."]
    L.append("")

    # ========== 4) TROCAS ==============================================
    L += ["## 4. Trocas", ""]
    sells = sorted((e for e in roster if e["tag"] == "VENDER_NA_ALTA"), key=lambda e: -e["delta_form"])
    if other_teams:
        trade_rows = []
        for tname, plist in other_teams:
            tpool = []
            for x in plist:
                pid, _ = idx.find(x["name"])
                if pid is None:
                    continue
                if pid not in pool:
                    pool[pid] = mk(profiles[pid], x.get("elig") or pos_of(pid), x.get("status", ""))
                tpool.append(pool[pid])
            for p in droppable:
                for q in tpool:
                    if q["status"] in ("OUT", "IR"):
                        continue
                    gain = q["proj_fppg"] - p["proj_fppg"]
                    if gain < 2.5 or p["perceived"] < q["perceived"] - 2.0:
                        continue
                    ids = [i for i in base_ids if i != p["pid"]] + [q["pid"]]
                    wg = week_value(ids, pool, days) - base_week
                    bonus = 0.25 * (p["perceived"] - q["perceived"])
                    trade_rows.append(dict(team=tname, give=p, get=q, gain=gain, wg=wg, score=gain + bonus + 0.02 * wg))
        trade_rows.sort(key=lambda r: -r["score"])
        out, per_team, used_g, used_r = [], defaultdict(int), set(), set()
        for r in trade_rows:
            if per_team[r["team"]] >= 2 or r["give"]["pid"] in used_g:
                continue
            out.append(r)
            per_team[r["team"]] += 1
            used_g.add(r["give"]["pid"])
            if len(out) >= 8:
                break
        if not out:
            L.append("Nenhuma troca 1x1 clara encontrada (ganho de projecao >= 2,5 FP/jogo sem parecer desvantajosa para o outro dono).")
        else:
            L.append("Troca 1x1 em que **voce ganha em projecao real** mas o outro dono enxerga valor igual ou maior "
                     "(pelo que o ESPN mostra: media da temporada + ultimos 10 jogos).")
            L.append("")
            rows = []
            for r in out:
                risk = " (risco de veto: muito desigual)" if r["gain"] > 8 else ""
                rows.append((r["team"], r["give"]["name"], r["get"]["name"], f"{r['give']['proj_fppg']:.1f} -> {r['get']['proj_fppg']:.1f}",
                             sgn(r["gain"]), f"{r['give']['perceived']:.1f} / {r['get']['perceived']:.1f}", sgn(r["wg"], 0) + risk))
            L += md_table(["Time", "Voce da", "Voce recebe", "Proj/jogo", "Ganho/jogo", "Valor percebido (seu/dele)", "Ganho semana"], rows)
            for r in out[:3]:
                summary.append(f"TROCA ({r['team']}): dar {r['give']['name']} por {r['get']['name']} ({r['gain']:+.1f}/jogo)")
        L += ["", "A aceitacao depende do outro dono; a liga tem 2 dias de revisao e 4 votos vetam. Evite trocas absurdamente desiguais."]
    else:
        L.append("Sem acesso aos elencos dos outros times (rode com `--espn`). Mesmo assim:")
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
    low = [e["name"] for e in roster if e["conf"] < 0.3]
    if low:
        notes.append("Amostra pequena na temporada para: " + ", ".join(low) +
                     ". A projecao pesa mais a temporada passada ate ~15 jogos.")
    stale = [e["name"] for e in roster if e["stale"]]
    if stale:
        notes.append("Sem jogos nesta temporada (time pode estar desatualizado): " + ", ".join(stale))
    if not any(e["status"] in ("DTD", "OUT", "IR") for e in roster) and not espn_status:
        notes.append("Status de lesao nao informado: use a 3a coluna do roster.csv (OUT/DTD/IR) ou o modo --espn.")
    if notes:
        L += ["## Avisos", ""] + [f"- {n}" for n in notes] + [""]
    return "\n".join(L), summary


# --------------------------------------------------------------------------
# Telegram (opcional)
# --------------------------------------------------------------------------
def send_telegram(text):
    tok, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        print("Telegram: defina TELEGRAM_TOKEN e TELEGRAM_CHAT_ID no .env", file=sys.stderr)
        return
    r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                      data={"chat_id": chat, "text": text[:3900]}, timeout=20)
    print("Telegram:", "enviado" if r.ok else f"erro {r.status_code}")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Agente de NBA Fantasy (ESPN, H2H Points)")
    ap.add_argument("--roster", default="roster.csv", help="CSV: Nome, Posicao[, Status]")
    ap.add_argument("--candidates", default=None, help="CSV opcional com nomes de free agents para avaliar")
    ap.add_argument("--espn", action="store_true", help="usa a API do ESPN (free agents, lesoes, outros elencos)")
    ap.add_argument("--adds-used", type=int, default=0, help="adds ja usados neste matchup (limite 5)")
    ap.add_argument("--recent", type=int, default=5, help="janela 'recente' em jogos")
    ap.add_argument("--replacement", type=float, default=24.0, help="FP/jogo de nivel de reposicao (sem --espn)")
    ap.add_argument("--date", default=None, help="AAAA-MM-DD (padrao: hoje no fuso da NBA)")
    ap.add_argument("--telegram", action="store_true")
    ap.add_argument("--refresh", action="store_true", help="ignora o cache de dados")
    ap.add_argument("--out-dir", default="reports")
    opts = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    today = date.fromisoformat(opts.date) if opts.date else datetime.now(ET).date()
    season = season_str(today)
    prev_season = prev_season_str(season)
    if not Path(opts.roster).exists():
        sys.exit(f"Arquivo {opts.roster} nao encontrado. Crie um CSV 'Nome, Posicao' (um jogador por linha).")
    roster_rows = read_roster_csv(opts.roster)

    print(f"Coletando dados da NBA ({season})...", file=sys.stderr)
    cur = prep_logs(cached(f"log_{season}", 3, lambda: fetch_gamelog(season), opts.refresh))
    prev = prep_logs(cached(f"log_{prev_season}", 168, lambda: fetch_gamelog(prev_season), opts.refresh))
    sched = cached(f"sched_{season}", 12, fetch_schedule, opts.refresh)
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

    fa = teams = status = None
    if opts.espn:
        try:
            print("Lendo liga no ESPN...", file=sys.stderr)
            data = fetch_espn(int(season[:4]) + 1, [r["name"] for r in roster_rows])
            fa, teams, status = data["fa"], data["teams"], data["my_status"]
            if not status:
                print("Aviso: nao consegui identificar seu time no ESPN; trocas podem incluir seu proprio elenco.",
                      file=sys.stderr)
        except Exception as ex:
            print(f"Aviso: falha no ESPN ({ex}). Seguindo sem ele "
                  "(confira ESPN_LEAGUE_ID, ESPN_SWID e ESPN_S2; os cookies expiram).", file=sys.stderr)

    report, summary = run_analysis(cur, prev, sched, tstats, positions, roster_rows, today, opts,
                                   fa_players=fa, other_teams=teams, espn_status=status)
    print(report)
    out = Path(opts.out_dir)
    out.mkdir(exist_ok=True)
    f = out / f"relatorio_{today.isoformat()}.md"
    f.write_text(report, encoding="utf-8")
    print(f"\n[salvo em {f}]", file=sys.stderr)
    if opts.telegram and summary:
        send_telegram(f"NBA Fantasy {today.strftime('%d/%m')}\n\n" + "\n".join(summary))


if __name__ == "__main__":
    main()