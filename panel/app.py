"""Painel de ligas e usuarios do NBA Fantasy.

Este app **nao roda o agente**: so faz CRUD no Postgres. O GitHub Actions
continua sendo o unico lugar que executa a analise, entao subir o painel nao
muda nada do que ja funciona.

Deploy: Vercel (Python runtime, ASGI). O Vercel procura a variavel `app` nos
arquivos de entrypoint (app.py, main.py, api/index.py...).
"""

from __future__ import annotations

import base64
import hashlib
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


# Rota publica: quem tem a chave nao precisa de login. Ela valida uma chave
# propria (SUGGESTION_KEY) dentro do handler — ver `_key_ok`.
PUBLIC_FORM_PATH = "/sugestoes"

# Chave curta e' chave fraca: 8 caracteres sao quebraveis por tentativa. Como
# esta rota aceita POST de qualquer um, recusamos subir com menos que isso em
# vez de fingir que esta protegido.
MIN_KEY_LEN = 16

# A chave tambem pode chegar pela URL (`?k=`), entao restringimos o alfabeto ao
# que sobrevive ao trajeto sem escape: letras, numeros e `. _ ~ -` (o mesmo
# alfabeto que `secrets.token_urlsafe` gera). Sem isso, uma chave com `+`
# viraria espaco ao ser lida da query string e o link daria erro sem explicacao.
KEY_RE = re.compile(r"^[A-Za-z0-9._~-]+$")

# Depois de acertar a chave, ela fica num cookie assinado para a pessoa nao
# digitar de novo a cada visita. O token e' derivado da PROPRIA chave: trocar a
# SUGGESTION_KEY invalida todos os cookies emitidos antes, sem precisar de
# tabela de sessoes nem de um segundo segredo para assinar.
COOKIE_NAME = "nba_sug_key"
COOKIE_MAX_AGE = 60 * 60 * 24 * 30          # 30 dias
COOKIE_SALT = b"nba-fantasy-sugestoes-v1"


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
    """Compara a chave digitada com a do ambiente.

    `compare_digest` evita distinguir acerto de erro pelo tempo de resposta.
    Chave ausente ou invalida nunca autoriza (fail closed): um deploy esquecido
    nao abre um formulario publico de escrita no banco.
    """
    if _key_problem():
        return False
    return hmac.compare_digest((supplied or "").strip(), _suggestion_key())


def _cookie_token() -> str:
    """Token do cookie, derivado da chave vigente.

    Como o segredo e' a propria SUGGESTION_KEY, rotacionar a chave derruba de
    uma vez todos os acessos ja concedidos — e' o botao de "sair de todo mundo".
    """
    return hmac.new(_suggestion_key().encode("utf-8"), COOKIE_SALT,
                    hashlib.sha256).hexdigest()


def _cookie_ok(request: Request) -> bool:
    """A pessoa ja digitou a chave neste navegador?"""
    if _key_problem():
        return False
    got = request.cookies.get(COOKIE_NAME) or ""
    return bool(got) and hmac.compare_digest(got, _cookie_token())


def _is_https(request: Request) -> bool:
    """O navegador chegou por HTTPS?

    `request.url.scheme` sozinho nao basta: atras do proxy do Vercel o ASGI ve
    `http`. Se marcarmos o cookie como `secure` numa conexao que o navegador
    considera http, ele descarta o cookie em silencio e a pessoa entra num
    ciclo de pedir a chave para sempre.
    """
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    return proto == "https" or request.url.scheme == "https"


def _grant(resp: Response, request: Request) -> Response:
    """Grava o cookie de acesso no redirect de sucesso."""
    resp.set_cookie(
        COOKIE_NAME,
        _cookie_token(),
        max_age=COOKIE_MAX_AGE,
        httponly=True,          # o JS da pagina nao precisa (nem deve) ler isto
        samesite="lax",         # POST de outro site nao manda o cookie
        secure=_is_https(request),
        path=PUBLIC_FORM_PATH,  # nao vai junto em /users, /leagues, /health
    )
    return resp


def _is_public_path(raw_path: str) -> bool:
    """`/sugestoes` e tudo abaixo dela ficam fora do Basic Auth.

    Restrito ao prefixo com barra: `/sugestoesX` continua exigindo login, em vez
    de herdar a isencao por um `startswith` frouxo.
    """
    path = raw_path.rstrip("/") or "/"
    return path == PUBLIC_FORM_PATH or path.startswith(PUBLIC_FORM_PATH + "/")


