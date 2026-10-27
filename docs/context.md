# CONTEXTO DO PROJETO: NBA Fantasy Agent (ESPN)

Este documento reúne tudo o que foi decidido e construído na conversa, além dos arquivos do projeto. Serve para retomar o trabalho depois, inclusive em outra conversa com o Claude (veja a seção 12).

Data de referência: 27/10/2026 (temporada NBA 2026-27 recém-iniciada).

---

## 1. Objetivo

Um agente em Python que ajuda a jogar o fantasy de NBA no ESPN. Ele lê estatísticas da NBA, analisa o elenco do usuário e recomenda ações. Ele **não executa nada no ESPN**: só recomenda, e o usuário aplica no app.

Pedidos do usuário:

1. Analisar quem rende bem ou mal na semana do confronto e sugerir trocar ou dropar.
2. Analisar free agents (quando der para identificá-los): vale contratar ou trocar? Por quem?
3. Analisar o lineup do dia: quem vale mais a pena usar, quem joga hoje e quem não joga.
4. A análise deve ser **aprofundada**, não só pontos e fantasy points.
5. O roster vem de um CSV na mesma pasta do script (`Nome Jogador, Posição`).
6. Detectar jogadores que tendem a cair de rendimento e sugerir vendê-los na alta.

---

## 2. Regras da liga (extraídas dos prints)

| Item | Valor |
|---|---|
| Nome | My 2024 League |
| Times | 8 (divisão única "EAST") |
| Formato de pontuação | Head to Head Points |
| Draft | Snake, 04/10/2026, 60 s por pick |
| Roster | 14 jogadores: 10 titulares, 4 no banco, 3 vagas de IR |
| Slots titulares | PG, SG, SF, PF, C, G, F, UTIL ×3 |
| Limite de Centers | 4 (por roster) |
| Troca de lineup | Diária, trava individual no horário do jogo do jogador |
| Aquisição | Waivers, período de 1 dia, ordem "move to last after claim, never reset" |
| Limite de adds | 5 por matchup; sem limite na temporada |
| Matchup | 1 semana (seg-dom); 20 matchups na temporada regular |
| Playoffs | 4 times; semifinal de 2 semanas; final de 2 semanas; consolation ladder |
| Trocas | Sem limite; prazo **22/03/2027 12:00 (GMT-3)**; revisão de 2 dias; 4 votos vetam |
| Outros | Lineup Protection off, sem keepers em 2027/2028, sem bônus de vitória |
| Lista de undroppables do ESPN | Observada (Sim) |

Times da liga: Salethunder, Guerreiros do Estado Dourado, Leigos da bola laranja, tapetinho Fc, Milwaukee Bagres, BH Mountains, LA F4K3RS, Operários da Bola Basketball (um deles é o do usuário).

### Pontuação

| Estatística | Pontos |
|---|---|
| PTS | +1 |
| FGM | +2 |
| FGA | −1 |
| FTM | +1 |
| FTA | −1 |
| 3PM | +1 |
| OREB | +0,5 |
| REB | +1 |
| AST | +2 |
| STL | +4 |
| BLK | +4 |
| TO | −2 |
| EJ | −2,5 |
| DD | +5 |
| TD | +10 |
| QD | +100 |

Fórmula implementada (ejeções ignoradas):

```
FP = PTS + 2·FGM − FGA + FTM − FTA + 3PM + 0,5·OREB + REB + 2·AST + 4·STL + 4·BLK − 2·TO + 5·DD + 10·TD + 100·QD
```

Valor marginal de cada arremesso convertido, que sustenta a análise de "sorte":

- Cesta de 2 convertida: **+4**.
- Cesta de 3 convertida: **+6** (4 mais 2 extras).
- Lance livre convertido: **+2**.
- Cada tentativa de arremesso custa −1 (FGA e FTA).

Consequência: eficiência importa muito nesta liga. Um jogador que só chuta muito com baixo aproveitamento rende pouco, e steals e blocks (4 pontos cada) pesam bastante.

