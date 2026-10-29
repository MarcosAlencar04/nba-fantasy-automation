# Cron confiavel do NBA Fantasy (Cloudflare Workers)

Este Worker faz **uma coisa só**: no horário certo, chama a API do GitHub para
disparar o workflow `nba-fantasy.yml`. Ele não analisa nada, não fala com o ESPN
e não guarda dados.

## Por que existe

O `schedule` do GitHub Actions é *best-effort*. A documentação avisa:

> The `schedule` event can be delayed during periods of high loads of GitHub
> Actions workflow runs. **High load times include the start of every hour.** If
> the load is sufficiently high enough, **some queued jobs may be dropped**.

Medido neste repositório: **6 execuções agendadas em ~48h**, quando o cron `*/30`
deveria produzir ~96. Aproximadamente 6% sobrevivem. O relatório da manhã não
saiu em dois dias seguidos.

Um Cron Trigger do Cloudflare é pontual e não é descartado em horário de pico.

## Como a duplicação é evitada

O GitHub e o Cloudflare **podem** disparar no mesmo minuto. O workflow roda duas
vezes, mas só uma notifica — a trava em `state.py` (tabela
`agent_notifications`) reserva `(tipo, dia, time)` no Postgres antes de enviar.

| Origem | Slot |
|---|---|
| Cron da manhã | `morning` |
| Pré-jogo | `pre:<hora do jogo em UTC>`, gerado pelo `gate.py` |

O slot do pré-jogo é derivado da hora do jogo, então as duas fontes calculam
**o mesmo** slot e a segunda encontra a reserva.

## Crons

Todos em UTC (o Cloudflare não tem campo de timezone).

| Cron | UTC | BRT | Modo |
|---|---|---|---|
| `7 13 * * *` | 13:07 | 10:07 | `morning` |
| `37 13 * * *` | 13:37 | 10:37 | `morning` |
| `7 14 * * *` | 14:07 | 11:07 | `morning` |
| `5,35 * * * *` | cada 30 min | — | `gate` |

Três tentativas na manhã porque até o Cloudflare pode ter segundos de atraso e o
GitHub pode estar com fila. A trava impede e-mail triplo.

`5,35` casa com o `PREGAME_LEAD_MIN=30` do gate: cada janela de jogo recebe
exatamente um tick.

## Rotas HTTP

| Rota | Auth | O que faz |
|---|---|---|
| `GET /` ou `/health` | nenhuma | estado do worker (nunca mostra o token) |
| `GET /dispatch?mode=morning&key=...` | `TRIGGER_KEY` | dispara na hora, útil para testar |

Sem `TRIGGER_KEY` configurada o `/dispatch` responde **503** (fail closed): um
deploy esquecido não pode virar um botão público que gasta minutos de Actions.

## Como publicar

Dois caminhos, e **escolha um**:

| Caminho | Quando usar | Onde ficam os crons |
|---|---|---|
| **Dashboard** (nada para instalar) | quer resolver pelo navegador | no dashboard |
| **Wrangler** (`npx wrangler deploy`) | quer tudo versionado no Git | no `wrangler.toml` |

O passo a passo do dashboard está em **[`SETUP-DASHBOARD.md`](SETUP-DASHBOARD.md)**.
O caminho por Wrangler:

```bash
npm install
npx wrangler login
npx wrangler secret put GH_TOKEN       # e TRIGGER_KEY, se usar /dispatch
npx wrangler deploy
```

> ⚠️ Rodar `wrangler deploy` **substitui** os crons que estiverem no dashboard
> pelos do `wrangler.toml`. Os segredos sobrevivem. Não misture os dois caminhos.


## Testes

```bash
cd cloudflare
npm test        # 32 asserts, sem rede
```

O `node --test` usa só builtins do Node, então `npm install` **não** é necessário
para rodar os testes — só para `wrangler dev`/`deploy`.

## Arquivos

```
cloudflare/
  wrangler.toml          crons + vars (nada de segredo aqui)
  package.json           wrangler como devDependency
  src/index.js           handler scheduled + fetch
  test/index.test.js     testes com fetch stubado
  SETUP-DASHBOARD.md     passo a passo sem instalar nada
```
