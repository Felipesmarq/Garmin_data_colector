# Case do Projeto — Pipeline Garmin → Recomendação de Treino com IA

> **Nota de revisão (2026-08-13):** case reescrito após sessão de refatoração.
> Mudanças principais: foco passa de "prevenção de canelite" para "evolução e
> geração de treino" (prevenção de sobrecarga vira guardrail, não objetivo
> central); troca de Anthropic → Gemini (custo zero); troca de DuckDB
> commitado no git → Postgres gerenciado (Neon); `fct_sessoes` deixa de ser
> tabela materializada e vira view por atividade (não mais por dia); Excel
> confirmado fora de escopo. Ver seção 11 para o racional de cada troca.

## 1. Contexto

Felipe é um corredor construindo o hábito de correr depois de meses lidando com
canelite (síndrome do estresse tibial medial). O treino evoluiu com um plano de
retomada gradual (método run-walk, regra dos 10%, fortalecimento de tibial
anterior). O relógio Garmin Forerunner 165 já capta dado objetivo (distância,
pace, FC, sono, recuperação) que hoje não vira recomendação nenhuma — só fica
armazenado.

## 2. Objetivo

**Funcional:** montar uma pipeline que colhe os dados do relógio
automaticamente e gera, toda semana, uma análise via IA generativa com
**recomendações concretas de treino que marquem evolução** (tipo de treino,
distância/duração alvo, intensidade) — com um cuidado explícito de não
propor carga que reproduza o padrão de sobrecarga que já causou canelite uma
vez (Acute:Chronic Workload Ratio — ver seção 6.1 —, tendência de sono/FC/body
battery como sinal de alerta).
Evolução é o objetivo; não-sobrecarga é uma restrição sobre esse objetivo,
não um objetivo paralelo.

**De aprendizado:** aplicar, num projeto pessoal, os mesmos conceitos do
projeto acadêmico de ELT (staging → view analítica → view agregada, SQL real)
e ganhar prática com automação via CI/CD (GitHub Actions) e integração com
uma API de LLM.

## 3. Arquitetura geral

```
Relógio Garmin ──sync──▶ Garmin Connect
                              │
                     (polling agendado, GitHub Actions)
                              ▼
                        [ Extração ]  ── garminconnect (lib não-oficial)
                              │
                              ▼
                      [ Armazenamento ] ── Postgres gerenciado (Neon) — sem dado no git
                              │
                              ▼
                     [ Transformação ] ── SQL: staging → vw_sessoes (por atividade) → vw_resumo_semanal
                              │
                              ▼
                       [ Análise (IA) ] ── Gemini API (free tier), a partir de vw_sessoes_ia (curada)
                              │
                              ▼
                       Relatório semanal (treino sugerido + alerta de sobrecarga se aplicável)
                              │
                              ▼
                    Você ajusta o treino ──┐
                              ▲             │
                              └─────────────┘ (vira dado da semana seguinte)
```

## 4. Stack tecnológico

| Camada | Tecnologia | Por quê |
|---|---|---|
| Linguagem | Python 3.11+ | Ecossistema maduro pras duas pontas (dados + API de IA) |
| Extração | `garminconnect` (PyPI) | Melhor opção existente pronta — sem alternativa madura melhor no momento |
| Armazenamento | **Postgres gerenciado (Neon)** | Free sem cartão, scale-to-zero (custo zero ocioso), SQL completo, elimina dado sensível versionado no git. Substitui o DuckDB commitado. |
| Transformação | SQL (views no Postgres) | Staging → view por atividade → view agregada semanal — sem tabela materializada nem script de ETL extra |
| Análise | **Gemini API (Google AI Studio)** | Free tier: 1.500 req/dia, sem cartão, contexto de até 1M tokens. Substitui Anthropic (custo zero era requisito). Chamada isolada atrás de uma função única para permitir trocar de provedor sem reescrever o resto — free tiers de LLM mudam com frequência. |
| Orquestração | GitHub Actions (2 workflows agendados) | Grátis, cron nativo, já versiona o código, Secrets pra credenciais. Único ambiente de produção — Docker fica só como conveniência de dev local, não faz parte da arquitetura de produção. |
| Dev local | Docker / docker-compose (opcional) | Só para rodar/testar mais rápido na máquina local, sem instalar Python direto. Não é usado em produção. |
| Versionamento | Git/GitHub (repositório **privado**) | Nenhum dado de saúde é commitado a partir desta revisão — nem `.duckdb`, nem export nenhum. |