---

## 3. Decisões tomadas

- **Plataforma:** ESPN. Não há API oficial de escrita, então o agente só lê e recomenda.
- **Roster:** vem de `roster.csv` (preferência do usuário), não do ESPN. Formato `Nome, Posição[, Status]`, com status opcional (`OUT`, `DTD`, `IR`).
- **Dados da NBA:** biblioteca `nba_api` (game logs, estatísticas avançadas dos times, posições) mais o calendário oficial em `cdn.nba.com`.
- **Free agents:** só dá para identificá-los com credenciais do ESPN (`--espn`). Sem isso, o agente avalia a NBA inteira e pede para o usuário conferir a disponibilidade. Há também a opção `--candidates` com uma lista manual.
- **Execução automática:** descartada por ora (frágil e pode violar os termos do ESPN). O fluxo é semi-automático: o agente recomenda, o usuário aplica.
- **Idioma:** relatório e comentários em português.
- **Dia do fantasy:** fuso `America/New_York`. A semana vai de segunda a domingo.

---

## 4. Arquitetura do código (`nba_fantasy_agent.py`)

Fluxo de `main()`:

1. Lê `roster.csv` e os argumentos de linha de comando.
2. Baixa (com cache em `.cache/`) os game logs da temporada atual e da anterior, o calendário, as estatísticas de ritmo/defesa dos times e as posições.
3. Se `--espn`: lê free agents, status de lesão e elencos dos outros times.
4. Chama `run_analysis(...)`, uma função pura que recebe os dados e devolve o relatório em Markdown. Foi isolada para permitir testes com dados sintéticos.
5. Imprime, salva em `reports/relatorio_AAAA-MM-DD.md` e, com `--telegram`, envia um resumo.

Funções principais:

| Função | O que faz |
|---|---|
| `fantasy_points`, `prep_logs` | Calcula FP por jogo (inclui DD/TD/QD). |
| `build_profile`, `build_profiles` | Gera o perfil estatístico e a projeção de cada jogador. |
| `form_tag` | Classifica a fase: vender na alta, comprar na baixa, papel encolhendo etc. |
| `Ctx` | Calendário, ritmo e defesa dos times, fator de ambiente. |
| `day_value` | Valor esperado de um jogador em um dia. |
| `optimize` | Atribuição jogador × slot (`scipy.optimize.linear_sum_assignment`). |
| `week_value` | Soma do lineup ótimo de cada dia restante. |
| `NameIndex` | Casa nomes (sem acento, sem Jr./III, com aproximação por similaridade). |
| `fetch_espn` | Lê free agents, lesões e elencos pelo `espn-api`. |
| `run_analysis` | Monta as seções 1 a 4 do relatório. |

### Arquivos da pasta

| Arquivo | Função |
|---|---|
| `nba_fantasy_agent.py` | O agente. |
| `roster.csv` | Elenco do usuário (hoje só tem 3 jogadores de exemplo). |
| `candidates.example.csv` | Modelo de lista de candidatos. |
| `env.example.txt` | Modelo de configuração; copiar para `.env`. |
| `requirements.txt` | Dependências. |
| `README.md` | Instruções de uso. |
| `CONTEXTO.md` | Este documento. |
| `PLANO.md` | Já estava na pasta; não foi criado nem alterado por mim. |

### Argumentos de linha de comando

`--roster`, `--candidates`, `--espn`, `--adds-used N`, `--recent N` (janela recente, padrão 5 jogos), `--replacement X` (nível de reposição, padrão 24), `--date AAAA-MM-DD`, `--telegram`, `--email`, `--notify`, `--refresh`, `--out-dir`.

---

## 5. Modelo estatístico

### 5.1 Projeção por jogo

