#!/usr/bin/env python3
"""Trava de envio: impede que as retentativas do cron dupliquem a notificacao.

Por que isso existe
-------------------
O `schedule` do GitHub Actions e' **best-effort**. A documentacao e' explicita:

    "The `schedule` event can be delayed during periods of high loads of GitHub
     Actions workflow runs. High load times include the start of every hour. If
     the load is sufficiently high enough, some queued jobs may be dropped."

Na pratica, este repositorio recebeu 6 execucoes agendadas em ~48h, quando o
cron `*/30` deveria ter disparado ~96. Com essa taxa, uma unica tentativa diaria
para o relatorio da manha entrega o relatorio em ~6% dos dias.

A correcao tem duas partes:

1. o workflow agenda **varias** tentativas para a manha (ver `nba-fantasy.yml`);
2. esta trava garante que apenas a primeira que passar notifique. As demais
   encontram a marca do dia e saem sem enviar nada.

Como a marca e' por (tipo, dia, time), cada dono recebe um email por dia mesmo
que dez tentativas rodem.

Sem `DATABASE_URL` nao ha onde guardar a marca: `claim` devolve True e o
comportamento volta ao antigo (uma notificacao por execucao). E' o modo legado
dos secrets, onde nao existe banco.

Falha aberta de proposito: se o banco cair, `claim` devolve True. Duplicar um
email e' menos grave do que perder o relatorio do dia.
"""

from __future__ import annotations

import os
import sys
from datetime import date
from typing import Any

# Mesmo motivo do teams.py: o Neon hiberna e a primeira conexao pode demorar.
DB_ATTEMPTS = 3

# `CREATE TABLE IF NOT EXISTS` concorrente (o matrix roda um job por time ao
# mesmo tempo) pode colidir no catalogo do Postgres com
# "duplicate key value violates unique constraint pg_type_typname_nsp_index".
# Nao e' erro real — a tabela existe depois da corrida —, entao engolimos.
TABLE_DDL = """
CREATE TABLE IF NOT EXISTS agent_notifications (
    kind     TEXT        NOT NULL,
    run_date DATE        NOT NULL,
    team_id  INTEGER     NOT NULL,
    sent_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (kind, run_date, team_id)
)
"""


def _database_url() -> str:
    return (os.getenv("DATABASE_URL") or "").strip()


def _connect():
    """Conexao curta com retry. Importa psycopg so quando ha banco.

    `autocommit=True` porque cada operacao aqui e' uma unidade propria: nao ha
    transacao maior para confirmar, e uma conexao esperando COMMIT seguraria a
    trava sem necessidade.
    """
    import psycopg  # importado aqui para o modo secrets nao precisar dele
    from psycopg.rows import dict_row

    last: Exception | None = None
    for attempt in range(1, DB_ATTEMPTS + 1):
        try:
            return psycopg.connect(_database_url(), connect_timeout=10,
                                   autocommit=True, row_factory=dict_row)
        except Exception as exc:  # pragma: no cover - depende da rede
            last = exc
            print(f"trava: conexao {attempt}/{DB_ATTEMPTS} falhou "
                  f"({type(exc).__name__})", file=sys.stderr)
    raise last  # type: ignore[misc]


def _ensure_table(conn) -> None:
    """Cria a tabela se ainda nao existir.

    Os jobs do matrix rodam em paralelo e chamam isto ao mesmo tempo. Depois da
    corrida a tabela existe e a reserva funciona normalmente, entao o erro de
    catalogo do Postgres (`UniqueViolation` em pg_type/pg_class) e' esperado e
    fica em silencio — imprimir 6 avisos por execucao so assustaria quem olha o
    log. Qualquer outro erro e' reportado.
    """
    try:
        conn.execute(TABLE_DDL)
    except Exception as exc:
        name = type(exc).__name__
        # DuplicateObject cobre DuplicateTable; UniqueViolation e' a colisao no
        # catalogo (pg_type/pg_class) quando dois CREATE TABLE IF NOT EXISTS
        # correm juntos. Os dois significam "a tabela existe".
        if "DuplicateObject" in name or "DuplicateTable" in name \
                or "UniqueViolation" in name:
            return
        print(f"trava: aviso ao criar a tabela ({name})", file=sys.stderr)


def claim(kind: str, team_id: int | None, day: date) -> bool:
    """Reserva a notificacao de hoje para este time.

    Devolve True quando **esta** execucao ganhou o direito de enviar — ou seja,
    e' a primeira do dia para este (kind, time). Devolve False quando outra ja
    enviou.

    Falha aberta: sem banco, sem team_id ou com erro de conexao devolve True.
    """
    if not kind or team_id is None:
        return True
    if not _database_url():
        return True

    conn = None
    try:
        conn = _connect()
        _ensure_table(conn)
        row = conn.execute(
            "INSERT INTO agent_notifications (kind, run_date, team_id) "
            "VALUES (%s, %s, %s) "
            "ON CONFLICT (kind, run_date, team_id) DO NOTHING "
            "RETURNING sent_at",
            (kind, day, int(team_id)),
        ).fetchone()
        return row is not None
    except Exception as exc:
        print(f"trava: indisponivel ({type(exc).__name__}: {exc}); "
              "seguindo sem deduplicar", file=sys.stderr)
        return True
    finally:
        if conn is not None:
            conn.close()


def release(kind: str, team_id: int | None, day: date) -> None:
    """Devolve a reserva quando o envio falhou, para um retry poder tentar.

    Chamado no caminho de excecao do agente. Sem isto, uma falha de SMTP
    queimaria a vaga do dia e o usuario ficaria sem relatorio.
    """
    if not kind or team_id is None or not _database_url():
        return
    conn = None
    try:
        conn = _connect()
        conn.execute(
            "DELETE FROM agent_notifications "
            "WHERE kind = %s AND run_date = %s AND team_id = %s",
            (kind, day, int(team_id)),
        )
    except Exception as exc:
        print(f"trava: nao consegui liberar a reserva ({type(exc).__name__})",
              file=sys.stderr)
    finally:
        if conn is not None:
            conn.close()


def status(team_id: int | None = None) -> list[dict[str, Any]]:
    """Diagnostico: o que ja foi reservado. Util para depurar entregas."""
    if not _database_url():
        return []
    conn = None
    try:
        conn = _connect()
        _ensure_table(conn)
        sql = "SELECT kind, run_date, team_id, sent_at FROM agent_notifications"
        params: tuple = ()
        if team_id is not None:
            sql += " WHERE team_id = %s"
            params = (int(team_id),)
        sql += " ORDER BY sent_at DESC LIMIT 50"
        return list(conn.execute(sql, params).fetchall())
    finally:
        if conn is not None:
            conn.close()
