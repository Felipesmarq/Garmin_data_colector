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
que esse é o único critério confiável disponível. Inclui também
`tipo_treino`, classificação mais específica (Rodagem/Longão/Ritmo/
Limiar/Intervalado/Tiro/Caminhada) pelo sistema de Jack Daniels, derivada
de % de tempo por zona de FC + efeito anaeróbico — ver seção 6.1.

**`vw_sessoes_ia` (view curada) — só os campos com sinal para geração de
treino + guardrail de sobrecarga:**
distância, duração, pace, FC média/máxima, cadência, tempo de contato com
solo, comprimento de passada, oscilação/razão vertical, velocidade máxima,
ganho/perda de elevação, efeito de treino aeróbico/anaeróbico, `tipo_treino`
(não `classificacao_atividade`/`efeito_treino_label` — mesmo vocabulário
que a Fase 7 usa pra recomendar, fecha o elo entre histórico e
recomendação), zonas de FC 1-5, sono (total + score), FC de repouso, body
battery ao acordar, dor (quando existir). **Excluído:** calorias — não
agrega sinal que duração+distância+FC já não capturem.
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

### Classificação de tipo de treino (`tipo_treino`) — fonte e método

Pesquisa feita em 2026-08-14 pra dar ao prompt da Fase 7 um vocabulário de
tipo de treino mais específico que `classificacao_atividade` (que só
repete o rótulo opaco da Garmin — seção anterior). Explicação genérica do
método — sem números reais do treino do Felipe.

**Fonte:** o sistema de zonas de treino de Jack Daniels (*Daniels' Running
Formula*), referência padrão em ciência do esporte pra treino de corrida,
divide sessões em cinco tipos por %VO2max: **E (Easy)** 65–78%, base
aeróbica/recuperação; **M (Marathon)** 80–84%, ritmo de prova longa;
**T (Threshold)** 88–92%, eleva limiar de lactato; **I (Interval)**
95–100%, maximiza VO2max, repetições de 3–5min; **R (Repetition)** acima
de 100% (ritmo de milha), potência anaeróbica/velocidade/economia,
repetições curtas com recuperação completa. Mapeado pra nomenclatura
usada no Brasil: Rodagem/Recuperação (E), Longão (E estendido), Ritmo
(M), Limiar (T), Intervalado (I), Tiro (R), mais Fartlek (variação livre
de ritmo, sem estrutura fixa — mistura estímulo aeróbico/anaeróbico).

**Por que não usar a estrutura de splits (RWD_RUN/RWD_WALK) como
critério:** a hipótese inicial era usar % de tempo em caminhada pra
detectar o tipo de treino, mas isso já tinha sido descartado pra
`classificacao_atividade` (seção 11) pelo mesmo motivo que se aplica
aqui — o método run-walk faz treinos de intensidade bem diferente
passarem proporções parecidas de tempo caminhando, só por estrutura.
Estrutura de split não é sinal de intensidade.

**Método adotado:** classificar por **distribuição de tempo por zona de
FC** (`tempo_zona_fc_1..5`, já validada como coluna por atividade —
seção anterior) mais `efeito_treino_anaerobico` como gatilho pra
Intervalado/Tiro. Zona é sinal direto de intensidade relativa (%FC
máxima), o que a estrutura de split não é. Regra (implementada em
`vw_sessoes`, coluna `tipo_treino`):
- Zonas 4+5 ≥ 40% do tempo → **Limiar**.
- Zonas 4+5 ≥ 20% ou zona 3 ≥ 30% → **Ritmo**.
- Zonas 1+2 ≥ 85% do tempo e duração ≥ 35min → **Longão**.
- Efeito anaeróbico ≥ 1.5 e zonas 4+5 ≥ 30% → **Tiro** (duração < 20min)
  ou **Intervalado** (duração ≥ 20min).
- `efeito_treino_label = 'UNKNOWN'` → **Caminhada** (mesmo critério de
  `classificacao_atividade`).
- Nenhum dos critérios acima → **Rodagem/Recuperação** (default).

**Validação:** testado contra o histórico real de atividades do Felipe
antes de virar código definitivo (não só teoria) — resultado confirmado
como coerente com o esforço percebido de cada sessão. Um achado notável:
com o volume de treino até 2026-08-14, `efeito_treino_anaerobico` nunca
passou de um patamar baixo em nenhuma atividade — ou seja, nenhuma
sessão registrada até agora foi um Intervalado/Tiro de verdade pelo
critério fisiológico da própria Garmin, mesmo em sessões com alternância
de ritmo. Confirma que classificar por estrutura de split teria gerado
falsos positivos.

**Limitação conhecida:** sem VO2max/paces de referência calibrados (ex.
via um tempo de prova recente), os limiares de zona usados são os que o
relógio já define, não os %VO2max exatos do sistema Daniels — a
classificação é uma aproximação por zona relativa, não o método
original ao pé da letra.

