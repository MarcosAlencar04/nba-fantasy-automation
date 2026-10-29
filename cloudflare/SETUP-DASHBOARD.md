# Configurar tudo pelo dash.cloudflare.com

Dá para fazer **sem instalar nada**: colar o código, criar os segredos, adicionar
os crons e ver os logs — tudo no navegador.

> **Escolha uma fonte de verdade.** Este repositório também tem `wrangler.toml`.
> Se um dia você rodar `wrangler deploy`, os crons e as variáveis do
> `wrangler.toml` **substituem** o que está no dashboard. Não misture: ou você
> administra pelo dashboard, ou pelo Wrangler. Veja [Divergência](#divergência-a-pegadinha-que-morde-depois)
> no fim.

## Antes de começar: 2 limites do plano Free

| Limite | Free | Nosso uso |
|---|---|---|
| Cron Triggers por **conta** | **5** | 4 — sobra 1 |
| CPU time por Cron Trigger | **10 ms** | sobra, porque `fetch()` não conta (só o tempo de CPU do JS) |

O segundo limite é o que mais assusta e o que menos importa aqui: o worker da
cron só faz `JSON.stringify` e um `fetch`. O tempo esperando a API do GitHub
**não** é CPU. Mas é a razão para não engordar esse worker depois.

## Passo 1 — Token no GitHub

1. <https://github.com/settings/personal-access-tokens/new>
2. **Repository access** → *Only select repositories* → `nba-fantasy-automation`
3. **Permissions** → *Repository permissions* → **Actions: Read and write**
   (só isso; `Contents` pode ficar *No access*)
4. **Expiration**: 90 dias
5. Copie o `github_pat_...`

## Passo 2 — Criar o Worker

1. <https://dash.cloudflare.com/?to=/:account/workers-and-pages>
2. **Create** → **Start with Hello World** (não escolha template nem Git)
3. Nome: `nba-fantasy-dispatch`
4. **Deploy** (aceite o código de exemplo por enquanto)
5. Depois do deploy, **Edit code** → apague tudo → cole o conteúdo de
   [`src/index.js`](src/index.js) → **Deploy**

O arquivo é único e autocontido de propósito: o editor do dashboard não lida
bem com múltiplos módulos.

**O editor do dashboard fica desabilitado** ("managed by Wrangler") se o worker
foi criado por `wrangler deploy`. Como você está começando pelo dashboard, isso
não acontece.

## Passo 3 — Variáveis e segredos

**Workers & Pages** → `nba-fantasy-dispatch` → **Settings** → **Variables and
Secrets** → **Add**.

Crie **5 entradas** — repare no tipo de cada uma:

| Nome | Tipo | Valor |
|---|---|---|
| `GH_TOKEN` | **Secret** | `github_pat_...` |
| `TRIGGER_KEY` | **Secret** | gere abaixo |
| `GH_REPO` | Text | `MarcosAlencar04/nba-fantasy-automation` |
| `GH_WORKFLOW` | Text | `nba-fantasy.yml` |
| `GH_REF` | Text | `development` |

Para gerar a `TRIGGER_KEY`:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(24))"
```

Clique em **Deploy** para aplicar. Segredo no dashboard é escrito uma vez e
depois só aparece como `(hidden)` — nem você relê. Se perder, sobrescreve.

> `TRIGGER_KEY` é opcional. Sem ela (ou com menos de 16 caracteres) o
> `/dispatch` responde **503** de propósito: um deploy esquecido não vira um
> botão público que gasta minutos do Actions. A cron continua funcionando.

## Passo 4 — Cron Triggers

**Settings** → **Triggers** → **Cron Triggers** → **Add** — um por vez, nesta
ordem (todos em **UTC**; o Cloudflare não tem campo de timezone):

| Expressão | UTC | Brasília | Modo |
|---|---|---|---|
| `7 13 * * *` | 13:07 | 10:07 | relatório da manhã |
| `37 13 * * *` | 13:37 | 10:37 | tentativa 2 |
| `7 14 * * *` | 14:07 | 11:07 | tentativa 3 |
| `5,35 * * * *` | cada 30 min | — | pré-jogo |

As três tentativas da manhã existem porque o cron do GitHub derruba a maioria
das execuções. A trava no banco (`state.py`) garante **um** e-mail por dia: as
tentativas extras saem sem notificar.

> **Atenção ao plano Free: o limite é 5 crons por conta, não por worker.** Se
> você já tem outro worker com crons, este projeto pode não caber.

## Passo 5 — Observabilidade

**Settings** → **Observability** → **Enable**.

Sem isso, uma falha aparece só como "1 erro" sem contexto. Com isso, **Logs**
mostra a linha que o worker imprime:

```
cron '7 13 * * *' -> mode 'morning'
disparado: .../nba-fantasy.yml@development mode=morning (HTTP 204)
```

## Passo 6 — Testar

**Sem esperar a cron.** No navegador:

```
https://nba-fantasy-dispatch.SEU-SUBDOMINIO.workers.dev/health
```

Deve responder:

```json
{
  "ok": true,
  "worker": "nba-fantasy-dispatch",
  "repo": "MarcosAlencar04/nba-fantasy-automation",
  "workflow": "nba-fantasy.yml",
  "ref": "development",
  "token": "configurado",
  "crons": ["7 13 * * *", "37 13 * * *", "7 14 * * *", "5,35 * * * *"]
}
```

Se vier `"token": "AUSENTE"`, o Passo 3 não foi aplicado (esqueceu o **Deploy**
no fim do drawer).

Agora o disparo real:

```
https://nba-fantasy-dispatch.SEU-SUBDOMINIO.workers.dev/dispatch?mode=morning&key=SUA_TRIGGER_KEY
```

Resposta esperada: `{"ok": true, "mode": "morning", ...}`. Em segundos aparece um
run na aba **Actions** do GitHub com evento `workflow_dispatch`.

Na 1ª vez, se o job `agente` logar:

```
ja enviado hoje (slot 'morning', time 7); esta execucao e' uma retentativa do cron
```

está **certo** — outra execução (a cron do GitHub) já entregou naquele dia.

## Passo 7 — Conferir as execuções da cron

**Workers & Pages** → `nba-fantasy-dispatch` → **Settings** → **Triggers** →
**View events**.

Guarda as **100 últimas** invocações do cron, com horário previsto, horário real
e resultado. É aqui que você vê se um dia a cron não rodou.

> Nas primeiras ~30 min depois de criar o worker (ou trocar o nome), essa lista
> pode estar vazia mesmo com a cron já ativa. Não é problema.

## Problemas comuns

| Sintoma | Causa provável |
|---|---|
| `/health` com `"token": "AUSENTE"` | segredo não aplicado — faltou **Deploy** no drawer |
| `/dispatch` responde 503 | `TRIGGER_KEY` vazia ou com menos de 16 caracteres |
| `/dispatch` responde 401 | chave errada (o `key=` é case-sensitive) |
| `/dispatch` responde 502 com "token invalido (401)" | PAT expirou ou sem **Actions: Read and write** |
| `/dispatch` responde 502 com "404" | `GH_REPO`/`GH_WORKFLOW` errados, ou o PAT não enxerga o repositório |
| `/dispatch` responde 502 com "422" | `GH_REF` não existe — precisa ser `development` |
| Cron não dispara | confira em **Cron Triggers** se os 4 estão listados; mudanças levam até 15 min para propagar |
| Cron "rodou" mas nada no GitHub | veja **View events** → o resultado foi exceção; abra **Logs** |

## Divergência: a pegadinha que morde depois

O `wrangler.toml` deste repositório declara os mesmos 4 crons e as mesmas 3
variáveis. Se você rodar `wrangler deploy` algum dia:

- os crons do dashboard são **substituídos** pelos do `wrangler.toml`;
- os segredos (`GH_TOKEN`, `TRIGGER_KEY`) **sobrevivem** — `wrangler deploy`
  não apaga secret que já existe;
- as variáveis textuais voltam para os valores do arquivo.

Consequência prática: se você editar um cron no dashboard e depois rodar
`wrangler deploy`, a edição se perde. Para evitar confusão, escolha um caminho:

| Você quer | Faça |
|---|---|
| Zero instalação | só dashboard; **não** rode `wrangler deploy` |
| Tudo versionado | `wrangler deploy`; não edite crons no dashboard |
