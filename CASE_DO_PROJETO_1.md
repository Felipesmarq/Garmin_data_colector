# Case do Projeto — Pipeline Garmin → Recomendação de Treino com IA

> **Nota de revisão (2026-08-13):** case reescrito após sessão de refatoração.
> Mudanças principais: foco passa de "prevenção de canelite" para "evolução e
> geração de treino" (prevenção de sobrecarga vira guardrail, não objetivo
> central); troca de Anthropic → Gemini (custo zero); troca de DuckDB
> commitado no git → Postgres gerenciado (Neon); `fct_sessoes` deixa de ser
> tabela materializada e vira view por atividade (não mais por dia); Excel
> confirmado fora de escopo. Ver seção 11 para o racional de cada troca.

> **Nota de revisão (2026-08-14):** registro de dor volta a entrar em
> escopo, antes da Fase 6/7 originais — não mais "quando voltar como
> feature" (seção 9 antiga). Substitui a ideia de Excel (rejeitada em
> 2026-08-13) por Google Sheets, que resolve o problema real (acessível
> do celular, sem passo manual de commit) sem os riscos de um form
> genérico. Vira a nova **Fase 6**, entre `vw_sessoes_ia` e a análise
> com IA — todas as fases depois dela sobem 1 número (IA: 6→7,
> orquestração: 7→8, ajuste fino: 8→9). `stg_dor` migra de PK `data`
> para PK `activity_id`, acompanhando o mesmo motivo que já tinha
> movido `fct_sessoes` → `vw_sessoes` (uma data pode ter mais de uma
> atividade). Ver seção 11 para o racional completo.

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
| Registro de dor | **Google Sheets (`gspread`)** | Acessível do celular, sem passo manual de commit — resolve o mesmo problema que tornou o Excel local inviável (ver seção 11), com autenticação via service account em vez de OAuth de usuário. |
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
│   ├── planilha.py              # conexão Google Sheets + abas/linhas (mecânica genérica, sem regra de negócio)
│   ├── extract/garmin.py       # puxa atividades + sono/FC de repouso
│   ├── load/
│   │   ├── schema.sql          # DDL Postgres: stg_atividades, stg_recuperacao_diaria, stg_dor
│   │   ├── views.sql           # vw_sessoes, vw_sessoes_ia, vw_resumo_semanal
│   │   ├── load_atividades.py / load_recuperacao.py  # upsert incremental (ON CONFLICT)
│   │   ├── load_dor.py         # Fase 6 -- aba "Dor": exporta pendentes + importa preenchidas
│   │   └── planilha_desempenho.py  # Fase 6 -- abas "Atividades"/"Resumo Semanal", somente-leitura
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
- `stg_dor` — 1 linha por **atividade** anotada (PK `activity_id`, não
  `data` — mesmo motivo de `vw_sessoes` ser por atividade: um dia pode ter
  mais de uma corrida, cada uma com dor própria). Alimentada pelo
  `load_dor.py` (Fase 6) a partir de uma planilha Google Sheets — ver
  seção 11.

**`vw_sessoes` (view, não mais tabela materializada) — 1 linha por
atividade:**
LEFT JOIN de `stg_atividades` com `stg_recuperacao_diaria` (pela data) e
`stg_dor` (por `activity_id`). Chave é `activity_id`, não `data` — **todas
as atividades entram**, incluindo múltiplas no mesmo dia. Substitui a
antiga `fct_sessoes` (tabela) e elimina a fase de transform/upsert que ela
exigia: é sempre recalculada na hora, sem risco de ficar dessincronizada.
Inclui `classificacao_atividade`, derivada de `efeito_treino_label`
(`'UNKNOWN'` → `'CAMINHADA'`, resto passa direto) — ver seção 6.1 pra por
que esse é o único critério confiável disponível.

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

### Como o `efeito_treino_label` (Primary Benefit) é calculado — e o que não é público

Pesquisa feita em 2026-08-14 (Firstbeat, fóruns Garmin) pra entender a
origem do texto categórico (`AEROBIC_BASE`, `RECOVERY`, `TEMPO`,
`UNKNOWN`, etc.) que aparece em `efeito_treino_label` — diferente do
score numérico 0-5 (`efeito_treino_aerobico`/`anaerobico`), que a
Firstbeat documenta publicamente (seção acima).