**Fontes:** [Daniels' Running Formula — resumo (Fellrnr)](https://fellrnr.com/wiki/Jack_Daniels) · [Jack Daniels' Running Intensity — Coach Ray](https://www.coachray.nz/2023/05/03/jack-daniels-running-intensity/) · [Fartlek — Wikipedia](https://en.wikipedia.org/wiki/Fartlek) · [Tipos de treino de corrida em português — Corrida Perfeita](https://www.corridaperfeita.com/treinos-de-corrida-intervalado-fartlek-de-ritmo-e-educativo/)

### Composição da semana — por que o plano gerado variava demais entre execuções

Pesquisa feita em 2026-08-14/15 depois de observar, na prática, que
gerações sucessivas do plano semanal (mesmos dados de entrada, sem
mudança real na semana) produziam estruturas bem diferentes entre si —
ora 1 treino de intensidade, ora 2, sem critério fixo. O prompt até
então descrevia os tipos de treino disponíveis, mas não restringia
*quantos* de cada tipo cabem numa semana nem *em que ordem* — deixava
essa decisão inteiramente a critério do LLM a cada chamada, que é
exatamente o tipo de grau de liberdade que gera variação arbitrária
entre execuções.

**Distribuição polarizada (Seiler & Kjerland, 2006, *Scandinavian
Journal of Medicine & Science in Sports*):** atletas de endurance de
elite treinam, na prática, ~80% do tempo em baixa intensidade e ~20%
em alta intensidade — não uma mistura de intensidade média (os
chamados "junk miles"), mas uma distribuição bimodal. Adotado como
regra fixa no prompt (`REGRAS_COMPOSICAO_SEMANA`,
`src/analyze/analisar_com_ia.py`): no máximo 1 treino de intensidade
(Ritmo, Limiar, Intervalado ou Tiro) por semana, o resto tem que ser
Rodagem/Recuperação ou Longão.

**Alternância hard/easy:** princípio clássico de treinamento
(popularizado por Bill Bowerman, ainda referência corrente) — nunca
dois dias de esforço seguidos sem pelo menos 1 dia de descanso entre
eles. Evita empilhar estímulo sem recuperação suficiente entre um
treino forte e o próximo.

**Guardrail de dor — progressão sessão a sessão, não só carga semanal
agregada:** o ACWR (seção acima) já cobre risco de sobrecarga na
carga *agregada* da semana, mas não decide se a *intensidade* deveria
avançar dado o que aconteceu na sessão mais recente especificamente.
Protocolos de retomada pós-MTSS/canelite documentados em medicina
esportiva seguem uma lógica de fases: avança intensidade só depois de
sessões consecutivas sem dor; se a dor voltar, regride uma fase
inteira. Implementado como segundo guardrail determinístico
(`_instrucao_guardrail_dor`, mesmo padrão do guardrail de ACWR — Python
calcula, prompt recebe instrução obrigatória, não sugestão): se a dor
mais recente registrada em `stg_dor` for **nível 2 ou mais** (escala
0-5), a semana inteira fica restrita a Rodagem/Recuperação ou Longão,
nenhum tipo de intensidade — mesmo que o ACWR estivesse permitindo.
Validado contra dado real: com dor recente = 2, o plano gerado zerou
intensidade nos treinos da semana e moveu Intervalado/Tiro só pras
`estimativas_futuras` (fora do plano em si), como esperado.

**Fontes:** [Seiler & Kjerland (2006) — resumo do treino polarizado](https://bodyrecomposition.com/training/hard-days-hard-easy-days-easy) · [Princípio hard/easy — RunSpirited](https://www.runspirited.com/single-post/why-runners-should-embrace-hard-days-hard-easy-days-easy) · [Protocolo de retomada em 6 fases pós-shin splints — Ubie Doctor's Note](https://ubiehealth.com/doctors-note/shin-splints-treatments-return-running-protocol-5762q2) · [Medial Tibial Stress Syndrome — Physiopedia](https://www.physio-pedia.com/Medial_Tibial_Stress_Syndrome)

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
  Nota: o mecanismo de escrita de "Atividades" é revisado na Fase 9
  (seção 7) — deixa de ser sobrescrita total e passa a ser upsert.

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
  testado rodando duas vezes seguidas, mesma contagem de linhas. Válido
  para "Resumo Semanal"; "Atividades" passa a fazer upsert a partir da
  Fase 9 (ver seção 7) — sobrescrita total ficou cara demais conforme o
  histórico cresce.
- [ ] Formatação (cabeçalho fixo/congelado) e validação de dados
  (dropdown pra `dor` 0-5 e pra `localizacao`/`superficie`/`tenis`) —
  ainda não implementado, avaliar se via código (`src/planilha.py`) ou
  ajuste manual na planilha.

### Fase 7 — Análise com IA (`analyze/analisar_com_ia.py`)
**Requisitos:** chamada à API Gemini (`google-genai`, modelo
`gemini-3.5-flash` configurável via `GEMINI_MODEL`) a partir de
`vw_sessoes_ia` (últimas `JANELA_SEMANAS` = 4 semanas, mesma unidade da
carga crônica do ACWR) + `vw_resumo_semanal`; chamada ao LLM isolada numa
função única (`gerar_analise(prompt) -> texto`) para permitir trocar de
provedor sem reescrever o resto -- devolve JSON validado contra o schema
`PlanoSemanal` (`response_schema` do Gemini), não texto livre, pra dar
pra inserir direto na planilha sem parsing frágil. Saída é o **plano da
semana** (exatamente 7 dias, Segunda a Domingo, incluindo descanso;
datas calculadas em Python via `_proxima_semana`, nunca confiadas ao
LLM) -- usa o vocabulário fechado de `tipo_treino` (seção 6.1) tanto pro
histórico quanto pra recomendação. Escrito na aba "Plano da Semana"
(`escrever_plano`) em modo **append-only**, idempotente por data da
segunda-feira -- cada semana é um registro histórico que se acumula, não
um snapshot pra sobrescrever (diferente do dashboard da Fase 6). Só as
7 linhas de dia vão pra planilha -- `estimativas_futuras` (tipos de
treino sem histórico ainda, ex. Intervalado/Tiro, cada uma com
`prazo_estimado` explicando quando/sob que critério tentar) e
`logica_geral` não têm "1 dia" pra ocupar numa tabela histórica, então
vão só pro terminal e pro **e-mail semanal** (`enviar_email_resumo`,
Gmail SMTP via `smtplib` da biblioteca padrão -- zero dependência nova).
Prompt também lê a aba "Observações" (`_observacoes_recentes`) -- texto
livre por semana, 100% preenchido pelo usuário, nunca escrito pelo
script, mesma janela de 4 semanas dos outros dados.

Dois guardrails determinísticos em Python, não uma aposta em o modelo
perceber sozinho: `_instrucao_guardrail_acwr` (carga semanal agregada,
só dispara acima de 1.3, nunca pra ACWR baixo) e
`_instrucao_guardrail_dor` (progressão sessão a sessão -- trava
intensidade se a dor mais recente registrada foi >= 2, protocolo de
retomada pós-canelite, seção 6.1). `REGRAS_COMPOSICAO_SEMANA`
(distribuição polarizada 80/20 + alternância hard/easy, seção 6.1)
reduz a variação arbitrária de estrutura da semana entre gerações
sucessivas com os mesmos dados de entrada.
**Critérios de aceite:**
- [x] Relatório inclui pelo menos uma recomendação concreta de treino (tipo da lista `tipo_treino`, distância/duração alvo, intensidade) -- garantido estruturalmente pelo `response_schema`, não só por instrução de texto.
- [x] Relatório sinaliza explicitamente quando o ACWR sai da zona segura acima de 1.3, com alerta reforçado acima de 1.5 (guardrail de sobrecarga, ver seção 6.1) -- testado com dado real (dispara/não dispara conforme esperado).
- [x] Chave da API vem de variável de ambiente (`GEMINI_API_KEY`), nunca hardcoded.
- [x] Chamada real ao Gemini testada ponta a ponta -- validada múltiplas vezes com dado real, incluindo os dois guardrails disparando juntos.
- [x] Escrita na planilha é idempotente (rodar 2x na mesma semana não duplica linha) -- testado.
- [x] E-mail semanal (plano completo + estimativas futuras + lógica geral) enviado e recebido -- confirmado pelo usuário em teste real.

### Fase 8 — Orquestração (GitHub Actions)
**Requisitos:** dois workflows, sem Docker (`actions/setup-python`,
Python 3.12 -- decisão tomada nesta fase, ver seção 11): `sync_atividades.yml`
(diário, 21h BRT -- carrega atividades, recuperação, sincroniza dor via
`load_dor.py` e atualiza o dashboard via `planilha_desempenho.py`, tudo
no mesmo workflow) e `analise_semanal.yml` (domingo, 20h BRT -- resincroniza
atividades/recuperação/dor na própria execução, não depende do horário
do sync diário ter rodado antes, e gera o plano via `analisar_com_ia.py`).
Cada passo usa `nick-fields/retry` (até 3 tentativas) antes de falhar de
vez, só aí o e-mail padrão do GitHub de workflow agendado que falhou
dispara. Secrets: `GARMIN_EMAIL`, `GARMIN_PASSWORD` (fallback), `GARMIN_TOKEN`
(token de sessão já autenticado -- ver seção 11 pra por que isso funciona
sem senha/MFA a cada run), `DATABASE_URL`, `GOOGLE_SHEET_ID`,
`GOOGLE_SHEETS_CREDENTIALS_JSON`, `GEMINI_API_KEY`, `GEMINI_MODEL`,
`EMAIL_REMETENTE`, `EMAIL_SENHA_APP` (senha de app do Gmail),
`EMAIL_DESTINATARIO` (opcional).
**Critérios de aceite:**
- [x] Workflows escritos e validados sintaticamente (`yaml.safe_load` sem erro, jobs corretos).
- [x] `conectar()` (`extract/garmin.py`) falha rápido com mensagem clara em CI sem senha disponível, em vez de travar esperando `input()`/`getpass()` que nunca chega (runner não tem terminal).
- [ ] Pipeline roda ponta a ponta sem intervenção manual por pelo menos 2 semanas seguidas -- pendente do usuário configurar os Secrets (comandos no README) e deixar rodar.
- [ ] Nenhum dado sensível (FC, sono, localização de dor) aparece em log do Actions -- por design (Secrets são mascarados automaticamente pelo GitHub, nenhum script imprime valor de Secret), falta confirmar num run real.
- [x] Nenhum commit automático de dado é gerado pelo workflow -- nenhum step chama `git commit`/`git push`, só leitura/escrita em Neon e Google Sheets.

### Fase 9 — Verificação de aderência (plano vs. treino real)
**Requisitos:** comparação determinística entre "Plano da Semana" (o que
foi planejado) e `vw_sessoes` (o que foi realizado), por dia da semana
corrente -- sem chamada ao Gemini, mesmo princípio dos guardrails de ACWR
e dor (o que é mensurável não depende do modelo perceber sozinho, ver
seção 11). Regra de correspondência: dia sem atividade real vira
`NÃO_REALIZADO`; mais de uma atividade no mesmo dia soma distância e
duração antes de comparar. Regra de aprovação: `tipo_treino` tem que
bater exatamente, e distância/duração/pace/FC precisam estar todas dentro
da margem (±20% distância/duração, ±30s/km pace, ±10bpm FC) para contar
como `REALIZADO_DENTRO_DA_MARGEM` -- fora de qualquer uma delas vira
`REALIZADO_FORA_DA_MARGEM`. Margens são um ponto de partida, ajustável no
código depois de ver algumas semanas de dado real.

Escrita: `escrever_atividades` deixa de sobrescrever a aba inteira a cada
execução (custo que só cresce conforme o histórico acumula) e passa a
fazer **upsert por `activity_id`** -- atividade já presente na planilha
não é tocada de novo, só as novas são inseridas. Isso por si só já
resolve, de graça, o problema original que motivava ler "Plano da
Semana" antes de reescrever: sem sobrescrita total, não existe mais o
risco de apagar linha nenhuma por acidente. Efeito colateral aceito
conscientemente: uma atividade corrigida ou apagada direto no Postgres
não é mais refletida automaticamente na planilha (ver seção 11) -- caso
raro, sem lógica de reconciliação.

As linhas de dia planejado usam uma segunda operação de upsert, por
`data` em vez de `activity_id` (não têm atividade real associada até
acontecerem): `analisar_com_ia.py`, no domingo, insere os 7 dias da
semana com status `PLANEJADO`. `planilha_desempenho.py` (job diário),
além de inserir atividades novas por `activity_id` como sempre, resolve
a linha de dia planejado correspondente à data **estritamente passada**
(nunca hoje -- ver bug corrigido em 2026-09-16 nesta seção) com o
resultado -- `REALIZADO_DENTRO_DA_MARGEM`, `REALIZADO_FORA_DA_MARGEM` ou
`NÃO_REALIZADO`. Quando existe atividade real pro dia, o veredito é
**fundido na própria linha da atividade** (só as colunas de plano são
escritas; as colunas de atividade real já preenchidas por
`escrever_atividades` não são tocadas) e a linha-placeholder é removida
-- sem isso, o dia aparecia duas vezes na aba (achado também em produção,
ver seção 11). Sem atividade real (`NÃO_REALIZADO`), não há o que
fundir, e o placeholder é só atualizado no lugar. Nenhuma operação lê ou
reescreve a aba inteira -- cada uma toca só as linhas que precisa.

Lembrete diário: `sync_atividades.yml` (job diário já existente) passa a
mandar um e-mail curto quando o dia anterior ficou `NÃO_REALIZADO` ou
`REALIZADO_FORA_DA_MARGEM` -- só dispara e-mail quando há algo a avisar.
Sem retroalimentação nesta fase: o resultado da verificação **não** entra
no prompt da análise semanal nem vira guardrail -- é só informativo.

**Critérios de aceite:**
- [x] Um dia planejado sem atividade correspondente **e já passado** (estritamente anterior a hoje) aparece como `NÃO_REALIZADO` na aba "Atividades" depois do sync diário; o dia de **hoje** nunca é resolvido, mesmo sem atividade ainda, porque o treino pode acontecer mais tarde no mesmo dia. Testado em 2026-09-08 com data sintética (2020-01-01, sem atividade no Postgres) e em 2026-09-16 com uma linha de teste pra hoje (confirmado que fica intocada). Bug real corrigido em 2026-09-16 -- ver "Bug real de produção" no fim desta seção.
- [x] Um dia com atividade dentro de todas as margens aparece como `REALIZADO_DENTRO_DA_MARGEM`; fora de qualquer margem, `REALIZADO_FORA_DA_MARGEM`. Testado com dado real (2026-08-15) e um plano propositalmente incompatível (2026-08-13).
- [x] Duas atividades no mesmo dia são somadas antes da comparação, não tratadas como linhas separadas. Testado em 2026-08-13 (2 atividades reais no dia, agregadas numa linha só antes da comparação).
- [x] Quando existe atividade real pro dia resolvido, o veredito funde na linha da atividade (não duplica a linha do dia). Bug real de duplicação corrigido em 2026-09-16 -- ver "Bug real de produção" no fim desta seção. Testado com 1 atividade (funde), 2 atividades (funde na de maior duração, a outra permanece intacta) e nenhuma atividade (sem fusão, placeholder atualizado no lugar).
- [x] Rodar o job diário duas vezes seguidas não duplica linha nem perde as linhas de dia planejado escritas no domingo. Testado em 2026-09-08.
- [x] E-mail diário de lembrete só chega quando há pelo menos um dia `NÃO_REALIZADO`/`REALIZADO_FORA_DA_MARGEM` no dia anterior -- mecanismo de envio testado (mesma função `enviar_email` do e-mail semanal, já validada); o gatilho por data (`ontem`) não foi validado em produção real ainda, só por inspeção de código.
- [x] `escrever_atividades` não reescreve linhas de atividade já presentes na planilha -- só insere as novas (upsert por `activity_id`, sem sobrescrita total). Testado em 2026-09-08 contra o Neon e a planilha reais: migração automática do cabeçalho (15 atividades reescritas com a coluna nova, ordem mais-recente-primeiro preservada) e, na segunda execução, 0 atividades reinseridas.

### Fase 10 — Filtro de tokens (rede de segurança pro Gemini)
**Requisitos:** antes de cada chamada a `gerar_analise()`, contar os
tokens do prompt final via `count_tokens` da API do Gemini (contagem
exata, não estimativa por caractere). Teto: 200 mil tokens -- bem abaixo
do limite real do modelo (até 1M), calibrado como sinal de que algo fugiu
do esperado (ex. observação colada por engano, bug na janela de 4
semanas), não como limite operacional do dia a dia. Acima do teto, a
execução aborta com mensagem clara (`RuntimeError`, mesmo padrão do
fail-fast de `conectar()` em `extract/garmin.py`) -- sem gerar plano
nenhum. O retry do workflow (`nick-fields/retry`, Fase 8) tenta de novo;
se persistir, dispara o e-mail padrão do GitHub de falha de workflow
agendado.

**Critérios de aceite:**
- [x] Prompt real (janela de 4 semanas atual) passa longe do teto -- confirma que o filtro não atrapalha a operação normal. Testado em 2026-09-10: 2.847 tokens, contra um teto de 200 mil.
- [x] Um prompt sintético acima de 200 mil tokens aborta a execução com mensagem clara, sem chamar `generate_content` (a checagem fica dentro de `gerar_analise()`, antes da chamada de geração em si -- não antes de `gerar_analise()` como um todo, pra manter a chamada ao Gemini isolada numa função só, ver seção 4). Testado com prompt sintético de ~400 mil tokens.
- [x] A contagem usa a API do Gemini (`count_tokens`), não uma aproximação local. Confirmado por leitura do código e pelos testes acima.

### Fase 11 — Revisão de coerência do plano gerado
**Requisitos:** depois de `_plano_semanal()` validar o schema (7 dias),
uma segunda chamada ao Gemini (`revisar_coerencia`, isolada de
`gerar_analise` -- não autoavaliação na mesma resposta, ver seção 11)
recebe o prompt original + o plano gerado (em texto, via `_texto_plano`)
e devolve um veredito estruturado via `response_schema` próprio
(`RevisaoCoerencia`: `coerente: bool`, `motivo: str`,
`problemas: list[str]`) -- mesmo padrão de saída estruturada já usado na
geração do plano, não texto livre. Quais guardrails dispararam **não** é
passado como argumento separado -- já vem embutido no prompt original
(`instrucoes_guardrail` já é concatenado em `_construir_prompt`), então
reenviar o prompt inteiro pro revisor já carrega essa informação, sem
duplicar. A revisão cobre três coisas: inconsistência interna (o
`motivo` de um dia não bate com o tipo/distância/intensidade
escolhidos), contradição com o histórico/dados de entrada, e violação de
guardrail que tenha escapado da restrição de vocabulário (ex.
intensidade alta disfarçada dentro de um treino rotulado como Rodagem,
numa semana em que o guardrail de dor deveria ter restringido o
vocabulário).

Se a revisão reprovar, gera o plano de novo (nova chamada a
`gerar_analise()` com o mesmo prompt) e revisa de novo, até
`MAX_TENTATIVAS_REVISAO` (3) tentativas no total. Se todas reprovarem,
aborta com `RuntimeError` claro (mesmo padrão fail-fast das Fases 9 e
10) -- sem chamar `escrever_plano`/`escrever_dias_planejados`/
`enviar_email_resumo`. Reprovações ficam registradas só no log da
execução do GitHub Actions (print no stdout), sem linha nova na
planilha -- é esperado ser raro. O teto de tokens da Fase 10 também se
aplica ao prompt de revisão (reutiliza `_verificar_teto_tokens`), já que
ele embute o prompt original inteiro e por isso é ainda maior.

**Critérios de aceite:**
- [x] Plano coerente (dado real) é aprovado na primeira revisão, sem gerar retry desnecessário. Testado em 2026-09-10 com plano real gerado pelo Gemini -- `coerente=True` na primeira tentativa.
- [x] Um plano construído propositalmente com inconsistência (ex. motivo que contradiz o tipo de treino) é reprovado pela revisão. Testado com um dia rotulado "Rodagem/Recuperação" mas com motivo/pace/FC de um treino de tiros máximos, numa semana com guardrail de dor ativo (nível 3) -- reprovado com 3 problemas específicos listados (dia, campo, contradição), não genéricos.
- [x] Esgotadas as 3 tentativas sem aprovação, a execução aborta com mensagem clara, sem chamar `escrever_plano`/`enviar_email_resumo`. Testado com `revisar_coerencia`/`gerar_analise` simulados (sempre reprova) -- confirmado exatamente `MAX_TENTATIVAS_REVISAO` chamadas de geração, zero chamadas a `escrever_plano`, `RuntimeError` levantado.

### Fase 12 — Enviar o plano da semana como treino estruturado pro relógio
**Requisitos:** depois que o plano é aprovado pela revisão de coerência
(Fase 11) -- nunca por tentativa/candidato reprovado -- cria um treino
estruturado no Garmin Connect (`RunningWorkout`, biblioteca
`garminconnect` já usada no projeto, sem dependência nova) pra cada dia
de treino da semana (dias de descanso não geram treino nenhum). Passos
por dia: aquecimento opcional, o treino principal (por distância se
`distancia_km` estiver preenchido, por duração se for `duracao_min`), e
desaquecimento -- usando `create_warmup_step`/`create_distance_interval_step`/
`create_interval_step`/`create_cooldown_step`.

Alvo do passo principal depende do tipo de treino, não uma escolha
única fixa: Rodagem/Recuperação, Longão e Fartlek usam **FC** como alvo
(foco em não passar do esforço seguro -- Fartlek entra aqui por não ter
estrutura fixa de ritmo, o que importa é o esforço); Ritmo, Limiar,
Intervalado e Tiro usam **pace** como alvo (foco em acertar o ritmo de
prova/performance). O Garmin só aceita um tipo de alvo por passo, não os
dois simultaneamente, mesmo que o relógio continue mostrando pace e FC
ao vivo independente do alvo configurado.

Nome do treino inclui tipo + data (ex. "Rodagem/Recuperação -- 15/09"),
descrição usa o `motivo` gerado pela IA pra aquele dia -- aparece como
contexto no relógio/app. Upload via `client.upload_running_workout(...)`
e agendamento na data do dia via `client.schedule_workout(workout_id,
data)`.

**Falha não aborta a semana**: é best-effort -- se o upload/agendamento
falhar (API não-oficial, pode quebrar sem aviso), registra no log e
segue gerando a planilha e o e-mail normalmente. A recomendação em si
(o que o projeto existe pra fazer) não fica refém de uma integração
extra mais frágil que o resto.

**Idempotência**: antes de criar, procura um treino já existente pra
aquela data (pelo nome, que inclui a data) via `get_workouts`/
`get_scheduled_workouts`, e usa `update_workout` em vez de criar de novo
se encontrar -- evita duplicar se a semana inteira for reprocessada
(retry do workflow, execução manual repetida).

**Fora de escopo por ora**: limpeza de treinos de semanas passadas
(aceito acumular na biblioteca do Garmin Connect; resolve depois, como
ajuste fino, se virar problema de uso real). Sincronização em si depende
do relógio conectar via Bluetooth com o Garmin Connect Mobile (o
Forerunner 165 não-Music não tem Wi-Fi embutido) -- o treino fica
esperando no Garmin Connect até a próxima sincronização do relógio, não
é instantâneo.

**Formato exato do valor de alvo (pace/FC) ainda não determinado**: as
funções helper da lib (`create_interval_step` etc.) só expõem o *tipo*
de alvo (`target_type`, ex. `PACE_ZONE`/`HEART_RATE_ZONE`); o *valor*
(a faixa de pace/FC em si) não tem parâmetro dedicado nos helpers --
`ExecutableStep` aceita campos extras (`model_config = {"extra":
"allow"}`), então prováveis candidatos são `targetValueOne`/
`targetValueTwo`, mas a unidade exata (pace em m/s? FC em bpm ou número
de zona 1-5?) precisa ser confirmada testando contra a API real antes de
fechar a implementação -- não é uma decisão de design, é uma
investigação técnica a fazer durante a implementação.

**Critérios de aceite:**
- [ ] Um dia de treino (não descanso) vira um treino agendado no Garmin Connect, na data certa, com o tipo de alvo certo (FC pra Rodagem/Recuperação/Longão/Fartlek, pace pros demais).
- [ ] Dia de descanso não gera treino nenhum.
- [ ] Falha simulada no upload não impede a escrita na planilha nem o envio do e-mail.
- [ ] Rodar a mesma semana duas vezes atualiza o treino existente (mesmo `workoutId`), não cria um segundo.
- [ ] Nome e descrição do treino no Garmin Connect batem com o tipo/data/motivo do dia.

### Fase 13 — Ajuste fino
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
- **`vw_sessoes.tipo_treino` (nova coluna, prep pra Fase 7)**: o prompt da
  Fase 7 precisava de um vocabulário de tipo de treino específico o
  bastante pra soar como um treinador de verdade (Rodagem, Tiro,
  Intervalado, Fartlek, etc.), não só o rótulo genérico da Garmin. Decisão
  explícita do usuário: essa classificação tem que ser **código
  determinístico testável**, não uma instrução solta no prompt esperando
  que o LLM infira sozinho. Adotado o sistema de Jack Daniels (seção 6.1)
  classificado por % de tempo por zona de FC + efeito anaeróbico — mesmo
  cuidado do item acima: **não** usar estrutura de split (RWD_RUN/
  RWD_WALK) como sinal de intensidade, porque o método run-walk faz
  sessões de intensidade bem diferente parecerem estruturalmente
  parecidas. Testado contra o histórico real do usuário antes de virar
  código definitivo — validado como coerente com o esforço percebido de
  cada sessão. `classificacao_atividade` continua existindo (usada pelo
  dashboard da Fase 6); `tipo_treino` substitui ela em `vw_sessoes_ia`
  por ser estritamente mais específica pro propósito da Fase 7.

Decisões da sessão de 2026-08-17 (Fase 8), para referência futura:

- **Token da Garmin: Secret estático, não cache do Actions**: pesquisado o
  comportamento da lib (`garth`/`garminconnect`) -- o token OAuth1 dura
  ~1 ano, e o token DI usado nas chamadas se auto-renova indefinidamente
  enquanto o refresh token continuar válido, sem precisar de senha/MFA a
  cada execução. Isso torna viável subir o token atual como Secret
  (`GARMIN_TOKEN`) e escrevê-lo no arquivo a cada run, sem reautenticar.
  Risco aceito conscientemente: não há confirmação de que o refresh token
  da Garmin seja reutilizável (mesmo valor sempre válido) vs. rotativo
  (cada renovação invalida o anterior) -- a documentação não é clara
  nisso. Se for rotativo, o Secret pode parar de funcionar em algum
  momento; mitigação é monitorar falhas (guardrail de `conectar()` abaixo)
  e reautenticar localmente quando acontecer, não uma solução preventiva
  mais robusta (ex. `actions/cache` auto-atualizável), que ficaria pra
  Fase 13 (Ajuste fino) se o problema realmente aparecer na prática.
- **`conectar()` falha rápido em CI sem senha**: sem isso, um token
  expirado em produção faria o código cair no loop de `getpass()`/
  `input()` esperando senha/MFA -- num runner sem terminal, isso trava até
  o timeout do job (podem ser horas) em vez de falhar em segundos com uma
  mensagem acionável. Detecção via `GITHUB_ACTIONS` (variável que o
  próprio Actions define em todo run).
- **`actions/setup-python`, não Docker, na CI**: decisão que já vinha
  sendo adiada desde a Fase 7 (ver discussão da época). Fechada a favor
  do runner nativo -- mais simples, mais rápido (sem build de imagem a
  cada execução), e o Dockerfile já existente continua servindo só pra
  dev local, como o case sempre documentou.
- **`analise_semanal.yml` resincroniza os próprios dados, não depende do
  `sync_atividades.yml` ter rodado antes**: achado durante o desenho da
  Fase 8 -- com sync diário às 21h BRT e análise domingo às 20h BRT, a
  análise rodaria *antes* do sync daquele domingo (20h < 21h), pegando
  dado de sábado em vez do fim de semana completo. Em vez de ajustar
  horários pra evitar a coincidência (frágil -- quebra de novo se algum
  horário mudar), a análise ganhou seus próprios passos de sync
  (atividades, recuperação, dor) logo antes de chamar a IA -- correto
  independente de quando o outro workflow rodou.
- **Retry por passo (`nick-fields/retry`), não custom em Python**:
  resiliência a falha transitória (rede, rate limit temporário) foi pedida
  explicitamente pra não disparar e-mail de falha na primeira tentativa.
  Implementado na camada de CI (Actions), não como loop de retry dentro
  dos scripts Python -- mantém os scripts focados só na lógica de negócio,
  e a política de quantas tentativas/quanto esperar fica declarativa no
  YAML, fácil de ajustar sem tocar em código.
- **`reusable workflow` (`workflow_call`) em vez de duplicar os passos de
  sync**: pedido do usuário depois de rodar `analise_semanal.yml`
  manualmente pela primeira vez e notar que os 4 passos de carga de dado
  apareciam idênticos nos dois workflows. Limitação aceita conscientemente:
  cada job do Actions roda numa VM isolada, sem filesystem compartilhado --
  o job "analisar" ainda repete checkout/setup-python/restaurar token e
  credencial (~15 linhas estáveis), só a lógica de negócio (carregar
  atividades/recuperação/dor) deixou de estar duplicada. Cache de pip
  (`actions/setup-python` `cache: pip`) adicionado no mesmo momento pra
  acelerar o setup sem reabrir a decisão Docker-vs-setup-python.
- **Estimativas futuras e resumo saem da planilha, vão pro e-mail**: as
  linhas de `estimativas_futuras`/`logica_geral` (sem dia da semana
  específico) não se encaixavam numa tabela pensada como histórico
  diário (1 linha = 1 dia) -- ficavam órfãs, sem `data`/`dia_semana`
  preenchido. Em vez de forçar essas informações numa estrutura tabular
  que não serve pra elas, viraram conteúdo do e-mail semanal (que já
  precisava existir por outro motivo -- ver item abaixo) e continuam no
  print do terminal; a planilha "Plano da Semana" ficou estritamente
  tabular, 7 linhas por semana, sem exceção.
- **`prazo_estimado` explícito em `estimativas_futuras`**: a versão
  original só dizia "pace X, FC Y" pra tipos de treino ainda não
  tentados, sem dizer quando o corredor teria uma base pra tentar --
  vago demais pra ser acionável. Prompt passou a exigir um critério
  concreto (ex. "depois de 3 semanas seguidas de Limiar sem dor") ou
  uma janela de tempo, ancorado no mesmo protocolo de retomada pós-MTSS
  que já embasa o guardrail de dor (seção 6.1).
- **E-mail semanal via Gmail SMTP (`smtplib`, biblioteca padrão)**: opção
  escolhida sobre um serviço terceiro (Resend/SendGrid) por não exigir
  cadastro em mais um serviço nem dependência nova -- o usuário já tem
  conta Gmail (a mesma usada em `GARMIN_EMAIL`) e só precisou gerar uma
  senha de app. Testado ponta a ponta: e-mail chegou, conteúdo (plano +
  estimativas + lógica geral) confirmado pelo usuário.
- **Aba "Observações" (texto livre, só leitura pelo script)**: pedido do
  usuário pra capturar contexto que os dados objetivos não capturam
  (viagem, imprevisto, sensação subjetiva da semana). Mesmo padrão de
  "input manual, script nunca escreve" da aba "Dor" (Fase 6), mas sem a
  etapa de export -- não há nada pra pré-popular, é uma nota livre por
  semana. Lida com a mesma janela `JANELA_SEMANAS` dos outros dados, pra
  manter consistência de "o que a IA vê" através do prompt inteiro.

Decisões da sessão de grilling de 2026-09-08 (Fases 9, 10 e 11), para
referência futura:

- **Verificação de aderência não realimenta o prompt (por enquanto)**:
  primeira ideia era usar a aderência como sinal pra IA gerar a semana
  seguinte, ou até como um terceiro guardrail. Decisão explícita do
  usuário: manter informativo por ora -- introduzir um sinal novo, ainda
  não validado contra dado real, dentro da lógica que já tem dois
  guardrails determinísticos, é exatamente o tipo de heurística que este
  projeto evita adotar sem antes ver como ela se comporta na prática.
- **Linhas de dia planejado na aba "Atividades", não uma aba nova**: pedido
  explícito do usuário, ao contrário da primeira proposta (aba
  "Aderência" dedicada). Consequência arquitetural encontrada durante o
  grilling: `escrever_atividades` (`planilha_desempenho.py`, job diário)
  sobrescreve a aba inteira só a partir de `vw_sessoes` -- sem mudança,
  o sync do dia seguinte apagaria as linhas de plano escritas no domingo.
  Resolvido fazendo o job diário ler a aba "Plano da Semana" via Sheets
  API antes de reconstruir "Atividades", em vez de criar uma tabela nova
  no Postgres pro plano -- menos mudança de schema, e a aba "Plano da
  Semana" já tem exatamente as colunas necessárias.
- **Lembrete diário, não semanal**: o pedido original ("lembrar de repor
  no dia seguinte") só faz sentido com checagem diária -- um e-mail só
  no domingo avisaria até 6 dias depois de um treino perdido na terça.
  Resolvido estendendo `sync_atividades.yml` (já roda todo dia) em vez de
  esperar o e-mail semanal de `analise_semanal.yml`.
- **Filtro de tokens é rede de segurança, não limite operacional**: a
  janela de 4 semanas já mantém o prompt bem abaixo de qualquer teto
  realista, e o teto de 200 mil tokens (bem abaixo do limite real de 1M
  do Gemini) foi calibrado pra nunca disparar em operação normal -- serve
  só pra pegar algo que fugiu do esperado (bug de janela, dado colado por
  engano), não pra podar prompt em uso legítimo.
- **Revisão de coerência é chamada separada, não autoavaliação na mesma
  resposta**: pedir pro modelo se autoavaliar na mesma resposta que gera
  o plano tende a ser menos crítico que uma segunda chamada fresca focada
  só em revisar -- mesmo racional de manter a chamada de geração isolada
  (seção 4), aplicado aqui a uma segunda responsabilidade.
- **Retry de até 3 tentativas antes de abortar, sem plano de fallback
  fixo**: descartada a opção de cair num plano conservador padrão quando
  a revisão reprova -- preferiu-se abortar com erro claro (mesmo padrão
  fail-fast das Fases 9 e 10) a mandar silenciosamente um plano genérico
  sem o usuário saber que a geração normal falhou.
- **`escrever_atividades`: sobrescrita total -> upsert por `activity_id`**:
  ajuste feito depois do desenho inicial da Fase 9, a pedido explícito do
  usuário -- reescrever a aba "Atividades" inteira a cada execução diária
  é trabalho que cresce sem limite conforme o histórico acumula (cada
  atividade que já existia é reenviada de novo pra API do Sheets, todo
  dia, pro resto da vida do projeto). Trocado por upsert: só a atividade
  nova é inserida, a existente não é tocada. Isso também tornou
  desnecessário o plano original de ler "Plano da Semana" antes de
  reconstruir a aba inteira (sem sobrescrita total, não há mais risco de
  apagar a linha de dia planejado por acidente). Efeito colateral aceito
  conscientemente: uma atividade corrigida ou apagada direto no Postgres
  deixa de se refletir automaticamente na planilha -- decisão explícita
  do usuário de não pagar o custo de uma lógica de reconciliação (diff
  completo Postgres x planilha) pra cobrir um caso raro.

Implementação da Fase 9 (2026-09-08) -- bugs encontrados testando contra
o Neon e a planilha reais, não só por inspeção de código:

- **Número planejado sem vírgula decimal quebrava a leitura de volta**:
  `escrever_dias_planejados` escrevia `distancia_km`/`duracao_min` do
  plano com `str(v)` direto (ponto decimal) -- o mesmo bug de locale
  pt-BR já documentado (seção 6, `planilha_desempenho.py`), só que numa
  coluna nova que ainda não tinha o cuidado aplicado. O Sheets confundiu
  `"4.57"` com separador de milhar e virou `"456.989..."` ao ler de
  volta, quebrando o parse em `resolver_dias_planejados`. Corrigido com
  um formatador local (`_numero_planilha`) na escrita, espelhando o `_numero`
  que `planilha_desempenho.py` já usa.
- **Idempotência de `escrever_dias_planejados` checando só a segunda-feira**:
  copiado do padrão de `escrever_plano` (checa se a segunda-feira já está
  na coluna `data`), mas segunda-feira pode ser dia de descanso -- que
  nunca ganha linha própria em "Atividades". Checar só ela nunca
  encontraria a semana já escrita, duplicando as linhas de dia planejado
  a cada execução. Corrigido pra checar qualquer um dos 7 dias da semana.
- **Ordem dos dias planejados invertida**: a primeira versão inseria os
  dias na ordem Segunda->Domingo, e como `inserir_linhas_no_topo` insere
  o bloco preservando a ordem, o dia mais **antigo** da semana ficava no
  topo -- inconsistente com o resto da aba, que é sempre mais-recente-
  primeiro. Corrigido percorrendo os dias de trás pra frente antes de
  montar as linhas.
- **Checagem de migração do cabeçalho excessivamente ampla**: a versão
  original de `escrever_atividades` comparava o cabeçalho inteiro
  (`row_values(1) != CABECALHO_ATIVIDADES`) e limpava a aba inteira em
  qualquer divergência -- inclusive uma que apareceu uma vez durante os
  testes desta fase sem causa determinada (possível inconsistência
  transitória da API do Sheets sob a sequência rápida de chamadas do
  teste manual), apagando as linhas de dia planejado já escritas.
  Restrito pra só disparar a migração quando `"activity_id"` está
  ausente do cabeçalho -- o caso real que a checagem existe pra cobrir --
  em vez de qualquer divergência, porque uma comparação ampla dispararia
  de novo em qualquer mudança futura de schema e apagaria dias
  planejados/resolvidos que só existem na planilha, sem como reconstruir
  a partir do Postgres.
- **E-mail diário de lembrete**: mecanismo de envio (`enviar_email`,
  compartilhado com o e-mail semanal via novo `src/email_util.py`)
  testado com envio real. O gatilho por data (`resolvidos` filtrado por
  `data == ontem`) não foi exercitado em produção real nesta sessão,
  porque as datas disponíveis pra teste eram todas passadas há muito
  tempo ou futuras -- confirmado só por leitura do código.

## Bug real de produção: `date.today()` no fuso errado (2026-09-08)

**Sintoma:** a execução agendada de `analise_semanal.yml` no domingo
2026-09-06 gerou o plano pra semana de **14/09**, não de **07/09** como
deveria.

**Causa raiz, confirmada pelo log real do GitHub Actions (run
`34070585809`):** a execução, agendada pra 23h UTC (20h BRT) de domingo,
efetivamente rodou às **2026-09-07T00:42 UTC** -- 1h42 de atraso, comum
em workflows agendados do GitHub Actions. Esse atraso foi suficiente pra
cruzar a virada de dia em UTC: `date.today()`, dentro do runner (que roda
em UTC, não BRT), já devolvia **segunda-feira** (07/09) mesmo sendo ainda
domingo à noite (21h42) no Brasil. `_proxima_semana()`, pra uma
segunda-feira, calcula "próxima segunda-feira" como `hoje + 7 dias` --
pulando a semana inteira que deveria ter sido gerada e indo direto pra
14/09.

**Por que não é só a Fase 7:** o mesmo padrão (`date.today()` sem fuso
explícito) apareceu em mais lugares do pipeline, com o mesmo risco:
`_observacoes_recentes` e a janela de `vw_sessoes_ia` (Fase 7, impacto
baixo -- só desloca a borda de uma janela de N dias em ±1), e mais grave,
`resolver_dias_planejados`/`ontem` (Fase 9, recém-implementada, mesmo
risco de pular a semana/dia errado) e `load_recuperacao.py` (Fase 3) --
esse último especialmente exposto, porque o sync diário roda às 21h BRT
= **exatamente 00h UTC**, ou seja, a virada de dia em UTC acontece bem no
horário agendado, não numa borda distante.

**Correção:** novo módulo `src/tempo.py`, com `hoje_brt()` -- usa
`zoneinfo.ZoneInfo("America/Sao_Paulo")` (pacote `tzdata` adicionado ao
`requirements.txt` como rede de segurança, caso a imagem não tenha o
banco de fusos do sistema) em vez de `date.today()`/`CURRENT_DATE` do
Postgres (que reflete o fuso do servidor, também UTC). Todo lugar do
pipeline que precisa saber "que dia é hoje" foi trocado pra usar
`hoje_brt()`: `_proxima_semana`, `_observacoes_recentes` e a janela de
`vw_sessoes_ia` (`analisar_com_ia.py`), `resolver_dias_planejados` e
`ontem` (`planilha_desempenho.py`), `atividades_novas` (`extract/
garmin.py`) e o loop de backfill de `load_recuperacao.py`.

**Validado** reproduzindo o timestamp exato da falha real (2026-09-07T00:42
UTC): convertido pra BRT dá 2026-09-06 (domingo), e a partir daí
`_proxima_semana()` calcula corretamente 07/09 -- confirma que a correção
teria evitado o bug real observado.

## Implementação da Fase 11 (2026-09-10)

- **Chamada de revisão isolada, reaproveitando o prompt original em vez
  de reconstruir contexto**: em vez de passar "quais guardrails
  dispararam" como argumento separado pro revisor, reenvia o prompt
  original inteiro -- as instruções de guardrail (`instrucoes_guardrail`)
  já estão concatenadas nele por `_construir_prompt`. Evita duplicar
  lógica de detecção de guardrail entre a geração e a revisão, e garante
  que o revisor veja exatamente o mesmo contexto que gerou o plano, sem
  risco de ficar dessincronizado se `_construir_prompt` mudar no futuro.
- **`_verificar_teto_tokens` extraída como função compartilhada**: o teto
  de tokens da Fase 10 se aplicava só a `gerar_analise`; como a revisão
  (Fase 11) manda um prompt ainda maior (o prompt original inteiro + o
  plano gerado), o mesmo risco existe lá, então a checagem foi extraída
  pra ser reaproveitada nas duas chamadas em vez de duplicada.
- **Reprovação testada com um caso de violação disfarçada**: montado um
  plano sintético com um dia rotulado `Rodagem/Recuperação` mas com
  `motivo`, pace (4:00 min/km) e FC (185-195 bpm) de um treino de tiros
  máximos, numa semana com o guardrail de dor ativo (nível 3, que deveria
  restringir a semana inteira a Rodagem/Recuperação ou Longão de verdade).
  O revisor reprovou corretamente, citando os três problemas específicos
  (dia, campos incompatíveis, contradição com o guardrail) -- não uma
  resposta genérica.
- **Teste do esgotamento de tentativas via simulação, não indução real**:
  como o Gemini normalmente produz planos coerentes, não dava pra contar
  com 3 reprovações reais seguidas pra testar o `RuntimeError` final.
  Testado simulando `gerar_analise`/`revisar_coerencia` (sempre reprova)
  e confirmando que o loop chama `gerar_analise` exatamente
  `MAX_TENTATIVAS_REVISAO` vezes, nunca chama `escrever_plano`, e levanta
  o erro esperado -- valida a lógica de controle sem depender de sorte
  numa chamada real ao Gemini.

Decisões da sessão de grilling de 2026-09-15 (Fase 12), para referência
futura:

- **Descoberta que motivou a fase**: pesquisa (não pedido original do
  usuário) revelou que a lib `garminconnect` já usada no projeto tem uma
  API dedicada de treino estruturado (`RunningWorkout`,
  `upload_running_workout`, `schedule_workout`) -- confirmado inspecionando
  a biblioteca já instalada (versão 0.3.13), não só por busca na web.
  Diferente de subir um `.fit` de atividade (que a Garmin não aceita como
  treino estruturado, só como atividade já realizada). Confirmado também
  que o Forerunner 165 do usuário suporta treino de intervalo estruturado
  e planos vindos do Garmin Connect, mas sincroniza só por Bluetooth
  (celular) ou USB -- a versão não-Music não tem Wi-Fi embutido, então o
  treino fica esperando no Garmin Connect até a próxima sincronização do
  relógio, não chega instantaneamente.
- **Alvo pace-vs-FC por tipo de treino, não uma escolha única**: o Garmin
  só aceita um tipo de alvo por passo. Decisão: treino de baixa
  intensidade (Rodagem/Recuperação, Longão, Fartlek) usa FC como alvo,
  porque o que importa ali é não passar do esforço seguro; treino de
  intensidade (Ritmo, Limiar, Intervalado, Tiro) usa pace, porque o que
  importa é acertar o ritmo de prova. Mesmo racional já aplicado noutras
  partes do projeto (ex. guardrail de dor, seção 6.1) de tratar
  intensidade e volume como coisas fisiologicamente distintas.
- **Best-effort, não fail-fast**: diferente das Fases 9-10-11, uma falha
  no envio ao relógio não aborta a semana -- decisão explícita do usuário
  de não deixar a entrega central do projeto (planilha + e-mail) refém de
  uma integração adicional sobre uma API já não-oficial (dobrando o
  risco).
- **Criação do treino só depois do plano aprovado, nunca por
  tentativa**: resposta do usuário a uma pergunta sobre deduplicação --
  entendida como regra de sequenciamento (só roda depois que o loop de
  revisão da Fase 11 sai com um `plano` aprovado, nunca dentro do loop
  por candidato) combinada com a checagem de duplicidade por nome+data já
  proposta (verifica se já existe antes de criar, atualiza em vez de
  duplicar) -- as duas regras resolvem ângulos diferentes do mesmo
  problema (não criar treino de um candidato que seria descartado; não
  duplicar se a semana inteira for reprocessada).
- **Limpeza de treinos antigos fora de escopo por ora**: aceito acumular
  na biblioteca do Garmin Connect -- mesmo padrão de "resolver quando (e
  se) virar problema real" já usado pra outros riscos aceitos do projeto
  (ex. rotação do refresh token da Garmin, seção 11, Fase 8).
- **Formato exato do valor de alvo ainda não determinado**: as funções
  helper da lib (`create_interval_step` etc.) só parametrizam o *tipo* de
  alvo, não o *valor* da faixa de pace/FC -- campo extra
  (`targetValueOne`/`targetValueTwo`, hipótese a confirmar) aceito pelo
  Pydantic (`extra: allow`), mas a unidade exata não está documentada nos
  helpers. Não é uma decisão de design pra grilling -- é investigação
  técnica a fazer contra a API real durante a implementação.

## Bug real de produção: dia de hoje resolvido antes de terminar (2026-09-16)

**Sintoma:** relatado pelo usuário -- um treino planejado pra 15/09,
feito de fato no mesmo dia, já tinha sido marcado `NÃO_REALIZADO` antes
mesmo de o dia terminar.

**Causa raiz:** `resolver_dias_planejados` (Fase 9) só pulava dias
**futuros** (`data_planejada > hoje`), mas resolvia o dia de **hoje**
normalmente. Como o sync diário roda 1x por dia (21h BRT), qualquer
treino planejado pra depois desse horário (comum -- treino à noite) era
avaliado sem atividade correspondente ainda, e marcado `NÃO_REALIZADO`
horas antes de o dia acabar. Diferente do bug de fuso da Fase 9/seção
anterior (`date.today()` vs. `hoje_brt()`) -- este é um erro de limite na
comparação (`>` em vez de `>=`), não de fuso horário; `hoje_brt()` já
estava correto aqui.

**Por que ajustar o horário do cron não resolveria de verdade:** não
existe horário que garanta "a essa altura o treino já foi feito" -- o
usuário pode treinar a qualquer hora da noite. A correção certa é nunca
resolver o dia de hoje, só dias estritamente anteriores, independente de
quando o sync roda.

**Correção:** `data_planejada > hoje` -> `data_planejada >= hoje` --
hoje sempre mantém `PLANEJADO`, só é resolvido no sync do dia seguinte,
quando já é estritamente passado.

**Validado:**
- A linha real de 15/09 (marcada `NÃO_REALIZADO` incorretamente) foi
  resetada pra `PLANEJADO` e reprocessada com o código corrigido -- como
  15/09 já era passado (hoje 16/09) e a atividade real já estava
  sincronizada (3,05km, 33,7min, FC 135, contra um alvo de 2km/23min),
  resolveu corretamente como `REALIZADO_FORA_DA_MARGEM` (correu, mas
  além da margem planejada -- não `NÃO_REALIZADO`).
- Teste com uma linha sintética pra **hoje** (16/09), sem atividade
  correspondente: confirmado que `resolver_dias_planejados` não a toca
  (continua `PLANEJADO`).

## Bug real de produção: dia resolvido duplicava a linha da atividade (2026-09-16)

**Sintoma:** relatado pelo usuário, ao investigar o bug acima -- depois
de resolvido, o dia de 15/09 aparecia **duas vezes** na aba "Atividades":
uma linha da atividade real (`escrever_atividades`, upsert por
`activity_id`) e uma segunda linha do placeholder resolvido (mesma data,
mesmos números agregados, `activity_id` em branco). Dado repetido lado a
lado, não uma linha só.

**Causa raiz:** decisão original da Fase 9 (ver grilling de 2026-09-08)
era deliberadamente misturar as duas granularidades -- linha por
atividade real (histórico) e linha por dia planejado (aderência) --
como duas coisas separadas que coexistem na mesma semana. Na prática,
pra um dia com atividade real, isso duplicava visualmente a mesma
informação (a mesma distância/duração/FC aparecendo nas duas linhas),
o que o usuário não queria.

**Correção:** quando existe atividade real pro dia, `resolver_dias_planejados`
agora **funde o veredito na própria linha da atividade** -- escreve só as
6 colunas de plano (`status`, `tipo_planejado`, etc.) na linha que já
tem o `activity_id`, sem tocar nas colunas de atividade real, e remove a
linha-placeholder (`delete_rows`, depois de todos os `batch_update` da
execução, em ordem decrescente de índice pra não invalidar os próximos).
Sem atividade real (`NÃO_REALIZADO`), não há linha de atividade pra
fundir -- o placeholder continua sendo atualizado no próprio lugar, como
antes. `_resolver_dia` passou a devolver também `activity_id_principal`
(o `activity_id` da atividade de maior duração no dia, mesma escolhida
pra `tipo_real`), usado pra localizar a linha de destino do merge.

**Validado:**
- Dia com 1 atividade (15/09, caso real do usuário): uma linha só depois
  de resolvido, com dado real e colunas de plano juntos.
- Dia sem nenhuma atividade (data sintética): continua uma linha só (o
  placeholder), sem tentativa de fundir.
- Dia com 2 atividades reais (13/08, caso real já existente no banco): o
  veredito funde na atividade de maior duração; a outra atividade do
  mesmo dia permanece como linha própria, sem colunas de plano --
  nenhuma duplicação, nenhuma perda de dado.
- Rodar o resolvedor de novo depois do merge não repete nada (as linhas
  fundidas não têm mais status `PLANEJADO`, então não voltam a ser
  processadas).