**Removido nesta revisão:** Excel/`openpyxl` (input manual de dor) — inviável
de manter sincronizado sem passo manual; confirmado fora de escopo (ver seção
9). Anthropic API — trocado por Gemini por custo. DuckDB commitado no repo —
trocado por Postgres externo.

## 5. Engenharia do projeto — estrutura de pastas

```
garmin-ia-pipeline/
├── .github/workflows/
│   ├── sync_atividades.yml     # roda em schedule (ex. a cada 2-3h): extrai atividades + recuperação novas
│   └── analise_semanal.yml     # roda todo domingo: gera a análise da semana
├── src/
│   ├── db.py                   # conexão + helpers do Postgres (psycopg)
│   ├── extract/garmin.py       # puxa atividades + sono/FC de repouso
│   ├── load/
│   │   ├── schema.sql          # DDL Postgres: stg_atividades, stg_recuperacao_diaria, stg_dor
│   │   ├── views.sql           # vw_sessoes, vw_sessoes_ia, vw_resumo_semanal
│   │   └── load_atividades.py / load_recuperacao.py  # upsert incremental (ON CONFLICT)
│   └── analyze/analisar_com_ia.py
├── explore/                    # scripts de investigação (não fazem parte do pipeline final)
├── requirements.txt · .env.example · .gitignore · README.md
```

Note: não existe mais pasta `data/` com banco versionado — o banco vive no
Neon, fora do repositório.

## 6. Modelo de dados

**Staging (tabelas físicas, sem filtro — captura tudo que a API devolve):**
- `stg_atividades` — 1 linha por atividade (PK `activity_id`), todos os
  campos do Garmin (distância, pace, FC, cadência, zonas de FC, contato com
  solo, oscilação vertical, elevação, efeito de treino, etc.)
- `stg_recuperacao_diaria` — 1 linha por dia (PK `data`): sono, FC de
  repouso, body battery, estresse, passos.
- `stg_dor` — mantida no schema (colunas ficam NULL por ora, fora de escopo
  — ver seção 9), sem loader ativo.

**`vw_sessoes` (view, não mais tabela materializada) — 1 linha por
atividade:**
LEFT JOIN de `stg_atividades` com `stg_recuperacao_diaria` e `stg_dor` pela
data da atividade. Chave é `activity_id`, não `data` — **todas as atividades
entram**, incluindo múltiplas no mesmo dia. Substitui a antiga `fct_sessoes`
(tabela) e elimina a fase de transform/upsert que ela exigia: é sempre
recalculada na hora, sem risco de ficar dessincronizada.

**`vw_sessoes_ia` (view curada) — só os campos com sinal para geração de
treino + guardrail de sobrecarga:**
distância, duração, pace, FC média/máxima, cadência, tempo de contato com
solo, comprimento de passada, oscilação/razão vertical, velocidade máxima,
ganho/perda de elevação, efeito de treino aeróbico/anaeróbico + label, zonas
de FC 1-5, sono (total + score), FC de repouso, body battery ao acordar, dor
(quando existir). **Excluído:** calorias — não agrega sinal que
duração+distância+FC já não capturem.
Motivo da curadoria: pesquisa recente (EMNLP 2025, benchmark controlado)
mostra degradação de acurácia de raciocínio em curva de lei de potência
conforme aumenta contexto irrelevante no prompt — não é sobre limite de
token (o Gemini tem 1M de contexto, sobra), é sobre qualidade do raciocínio
piorar com ruído mesmo havendo espaço de sobra.

**`vw_resumo_semanal` (view, como já era) — agregação por semana ISO:**
km total, tempo total, nº de atividades, **carga aguda (últimos 7 dias) e
carga crônica (média móvel dos últimos 28 dias)** para cálculo do ACWR (ver
seção 6.1), tendência de sono/FC repouso/body battery.

