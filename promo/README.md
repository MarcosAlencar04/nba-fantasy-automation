# Motion de divulgação

Peça animada de 36s, sem narração, para divulgar o projeto. Roda no navegador:
não precisa de Node, npm, ffmpeg nem internet.

```
Abra promo/motion.html no Chrome ou Edge.
```

## Como gerar o vídeo

1. Abra o arquivo (duplo clique basta).
2. **Tocar** para revisar.
3. Escolha o formato.
4. **⤓ Baixar vídeo** — a gravação roda em tempo real (36s), com barra de
   progresso. **Deixe esta aba em primeiro plano durante a gravação.**

   O navegador estrangula timers de abas em segundo plano (~1 chamada/segundo),
   o que produziria um vídeo tremido. A página conta os quadros desenhados e
   avisa em âmbar se isso acontecer — basta regravar com a aba à frente. Com a
   aba visível saem ~2.000 quadros e o MP4 fica em torno de 4–7 MB.

Saída: **MP4 (H.264)** quando o navegador suporta, com fallback para WebM VP9.
O MP4 abre em WhatsApp, Instagram, QuickTime e PowerPoint sem conversão.

| Formato | Resolução | Uso |
|---|---|---|
| 16:9 | 1920×1080 | YouTube, site, apresentação |
| 1:1 | 1080×1080 | feed quadrado |
| 9:16 | 1080×1920 | Stories / Reels |

**1:1 e 9:16 reaproveitam a composição 16:9** com moldura de marca (headline em
cima, CTA embaixo). Para vertical nativo — layout desenhado desde o começo — seria
preciso uma composição própria; aqui o conteúdo é preservado inteiro, sem corte.

## Usar o print real do email

Arraste um PNG/JPG do email para a caixa **"Print do email"**. Ele substitui a
réplica vetorial nas cenas de escalação e de trocas. Recomendado: recorte vertical
da seção, com pelo menos 900px de largura.

Sem print, a animação usa uma réplica desenhada com as mesmas cores e o mesmo
layout do produto — não é um placeholder cinza.

## Conferir um frame específico

`motion.html?t=9` abre congelado em 9s (aceita decimais: `?t=14.5`).
Atalhos: `espaço` toca/pausa, `←`/`→` anda 1s.

**⤓ Baixar quadro atual** exporta PNG na resolução escolhida — serve para
thumbnail, capa ou para mandar um frame aprovado para alguém.

## Roteiro

| Tempo | Cena | O que aparece |
|---|---|---|
| 0–3s | problema | relógio, abas se abrindo, contador de minutos perdidos |
| 3–6s | chegada | notificação de email entrando |
| 6–12s | escalação | card do email, scanner nos 10 slots, FP contando |
| 12–17s | IR e trocas | foco no bloco de IR, troca 2x1 com ganho em FP |
| 17–23s | **relatório pré-jogo** | email enxuto (só escalação + IR), contagem de 30 min, "1 troca necessária" e o caso "nada a fazer" |
| 23–28s | escala | grade de 6 relatórios, fio para "1 liga" |
| 28–32s | roda na nuvem | GitHub Actions → Postgres → Vercel, "nada para instalar" |
| 32–36s | fechamento | marca, assinatura **67 agence** e tagline |

A cena de **relatório pré-jogo** mostra o email enxuto: só escalação e IR,
sem add/drop nem trocas. É o produto que sai 30 min antes de cada partida, e
só quando há mudança a fazer — o caso "nada a fazer" aparece na mesma cena.

## Paleta (a mesma do email)

`#0a0d13` fundo · `#141a24` card · `#eef2f8` texto · `#f97316` acento ·
`#4ade80` positivo · `#f87171` negativo · `#fbbf24` alerta

## O que a peça não promete

- Não mostra jogadores, logos ou uniformes da NBA.
- O agente **recomenda**; quem altera o time é o dono. A peça não sugere
  automação de escrita no ESPN.
- Projeção estatística, não "IA". A palavra não aparece.
- **Nenhuma informação de custo.** A peça fala do que o cliente ganha (roda na
  nuvem, nada para instalar), não da estrutura de custo de quem opera.
