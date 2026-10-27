# NBA Fantasy Agent (ESPN, H2H Points)

Agente que analisa seu elenco de NBA Fantasy no ESPN e manda o resumo por Telegram e/ou email.
Roda localmente e de graça (o agendamento está na seção 6). Ele só recomenda: você aplica no app.

## 1. Instalar

```bash
python -m venv venv
source venv/bin/activate
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

## 6. Agendar de graca na nuvem (GitHub Actions)

Roda 2x por dia sem depender da sua maquina e sem custo:

1. **10h (Brasilia)**: relatorio completo.
2. **30 min antes do primeiro jogo**: so roda se aquele momento estiver na janela
   (`gate.py` verifica o horario do primeiro jogo na API do ESPN e libera a execucao).

O workflow e `.github/workflows/nba-fantasy.yml`. Ele usa dois cron triggers:

| Cron (UTC) | Quando | O que faz |
|---|---|---|
| `0 13 * * *` | 10h BRT | roda direto |
| `*/30 * * * *` | a cada 30 min | `gate.py` decide se e hora |

### Configurar (uma vez, no GitHub)

1. **Settings > Secrets and variables > Actions > Secrets**: adicione
   `ESPN_LEAGUE_ID`, `ESPN_SWID`, `ESPN_S2`, `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`,
   `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `EMAIL_FROM`, `EMAIL_TO`.
2. **Aba Variables**: crie `ROSTER_CSV` com seu elenco (um jogador por linha, mesma
   sintaxe do `roster.csv`) e, se quiser, `ADDS_USED`.
3. **Actions > NBA Fantasy > Run workflow** com `mode = morning` para testar.

O relatorio fica como artifact de cada execucao (7 dias).

### Por que nao no Cloudflare

Testado e descartado, no plano gratuito:

| Limite do Free | Valor | Efeito |
|---|---|---|
| CPU por invocacao | **10 ms** | o parsing de 1 game log (~78 KB) ja consome boa parte disso |
| Subrequests por invocacao | 50 | seriam necessarios ~260 requests para elenco + free agents |
| Python Workers | Pyodide/WASM | sem `scipy` (otimizador de lineup) e sem sockets (`nba_api`/`espn_api`) |
| Containers | **plano pago** | e o unico runtime com CPython de verdade |

### Fonte de dados

O `stats.nba.com` bloqueia IPs de datacenter, entao o agente detecta o ambiente
(`--source auto`) e, quando bloqueado, passa a usar a **API publica do ESPN**
(game logs, calendario e, com cookies, free agents/lesoes). Diferencas nessa fonte:

- Rebotes ofensivos sao estimados em 24% do total (o gamelog do ESPN nao separa OREB/DREB);
- Ritmo/defesa dos times (fator de ambiente) fica desligado;
- O universo analisado e o da sua liga + free agents (precisa de `--espn`).

Para forcar uma fonte: `--source nba` ou `--source espn`.

### Rodar na sua maquina (alternativa offline)

```bash
python scheduler.py --plan          # mostra os proximos horarios
python scheduler.py --once morning  # roda agora, uma vez
python scheduler.py                 # loop continuo (deixe rodando)
```

O estado do dia fica em `.cache/scheduler_state.json`: reiniciar o processo nao repete o
envio, e uma falha e tentada ate 3 vezes. Para manter sempre ativo no Linux, use o unit em
`systemd/nba-fantasy.service` (`systemctl --user enable --now nba-fantasy`).

