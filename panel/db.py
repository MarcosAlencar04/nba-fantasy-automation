"""Acesso ao Postgres do painel.

Todas as queries ficam aqui para o app.py cuidar apenas de HTTP/HTML. Usa
psycopg 3 com `dict_row`, entao as linhas chegam como dicionario e vao direto
para o template.
"""

from __future__ import annotations

import os
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

# Filtros da listagem. Os valores sao fragmentos SQL fixos (nunca vem do
# usuario), por isso e seguro interpolar — a chave e validada contra este
# dicionario antes de qualquer coisa.
#
# `{t}` recebe o prefixo da tabela: users tem JOIN com leagues, e as duas tem
# `is_active`/`deleted_at`, entao sem qualificar o Postgres acusa ambiguidade.
FILTERS: dict[str, tuple[str, str]] = {
    "active": ("{t}is_active AND {t}deleted_at IS NULL", "Ativos"),
    "inactive": ("NOT {t}is_active AND {t}deleted_at IS NULL", "Inativos"),
    "deleted": ("{t}deleted_at IS NOT NULL", "Excluidos"),
    "all": ("{t}deleted_at IS NULL", "Todos os vivos"),
}

# Sugestoes nao tem `is_active`: o que separa as visoes e' ter sido lida.
SUGGESTION_FILTERS: dict[str, tuple[str, str]] = {
    "new": ("{t}read_at IS NULL AND {t}deleted_at IS NULL", "Novas"),
    "read": ("{t}read_at IS NOT NULL AND {t}deleted_at IS NULL", "Lidas"),
    "deleted": ("{t}deleted_at IS NOT NULL", "Excluidas"),
    "all": ("{t}deleted_at IS NULL", "Todas as vivas"),
}


def _where(status: str, table: str = "",
           filters: dict[str, tuple[str, str]] | None = None) -> str:
    """Fragmento WHERE do filtro, com as colunas qualificadas."""
    prefix = f"{table}." if table else ""
    table_filters = filters or FILTERS
    # Sem filtro explicito o padrao e' "ativos"; para sugestoes e' "todas",
    # porque la nao existe o conceito de ativo.
    fallback = FILTERS["active"] if filters is None else table_filters["all"]
    return table_filters.get(status, fallback)[0].format(t=prefix)


def conninfo() -> str:
    url = (os.getenv("DATABASE_URL") or "").strip()
    if not url:
        raise RuntimeError(
            "DATABASE_URL nao configurada. Copie a connection string do Neon "
            "(com '-pooler') para as variaveis de ambiente."
        )
    return url


@contextmanager
def conn():
    """Conexao curta por request — serverless nao mantem pool entre invocacoes."""
    with psycopg.connect(conninfo(), row_factory=dict_row, connect_timeout=10) as c:
        yield c


def ping() -> str:
    """Valida credencial e conectividade. Usado pelo /health."""
    with conn() as c:
        row = c.execute("SELECT version() AS v").fetchone()
        return str(row["v"])


# --------------------------------------------------------------------------
# Ligas
# --------------------------------------------------------------------------
def list_leagues(status: str = "active") -> list[dict]:
    where = _where(status, "l")
    with conn() as c:
        cur = c.execute(
            f"""
            SELECT l.*,
                   (SELECT count(*) FROM users u
                     WHERE u.league_id = l.id AND u.deleted_at IS NULL) AS user_count
              FROM leagues l
             WHERE {where}
             ORDER BY l.is_active DESC, l.name
            """
        )
        return list(cur.fetchall())


def get_league(league_id: int) -> dict | None:
    with conn() as c:
        return c.execute("SELECT * FROM leagues WHERE id = %s", (league_id,)).fetchone()


def create_league(name: str, espn_league_id: str, espn_swid: str,
                  espn_s2: str) -> int:
    with conn() as c:
        row = c.execute(
            """
            INSERT INTO leagues (name, espn_league_id, espn_swid, espn_s2)
            VALUES (%s, %s, %s, %s)
            RETURNING id
            """,
            (name, espn_league_id, espn_swid, espn_s2),
        ).fetchone()
        return int(row["id"])


def update_league(league_id: int, name: str, espn_league_id: str,
                  espn_swid: str, espn_s2: str, is_active: bool) -> None:
    with conn() as c:
        c.execute(
            """
            UPDATE leagues
               SET name = %s, espn_league_id = %s, espn_swid = %s,
                   espn_s2 = %s, is_active = %s
             WHERE id = %s
            """,
            (name, espn_league_id, espn_swid, espn_s2, is_active, league_id),
        )