## 6.1 Fundamentação: o que os dados do Garmin significam pra análise de performance

Pesquisa feita antes de implementar, pra embasar (e revisar) o que entra em
`vw_sessoes_ia` com base na literatura de ciência do esporte, não em
intuição. Explicação genérica dos grupos de dado — não usa números reais do
Felipe.

### Carga de treino e risco de sobrecarga

A "regra dos 10%" (não aumentar volume semanal em mais de 10%) que estava no
case original **não tem validação científica**: segundo a pesquisa, nenhum
estudo revisado por pares validou 10% como o limiar correto — a regra surgiu
de intuição de treinadores nos anos 1980 e se espalhou por ser simples de
lembrar, não por ter sido testada. Ela é conservadora demais pra quem corre
pouco volume e possivelmente não conservadora o suficiente pra quem está
voltando de lesão com volume alto.

O framework com base científica real é o **Acute:Chronic Workload Ratio
(ACWR)**, de Gabbett (2016), validado em múltiplos esportes incluindo corrida
de endurance: `carga aguda (últimos 7 dias) / carga crônica (média dos
últimos 28 dias)`. Diferença chave pro "10%": o ACWR é relativo à sua própria
base de treino — um corredor com carga crônica alta pode aumentar mais que
10% com segurança, enquanto alguém voltando de parada precisa de menos que
isso. O consenso do Comitê Olímpico Internacional aponta uma "zona segura"
entre **0.8 e 1.3**, com risco substancialmente maior acima de **1.5**
(pesquisa de Gabbett mostra 2-4x mais chance de lesão na semana seguinte
nessa faixa). **`vw_resumo_semanal` calcula ACWR** (carga aguda/carga
crônica) além da variação % semana a semana simples — é o número que
efetivamente embasa o guardrail no prompt da IA.

### Como o ACWR é calculado na prática (`vw_resumo_semanal`)

Implementado como view SQL (`src/load/schema.sql`), não script Python —
sempre recalculado a partir do dado atual, nunca fica desatualizado. A
fórmula usada tem uma diferença deliberada da definição "de livro-texto",
pra caber no ritmo do projeto.

**Definição de livro-texto (Gabbett, 2016):** carga aguda = soma dos
últimos 7 dias corridos (janela móvel diária); carga crônica = média das
últimas 4 semanas de carga aguda (também janela móvel diária).

**O que `vw_resumo_semanal` calcula (granularidade semanal, não diária):**
- **Carga aguda** = km total da semana ISO corrente (coluna `km_total`).
- **Carga crônica** = média do km total das últimas 4 semanas ISO — a
  semana corrente + as 3 anteriores:
  `AVG(km_total) OVER (ORDER BY semana_inicio ROWS BETWEEN 3 PRECEDING AND CURRENT ROW)`.
- **ACWR** = carga aguda / carga crônica.

**Por que semanal e não diário:** o relatório roda 1x por semana (domingo).
Uma janela diária de 7/28 dias exigiria uma "espinha" de todos os dias do
calendário (inclusive dias sem corrida, contando como zero) só pra
alimentar um número que só é lido semanalmente — complexidade sem
benefício prático nesse caso de uso. A aproximação semanal (4 semanas ISO
em vez de 28 dias corridos) entrega essencialmente o mesmo sinal com uma
query bem mais simples, e casa com o fato de o relatório em si só existir
1x por semana.

**Por que isso importa mais que uma regra de % fixa:** a variação %
simples (semana atual vs. anterior) trata todo mundo igual — 10% de
aumento é "o limite" tanto pra quem treina há anos quanto pra quem está
voltando de lesão do zero. O ACWR é relativo à sua própria base recente:
compara o que você fez essa semana com a média do que você vem
sustentando nas últimas 4 semanas. Isso captura dois cenários que uma
regra fixa erra: (1) alguém com base crônica alta e estável pode absorver
um aumento maior que 10% com segurança — o denominador (carga crônica) já
é alto, então o mesmo aumento absoluto gera um ACWR menor; (2) alguém com
base baixa (poucas semanas de retomada) que dá um salto de volume, mesmo
que o salto pareça pequeno em termos absolutos, gera um ACWR alto porque o
denominador é pequeno — é exatamente o padrão que precede reincidência de
lesão, segundo a pesquisa de Gabbett (2-4x mais chance de lesão na semana
seguinte quando ACWR passa de 1.5). É esse relativismo — carga de agora
medida contra a sua própria base, não contra um número universal — que
torna o ACWR o guardrail certo pra esse projeto.