1. **Base da temporada anterior.** FP por minuto, regredido à média da liga (peso de 200 minutos). Percentuais de arremesso também regredidos (FG 200 tentativas, 3P 250, FT 100).
2. **Temporada atual sem a sorte.** O "talento" de arremesso é o percentual atual regredido à base (FG 150 tentativas, 3P 200, FT 80). A parte acima ou abaixo disso, convertida em FP (pesos 4, 2 e 2), é a **sorte** e é removida.
3. **Mistura.** `w = minutos_atuais / (minutos_atuais + 250)`; `FP/min projetado = w·atual_ajustado + (1−w)·anterior`.
4. **Minutos projetados.** Média da temporada (com peso de 6 jogos da temporada passada) e média recente (últimos N jogos), meio a meio quando há 3 ou mais jogos. Teto de 40 min.
5. **Projeção** = FP/min projetado × minutos projetados.

Efeito prático: no começo da temporada a projeção pesa a temporada passada; com cerca de 15 jogos já confia mais no presente.

### 5.2 Fase do jogador (regressão à média)

Compara os últimos N jogos com a projeção. Limite: `max(4 FP, 12% da projeção)`.

| Fase | Condição |
|---|---|
| VENDER NA ALTA | Forma ≥ limite acima **e** (sorte ≥ +2 FP/jogo **ou** minutos ≥ +3 sobre o normal). |
| ALTA SUSTENTÁVEL | Forma acima do limite, mas sem sinal de sorte nem de minutos extras. |
| COMPRAR NA BAIXA | Forma ≥ limite abaixo, com minutos estáveis (sorte ≤ −1,5 reforça o motivo). |
| PAPEL ENCOLHENDO | Forma abaixo do limite e minutos ≤ −4. É risco real, não azar. |
| AMOSTRA PEQUENA | Menos de 3 jogos na temporada. |

### 5.3 Valor esperado por dia

```
valor = projeção × fator_ambiente × prob_disponibilidade × (0,96 se back-to-back)
```

- **Fator de ambiente:** `(ritmo_médio_dos_dois / ritmo_liga)^0,7 × (def_rating_adversário / def_rating_liga)^0,5`, suavizado (metade do efeito, escalado por `min(1, jogos/20)`) e limitado a 0,94–1,06.
- **Disponibilidade:** OUT e IR valem 0; DTD vale 0,6 no dia e 0,85 nos dias seguintes; ativo vale 1.
- Sem jogo no dia, o valor é 0. Se o jogo de hoje do jogador já terminou, hoje vale 0.

### 5.4 Lineup

Problema de atribuição jogador × slot que maximiza a soma dos valores. Elegibilidade: G = PG/SG, F = SF/PF, UTIL = qualquer um. Jogadores com valor 0 ficam no banco.

### 5.5 Add/drop

- O candidato é avaliado contra **cada** jogador dropável do elenco (IR excluído). Mede-se a diferença no `week_value` (soma dos lineups ótimos dos dias restantes), o que já respeita posições e quantidade de jogos.
- **Score** = ganho na semana + 6 × ganho por jogo (o 6 é `HORIZON_EXTRA_GAMES`, que representa valor além da semana). Só entram sugestões com score ≥ 8.
- Respeita o limite de 4 centers e o saldo de adds do matchup.
- Se faltarem 2 dias ou menos na semana, o horizonte se estende até a semana seguinte.
- **Nível de reposição:** 5º melhor free agent do ESPN, ou 24 FP/jogo se não houver lista (ajustável com `--replacement`).

### 5.6 Trocas (só com `--espn`)

Troca 1x1 em que o usuário ganha pelo menos 2,5 FP/jogo de projeção real e o outro dono "percebe" valor igual ou maior. O valor percebido é `0,5 × média da temporada + 0,5 × média dos últimos 10 jogos`, aproximando o que o ESPN mostra. No máximo 2 sugestões por time e 8 no total; avisa risco de veto quando o ganho passa de 8 FP/jogo. Sem `--espn`, mostra apenas quem vender e alvos de "comprar na baixa" na NBA inteira.

### 5.7 Cache

