#!/usr/bin/env python3
"""Cria/atualiza o schema do painel. Rode uma vez, com a DATABASE_URL no ambiente.

    DATABASE_URL='postgresql://...' python init_db.py

O schema.sql e idempotente, entao rodar de novo nao apaga dados.
"""

from __future__ import annotations

import sys
from pathlib import Path

import psycopg

HERE = Path(__file__).resolve().parent


def main() -> int:
    sql = (HERE / "schema.sql").read_text(encoding="utf-8")

    import db  # importado aqui para o erro de DATABASE_URL ficar claro

    try:
        with psycopg.connect(db.conninfo(), autocommit=True) as c:
            with c.cursor() as cur:
                cur.execute(sql)
        print("schema aplicado com sucesso")
    except Exception as exc:
        print(f"falhou: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    # Confirma o que existe, util para saber se o deploy enxerga o banco certo.
    with db.conn() as c:
        cur = c.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' ORDER BY table_name"
        )
        tables = [r["table_name"] for r in cur.fetchall()]
        print("tabelas:", ", ".join(tables) or "(nenhuma)")
        for t in ("leagues", "users", "suggestions", "agent_notifications"):
            if t in tables:
                n = c.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"]
                print(f"  {t}: {n} linha(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