def set_league_deleted(league_id: int, deleted: bool) -> int:
    """Soft delete/restore. Devolve quantos usuarios vivos ficaram sem liga."""
    with conn() as c:
        c.execute(
            "UPDATE leagues SET deleted_at = CASE WHEN %s THEN now() ELSE NULL END "
            "WHERE id = %s",
            (deleted, league_id),
        )
        if not deleted:
            return 0
        row = c.execute(
            "SELECT count(*) AS n FROM users WHERE league_id = %s AND deleted_at IS NULL",
            (league_id,),
        ).fetchone()
        return int(row["n"])


def active_league_options() -> list[dict]:
    with conn() as c:
        cur = c.execute(
            "SELECT id, name FROM leagues "
            "WHERE is_active AND deleted_at IS NULL ORDER BY name"
        )
        return list(cur.fetchall())


# --------------------------------------------------------------------------
# Usuarios
# --------------------------------------------------------------------------
_USER_SELECT = """
    SELECT u.*, l.name AS league_name
      FROM users u
      JOIN leagues l ON l.id = u.league_id
     WHERE {where}
     ORDER BY l.name, u.is_active DESC, u.name
"""


def list_users(status: str = "active", league_id: int | None = None) -> list[dict]:
    where = _where(status, "u")
    params: list = []
    if league_id is not None:
        where += " AND u.league_id = %s"
        params.append(league_id)
    with conn() as c:
        return list(c.execute(_USER_SELECT.format(where=where), params).fetchall())


def get_user(user_id: int) -> dict | None:
    with conn() as c:
        return c.execute(
            "SELECT u.*, l.name AS league_name FROM users u "
            "JOIN leagues l ON l.id = u.league_id WHERE u.id = %s",
            (user_id,),
        ).fetchone()


def create_user(league_id: int, name: str, espn_team_id: int, email: str,
                telegram_id: str, notes: str, is_active: bool) -> int:
    with conn() as c:
        row = c.execute(
            """
            INSERT INTO users (league_id, name, espn_team_id, email, telegram_id,
                               notes, is_active)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (league_id, name, espn_team_id, email, telegram_id, notes, is_active),
        ).fetchone()
        return int(row["id"])


def update_user(user_id: int, league_id: int, name: str, espn_team_id: int,
                email: str, telegram_id: str, notes: str, is_active: bool) -> None:
    with conn() as c:
        c.execute(
            """
            UPDATE users
               SET league_id = %s, name = %s, espn_team_id = %s, email = %s,
                   telegram_id = %s, notes = %s, is_active = %s
             WHERE id = %s
            """,
            (league_id, name, espn_team_id, email, telegram_id, notes,
             is_active, user_id),
        )


def set_user_deleted(user_id: int, deleted: bool) -> None:
    with conn() as c:
        c.execute(
            "UPDATE users SET deleted_at = CASE WHEN %s THEN now() ELSE NULL END "
            "WHERE id = %s",
            (deleted, user_id),
        )


# --------------------------------------------------------------------------
# Sugestoes
# --------------------------------------------------------------------------
# Limites duplicados aqui e no CHECK da tabela: o app valida para dar mensagem
# amigavel, o banco valida para garantir o dado.
MAX_SUGGESTION_NAME = 120
MAX_SUGGESTION_BODY = 4000


def create_suggestion(name: str, body: str) -> int:
    """Grava uma sugestao vinda do formulario publico."""
    with conn() as c:
        row = c.execute(
            "INSERT INTO suggestions (name, body) VALUES (%s, %s) RETURNING id",
            (name, body),
        ).fetchone()
        return int(row["id"])


def list_suggestions(status: str = "new") -> list[dict]:
    """Nao lidas primeiro, depois as mais recentes."""
    where = _where(status, "s", SUGGESTION_FILTERS)
    with conn() as c:
        return list(c.execute(
            f"SELECT s.* FROM suggestions s WHERE {where} "
            "ORDER BY s.read_at NULLS FIRST, s.created_at DESC"
        ).fetchall())


def count_suggestions_unread() -> int:
    """Usado no cabecalho da listagem. Idx parcial cobre este WHERE."""
    with conn() as c:
        row = c.execute(
            "SELECT count(*) AS n FROM suggestions "
            "WHERE read_at IS NULL AND deleted_at IS NULL"
        ).fetchone()
        return int(row["n"])


def set_suggestion_read(suggestion_id: int, read: bool) -> None:
    with conn() as c:
        c.execute(
            "UPDATE suggestions SET read_at = "
            "CASE WHEN %s THEN now() ELSE NULL END WHERE id = %s",
            (read, suggestion_id),
        )


def set_suggestion_deleted(suggestion_id: int, deleted: bool) -> None:
    with conn() as c:
        c.execute(
            "UPDATE suggestions SET deleted_at = "
            "CASE WHEN %s THEN now() ELSE NULL END WHERE id = %s",
            (deleted, suggestion_id),
        )
