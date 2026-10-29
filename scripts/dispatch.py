#!/usr/bin/env python3
"""Dispara o workflow do agente pela API do GitHub (workflow_dispatch).

Para que serve
--------------
O `schedule` do GitHub Actions e' best-effort e, medido neste repositorio,
derruba a maior parte das execucoes agendadas. Este script e' o gatilho
**confiavel**: qualquer agendador externo (cron-job.org, Cloudflare Worker,
cron de uma VPS, `systemd timer`) que consiga rodar um comando chama este
script — ou faz o mesmo POST por conta propria — e o run acontece na hora.

O disparo nao concorre com os crons: a trava em `state.py` garante um envio por
(tipo, dia, time). Se o cron do GitHub tambem passar no mesmo dia, ele encontra
a reserva e nao notifica de novo.

Token
-----
Precisa de um token com permissao de **Actions: write** no repositorio. Um
fine-grained PAT limitado a este repo e a essa permissao basta: mesmo vazado,
nao le codigo, nao le secrets e nao escreve em branch.

    export NBA_DISPATCH_TOKEN=github_pat_...

Uso
---
    python scripts/dispatch.py --mode morning
    python scripts/dispatch.py --mode gate --dry-run

Variaveis de ambiente reconhecidas (nesta ordem):
    NBA_DISPATCH_TOKEN, GH_TOKEN, GITHUB_TOKEN
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_REPO = "MarcosAlencar04/nba-fantasy-automation"
DEFAULT_WORKFLOW = "nba-fantasy.yml"
DEFAULT_REF = "development"

# Sobrescrevivel para GitHub Enterprise ou para apontar a um mock em teste.
API = (os.getenv("NBA_DISPATCH_API") or "https://api.github.com").rstrip("/")

TOKEN_ENVS = ("NBA_DISPATCH_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")


def find_token() -> tuple[str, str]:
    """Primeiro token encontrado no ambiente, com o nome da variavel.

    Devolver o nome junto evita a duvida de "qual token o script usou" quando
    ha varios exportados no shell.
    """
    for name in TOKEN_ENVS:
        value = (os.getenv(name) or "").strip()
        if value:
            return value, name
    return "", ""


def build_request(repo: str, workflow: str, ref: str, mode: str,
                  token: str) -> urllib.request.Request:
    url = f"{API}/repos/{repo}/actions/workflows/{workflow}/dispatches"
    # `inputs` vai explicito mesmo sendo o default do workflow: deixa o run
    # auto-explicativo na lista do Actions e nao depende de o default existir.
    body = json.dumps({"ref": ref, "inputs": {"mode": mode}}).encode("utf-8")
    return urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "nba-fantasy-dispatch",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )


def explain(status: int, detail: str) -> str:
    """Traduz o codigo HTTP no que a pessoa precisa corrigir."""
    if status == 204 or status == 200:
        return "disparado"
    if status == 401:
        return ("token invalido ou expirado (401). Gere outro PAT com "
                "Actions: write neste repositorio.")
    if status == 403:
        return (f"sem permissao (403). O token precisa de Actions: write. "
                f"{detail}")
    if status == 404:
        return ("repositorio ou workflow nao encontrado (404). Confira "
                "--repo/--workflow e se o token enxerga este repositorio.")
    if status == 422:
        return (f"workflow recusou o payload (422): {detail}. Normalmente e o "
                "`ref` — o arquivo precisa existir naquele branch.")
    return f"falhou com HTTP {status}: {detail}"


def dispatch(repo: str, workflow: str, ref: str, mode: str, token: str,
             dry_run: bool) -> int:
    req = build_request(repo, workflow, ref, mode, token)

    if dry_run:
        print(f"POST {req.full_url}")
        print(f"mode={mode} ref={ref}")
        print("(dry-run: nada foi enviado)")
        return 0

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            detail = resp.read().decode("utf-8", "replace")
            print(explain(resp.status, detail))
            return 0
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        print(f"erro: {explain(exc.code, detail)}", file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"erro de rede: {exc.reason}", file=sys.stderr)
        return 1
    except TimeoutError:
        # `socket.timeout` vira TimeoutError no Python 3.10+ e **nao** e' um
        # URLError: sem este ramo ele escaparia como traceback cru, e o
        # agendador externo so veria "exit 1" sem pista nenhuma.
        print("erro de rede: tempo esgotado (30s) ao falar com a API. "
              "O run pode ter sido criado mesmo assim — confira a aba Actions.",
              file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"erro de rede: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Dispara o workflow do agente pela API do GitHub.")
    ap.add_argument("--repo", default=os.getenv("NBA_DISPATCH_REPO") or DEFAULT_REPO,
                    help=f"owner/repo (padrao: {DEFAULT_REPO})")
    ap.add_argument("--workflow", default=DEFAULT_WORKFLOW,
                    help=f"arquivo do workflow (padrao: {DEFAULT_WORKFLOW})")
    ap.add_argument("--ref", default=os.getenv("NBA_DISPATCH_REF") or DEFAULT_REF,
                    help=f"branch (padrao: {DEFAULT_REF})")
    ap.add_argument("--mode", choices=["morning", "gate"], default="morning",
                    help="morning = relatorio completo; gate = so perto de jogo")
    ap.add_argument("--dry-run", action="store_true",
                    help="mostra o request sem enviar")
    opts = ap.parse_args(argv)

    token, token_env = find_token()
    if not token and not opts.dry_run:
        others = ", ".join(TOKEN_ENVS[1:])
        print(f"erro: nenhum token no ambiente. Defina {TOKEN_ENVS[0]} "
              f"(ou {others}) com permissao Actions: write.", file=sys.stderr)
        return 2

    if token_env:
        print(f"token: {token_env}", file=sys.stderr)

    return dispatch(opts.repo, opts.workflow, opts.ref, opts.mode, token,
                    opts.dry_run)


if __name__ == "__main__":
    sys.exit(main())