Game log da temporada atual: 3 h. Temporada anterior: 7 dias. Calendário: 12 h. Estatísticas dos times: 6 h. Posições: 72 h. `--refresh` ignora.

---

## 6. O que foi testado e o que não foi

**Testado (dados sintéticos):**

- Sintaxe do código.
- Cálculo de FP conferido contra conta manual (68 FP).
- Parser de posições e otimizador de lineup.
- Associação de nomes (`Ayo Donsumo` virou `Ayo Dosunmu`; `Jaren Jackson` e `Nikola Jokic` casam sem acento e sem Jr.).
- Relatório completo nos modos com e sem ESPN, inclusive o caminho em que há jogos já realizados na semana.

**NÃO testado:**

- Chamadas reais à NBA (`stats.nba.com` não era acessível no ambiente de teste).
- Chamadas reais ao ESPN.
- Telegram.

Pontos que podem falhar na primeira execução real e merecem atenção:

- Nomes de colunas e endpoints do `nba_api` (`LeagueGameLog`, `LeagueDashTeamStats`, `PlayerIndex`).
- Formato do JSON do calendário em `cdn.nba.com` (há um fallback pelo `nba_api`).
- Atributos dos objetos do `espn-api` (`eligibleSlots`, `injuryStatus`, `percent_owned`, `free_agents`).

---

## 7. Premissas e limitações conhecidas

- **Triple-double:** assumi que também conta como double-double (15 pontos de bônus no total). Se na liga for só 10, ajuste em `prep_logs`.
- **Ejeções (−2,5):** ignoradas.
- **Posições dos candidatos sem ESPN:** vêm do `PlayerIndex` da NBA, que é grosseiro (G, F, C, G-F, F-C), então a elegibilidade é aproximada.
- **Lesões sem ESPN:** o agente não sabe. É preciso informar pela 3ª coluna do CSV.
- **Duração da lesão:** jogador OUT é tratado como fora em todos os dias da janela.
- **Trava por horário de jogo:** o agente não verifica horários; só desconsidera o jogo de hoje se ele já aparece no game log.
- **Waivers:** a recomendação é um claim (processa no dia seguinte, com ordem fixa), não uma contratação instantânea.
- **Aceitação de trocas:** é heurística. Não considera necessidade posicional do outro time.
- **Início de temporada:** com poucos jogos, os sinais de "vender na alta" e "comprar na baixa" são pouco confiáveis; o relatório marca amostra pequena.
- **Limite de Centers:** conta jogadores com elegibilidade C (pode divergir de como o ESPN conta híbridos).
- **IR:** os 3 slots de IR não estão modelados como regra; jogadores com status IR apenas ficam fora do lineup e da lista de drops.
- **Hospedagem:** o `stats.nba.com` costuma bloquear IPs de servidores, então GitHub Actions provavelmente não funciona; rode localmente.
- **Projeção estatística, não garantia:** lesões e notícias de última hora precisam ser conferidas antes de agir.

---

## 8. Configuração do modo ESPN

Arquivo `.env` (copiar de `env.example.txt`):

```
ESPN_LEAGUE_ID=
ESPN_SWID={...}
ESPN_S2=
TELEGRAM_TOKEN=
TELEGRAM_CHAT_ID=
SMTP_HOST=
SMTP_PORT=
SMTP_USER=
SMTP_PASS=
EMAIL_FROM=
EMAIL_TO=
```

- `ESPN_LEAGUE_ID`: o `leagueId=` na URL da liga.
- `ESPN_SWID` e `ESPN_S2`: cookies do navegador logado em espn.com (F12, Application ou Storage, Cookies). Expiram de tempos em tempos.
- **Email:** `SMTP_HOST`/`SMTP_PORT` (587 com STARTTLS ou 465 com SSL), `SMTP_USER`, `SMTP_PASS` (no Gmail, app password), `EMAIL_FROM` (opcional, padrão `SMTP_USER`) e `EMAIL_TO` (vários destinatários separados por vírgula). Enviado por `smtplib` da stdlib, sem dependência nova.
- A temporada 2026-27 corresponde a `year=2027` no `espn-api` (o código calcula sozinho).
- O time do usuário é identificado automaticamente pela maior sobreposição de nomes com o `roster.csv` (mínimo de 3 jogadores).

