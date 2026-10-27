# NBA Fantasy Agent (ESPN, H2H Points)

## 1. Instalar

```bash
pip install -r requirements.txt
```

## 2. Montar o roster

Edite o `roster.csv`, um jogador por linha:

```
Lebron James, SF/PF
Nikola Jokic, C
Ayo Dosunmu, SG/PG, DTD
```

A 3a coluna (status) e opcional: `OUT`, `DTD` ou `IR`.

## 3. Rodar

```bash
python nba_fantasy_agent.py
```

Opcoes uteis:

```bash
python nba_fantasy_agent.py --adds-used 2
python nba_fantasy_agent.py --candidates candidates.csv
python nba_fantasy_agent.py --refresh
```

- `--adds-used N`: quantos dos 5 adds do matchup voce ja gastou.
- `--candidates arquivo.csv`: free agents especificos para avaliar (um nome por linha).
- `--refresh`: ignora o cache e baixa os dados de novo.

O relatorio e salvo em `reports/relatorio_AAAA-MM-DD.md`.

## 4. Modo ESPN (free agents reais, lesoes e trocas)

1. Copie `env.example.txt` para um arquivo chamado `.env`.
2. `ESPN_LEAGUE_ID`: o numero `leagueId=...` na URL da sua liga.
3. `ESPN_SWID` e `ESPN_S2`: no navegador logado no espn.com, aperte F12, va em Application (Chrome) ou Storage (Firefox), depois Cookies, e copie os valores.
4. Rode:

```bash
python nba_fantasy_agent.py --espn
```

Os cookies expiram de tempos em tempos. Se der erro de autenticacao, copie de novo.

## 5. Telegram e/ou email (opcional)

Preencha as variaveis no `.env` e escolha o canal na execucao:

```bash
python nba_fantasy_agent.py --espn --telegram   # so Telegram
python nba_fantasy_agent.py --espn --email      # so email
python nba_fantasy_agent.py --espn --notify     # os dois
```

Telegram: `TELEGRAM_TOKEN` e `TELEGRAM_CHAT_ID` (crie o bot com o `@BotFather`).

Email (SMTP): `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `EMAIL_FROM` e `EMAIL_TO`
(destinatarios separados por virgula). No Gmail, use `smtp.gmail.com`, porta 587 e uma
**app password** gerada na conta Google - a senha normal nao funciona. A porta 465 usa SSL.

A mensagem e a mesma nos dois canais: o resumo do dia (lineup, vendas, adds e trocas),
nao o relatorio completo. Se nao houver nada a sinalizar, nada e enviado.

## 6. Automatizar

Linux/Mac, todo dia as 10h (cron):

```
0 10 * * * cd /caminho/da/pasta && python nba_fantasy_agent.py --espn --notify
```

Windows: use o Agendador de Tarefas.

Rode na sua maquina: o stats.nba.com costuma bloquear IPs de servidores (GitHub Actions, por exemplo).

## 7. Cloudflare (Container + Worker)

Roda na nuvem sem depender da sua maquina. Precisa do plano **Workers Paid** (US$ 5/mes).
O Worker cuida do agendamento; o agente roda dentro de um Container Python.

Como funciona:

- `crons = ["0 13 * * *"]` no `wrangler.toml`: 10h em Brasilia = 13h UTC. Cron do Workers e sempre UTC.
- Depois da execucao da manha, o Durable Object consulta o calendario (ESPN) e agenda um alarme para
  **30 minutos antes do primeiro jogo do dia**. Se nao houver jogo, nenhum alarme e criado.
- O Container sobe, roda `nba-fantasy-agent.py --espn --notify` e devolve o relatorio em JSON.

Arquivos:

| Arquivo | Papel |
|---|---|
| `Dockerfile` | Imagem Python com `requirements.txt`, o agente e o `container/server.py`. |
| `container/server.py` | Servidor HTTP minimo com `/health` e `/run`. |
| `src/index.ts` | Worker (cron + trigger manual) e Durable Object (alarme, ciclo de vida do Container). |
| `wrangler.toml` | Configuracao de cron, container e binding. |

### Subir

```bash
npm install
npx wrangler login
```

Segredos (os mesmos nomes do `.env`):

```bash
npx wrangler secret put ESPN_LEAGUE_ID
npx wrangler secret put ESPN_SWID
npx wrangler secret put ESPN_S2
npx wrangler secret put TELEGRAM_TOKEN
npx wrangler secret put TELEGRAM_CHAT_ID
npx wrangler secret put SMTP_HOST
npx wrangler secret put SMTP_PORT
npx wrangler secret put SMTP_USER
npx wrangler secret put SMTP_PASS
npx wrangler secret put EMAIL_FROM
npx wrangler secret put EMAIL_TO
npx wrangler secret put RUN_TOKEN      # protege o disparo manual
```

O roster pode vir do `roster.csv` da imagem (exige redeploy a cada mudanca) ou da variavel
`ROSTER_CSV`, que sobrevive sem rebuild:

```toml
[vars]
ROSTER_CSV = """
Nikola Jokic, C
LeBron James, SF/PF
Ayo Dosunmu, SG/PG, DTD
"""
```

Deploy e disparo manual:

```bash
npx wrangler deploy
curl -X POST "https://nba-fantasy.SEU-SUBDOMINIO.workers.dev/run?token=SEU_RUN_TOKEN&notify=0"
```

Logs:

```bash
npx wrangler tail
```

### Antes de confiar, valide

1. **`stats.nba.com` a partir do Cloudflare.** Ele bloqueia varios IPs de datacenter. Se o relatorio
   sair vazio, use o deploy local (secao 6).
2. **SMTP a partir do Container.** Se o envio falhar, troque o email por uma API HTTP (Resend,
   SendGrid) dentro de `send_email`.
3. **Tempo de execucao.** Sem cache local, cada execucao baixa tudo de novo (30-90 s).

Alternativa mais barata e sem esses riscos: manter o Python na sua maquina (secao 6) e usar o
Worker so para agendar/disparar.
