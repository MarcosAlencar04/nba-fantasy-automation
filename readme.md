# NBA Fantasy Agent (ESPN, H2H Points)

Agente que analisa seu elenco de NBA Fantasy no ESPN e manda o resumo por Telegram e/ou email.
Roda localmente e de graça (o agendamento está na seção 6). Ele só recomenda: você aplica no app.

## 1. Instalar

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 2. Elenco (100% da API do ESPN)

Nao existe mais arquivo de roster: o agente le o seu elenco direto da API do ESPN.
Basta ter `ESPN_LEAGUE_ID` e `ESPN_TEAM_ID` no `.env` (veja a secao 4).

Onde achar o `ESPN_TEAM_ID`: na URL do seu time (`...&teamId=3`). Se voce errar o numero,
o agente lista os IDs validos da liga.

Posicoes e status de lesao (`OUT`/`DTD`) tambem vem do ESPN (`injuryStatus`).

## 3. Rodar

```bash
python nba-fantasy-agent.py --notify
```

Opcoes uteis:

```bash
python nba-fantasy-agent.py --notify --adds-used 2
python nba-fantasy-agent.py                  # sem notificar, so imprime
python nba-fantasy-agent.py --refresh        # ignora o cache
python nba-fantasy-agent.py --source espn    # forca a fonte ESPN
```

- `--adds-used N`: quantos dos 5 adds do matchup voce ja gastou.
- `--refresh`: ignora o cache e baixa os dados de novo.
- `--source`: `auto` (detecta), `nba` ou `espn`.

O relatorio e salvo em `reports/relatorio_AAAA-MM-DD.md`.

## 4. Credenciais do ESPN (obrigatorio)

1. Copie `.env.example` para `.env`.
2. `ESPN_LEAGUE_ID`: o numero `leagueId=...` na URL da sua liga.
3. `ESPN_TEAM_ID`: o numero `teamId=...` na URL do seu time.
4. `ESPN_SWID` e `ESPN_S2`: no navegador logado no espn.com, aperte F12, va em Application (Chrome) ou Storage (Firefox), depois Cookies, e copie os valores.
5. Rode:

```bash
python nba-fantasy-agent.py --notify
```

Na primeira execucao ele imprime `Seu time: <nome> (id N, 14 jogadores)`. Se o time nao for
identificado, confira o `ESPN_TEAM_ID` (o agente lista os IDs validos).

Os cookies expiram de tempos em tempos. Se der erro de autenticacao, copie de novo.

## 5. Telegram e/ou email (opcional)

Preencha as variaveis no `.env` e escolha o canal na execucao:

```bash
python nba-fantasy-agent.py --telegram   # so Telegram
python nba-fantasy-agent.py --email      # so email
python nba-fantasy-agent.py --notify     # os dois
```

Telegram: `TELEGRAM_TOKEN` e `TELEGRAM_CHAT_ID` (crie o bot com o `@BotFather`).

Email (SMTP): `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `EMAIL_FROM` e `EMAIL_TO`
(destinatarios separados por virgula). No Gmail, use `smtp.gmail.com`, porta 587 e uma
**app password** gerada na conta Google - a senha normal nao funciona. A porta 465 usa SSL.

A mensagem e a mesma nos dois canais: o resumo do dia (lineup, vendas, adds e trocas),
nao o relatorio completo. Se nao houver nada a sinalizar, nada e enviado.

O **email** vai em HTML com **tema escuro**: cabecalho com o nome do time (e o logo, quando a
liga tem um), faixa com FP projetado / adds restantes / nivel de reposicao, escalacao na ordem
dos slots com foto do jogador, **disputa por vaga** (quem entra e quem sai quando ha mais de 10
elegiveis), **IR e status** (com ate 3 opcoes de drop para ativar), **sinais da semana** com
alvos de compra nos elencos rivais, add/drop e trocas com **risco de recusa**. Versao em texto
puro vai no mesmo email para clientes que nao renderizam HTML.

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
   `ESPN_LEAGUE_ID`, `ESPN_TEAM_ID`, `ESPN_SWID`, `ESPN_S2`, `TELEGRAM_TOKEN`,
   `TELEGRAM_CHAT_ID`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`,
   `EMAIL_FROM`, `EMAIL_TO`.
2. **Aba Variables**: opcionalmente `ADDS_USED`. Nao precisa de `ROSTER_CSV`:
   o elenco vem do ESPN pelo `ESPN_TEAM_ID`.
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
- O universo analisado e o da sua liga + free agents.

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

