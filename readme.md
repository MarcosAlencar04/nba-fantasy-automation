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