### Efeito de treino (aeróbico/anaeróbico) — já vem pré-processado

O `efeito_treino_aerobico`/`anaerobico` do Garmin não é dado bruto — é a
saída de um algoritmo (Firstbeat/EPOC) que já calibra o estímulo pela sua
própria capacidade estimada (VO2max), então o mesmo treino gera notas
diferentes pra pessoas diferentes. Faixas de leitura: abaixo de 1.0 = sem
estímulo relevante; 2.0–2.9 = mantém condicionamento mas não avança; a partir
de 3.0 = melhora; 5.0 = overreaching, exige recuperação estendida antes do
próximo esforço forte. Isso é sinal direto e já individualizado pra decidir o
próximo treino (ex.: se a última sessão veio com 5.0, a recomendação seguinte
deveria puxar pra recuperação, não pra intensidade) — por isso continua em
`vw_sessoes_ia`.

### Dinâmica de corrida — cadência, tempo de contato com solo, oscilação vertical, comprimento de passada

Tempo de contato com solo (GCT) tem correlação forte com economia de corrida
prejudicada (r=.808 num dos estudos) e contato prolongado no solo é associado
a maior risco de lesão. Cadência baixa está associada a passada larga
("overstriding") e maior risco de lesão; aumentar cadência e evitar
aterrissagem de calcanhar reduz força de impacto. Oscilação vertical baixa
geralmente indica melhor economia de corrida (mais força convertida em
deslocamento horizontal, menos em "quicar"). Essas quatro métricas servem
tanto pro objetivo de evolução (economia de corrida melhorando ao longo do
tempo) quanto pro guardrail (sinais de risco de lesão) — confirma a decisão
de mantê-las em `vw_sessoes_ia`.

### Recuperação — sono, FC de repouso, body battery

FC de repouso e variabilidade de FC (HRV) são os marcadores mais usados pra
avaliar função do sistema nervoso autônomo em atletas: alta atividade
parassimpática em repouso (alta HRV) reflete boa capacidade de adaptar ao
estímulo de treino. Pesquisa em corredores recreacionais mostra que FC
noturna + HRV combinadas têm ≥85% de valor preditivo pra distinguir atletas
"overreached" (sobrecarregados) de "respondendo bem" ao treino. **Nota:** a
API usada aqui (`get_heart_rates`, `get_sleep_data`, `get_user_summary`,
`get_body_battery`) não expõe HRV bruta — o `body_battery` do Garmin é
justamente a métrica proprietária que já combina HRV, estresse, sono e
atividade num único score, funcionando como o melhor proxy disponível de
"prontidão" sem precisar calcular isso manualmente. Reforça a decisão de
manter sono, FC de repouso e body battery em `vw_sessoes_ia` como os sinais
de recuperação centrais.