**O que é documentado:** o score é derivado de **EPOC** (Excess
Post-Exercise Oxygen Consumption, "débito de oxigênio"), previsto em
tempo real a partir de FC — a Firstbeat usa uma rede neural que combina
FC, variabilidade de FC (proxy de frequência respiratória) e a dinâmica
de subida/descida da FC ("on/off-kinetics") pra estimar intensidade
relativa (%VO2max) segundo a segundo. O **aeróbico** usa o **pico** de
EPOC atingido na sessão (não o total acumulado — um pico curto de alta
intensidade pontua diferente de manter esse nível a sessão toda); o
**anaeróbico** usa FC combinada com velocidade/potência. O resultado é
calibrado pela sua capacidade estimada (VO2max) — por isso o mesmo
treino físico gera notas diferentes pra pessoas diferentes. Escala:
0.0–0.9 sem efeito, 1.0–1.9 recuperação, 2.0–2.9 mantém condicionamento,
3.0–3.9 melhora, 4.0–4.9 melhora bastante, 5.0 overreaching.

**O que NÃO é documentado:** a regra exata que transforma esse par de
scores (+ o que mais for usado — duração, distribuição de tempo por
zona de FC, %FC máxima) num dos rótulos categóricos (`AEROBIC_BASE`,
`RECOVERY`, `TEMPO`, `LACTATE_THRESHOLD`, `VO2MAX`,
`ANAEROBIC_CAPACITY`, `SPRINT`, `UNKNOWN`, ...) é proprietária da
Firstbeat/Garmin e não está em nenhum white paper público. Um
funcionário do fórum da Garmin confirma que o cálculo usa **% da FC
máxima, não as zonas configuradas no relógio** ("the algorithm is based
on % of MHR, not on zones"), e que a mesma sessão pode gerar rótulos
diferentes em re-processamentos (ruído de GPS/HR). `UNKNOWN`
especificamente **não tem definição pública** — o que sabemos sobre ele
é 100% empírico, observado nos próprios dados deste projeto (ver seção
11: nas duas atividades `UNKNOWN` registradas até agora, o score
aeróbico é 0.4 e a duração é curta, <10 min — consistente com "curto/
fraco demais pra classificar", mas isso é inferência nossa, não
documentação da Garmin).

**Por que isso importa pro projeto:** `classificacao_atividade` (seção
6, Fase 6) trata `UNKNOWN` como um caso especial exatamente por essa
razão — como a regra de classificação da Garmin é opaca, não dá pra
tentar replicá-la ou prever quando ela vai gerar outro rótulo raro; o
mais seguro é confiar no rótulo que a própria Garmin já decidiu, e só
reinterpretar o único caso (`UNKNOWN`) onde a falta de rótulo, por si
só, já é o sinal.

**Fontes:** [Firstbeat — EPOC and Training Effect](https://www.firstbeat.com/en/science-and-physiology/epoc-and-training-effect/) · [Firstbeat — EPOC white paper (PDF)](https://www.firstbeat.com/wp-content/uploads/2015/10/white_paper_epoc.pdf) · [the5krunner — Garmin Training Effect explicado](https://the5krunner.com/garmin-features/training/training-effect/) · [Fórum Garmin — "How is a tempo run Training Effect defined?"](https://forums.garmin.com/sports-fitness/running-multisport/f/forerunner-945/275419/how-is-a-tempo-run-training-effect-defined)

### Zonas de FC — o que é armazenado (e o que não é)

Verificado no código (`extract/garmin.py`, `load/schema.sql`) em
2026-08-14: `stg_atividades` guarda **5 colunas por atividade**
(`tempo_zona_fc_1` a `tempo_zona_fc_5`, mapeadas direto de
`hrTimeInZone_1..5` da API) — cada uma é o **tempo em segundos** que
você passou naquela zona **durante aquela atividade específica**. É
granularidade por atividade (`activity_id`), não por dia — a mesma
lógica de `vw_sessoes` desde a Fase 4.

**O que não é capturado:** os **limites de BPM** que definem onde cada
zona começa/termina (ex. "zona 2 = 120–140 bpm") não vêm nesses campos
e não são armazenados em lugar nenhum do schema atual — só a duração
resultante em cada zona. Se um dia for preciso saber os limiares em si
(pra recalcular zona com uma fórmula diferente, por exemplo), seria
preciso um campo novo (a API expõe isso separadamente do resumo da
atividade, não investigado ainda) e um loader novo — fora de escopo por
ora, porque `vw_sessoes_ia` já usa os tempos por zona como estão, sem
precisar saber os limiares.

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
- [x] Conexão funciona local **(confirmado)** — falta a parte do GitHub Actions, ver Fase 8.
- [x] Nenhum arquivo de banco (`.duckdb`, dump, etc.) é commitado a partir desta fase.

### Fase 2 — Extração (`extract/garmin.py`)
**Status:** ✅ concluída, sem mudança — o mapeamento de campos independe do banco de destino.

### Fase 3 — Load incremental (revisão)
**Requisitos:** `load_atividades.py` e `load_recuperacao.py` trocam o driver
DuckDB por psycopg; lógica de `ON CONFLICT` (upsert) é compatível com
Postgres sem mudança de sintaxe.
**Critérios de aceite:**
- [x] Rodar o load duas vezes seguidas com os mesmos dados não duplica linhas (idempotência mantida) — testado em 2026-08-14 contra o Neon real, com dado sintético, para as duas tabelas.
- [ ] `stg_atividades` e `stg_recuperacao_diaria` continuam capturando todos os campos brutos, sem corte.

### Fase 4 — `vw_sessoes` (nova, substitui `fct_sessoes`)
**Requisitos:** view por `activity_id`, LEFT JOIN com recuperação (por data) e dor (por `activity_id`, ver Fase 6).
**Critérios de aceite:**
- [ ] Um dia com duas atividades gera duas linhas na view (nenhuma atividade descartada).
- [x] Nenhum script de transform/upsert é necessário para manter a view atualizada — é sempre live.

### Fase 5 — `vw_sessoes_ia` (nova)
**Requisitos:** view curada conforme lista da seção 6.
**Critérios de aceite:**
- [ ] Contém todos os campos listados na seção 6, e nenhum outro (em particular, sem `calorias`). Nota: com dor ainda sem nenhuma linha registrada, `dor`/`localizacao_dor`/`comentario_dor` chegam sempre `NULL` até a Fase 6 ter dado real — decidir antes da Fase 7 se isso é filtrado no Python ou tolerado como está.
- [ ] Consulta roda em menos de 1s (garante que não virou gargalo desnecessário).

### Fase 6 — Registro de dor via planilha + dashboard de desempenho
**Requisitos:** uma planilha Google Sheets com três abas, cada uma com uma
responsabilidade:
- **"Dor"** (`load_dor.py`) — input manual. Roda em duas direções na
  mesma execução: exporta pra planilha as atividades de `stg_atividades`
  sem linha correspondente em `stg_dor` (activity_id, data, nome, tipo,
  colunas de dor em branco), e importa de volta pra `stg_dor` (upsert por
  `activity_id`) as linhas já preenchidas.
- **"Atividades"** e **"Resumo Semanal"** (`planilha_desempenho.py`) —
  somente leitura, snapshot de `vw_sessoes`/`vw_resumo_semanal`
  (pace, velocidade, km, ACWR) sobrescrito a cada execução, sem upsert em
  nenhuma tabela — pra acompanhar desempenho/evolução sem abrir o Neon.

A mecânica de planilha (conectar, abrir/criar aba, inserir/sobrescrever
linhas) foi extraída pra `src/planilha.py`, compartilhada pelos dois
scripts — mesmo padrão de `src/db.py` do lado do Postgres, mantendo
`load_dor.py` e `planilha_desempenho.py` focados só na regra de negócio
de cada um.

Autenticação via service account do Google Cloud
(`GOOGLE_SHEETS_CREDENTIALS_PATH`, `GOOGLE_SHEET_ID` no `.env`/Secret).
**Critérios de aceite:**
- [x] `stg_dor` com PK `activity_id` (migrado de `data`), aplicado e
  validado contra o Neon real em 2026-08-14.
- [x] Fluxo testado ponta a ponta contra uma planilha real em 2026-08-14:
  exportar → preencher manualmente → importar → confirmado em `stg_dor`.
- [x] Rodar `load_dor.py` duas vezes seguidas não duplica linha nem na
  planilha nem em `stg_dor` — testado em 2026-08-14.
- [x] `planilha_desempenho.py` sobrescreve as abas em vez de acumular —
  testado rodando duas vezes seguidas, mesma contagem de linhas.
- [ ] Formatação (cabeçalho fixo/congelado) e validação de dados
  (dropdown pra `dor` 0-5 e pra `localizacao`/`superficie`/`tenis`) —
  ainda não implementado, avaliar se via código (`src/planilha.py`) ou
  ajuste manual na planilha.

### Fase 7 — Análise com IA (`analyze/analisar_com_ia.py`)
**Requisitos:** chamada à API Gemini a partir de `vw_sessoes_ia` +
`vw_resumo_semanal`; chamada ao LLM isolada numa função única
(`gerar_analise(prompt) -> texto`) para permitir trocar de provedor sem
reescrever o resto.
**Critérios de aceite:**
- [ ] Relatório inclui pelo menos uma recomendação concreta de treino (tipo, distância/duração alvo, intensidade).
- [ ] Relatório sinaliza explicitamente quando o ACWR (carga aguda/carga crônica) sai da zona segura 0.8–1.3, com alerta reforçado acima de 1.5 (guardrail de sobrecarga, ver seção 6.1).
- [ ] Chave da API vem de variável de ambiente, nunca hardcoded.

### Fase 8 — Orquestração (GitHub Actions)
**Requisitos:** `sync_atividades.yml` (schedule, ex. a cada 2-3h),
`sync_dor.yml` (ou incorporado ao `analise_semanal.yml` — roda
`load_dor.py` antes de gerar a análise, pra pegar dor preenchida durante
a semana) e `analise_semanal.yml` (schedule, domingo). Secrets: token
Garmin, `DATABASE_URL`, chave Gemini, credencial da service account do
Google Sheets.
**Critérios de aceite:**
- [ ] Pipeline roda ponta a ponta sem intervenção manual por pelo menos 2 semanas seguidas.
- [ ] Nenhum dado sensível (FC, sono, localização de dor) aparece em log do Actions.
- [ ] Nenhum commit automático de dado é gerado pelo workflow.

### Fase 9 — Ajuste fino
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

- **Excel como interface de dor** — confirmado fora de escopo em
  2026-08-13 (inviável manter sincronizado sem passo manual). Substituído
  por Google Sheets — ver Fase 6 (seção 7) e racional na seção 11. O
  registro de dor em si **não está mais fora de escopo** a partir desta
  revisão.
- **Docker em produção** — fica só como conveniência de dev local.

## 10. Critérios de sucesso

- Pipeline roda ponta a ponta sem intervenção manual.
- Relatório semanal chega automaticamente todo domingo com pelo menos uma
  recomendação concreta de treino.
- Relatório sinaliza risco de sobrecarga (ACWR fora da zona 0.8–1.3) quando aplicável.
- Nenhum dado sensível commitado no GitHub a partir desta revisão.
- Histórico consultável via SQL (Postgres) para qualquer análise futura.
- Manter constância de treino por 4+ semanas sem recidiva de dor ≥3
  (mensurável a partir da Fase 6, quando `stg_dor` passa a ter dado real).

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

Decisões da sessão de 2026-08-14, para referência futura:

- **Excel → Google Sheets, e dor volta a entrar em escopo**: a rejeição de
  2026-08-13 era do Excel especificamente (arquivo local, exige commit
  manual a cada anotação — incompatível com "totalmente automático"), não
  do registro de dor em si. Google Sheets resolve o problema real
  (acessível do celular, editável de qualquer lugar, sem passo de
  commit/push) via API com autenticação de service account — mesmo
  princípio do Neon (credencial de sistema, não login pessoal por trás de
  cada execução automatizada).
- **Por que vira Fase 6, entre `vw_sessoes_ia` e a análise com IA, e não
  depois da Fase 7/8 como o plano original implicava**: a Fase 7 (análise)
  já consome `vw_sessoes_ia`, que inclui `dor`/`localizacao_dor`/
  `comentario_dor` desde que essas colunas foram adicionadas ao join —
  ficariam sempre `NULL` no prompt até o loader existir, que é exatamente
  o tipo de contexto sem sinal que a curadoria da view foi desenhada pra
  evitar (seção 6.1). Resolver o registro de dor antes da Fase 7 evita
  escrever/testar o prompt contra um campo garantidamente vazio.
- **`stg_dor`: PK `data` → PK `activity_id`**: mesma motivação que já tinha
  levado `fct_sessoes` a virar `vw_sessoes` — uma data pode ter mais de uma
  atividade (múltiplas corridas no mesmo dia), cada uma com dor própria.
  Chave por `data` colapsaria as duas. A tabela estava vazia (sem loader
  ativo até agora), então a migração foi um `DROP TABLE` + reaplicação do
  `schema.sql`, sem risco de perda de dado real.
- **`vw_sessoes.classificacao_atividade` (nova coluna)**: hipótese inicial
  era usar % de tempo em `RWD_WALK` (>80% do tempo) pra identificar
  atividades que são "só caminhada" (aquecimento/deslocamento a pé) e
  rotulá-las como tal na planilha de desempenho. **Descartada após checar
  os dados**: como o treino usa o método run-walk, sessões de treino de
  verdade (`RECOVERY`, `AEROBIC_BASE`) também passam 70-96% do tempo em
  `RWD_WALK` por estrutura — a proporção de caminhada não distingue "foi
  só um aquecimento" de "foi treino de verdade". (Nota: uma investigação
  inicial, apresentada de forma incorreta durante a sessão por troca dos
  valores de correndo/caminhando na leitura da query, tinha "confirmado"
  a hipótese contrária — corrigido depois com uma query isolada e sem
  ambiguidade.) O sinal que sobrou, e que efetivamente diferencia as duas
  atividades curtas observadas: o próprio `efeito_treino_label = 'UNKNOWN'`
  da Garmin, sem nenhum cálculo adicional — ver seção 6.1 pra por que a
  regra de classificação da Garmin em si não é pública, então não há como
  fazer melhor que confiar no rótulo que ela já decidiu.
