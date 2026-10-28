"""Painel de ligas e usuarios do NBA Fantasy.

Este app **nao roda o agente**: so faz CRUD no Postgres. O GitHub Actions
continua sendo o unico lugar que executa a analise, entao subir o painel nao
muda nada do que ja funciona.

Deploy: Vercel (Python runtime, ASGI). O Vercel procura a variavel `app` nos
arquivos de entrypoint (app.py, main.py, api/index.py...).
"""

from __future__ import annotations

import base64
import hmac
import os
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import db

BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE / "templates"))

app = FastAPI(title="NBA Fantasy Panel", docs_url=None, redoc_url=None)


# --------------------------------------------------------------------------
# Autenticacao
# --------------------------------------------------------------------------
def _creds() -> tuple[str, str]:
    return (os.getenv("PANEL_USER") or "").strip(), (os.getenv("PANEL_PASSWORD") or "")


@app.middleware("http")
async def require_auth(request: Request, call_next):
    """HTTP Basic Auth.

    Fail closed de proposito: a tabela tem e-mail de terceiros e os cookies do
    ESPN (ESPN_S2 da acesso de dono na liga). Sem credencial configurada o
    painel nao atende nada, em vez de abrir.
    """
    user, password = _creds()
    if not user or not password:
        return HTMLResponse(
            "<h1>Painel bloqueado</h1>"
            "<p>Defina as variaveis <code>PANEL_USER</code> e "
            "<code>PANEL_PASSWORD</code> no projeto.</p>",
            status_code=503,
        )

    header = request.headers.get("authorization", "")
    if header.startswith("Basic "):
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
            got_user, _, got_pass = decoded.partition(":")
        except Exception:
            got_user, got_pass = "", ""
        if (hmac.compare_digest(got_user, user)
                and hmac.compare_digest(got_pass, password)):
            return await call_next(request)

    return HTMLResponse(
        "Autenticacao necessaria.",
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="NBA Fantasy Panel"'},
    )


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def go(url: str, msg: str | None = None) -> RedirectResponse:
    if msg:
        url += ("&" if "?" in url else "?") + "msg=" + quote(msg)
    return RedirectResponse(url, status_code=303)


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    ctx.setdefault("msg", request.query_params.get("msg", ""))
    ctx.setdefault("err", request.query_params.get("err", ""))
    ctx.setdefault("filters", db.FILTERS)
    return templates.TemplateResponse(request, name, ctx)


@app.exception_handler(Exception)
async def boom(request: Request, exc: Exception):
    """Mostra o erro na tela em vez de um JSON cru — este app e para humanos."""
    return HTMLResponse(
        f"<h1>Erro</h1><pre>{type(exc).__name__}: {exc}</pre>"
        f"<p><a href='/users'>Voltar</a></p>",
        status_code=500,
    )


# --------------------------------------------------------------------------
# Saude
# --------------------------------------------------------------------------
@app.get("/health")
def health():
    """Confirma que o banco responde. Bom para testar o deploy sem abrir nada."""
    return {"ok": True, "db": db.ping()[:60]}


# --------------------------------------------------------------------------
# Raiz
# --------------------------------------------------------------------------
@app.get("/")
def root():
    return RedirectResponse("/users", status_code=303)


# --------------------------------------------------------------------------
# Usuarios
# --------------------------------------------------------------------------
@app.get("/users")
def users_list(request: Request, status: str = "active", league: int | None = None):
    rows = db.list_users(status, league)
    return render(request, "users.html", rows=rows, status=status,
                  league_id=league,
                  leagues=db.active_league_options())


@app.get("/users/new")
def user_new(request: Request):
    leagues = db.active_league_options()
    if not leagues:
        return go("/leagues/new", "Crie uma liga antes de cadastrar usuarios.")
    return render(request, "user_form.html", row=None, leagues=leagues)