---

## 9. Problemas já ocorridos

- **README e `.env.example` "bugados":** o README havia sumido da pasta e o `.env.example` é um arquivo oculto (nome começando com ponto). Corrigido: README recriado e exemplo renomeado para `env.example.txt`.
- **`PLANO.md`:** apareceu na pasta sem ter sido criado por mim. Foi deixado intacto.

---

## 10. Próximos passos sugeridos

1. Preencher o `roster.csv` com os 14 jogadores (posições como aparecem no ESPN) e rodar de verdade; trazer qualquer erro para correção.
2. Configurar o modo `--espn` e validar a leitura de free agents e outros times.
3. Rodar durante 1 a 2 semanas só como consulta, comparando as sugestões com o que acontece, antes de confiar nelas.
4. Possíveis evoluções:
   - Trocas 2x1 (consolidação) e consideração de necessidade posicional do outro time.
   - Duas execuções por dia (manhã e uma hora antes do primeiro jogo, para rechecar lesões).
   - Leitura de horários de jogo para respeitar a trava individual.
   - Fonte de lesões independente do ESPN.
   - Projeção de jogos restantes até o fim da temporada regular e dos playoffs fantasy (semanas 21 a 24).
   - Cálculo de probabilidade de vencer o confronto semanal usando o adversário da semana.
   - Backtest com a temporada anterior para calibrar limiares (12%, +2 de sorte, +3 de minutos etc.).

---

## 11. Constantes ajustáveis (no topo e dentro de `build_profile`)

| Constante | Valor | Onde |
|---|---|---|
| `LINEUP_SLOTS` | PG SG SF PF C G F UTIL×3 | topo |
| `MAX_CENTERS` | 4 | topo |
| `ADDS_LIMIT` | 5 | topo |
| `TRADE_DEADLINE` | 22/03/2027 | topo |
| `HORIZON_EXTRA_GAMES` | 6 | topo |
| `LUCK_W` | (4, 2, 2) | topo |
| Pesos de regressão | 200/250/100 e 150/200/80 | `build_profile` |
| Peso da temporada atual | `min/(min+250)` | `build_profile` |
| Limite de fase | `max(4, 12%)` | `form_tag` |
| Fator de back-to-back | 0,96 | `day_value` |
| Disponibilidade DTD | 0,6 hoje, 0,85 depois | `avail_prob` |
| Corte de add/drop | score ≥ 8 | `run_analysis` |
| Corte de troca | ganho ≥ 2,5 FP/jogo | `run_analysis` |

---

## 12. Prompt para retomar em outra conversa

Cole isto (junto com o código, se quiser alterações):

```
Tenho um agente em Python para o meu NBA Fantasy no ESPN (liga H2H Points, 8 times,
14 jogadores, lineup PG SG SF PF C G F UTIL x3, limite de 5 adds por matchup, waivers
de 1 dia, prazo de trocas em 22/03/2027). Ele lê o roster de um roster.csv, usa nba_api
para estatisticas e calendario, projeta FP por jogo (ajustado por sorte de arremesso e
regredido a temporada passada), otimiza o lineup diario, sugere add/drop pelo ganho no
valor semanal do lineup e aponta quem vender na alta. O modo --espn usa o espn-api para
free agents e outros elencos. Pontuacao: PTS 1, FGM 2, FGA -1, FTM 1, FTA -1, 3PM 1,
OREB 0,5, REB 1, AST 2, STL 4, BLK 4, TO -2, DD 5, TD 10, QD 100.
Quero [descreva a mudanca]. Segue o codigo: [cole nba_fantasy_agent.py]
```