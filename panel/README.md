# Painel de ligas e usuarios

CRUD simples em Postgres, **separado do agente**. Este app nao roda analise, nao
notifica e nao le o ESPN — ele so mantem o cadastro de quem recebe os relatorios.

O GitHub Actions continua fazendo exatamente o que fazia. Nada do `nba-fantasy.yml`
muda enquanto o agente nao passar a ler do banco (fase seguinte).

Tem tambem um **formulario publico de sugestoes** (`/sugestoes`), que fica fora do
Basic Auth e usa uma chave propria — veja *Formulario publico de sugestoes*.

```
panel/
  app.py            FastAPI + Jinja2 (server-rendered, sem build step)
  db.py             todas as queries
  schema.sql        tres tabelas, idempotente
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

suggestions
-----------
id            PK
name                             <- digitado, 1..120
body                             <- digitado, 1..4000
read_at                          <- NULL = nao lida
created_at
deleted_at
```

`*` os indices unicos sao **parciais** (`WHERE deleted_at IS NULL`): recriar uma
liga ou um time depois de excluido nao e bloqueado pelo registro antigo.

`suggestions` nao tem FK nem relacao com `users`: o formulario e' publico, entao
quem escreve nao precisa estar cadastrado. Guardamos apenas o nome e o texto
digitados — sem IP, sem user-agent, sem cookie. O cookie de acesso ao formulario
(`nba_sug_key`) so guarda um token derivado da chave: nao identifica ninguem.

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
export SUGGESTION_KEY=$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')

python init_db.py                      # aplica o schema
uvicorn app:app --reload --port 8000   # abre em http://127.0.0.1:8000

# o formulario publico (pede a chave na tela de entrada) fica em:
#   http://127.0.0.1:8000/sugestoes
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
   | `SUGGESTION_KEY` | chave do formulario publico — veja abaixo |

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

## Formulario publico de sugestoes

O prefixo `/sugestoes` (e tudo abaixo dele) fica fora do Basic Auth. No lugar do
login, ele pede uma **chave**.

```
https://seu-projeto.vercel.app/sugestoes
```

Abrir esse endereco mostra uma tela de entrada com um campo de chave (mascarado,
com botao *mostrar*). Ao acertar, um cookie assinado e' gravado e a pessoa passa
a ver o formulario direto — so digita a chave uma vez por navegador.

Gerar a chave:

```bash
python3 -c 'import secrets;print(secrets.token_urlsafe(24))'
```

O link com a chave embutida tambem funciona, para mandar de uma vez so:

```
https://seu-projeto.vercel.app/sugestoes?k=<SUGGESTION_KEY>
```

Nesse caso a chave e' validada, o cookie e' gravado e o navegador e'
redirecionado para `/sugestoes` **sem** a chave — assim ela nao fica no historico
nem e' repassada junto quando a pessoa compartilhar o endereco.

### Regras da chave

O formulario se **desliga** (503, com o motivo na tela) se alguma falhar:

| Regra | Por que |
|---|---|
| minimo 16 caracteres | um POST aberto com chave de 4 digitos e' forcavel |
| so `A-Z a-z 0-9 . _ ~ -` | a chave pode ir na URL; `+` viraria espaco e o link daria erro sem explicacao |
| variavel ausente | fail closed, igual ao resto do painel |

### O cookie

| Atributo | Valor | Motivo |
|---|---|---|
| nome | `nba_sug_key` | — |
| valor | HMAC-SHA256 da chave | nao da para derivar a chave a partir dele |
| `HttpOnly` | sim | o JS da pagina nao precisa ler |
| `SameSite` | `Lax` | POST vindo de outro site nao carrega o cookie |
| `Path` | `/sugestoes` | nao acompanha `/users`, `/leagues`, `/health` |
| `Max-Age` | 30 dias | — |
| `Secure` | conforme o esquema | ver abaixo |

O token e' derivado da propria `SUGGESTION_KEY`, entao **trocar a chave derruba
todos os acessos ja concedidos** de uma vez. E' o botao de "sair de todo mundo",
sem tabela de sessoes e sem um segundo segredo para assinar.

`Secure` e' decidido por request (`X-Forwarded-Proto`, senao
`request.url.scheme`). Marcar `Secure` numa conexao que o navegador considera
http faz ele **descartar o cookie em silencio** — a pessoa entraria num ciclo de
pedir a chave para sempre.

### O que a rota faz e o que **nao** faz