@app.post("/users/new")
def user_create(
    league_id: int = Form(...),
    name: str = Form(...),
    espn_team_id: str = Form(...),
    email: str = Form(""),
    telegram_id: str = Form(""),
    notes: str = Form(""),
    is_active: str | None = Form(None),
):
    try:
        team = int(espn_team_id)
    except ValueError:
        return go("/users/new", f"team_id precisa ser numero (veio '{espn_team_id}').")
    try:
        db.create_user(league_id, name.strip(), team, email.strip(),
                       telegram_id.strip(), notes.strip(), is_active is not None)
    except Exception as exc:
        return go("/users/new", f"Falhou: {exc}")
    return go("/users", f"Usuario '{name}' criado.")


@app.get("/users/{user_id}/edit")
def user_edit(request: Request, user_id: int):
    row = db.get_user(user_id)
    if not row:
        return go("/users", "Usuario nao encontrado.")
    return render(request, "user_form.html", row=row,
                  leagues=db.active_league_options())


@app.post("/users/{user_id}/edit")
def user_update(
    user_id: int,
    league_id: int = Form(...),
    name: str = Form(...),
    espn_team_id: str = Form(...),
    email: str = Form(""),
    telegram_id: str = Form(""),
    notes: str = Form(""),
    is_active: str | None = Form(None),
):
    try:
        team = int(espn_team_id)
    except ValueError:
        return go(f"/users/{user_id}/edit", f"team_id precisa ser numero (veio '{espn_team_id}').")
    try:
        db.update_user(user_id, league_id, name.strip(), team, email.strip(),
                       telegram_id.strip(), notes.strip(), is_active is not None)
    except Exception as exc:
        return go(f"/users/{user_id}/edit", f"Falhou: {exc}")
    return go("/users?status=all", f"Usuario '{name}' atualizado.")


@app.post("/users/{user_id}/delete")
def user_delete(user_id: int):
    db.set_user_deleted(user_id, True)
    return go("/users", "Usuario excluido (soft delete — o historico fica).")


@app.post("/users/{user_id}/restore")
def user_restore(user_id: int):
    db.set_user_deleted(user_id, False)
    return go("/users?status=all", "Usuario restaurado.")


# --------------------------------------------------------------------------
# Ligas
# --------------------------------------------------------------------------
@app.get("/leagues")
def leagues_list(request: Request, status: str = "active"):
    rows = db.list_leagues(status)
    return render(request, "leagues.html", rows=rows, status=status)


@app.get("/leagues/new")
def league_new(request: Request):
    return render(request, "league_form.html", row=None)


@app.post("/leagues/new")
def league_create(
    name: str = Form(...),
    espn_league_id: str = Form(...),
    espn_swid: str = Form(...),
    espn_s2: str = Form(...),
):
    try:
        db.create_league(name.strip(), espn_league_id.strip(),
                         espn_swid.strip(), espn_s2.strip())
    except Exception as exc:
        return go("/leagues/new", f"Falhou: {exc}")
    return go("/leagues", f"Liga '{name}' criada.")


@app.get("/leagues/{league_id}/edit")
def league_edit(request: Request, league_id: int):
    row = db.get_league(league_id)
    if not row:
        return go("/leagues", "Liga nao encontrada.")
    return render(request, "league_form.html", row=row)


@app.post("/leagues/{league_id}/edit")
def league_update(
    league_id: int,
    name: str = Form(...),
    espn_league_id: str = Form(...),
    espn_swid: str = Form(...),
    espn_s2: str = Form(...),
    is_active: str | None = Form(None),
):
    try:
        db.update_league(league_id, name.strip(), espn_league_id.strip(),
                         espn_swid.strip(), espn_s2.strip(), is_active is not None)
    except Exception as exc:
        return go(f"/leagues/{league_id}/edit", f"Falhou: {exc}")
    return go("/leagues?status=all", f"Liga '{name}' atualizada.")


@app.post("/leagues/{league_id}/delete")
def league_delete(league_id: int):
    orphans = db.set_league_deleted(league_id, True)
    msg = "Liga excluida (soft delete)."
    if orphans:
        msg += f" Atencao: {orphans} usuario(s) desta liga continuam vivos."
    return go("/leagues", msg)


@app.post("/leagues/{league_id}/restore")
def league_restore(league_id: int):
    db.set_league_deleted(league_id, False)
    return go("/leagues?status=all", "Liga restaurada.")
