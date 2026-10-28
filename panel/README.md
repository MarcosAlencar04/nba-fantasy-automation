# Painel de ligas e usuarios

CRUD simples em Postgres, **separado do agente**. Este app nao roda analise, nao
notifica e nao le o ESPN — ele so mantem o cadastro de quem recebe os relatorios.

O GitHub Actions continua fazendo exatamente o que fazia. Nada do `nba-fantasy.yml`
muda enquanto o agente nao passar a ler do banco (fase seguinte).

```
panel/
  app.py            FastAPI + Jinja2 (server-rendered, sem build step)
  db.py             todas as queries
  schema.sql        duas tabelas, idempotente
  init_db.py        aplica o schema
  templates/        HTML
  requirements.txt
  vercel.json       preset fastapi + includeFiles dos templates
```

## Stack escolhida e por que

| Decisao | Motivo |
|---|---|
| **FastAPI + Jinja2** | roda no preset Python nativo do Vercel, sem build step e sem segunda linguagem |
| **Server-side rendering** | a `DATABASE_URL` nunca chega ao navegador |
| **Sem ORM** | 2 tabelas, ~90 linhas de SQL — SQLAlchemy seria mais peso que beneficio |
| **Soft delete** | historico preservado; permite reativar sem perder o cadastro |
| **Basic Auth** | um login so, sem tabela de sessoes nem fluxo de senha |

## Modelo de dados

```
leagues                          users
-------                          -----
id            PK                 id            PK
name                             league_id     FK -> leagues(id)
espn_league_id  UNIQUE*          name
espn_swid                        espn_team_id
espn_s2                          email
is_active                        telegram_id
created_at                       is_active
updated_at                       notes
deleted_at                       created_at / updated_at / deleted_at
                                 UNIQUE* (league_id, espn_team_id)
```

`*` os indices unicos sao **parciais** (`WHERE deleted_at IS NULL`): recriar uma
liga ou um time depois de excluido nao e bloqueado pelo registro antigo.

## Rodar local

```bash
cd panel
pip install -r requirements.txt

# com um Postgres local (docker é o mais rápido)
docker run -d --name pg -e POSTGRES_PASSWORD=test -e POSTGRES_DB=panel \
  -p 55432:5432 postgres:17-alpine

export DATABASE_URL='postgresql://postgres:test@127.0.0.1:55432/panel'
export PANEL_USER=admin
export PANEL_PASSWORD=troque-isto

python init_db.py                      # aplica o schema
uvicorn app:app --reload --port 8000   # abre em http://127.0.0.1:8000
```

---

# Deploy no Vercel

## Precisa de repositorio novo?

**Nao.** O Vercel aceita um subdiretorio como *Root Directory*. Um repo novo
significaria manter duas branches em sincronia sem nenhum ganho.

## Passo 1 — banco (Neon)

1. <https://neon.com> → signup (nao pede cartao)
2. **Create project** → nome `nba-fantasy`, regiao `AWS us-east-2` (ou a mais perto)
3. No painel do projeto, copie a **Connection string** — escolha a que tem
   `-pooler` no host:

   ```
   postgresql://usuario:senha@ep-xxx-pooler.us-east-2.aws.neon.tech/neondb?sslmode=require
   ```

   > O `-pooler` importa: o Vercel abre uma conexao por invocacao e o pooler
   > evita estourar o limite de conexoes do banco.

4. Aplique o schema. Duas opcoes:
   - **Neon SQL Editor** → cole o conteudo de `schema.sql` → Run
   - ou local: `DATABASE_URL='<a string acima>' python init_db.py`

## Passo 2 — projeto no Vercel

1. <https://vercel.com/new> → importe `MarcosAlencar04/nba-fantasy-automation`
2. **Configure Project**, com atencao a dois campos:

   | Campo | Valor |
   |---|---|
   | **Framework Preset** | `FastAPI` (o `vercel.json` ja declara) |
   | **Root Directory** | `panel` ← **critico**; sem isso o Vercel procura o entrypoint na raiz |

3. **Environment Variables** (Production **e** Preview):

   | Variavel | Valor |
   |---|---|
   | `DATABASE_URL` | a connection string do Neon (com `-pooler`) |
   | `PANEL_USER` | seu usuario |
   | `PANEL_PASSWORD` | senha forte (nao reaproveite a do e-mail) |

4. **Deploy**.

> Deploy sem `PANEL_USER`/`PANEL_PASSWORD` responde **503** de proposito. A tabela
> guarda e-mail de terceiros e os cookies `ESPN_S2` (que dao acesso de dono na
> liga), entao o padrao e fechado, nao aberto.

## Passo 3 — validar

```bash
# troque pela sua URL
curl -u SEU_USER:SUA_SENHA https://seu-projeto.vercel.app/health
# => {"ok":true,"db":"PostgreSQL 17..."}
```

Se `/health` devolver `ok:false` ou erro 500, a `DATABASE_URL` esta errada ou o
schema nao foi aplicado.

Depois abra `https://seu-projeto.vercel.app` e o navegador pede usuario/senha.

## Ordem de cadastro (importante)

```
1. Ligas  →  nova liga (nome + leagueId + SWID + S2)
2. Usuarios  →  novo usuario (escolher a liga)
```

O formulario de usuario exige uma liga existente; sem nenhuma, ele redireciona
para criar a liga primeiro.

## Pegadinhas

| Situacao | Causa / solucao |
|---|---|
| `404 NOT_FOUND` no deploy | Root Directory nao esta como `panel` |
| `TemplateNotFound` | `vercel.json` precisa do `includeFiles: "templates/**"` |
| `DATABASE_URL nao configurada` | variavel ausente no ambiente escolhido (Production ≠ Preview) |
| `too many connections` | faltou o `-pooler` no host |
| Conexao lenta na 1a request | Neon free faz scale-to-zero; acorda em ~500 ms |
| Esqueci a senha do painel | troque `PANEL_PASSWORD` e faca redeploy |

## Seguranca

- Basic Auth com `hmac.compare_digest` (comparacao em tempo constante)
- **Sem** modo aberto: sem credencial configurada, nada responde
- `ESPN_S2` e gravado no banco: quem tem acesso ao painel tem acesso de dono na
  liga. Nao compartilhe a senha do painel.
- `vercel.json` nao expoe `/docs` (desligado no `FastAPI(...)`)

## O que **nao** esta implementado ainda

- O agente continua lendo os secrets posicionais (`ESPN_TEAM_ID=7,9,11`).
  Ligar o Actions ao banco e a fase seguinte, e vai precisar de fallback para os
  secrets caso o banco esteja indisponivel.
- Sem historico de auditoria (quem alterou o que). O `updated_at` existe, o autor
  nao.