- valida a chave com `hmac.compare_digest` (tempo constante)
- limita nome a 120 e sugestao a 4000 caracteres, no app **e** no `CHECK` do banco
- tem honeypot (`website`): robo recebe tela de sucesso e nada e' gravado
- devolve o texto digitado quando a validacao falha (ninguem perde o que escreveu)
- usa PRG (redirect apos POST): recarregar nao duplica o registro
- se o cookie expirar no meio de um envio, volta a tela de chave avisando — em vez
  de um 403 seco
- **nao** tem rate limit. Uma chave vazada permite encher a tabela; nesse caso
  troque a `SUGGESTION_KEY` (invalida tudo na hora) e exclua o lixo
- **nao** manda e-mail nem notificacao quando chega sugestao. Veja em
  `/suggestions` — o contador de nao lidas fica no topo

### Rotas

| Metodo | Caminho | Autenticacao | O que faz |
|---|---|---|---|
| `GET` | `/sugestoes` | cookie (ou `?k=`) | tela de chave ou formulario |
| `POST` | `/sugestoes` | a chave no corpo | valida a chave, grava o cookie |
| `POST` | `/sugestoes/enviar` | cookie | grava a sugestao |
| `GET` | `/suggestions` | Basic Auth | listagem do admin |

`/sugestoesX` **nao** herda a isencao: o match do prefixo exige a barra.

## O que **nao** esta implementado ainda

- Sem historico de auditoria (quem alterou o que). O `updated_at` existe, o autor
  nao.
- Sugestoes nao geram notificacao (e-mail/Telegram) — so aparecem no painel.

---

# Ligacao com o agente (GitHub Actions)

O agente le do banco. Nao ha sincronizacao manual: o que estiver cadastrado e
**ativo** no painel entra no proximo run.

```
painel (CRUD)  ->  users + leagues  ->  teams.py matrix  ->  N jobs do agente
```

## Como o agente consulta

```sql
SELECT u.name, u.espn_team_id, u.email, u.telegram_id,
       l.name, l.espn_league_id, l.espn_swid, l.espn_s2
  FROM users u JOIN leagues l ON l.id = u.league_id
 WHERE u.is_active AND u.deleted_at IS NULL
   AND l.is_active AND l.deleted_at IS NULL
 ORDER BY l.name, u.name
```

Consequencias praticas, todas controladas pelo painel:

| Acao no painel | Efeito no proximo run |
|---|---|
| `is_active = false` no usuario | sai do run (nao recebe nada) |
| `is_active = false` na liga | todos os usuarios dela saem |
| Excluir (soft delete) | sai do run; dados ficam para historico |
| Preencher `email` | passa a receber email |
| Preencher `telegram_id` | passa a receber no Telegram |
| Mudar `espn_team_id` | passa a analisar outro time |
| Mudar cookies da liga | vale para todos os usuarios daquela liga |

## Configuracao no GitHub

Um unico secret novo:

```
DATABASE_URL = postgresql://...-pooler...neon.tech/neondb?sslmode=require
```

Os antigos `ESPN_LEAGUE_ID` / `ESPN_SWID` / `ESPN_S2` deixam de ser necessarios —
vem da tabela `leagues`. `ESPN_TEAM_ID`, `EMAIL_TO` e `TELEGRAM_CHAT_ID` tambem
podem ser removidos (so servem ao modo legado sem banco).

## Testar sem rodar o workflow

```bash
export DATABASE_URL='postgresql://...'

python3 teams.py matrix        # lista quem entraria no run
python3 teams.py resolve 0     # mostra o que o job #0 receberia
```

`resolve` imprime no formato do `$GITHUB_ENV` (heredoc), que e' util para
conferir se os cookies chegaram inteiros:

```bash
python3 teams.py resolve 0 | grep -A1 ESPN_S2
```

## Saudacao personalizada

O `name` do usuario vai para o email como `Oi, Fulano!` (texto, HTML e Telegram).
Sem nome cadastrado, o bloco nao aparece.

## Ordem importa

Se uma liga estiver com `espn_s2` vazio, o `teams.py matrix` **aborta** nomeando a
liga e o usuario. E proposital: sem cookie a leitura do ESPN falharia de qualquer
forma, e o meio do run e' um lugar pior para descobrir.

O mesmo vale para banco vazio: `matrix` com zero usuarios e' erro explicito, nao
um matrix vazio que faria o Actions nao rodar nada em silencio.
