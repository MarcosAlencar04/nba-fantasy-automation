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
import html
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
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


# Rota publica: quem tem o link nao precisa de login. Ela valida uma chave
# propria (SUGGESTION_KEY) dentro do handler — ver `_key_ok`.
PUBLIC_FORM_PATH = "/sugestoes"

# Chave curta e' chave fraca: 8 caracteres sao quebraveis por tentativa. Como
# esta rota aceita POST de qualquer um, recusamos subir com menos que isso em
# vez de fingir que esta protegido.
MIN_KEY_LEN = 16

# A chave viaja na URL, entao restringimos o alfabeto ao que sobrevive ao
# trajeto sem escape: letras, numeros e `. _ ~ -` (o mesmo alfabeto que
# `secrets.token_urlsafe` gera). Sem isso, uma chave com `+` viraria espaco ao
# ser lida da query string e o link daria 403 sem explicacao obvia.
KEY_RE = re.compile(r"^[A-Za-z0-9._~-]+$")


def _suggestion_key() -> str:
    return (os.getenv("SUGGESTION_KEY") or "").strip()


def _key_problem() -> str:
    """Motivo pelo qual o formulario esta' desligado. Vazio = chave valida."""
    key = _suggestion_key()
    if not key:
        return "SUGGESTION_KEY nao configurada."
    if len(key) < MIN_KEY_LEN:
        return (f"SUGGESTION_KEY tem {len(key)} caracteres; "
                f"o minimo e' {MIN_KEY_LEN}.")
    if not KEY_RE.match(key):
        return ("SUGGESTION_KEY so pode ter letras, numeros e . _ ~ - "
                "(caracteres que sobrevivem a URL sem escape).")
    return ""


def _key_ok(supplied: str) -> bool:
    """Compara a chave do link com a do ambiente.

    `compare_digest` evita distinguir acerto de erro pelo tempo de resposta.
    Chave ausente ou invalida nunca autoriza (fail closed): um deploy esquecido
    nao abre um formulario publico de escrita no banco.
    """
    if _key_problem():
        return False
    return hmac.compare_digest((supplied or "").strip(), _suggestion_key())