@app.middleware("http")
async def require_auth(request: Request, call_next):
    """HTTP Basic Auth, com uma excecao: o formulario publico de sugestoes.

    Fail closed de proposito: a tabela tem e-mail de terceiros e os cookies do
    ESPN (ESPN_S2 da acesso de dono na liga). Sem credencial configurada o
    painel nao atende nada, em vez de abrir.

    A excecao vem ANTES da checagem de credencial porque o formulario tem
    autenticacao propria (a chave) e precisa continuar de pe mesmo se
    PANEL_USER estiver faltando.
    """
    if _is_public_path(request.url.path):
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
# Sugestoes — formulario PUBLICO (chave no lugar do login)
# --------------------------------------------------------------------------
# Fluxo: abrir `/sugestoes` mostra a tela de chave (parecida com login). Ao
# acertar, o cookie assinado e' gravado e a pessoa passa a ver o formulario
# direto nas proximas visitas. Um link com `?k=<chave>` tambem funciona: ele
# valida, grava o cookie e redireciona para a URL limpa, para a chave nao
# passe adiante quando a pessoa compartilhar o endereco.
def _page(name: str, request: Request, status_code: int = 200, **ctx) -> Response:
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def _key_page(request: Request, *, err: str = "",
              status_code: int = 200) -> Response:
    return _page("key_gate.html", request, status_code, err=err,
                 min_len=MIN_KEY_LEN)


def _public_page(request: Request, *, values: dict, err: str = "",
                 ok: bool = False, status_code: int = 200) -> Response:
    return _page(
        "public_suggestion.html", request, status_code,
        values=values,
        err=err,
        ok=ok,
        max_name=db.MAX_SUGGESTION_NAME,
        max_body=db.MAX_SUGGESTION_BODY,
    )


def _disabled(problem: str) -> HTMLResponse:
    """Formulario desligado por configuracao — vale a pena explicar o motivo."""
    print(f"sugestoes: formulario desligado — {problem}", file=sys.stderr)
    return HTMLResponse(
        "<h1>Formulario desligado</h1>"
        f"<p>{html.escape(problem)}</p>"
        "<p>Gere uma chave com:<br><code>python3 -c &quot;import secrets;"
        "print(secrets.token_urlsafe(24))&quot;</code></p>",
        status_code=503,
    )


@app.get(PUBLIC_FORM_PATH, response_class=HTMLResponse)
def suggestion_form(request: Request, k: str = "", ok: str = ""):
    """Tela de chave ou formulario, conforme o cookie deste navegador."""
    problem = _key_problem()
    if problem:
        return _disabled(problem)

    if k:
        # Link de acesso direto. Acertou: grava o cookie e sai da URL com a
        # chave, para ela nao ficar no historico nem ser repassada adiante.
        if _key_ok(k):
            return _grant(
                RedirectResponse(PUBLIC_FORM_PATH, status_code=303), request)
        return _key_page(request, err="Chave incorreta.", status_code=401)

    if not _cookie_ok(request):
        return _key_page(request)

    return _public_page(request, values={"name": "", "body": ""}, ok=ok == "1")


@app.post(PUBLIC_FORM_PATH, response_class=HTMLResponse)
def suggestion_login(request: Request, chave: str = Form("")):
    """Valida a chave digitada na tela de entrada."""
    problem = _key_problem()
    if problem:
        return _disabled(problem)
    if not _key_ok(chave):
        return _key_page(request, err="Chave incorreta. Confira e tente de novo.",
                         status_code=401)
    return _grant(RedirectResponse(PUBLIC_FORM_PATH, status_code=303), request)


@app.post(PUBLIC_FORM_PATH + "/enviar", response_class=HTMLResponse)
def suggestion_submit(
    request: Request,
    name: str = Form(""),
    body: str = Form(""),
    website: str = Form(""),
):
    if _key_problem():
        return HTMLResponse("<h1>Formulario desligado</h1>", status_code=503)
    if not _cookie_ok(request):
        # Cookie expirou (ou o navegador comeu). Pedimos a chave de novo com um
        # aviso — sem isso a pessoa veria so um erro generico e perderia o texto.
        return _key_page(
            request, status_code=401,
            err="Sua sessao expirou. Digite a chave para enviar de novo.")

    name, body = name.strip(), body.strip()
    values = {"name": name, "body": body}

    # Honeypot: o campo `website` fica escondido por CSS, entao um humano nunca
    # preenche. Se veio preenchido e' robo — respondemos como sucesso para nao
    # ensinar o que ajustar, mas nao gravamos nada.
    if website.strip():
        return go(f"{PUBLIC_FORM_PATH}?ok=1")

    if not name:
        return _public_page(request, values=values,
                            err="Informe seu nome.", status_code=400)
    if len(name) > db.MAX_SUGGESTION_NAME:
        return _public_page(
            request, values=values, status_code=400,
            err=f"O nome passa de {db.MAX_SUGGESTION_NAME} caracteres.")
    if not body:
        return _public_page(request, values=values,
                            err="Escreva sua sugestao.", status_code=400)
    if len(body) > db.MAX_SUGGESTION_BODY:
        return _public_page(
            request, values=values, status_code=400,
            err=f"A sugestao passa de {db.MAX_SUGGESTION_BODY} caracteres.")

    try:
        db.create_suggestion(name, body)
    except Exception as exc:
        # Erro de banco aparece na propria pagina: a pessoa nao tem (nem deve
        # ter) acesso ao resto do painel para entender um 500.
        print(f"sugestao: falhou ao gravar ({type(exc).__name__})",
              file=sys.stderr)
        return _public_page(
            request, values=values, status_code=500,
            err="Nao foi possivel salvar agora. Tente novamente em instantes.")

    # PRG: recarregar a pagina nao reenvia o formulario.
    return go(f"{PUBLIC_FORM_PATH}?ok=1")


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
