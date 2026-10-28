#!/usr/bin/env python3
"""Listas posicionais de times, lidas dos secrets. Somente stdlib.

Os secrets guardam uma lista por atributo, sempre na mesma ordem:

    ESPN_TEAM_ID     = 7,9,11
    EMAIL_TO         = a@x.com,b@y.com,c@z.com
    TELEGRAM_CHAT_ID = 111,XXX,333

A posicao N de ESPN_TEAM_ID pareia com a posicao N dos destinatarios. Tanto o
job que monta o matrix quanto o job que roda o agente usam ESTE arquivo, entao a
normalizacao e' identica nos dois lados — `awk -F,` divergindo entre eles foi o
que quebrou quando a lista passou de 2 para 3 times.

Comandos:
    teams.py matrix            -> JSON do matrix, so com os indices validos
    teams.py resolve <indice>  -> linhas KEY=VALUE para o $GITHUB_ENV
"""

from __future__ import annotations

import json
import os
import sys

# Separadores ENTRE times. Ponto e virgula fica de fora de proposito: ele separa
# varios destinatarios do MESMO time, e `send_email` tambem quebra por ele.
# Espaco, tabulacao e quebra de linha entram porque colar no campo de secret do
# GitHub costuma trazer esses caracteres.
SEPARATORS = ",\n\r\t "


def split_list(raw: str | None) -> list[str]:
    """Quebra em entradas **nao vazias**, ignorando separadores repetidos.

    `7,9,11,` (virgula final) e `7,,11` viram duas e tres entradas validas em vez
    de campos vazios, que era o que fazia o matrix ganhar indices sem time.
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


def team_ids() -> list[int]:
    """Ids de time, validados. Uma entrada invalida aborta com posicao exata."""
    raw = os.getenv("ESPN_TEAM_ID", "")
    items = split_list(raw)
    if not items:
        raise SystemExit(
            "erro: ESPN_TEAM_ID vazio. Defina um ou mais ids separados por virgula "
            "(ex.: 7,9,11)."
        )
    out = []
    for i, item in enumerate(items):
        if not item.isdigit():
            raise SystemExit(
                f"erro: ESPN_TEAM_ID posicao {i}: '{item}' nao e um id numerico. "
                f"Lista lida: {items}"
            )
        out.append(int(item))
    return out


def emails() -> list[str]:
    return split_list(os.getenv("EMAIL_TO", ""))


def chats() -> list[str]:
    return split_list(os.getenv("TELEGRAM_CHAT_ID", ""))


def at(items: list[str], i: int) -> str:
    return items[i] if 0 <= i < len(items) else ""


def chat_ok(raw: str) -> str:
    """Chat id do Telegram e' numerico; grupos usam negativo.

    Qualquer outra coisa ("XXX", vazio, lixo) vira string vazia, que o agente
    interpreta como canal desligado. Assim um placeholder no secret nao vira
    tentativa de envio com erro.
    """
    value = raw.strip()
    if value and value.lstrip("-").isdigit():
        return value
    if value:
        print(
            f"aviso: TELEGRAM_CHAT_ID '{value}' nao e numerico; "
            f"Telegram desligado para este time",
            file=sys.stderr,
        )
    return ""


def cmd_matrix() -> int:
    ids = team_ids()
    matrix = {"include": [{"index": i} for i in range(len(ids))]}
    mail, chat = emails(), chats()

    # Diagnostico SEMPRE em stderr: o stdout deste comando e' capturado inteiro
    # por `$(python3 teams.py matrix)` e vai para o $GITHUB_OUTPUT. Qualquer
    # linha extra aqui corrompe o valor e o `fromJSON` do matrix quebra.
    print(f"times={len(ids)} emails={len(mail)} chats={len(chat)}", file=sys.stderr)
    if len(mail) < len(ids):
        print(
            f"aviso: EMAIL_TO tem {len(mail)} entrada(s) para {len(ids)} time(s); "
            f"{len(ids) - len(mail)} time(s) ficarao sem email",
            file=sys.stderr,
        )
    elif len(mail) > len(ids):
        print(
            f"aviso: EMAIL_TO tem {len(mail)} entrada(s); "
            f"{len(mail) - len(ids)} sobrando(s) serao ignoradas",
            file=sys.stderr,
        )
    if len(chat) < len(ids):
        print(
            f"aviso: TELEGRAM_CHAT_ID tem {len(chat)} entrada(s) para {len(ids)} time(s)",
            file=sys.stderr,
        )

    print(json.dumps(matrix))
    return 0


def cmd_resolve(index_raw: str) -> int:
    if not index_raw.strip().isdigit():
        raise SystemExit(f"erro: indice invalido '{index_raw}'")
    index = int(index_raw)

    ids = team_ids()
    if index >= len(ids):
        raise SystemExit(
            f"erro: indice {index} fora da lista ({len(ids)} time(s) configurado(s))"
        )

    team_id = ids[index]
    email = at(emails(), index)
    chat = chat_ok(at(chats(), index))

    if email and "@" not in email:
        print(f"aviso: EMAIL_TO '{email}' nao parece um email valido", file=sys.stderr)

    # Vai para o $GITHUB_ENV: sobrescreve os secrets com o valor deste time.
    print(f"ESPN_TEAM_ID={team_id}")
    print(f"EMAIL_TO={email}")
    print(f"TELEGRAM_CHAT_ID={chat}")

    # Log legivel (stderr, para nao contaminar o $GITHUB_ENV).
    print(
        f"time #{index} -> team_id={team_id} | "
        f"email={email or '<nenhum>'} | telegram={chat or '<nenhum>'}",
        file=sys.stderr,
    )
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