@app.middleware("http")
async def require_auth(request: Request, call_next):
    """HTTP Basic Auth, com uma excecao: o formulario publico de sugestoes.

    Fail closed de proposito: a tabela tem e-mail de terceiros e os cookies do
    ESPN (ESPN_S2 da acesso de dono na liga). Sem credencial configurada o
    painel nao atende nada, em vez de abrir.

    A excecao vem ANTES da checagem de credencial porque o formulario tem
    autenticacao propria e precisa continuar de pe mesmo se PANEL_USER estiver
    faltando. O `rstrip("/")` garante que `/sugestoes/` caia na mesma regra em
    vez de vazar para o 401.
    """
    if (request.url.path.rstrip("/") or "/") == PUBLIC_FORM_PATH:
        return await call_next(request)

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
    """Mostra o erro na tela em vez de um JSON cru — este app e para humanos.

    Duas precaucoes:
    - `html.escape`: a mensagem de uma excecao pode conter texto que o usuario
      digitou (um erro do Postgres cita o valor da linha), e sem escape isso
      viraria HTML executavel;
    - na rota publica nao mostramos a mensagem nem o tipo: podem citar host do
      banco ou DSN. O detalhe vai para stderr, que so o dono do deploy le.
    """
    public = (request.url.path.rstrip("/") or "/") == PUBLIC_FORM_PATH
    print(f"erro em {request.url.path}: {type(exc).__name__}: {exc}",
          file=sys.stderr)
    if public:
        return HTMLResponse(
            "<h1>Algo deu errado</h1>"
            "<p>Tente novamente em instantes.</p>"
            f"<p><a href='{PUBLIC_FORM_PATH}'>Voltar ao formulario</a></p>",
            status_code=500,
        )
    return HTMLResponse(
        f"<h1>Erro</h1><pre>{html.escape(f'{type(exc).__name__}: {exc}')}</pre>"
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


# --------------------------------------------------------------------------
# Sugestoes — formulario PUBLICO (sem login, protegido por chave)
# --------------------------------------------------------------------------
def _public_page(request: Request, k: str, *, values: dict, err: str = "",
                 ok: bool = False, status_code: int = 200) -> Response:
    """Renderiza o formulario publico. `TemplateResponse` ja e' um Response."""
    return templates.TemplateResponse(
        request,
        "public_suggestion.html",
        {
            "k": k,
            "values": values,
            "err": err,
            "ok": ok,
            "max_name": db.MAX_SUGGESTION_NAME,
            "max_body": db.MAX_SUGGESTION_BODY,
        },
        status_code=status_code,
    )


@app.get(PUBLIC_FORM_PATH, response_class=HTMLResponse)
def suggestion_form(request: Request, k: str = "", ok: str = ""):
    """Formulario aberto para quem tem o link.

    Tres estados: chave nao configurada/invalida (503, formulario desligado),
    chave errada (403) e chave certa (o formulario).
    """
    problem = _key_problem()
    if problem:
        print(f"sugestoes: formulario desligado — {problem}", file=sys.stderr)
        return HTMLResponse(
            "<h1>Formulario desligado</h1>"
            f"<p>{problem}</p>"
            "<p>Gere uma chave com:<br><code>python3 -c &quot;import secrets;"
            "print(secrets.token_urlsafe(24))&quot;</code></p>",
            status_code=503,
        )
    if not _key_ok(k):
        return HTMLResponse(
            "<h1>Link invalido</h1>"
            "<p>Confira o endereco completo, incluindo o trecho depois de "
            "<code>?k=</code>.</p>",
            status_code=403,
        )
    return _public_page(request, k, values={"name": "", "body": ""},
                        ok=ok == "1")


@app.post(PUBLIC_FORM_PATH, response_class=HTMLResponse)
def suggestion_submit(
    request: Request,
    k: str = "",
    name: str = Form(""),
    body: str = Form(""),
    website: str = Form(""),
):
    if _key_problem():
        return HTMLResponse("<h1>Formulario desligado</h1>", status_code=503)
    if not _key_ok(k):
        return HTMLResponse("<h1>Link invalido</h1>", status_code=403)

    name, body = name.strip(), body.strip()
    values = {"name": name, "body": body}

    # Honeypot: o campo `website` fica escondido por CSS, entao um humano nunca
    # preenche. Se veio preenchido e' robo — respondemos como sucesso para nao
    # ensinar o que ajustar, mas nao gravamos nada.
    if website.strip():
        return go(f"{PUBLIC_FORM_PATH}?k={quote(k)}&ok=1")

    if not name:
        return _public_page(request, k, values=values,
                            err="Informe seu nome.", status_code=400)
    if len(name) > db.MAX_SUGGESTION_NAME:
        return _public_page(
            request, k, values=values, status_code=400,
            err=f"O nome passa de {db.MAX_SUGGESTION_NAME} caracteres.")
    if not body:
        return _public_page(request, k, values=values,
                            err="Escreva sua sugestao.", status_code=400)
    if len(body) > db.MAX_SUGGESTION_BODY:
        return _public_page(
            request, k, values=values, status_code=400,
            err=f"A sugestao passa de {db.MAX_SUGGESTION_BODY} caracteres.")

    try:
        db.create_suggestion(name, body)
    except Exception as exc:
        # Erro de banco aparece na propria pagina: a pessoa nao tem (nem deve
        # ter) acesso ao resto do painel para entender um 500.
        print(f"sugestao: falhou ao gravar ({type(exc).__name__})",
              file=sys.stderr)
        return _public_page(
            request, k, values=values, status_code=500,
            err="Nao foi possivel salvar agora. Tente novamente em instantes.")

    # PRG: recarregar a pagina nao reenvia o formulario.
    return go(f"{PUBLIC_FORM_PATH}?k={quote(k)}&ok=1")


# --------------------------------------------------------------------------
# Sugestoes — listagem do admin (autenticada pelo middleware)
# --------------------------------------------------------------------------
def _suggestion_filter(status: str) -> str:
    """So aceita chaves conhecidas, senao volta para 'new'."""
    return status if status in db.SUGGESTION_FILTERS else "new"


@app.get("/suggestions")
def suggestions_list(request: Request, status: str = "new"):
    status = _suggestion_filter(status)
    return render(request, "suggestions.html",
                  rows=db.list_suggestions(status),
                  status=status,
                  unread=db.count_suggestions_unread(),
                  filters=db.SUGGESTION_FILTERS)


@app.post("/suggestions/{suggestion_id}/read")
def suggestion_mark_read(suggestion_id: int, back: str = Form("new")):
    db.set_suggestion_read(suggestion_id, True)
    return go(f"/suggestions?status={_suggestion_filter(back)}",
              "Marcada como lida.")


@app.post("/suggestions/{suggestion_id}/unread")
def suggestion_mark_unread(suggestion_id: int, back: str = Form("read")):
    db.set_suggestion_read(suggestion_id, False)
    return go(f"/suggestions?status={_suggestion_filter(back)}",
              "Marcada como nova.")


@app.post("/suggestions/{suggestion_id}/delete")
def suggestion_delete(suggestion_id: int, back: str = Form("new")):
    db.set_suggestion_deleted(suggestion_id, True)
    return go(f"/suggestions?status={_suggestion_filter(back)}",
              "Sugestao excluida (soft delete).")


@app.post("/suggestions/{suggestion_id}/restore")
def suggestion_restore(suggestion_id: int):
    db.set_suggestion_deleted(suggestion_id, False)
    return go("/suggestions?status=all", "Sugestao restaurada.")