**Fontes:** [ACWR - Outside Online](https://www.outsideonline.com/health/training-performance/injury-prevention-acute-chronic-workload-ratio-research/) · [10% rule sem validação - RunnersConnect](https://runnersconnect.net/injury-prevention/) · [GCT e economia de corrida - PubMed](https://pubmed.ncbi.nlm.nih.gov/32509121/) · [Cadência e overstriding - Baseline](https://www.baselineathlete.com/blog/running-form-cadence-vertical-oscillation) · [Training Effect/Firstbeat - the5krunner](https://the5krunner.com/garmin-features/training/training-effect/) · [HR/HRV noturna em corredores - Sports Medicine Open](https://link.springer.com/article/10.1186/s40798-024-00779-5)

## 7. Roadmap e critérios de aceite

### Fase 0 — Explorar formato real dos dados do Garmin
**Status:** ✅ concluída.

### Fase 1 — Infraestrutura Postgres (Neon)
**Requisitos:** provisionar banco no Neon; migrar `schema.sql` de sintaxe
DuckDB para Postgres; migrar `src/db.py` de `duckdb` para `psycopg`;
conexão via `DATABASE_URL` em variável de ambiente (local `.env`, produção
GitHub Secret).
**Critérios de aceite:**
- [x] `schema.sql` aplica sem erro num banco Neon vazio (validado em 2026-08-13: 4 tabelas + 3 views criadas e consultáveis).
- [ ] Conexão funciona local **(confirmado)** e via GitHub Actions usando o mesmo `DATABASE_URL` (Secret) — falta a parte do GitHub Actions, ver Fase 7.
- [x] Nenhum arquivo de banco (`.duckdb`, dump, etc.) é commitado a partir desta fase.

### Fase 2 — Extração (`extract/garmin.py`)
**Status:** ✅ concluída, sem mudança — o mapeamento de campos independe do banco de destino.

### Fase 3 — Load incremental (revisão)
**Requisitos:** `load_atividades.py` e `load_recuperacao.py` trocam o driver
DuckDB por psycopg; lógica de `ON CONFLICT` (upsert) é compatível com
Postgres sem mudança de sintaxe.
**Critérios de aceite:**
- [ ] Rodar o load duas vezes seguidas com os mesmos dados não duplica linhas (idempotência mantida).
- [ ] `stg_atividades` e `stg_recuperacao_diaria` continuam capturando todos os campos brutos, sem corte.

### Fase 4 — `vw_sessoes` (nova, substitui `fct_sessoes`)
**Requisitos:** view por `activity_id`, LEFT JOIN com recuperação e dor do dia.
**Critérios de aceite:**
- [ ] Um dia com duas atividades gera duas linhas na view (nenhuma atividade descartada).
- [ ] Nenhum script de transform/upsert é necessário para manter a view atualizada — é sempre live.

### Fase 5 — `vw_sessoes_ia` (nova)
**Requisitos:** view curada conforme lista da seção 6.
**Critérios de aceite:**
- [ ] Contém todos os campos listados na seção 6, e nenhum outro (em particular, sem `calorias`).
- [ ] Consulta roda em menos de 1s (garante que não virou gargalo desnecessário).

### Fase 6 — Análise com IA (`analyze/analisar_com_ia.py`)
**Requisitos:** chamada à API Gemini a partir de `vw_sessoes_ia` +
`vw_resumo_semanal`; chamada ao LLM isolada numa função única
(`gerar_analise(prompt) -> texto`) para permitir trocar de provedor sem
reescrever o resto.
**Critérios de aceite:**
- [ ] Relatório inclui pelo menos uma recomendação concreta de treino (tipo, distância/duração alvo, intensidade).
- [ ] Relatório sinaliza explicitamente quando o ACWR (carga aguda/carga crônica) sai da zona segura 0.8–1.3, com alerta reforçado acima de 1.5 (guardrail de sobrecarga, ver seção 6.1).
- [ ] Chave da API vem de variável de ambiente, nunca hardcoded.

### Fase 7 — Orquestração (GitHub Actions)
**Requisitos:** `sync_atividades.yml` (schedule, ex. a cada 2-3h) e
`analise_semanal.yml` (schedule, domingo). Secrets: token Garmin,
`DATABASE_URL`, chave Gemini.
**Critérios de aceite:**
- [ ] Pipeline roda ponta a ponta sem intervenção manual por pelo menos 2 semanas seguidas.
- [ ] Nenhum dado sensível (FC, sono, localização de dor) aparece em log do Actions.
- [ ] Nenhum commit automático de dado é gerado pelo workflow.

### Fase 8 — Ajuste fino
Frequência de polling, prompt, extras — a definir com base no uso real.

## 8. Riscos e decisões (atualizado)

- **Lib não-oficial do Garmin**: risco aceito, sem alternativa madura melhor
  disponível. Mitigação: fixar versão no `requirements.txt`.
- **Sem webhook real**: polling agendado é a via principal e suficiente para
  "totalmente automático"; `repository_dispatch` via atalho no celular fica
  como gatilho manual opcional, não como requisito.
- **Dados sensíveis**: resolvido nesta revisão — Postgres externo (Neon)
  em vez de banco versionado no git. Repositório continua privado como
  camada extra de proteção do código, não como proteção primária do dado.
- **Custo:** resolvido — Gemini free tier (sem cartão) e Neon free tier
  (scale-to-zero, sem cartão) cobrem o volume do projeto (1 análise/semana,
  poucas atividades) com folga.
- **Volatilidade de tier gratuito**: tiers de LLM gratuito mudam com
  frequência (ex. corte do Gemini em fins de 2025). Mitigado pela função de
  chamada isolada (seção 4) — trocar provedor é mudança de configuração, não
  reescrita.

## 9. Fora de escopo (por ora)

- **Registro de dor** — Excel confirmado fora de escopo (inviável manter
  sincronizado sem passo manual). `stg_dor` permanece no schema com colunas
  NULL; quando voltar como feature, falta decidir a fonte (form acessível
  via API) e escrever o loader — schema já suporta.
- **Docker em produção** — fica só como conveniência de dev local.

## 10. Critérios de sucesso

- Pipeline roda ponta a ponta sem intervenção manual.
- Relatório semanal chega automaticamente todo domingo com pelo menos uma
  recomendação concreta de treino.
- Relatório sinaliza risco de sobrecarga (ACWR fora da zona 0.8–1.3) quando aplicável.
- Nenhum dado sensível commitado no GitHub a partir desta revisão.
- Histórico consultável via SQL (Postgres) para qualquer análise futura.
- *(quando o registro de dor voltar)*: manter constância de treino por 4+
  semanas sem recidiva de dor ≥3.

## 11. Racional das trocas desta revisão

Resumo das decisões tomadas na sessão de refatoração (2026-08-13), para
referência futura:

- **DuckDB commitado → Neon (Postgres)**: commitar o `.duckdb` a cada run
  inflava o histórico do git com um blob binário (sem diff útil) e deixava
  dado de saúde permanentemente no histórico mesmo que uma linha fosse
  apagada depois. Neon resolve com scale-to-zero gratuito e sem dado no git.
- **Anthropic → Gemini**: requisito explícito de custo zero; Gemini free
  tier tem volume e contexto (1M tokens) que sobram para o caso de uso.
- **Ollama local descartado**: incompatível com "totalmente automático" —
  exigiria máquina própria ligada, e os runners do GitHub Actions são
  efêmeros e sem GPU.
- **`fct_sessoes` (tabela) → `vw_sessoes` (view)**: materializar só compensa
  quando a mesma query pesada roda muitas vezes e recomputar é caro — não é
  o caso aqui (poucas linhas novas por semana). Como tabela, exigia um
  script de transform próprio (nunca escrito) e carregava um bug conhecido
  (chave por `data`, no máximo 1 atividade/dia). Como view por
  `activity_id`, o bug desaparece e não há script de sincronização para
  quebrar.
- **`vw_sessoes_ia` curada**: contexto irrelevante mede-se em degradação de
  raciocínio do LLM (não só custo de token) — mitigado sem perder dado
  nenhum, porque o staging continua completo; só o que vai pro prompt é
  filtrado.
- **Foco: evolução de treino > prevenção de canelite**: prevenção continua
  presente como guardrail (ACWR, sinais de sono/FC — ver seção 6.1), mas
  deixou de ser o objetivo central — o objetivo central passou a ser gerar
  recomendações de treino que marquem evolução.
- **Regra dos 10% → ACWR**: pesquisa de ciência do esporte (seção 6.1) mostra
  que a regra dos 10% nunca foi validada em estudo revisado por pares; o
  Acute:Chronic Workload Ratio (Gabbett, 2016) é o framework com base
  científica real e já computável com os dados que já são coletados (carga
  aguda 7 dias / carga crônica 28 dias).
