#!/usr/bin/env python3
"""Fonte unica da lista de times: banco de dados (padrao) ou secrets (legado).

Ordem de precedencia:

1. **DATABASE_URL definida** -> o banco e' a unica fonte. Se a consulta falhar,
   o script aborta com erro explicito em vez de cair nos secrets: um fallback
   silencioso para uma lista velha esconderia o problema e poderia notificar as
   pessoas erradas.
2. **Sem DATABASE_URL** -> usa as listas posicionais dos secrets, como antes.

Os dois jobs do workflow (`times` e `agente`) chamam ESTE arquivo, entao a
normalizacao e' identica nos dois lados — foi a divergencia entre `awk -F,` e um
heredoc Python que quebrou o run quando a lista passou de 2 para 3 times.

Comandos:
    teams.py matrix            -> JSON: {"include": [{"index": 0, "name": "..."}]}
    teams.py resolve <indice>  -> linhas KEY<<EOF para o $GITHUB_ENV

Somente stdlib no caminho dos secrets; `psycopg` so e' importado quando o banco
esta configurado.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

# Separadores ENTRE times no modo secrets. Ponto e virgula fica de fora de
# proposito: ele separa varios destinatarios do MESMO time, e `send_email`
# tambem quebra por ele. Espaco, tab e quebra de linha entram porque colar no
# campo de secret do GitHub costuma trazer esses caracteres.
SEPARATORS = ",\n\r\t "

# Tenta de novo antes de desistir: o Neon hiberna e a primeira conexao pode
# passar de alguns segundos.
DB_ATTEMPTS = 3


# --------------------------------------------------------------------------
# Normalizacao das listas (modo secrets)
# --------------------------------------------------------------------------
def split_list(raw: str | None) -> list[str]:
    """Quebra em entradas **nao vazias**, ignorando separadores repetidos.

    `7,9,11,` (virgula final) e `7,,11` viram entradas validas em vez de campos
    vazios, que era o que fazia o matrix ganhar indices sem time.
    """
    items: list[str] = []
    buf: list[str] = []
    for ch in raw or "":
        if ch in SEPARATORS:
            if buf:
                items.append("".join(buf))
                buf = []
        else:
            buf.append(ch)
    if buf:
        items.append("".join(buf))
    return items


def at(items: list[str], i: int) -> str:
    return items[i] if 0 <= i < len(items) else ""


def chat_ok(raw: str) -> str:
    """Chat id do Telegram e' numerico; grupos usam negativo.

    Qualquer outra coisa ("XXX", vazio, lixo) vira string vazia, que o agente
    interpreta como canal desligado. Assim um placeholder nao vira tentativa de
    envio com erro.
    """
    value = (raw or "").strip()
    if value and value.lstrip("-").isdigit():
        return value
    if value:
        print(f"aviso: telegram_id '{value}' nao e numerico; "
              f"Telegram desligado para este time", file=sys.stderr)
    return ""


# --------------------------------------------------------------------------
# Fontes
# --------------------------------------------------------------------------
def _database_url() -> str:
    return (os.getenv("DATABASE_URL") or "").strip()


def load_from_db() -> list[dict[str, Any]]:
    """Usuarios ativos, de ligas ativas, com as credenciais do ESPN da liga."""
    import psycopg  # importado aqui: so o modo banco precisa dele
    from psycopg.rows import dict_row

    sql = """
        SELECT u.name,
               u.espn_team_id,
               u.email,
               u.telegram_id,
               l.name           AS league_name,
               l.espn_league_id,
               l.espn_swid,
               l.espn_s2
          FROM users u
          JOIN leagues l ON l.id = u.league_id
         WHERE u.is_active  AND u.deleted_at IS NULL
           AND l.is_active  AND l.deleted_at IS NULL
         ORDER BY l.name, u.name
    """

    last: Exception | None = None
    rows = None
    for attempt in range(1, DB_ATTEMPTS + 1):
        try:
            with psycopg.connect(_database_url(), row_factory=dict_row,
                                 connect_timeout=15) as conn:
                rows = conn.execute(sql).fetchall()
            break
        except Exception as exc:  # noqa: BLE001 - qualquer falha de banco
            last = exc
            print(f"aviso: consulta ao banco falhou (tentativa {attempt}/"
                  f"{DB_ATTEMPTS}): {type(exc).__name__}: {exc}", file=sys.stderr)

    if rows is None:
        raise SystemExit(
            "erro: DATABASE_URL esta definida mas o banco nao respondeu "
            f"({type(last).__name__}: {last}). O agente NAO cai para os secrets "
            "de proposito: uma lista velha notificaria as pessoas erradas. "
            "Confira o secret DATABASE_URL e o status do Neon."
        )

    users: list[dict[str, Any]] = []
    for r in rows:
        # Uma liga sem cookie util tornaria a leitura do ESPN impossivel; melhor
        # falhar aqui, com o nome na mensagem, do que no meio do run.
        missing = [k for k in ("espn_league_id", "espn_swid", "espn_s2")
                   if not str(r.get(k) or "").strip()]
        if missing:
            raise SystemExit(
                f"erro: a liga '{r['league_name']}' (usuario '{r['name']}') esta "
                f"sem {', '.join(missing)}. Preencha no painel."
            )
        users.append(dict(
            name=str(r["name"]).strip(),
            espn_team_id=int(r["espn_team_id"]),
            email=str(r.get("email") or "").strip(),
            telegram_id=str(r.get("telegram_id") or "").strip(),
            espn_league_id=str(r["espn_league_id"]).strip(),
            espn_swid=str(r["espn_swid"]).strip(),
            espn_s2=str(r["espn_s2"]).strip(),
        ))
    return users


def load_from_secrets() -> list[dict[str, Any]]:
    """Modo legado: tres listas posicionais nos secrets."""
    ids = split_list(os.getenv("ESPN_TEAM_ID", ""))
    if not ids:
        raise SystemExit(
            "erro: nem DATABASE_URL nem ESPN_TEAM_ID estao definidos. "
            "Configure DATABASE_URL (recomendado) ou as listas posicionais."
        )
    mail = split_list(os.getenv("EMAIL_TO", ""))
    chat = split_list(os.getenv("TELEGRAM_CHAT_ID", ""))

    users = []
    # No modo legado a liga e' unica e vem do ambiente (uma liga por execucao).
    league = os.getenv("ESPN_LEAGUE_ID", "").strip()
    swid = os.getenv("ESPN_SWID", "").strip()
    s2 = os.getenv("ESPN_S2", "").strip()

    for i, raw_id in enumerate(ids):
        if not raw_id.isdigit():
            raise SystemExit(
                f"erro: ESPN_TEAM_ID posicao {i}: '{raw_id}' nao e um id numerico. "
                f"Lista lida: {ids}"
            )
        users.append(dict(
            # No modo secrets nao ha nome cadastrado; a saudacao fica generica.
            name="",
            espn_team_id=int(raw_id),
            email=at(mail, i),
            telegram_id=at(chat, i),
            espn_league_id=league,
            espn_swid=swid,
            espn_s2=s2,
        ))
    if len(mail) < len(ids):
        print(f"aviso: EMAIL_TO tem {len(mail)} entrada(s) para {len(ids)} time(s)",
              file=sys.stderr)
    if len(chat) < len(ids):
        print(f"aviso: TELEGRAM_CHAT_ID tem {len(chat)} entrada(s) para {len(ids)} time(s)",
              file=sys.stderr)
    return users


def load_users() -> tuple[list[dict[str, Any]], str]:
    """Usuarios e a fonte usada ("banco" | "secrets")."""
    if _database_url():
        return load_from_db(), "banco"
    return load_from_secrets(), "secrets"


# --------------------------------------------------------------------------
# Mascaramento para log
# --------------------------------------------------------------------------
# O repositorio e' publico, entao os logs do Actions (e o "Set up job" de cada
# job do matrix) sao legiveis por qualquer pessoa. O GitHub so oculta valores
# que vem DIRETO de `secrets.X`; o que este script le do banco nao passa por
# esse filtro e sairia em texto limpo. Mascaramos aqui e mantemos apenas o que
# serve para diagnosticar (o dado existe? o dominio esta certo?).
def mask_email(addr: str) -> str:
    """marcos@gmail.com -> ma***@gmail.com (o dominio revela typo de provedor)."""
    if not addr:
        return "<nenhum>"
    local, sep, domain = addr.partition("@")
    if not sep:
        # Nao parece e-mail; nao ha o que preservar, entao nao mostre nada.
        return "***"
    return f"{local[:2]}***@{domain}" if len(local) > 2 else f"***@{domain}"


def mask_name(name: str) -> str:
    """Marcos -> M*** (prova que veio preenchido sem expor o nome)."""
    return f"{name.strip()[0]}***" if name and name.strip() else "(sem nome)"


# --------------------------------------------------------------------------
# Comandos
# --------------------------------------------------------------------------
def cmd_matrix() -> int:
    users, source = load_users()
    if not users:
        # Antes isto geraria `{"include": []}`, que o Actions trata como matrix
        # vazio e simplesmente nao roda nada — falha silenciosa. Melhor gritar.
        raise SystemExit(
            "erro: nenhum usuario ativo encontrado. Cadastre e ative ao menos um "
            "no painel (ou use os secrets posicionais)."
        )

    # O valor do matrix aparece em texto limpo no "Set up job" de cada job
    # filho, que tambem e' publico. Por isso o rotulo e' sempre `time-<i>`: o
    # nome real do usuario nunca entra no matrix (o agente recebe o nome via
    # `resolve`, escrito em $GITHUB_ENV e nao ecoado).
    matrix = {"include": [{"index": i, "name": f"time-{i}"}
                          for i, _ in enumerate(users)]}

    # Diagnostico SEMPRE em stderr: o stdout deste comando e' capturado inteiro
    # por `$(python3 teams.py matrix)` e vai para o $GITHUB_OUTPUT. Qualquer
    # linha extra aqui corrompe o valor e o `fromJSON` do matrix quebra.
    sem_email = sum(1 for u in users if not u["email"])
    sem_chat = sum(1 for u in users if not u["telegram_id"])
    print(f"fonte={source} usuarios={len(users)} "
          f"sem_email={sem_email} sem_telegram={sem_chat}", file=sys.stderr)
    # team_id fica visivel: e' o mesmo numero que aparece na URL publica do
    # ESPN e e' o que permite casar um job do matrix com o time certo.
    for i, u in enumerate(users):
        print(f"  #{i} {mask_name(u['name'])} team_id={u['espn_team_id']}",
              file=sys.stderr)

    print(json.dumps(matrix))
    return 0


def _emit(name: str, value: str) -> None:
    """Escreve uma variavel no formato heredoc do $GITHUB_ENV.

    Heredoc em vez de `KEY=valor` porque os cookies do ESPN vem com `%2B`, `=`
    e `+`, e um valor com quebra de linha corromperia o arquivo de ambiente.
    """
    clean = (value or "").replace("\r", "").strip()
    print(f"{name}<<__NBA_EOF__")
    print(clean)
    print("__NBA_EOF__")


def cmd_resolve(index_raw: str) -> int:
    if not index_raw.strip().isdigit():
        raise SystemExit(f"erro: indice invalido '{index_raw}'")
    index = int(index_raw)

    users, source = load_users()
    if index >= len(users):
        raise SystemExit(
            f"erro: indice {index} fora da lista ({len(users)} usuario(s) na fonte "
            f"'{source}')"
        )

    u = users[index]
    email = u["email"]
    chat = chat_ok(u["telegram_id"])
    if email and "@" not in email:
        print(f"aviso: EMAIL_TO do usuario #{index} nao tem '@' "
              "(valor omitido do log)", file=sys.stderr)

    # Vai para o $GITHUB_ENV, sobrescrevendo o que veio dos secrets.
    _emit("ESPN_TEAM_ID", str(u["espn_team_id"]))
    _emit("EMAIL_TO", email)
    _emit("TELEGRAM_CHAT_ID", chat)
    _emit("USER_NAME", u["name"])
    # Credenciais do ESPN: em ambos os modos vem daqui, nunca de um secret
    # fixo no workflow. No modo banco sao os cookies da liga DESTE usuario
    # (varias ligas convivem no mesmo run); no modo legado sao os do ambiente.
    for key, field in (("ESPN_LEAGUE_ID", "espn_league_id"),
                       ("ESPN_SWID", "espn_swid"),
                       ("ESPN_S2", "espn_s2")):
        if u[field]:
            _emit(key, u[field])

    # Log legivel (stderr, para nao contaminar o $GITHUB_ENV). Nome, e-mail e
    # chat id saem mascarados porque este log fica publico; o que importa aqui
    # e' saber se cada canal foi resolvido, nao para quem.
    print(f"[{source}] #{index} {mask_name(u['name'])} -> "
          f"team_id={u['espn_team_id']} | "
          f"email={mask_email(email)} | "
          f"telegram={'sim' if chat else '<nenhum>'} | "
          f"liga={'sim' if u['espn_league_id'] else '<nenhuma>'}", file=sys.stderr)
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        raise SystemExit("uso: teams.py matrix | teams.py resolve <indice>")
    cmd = argv[1]
    if cmd == "matrix":
        return cmd_matrix()
    if cmd == "resolve":
        if len(argv) < 3:
            raise SystemExit("uso: teams.py resolve <indice>")
        return cmd_resolve(argv[2])
    raise SystemExit(f"comando desconhecido: {cmd}")


if __name__ == "__main__":
    sys.exit(main(sys.argv))
