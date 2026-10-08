# Relatório — Fase 0: Exploração da Garmin Connect API

**Data:** 2026-07-28
**Script:** `explore/inspect_garmin.py`
**Status:** concluída

## 1. Objetivo

Confirmar, com dado real (não suposição), quais campos a Garmin Connect API
devolve pra atividades, sono, frequência cardíaca e recuperação — pra fechar
o `schema.sql` da Fase 1 com base na realidade da conta do usuário (Forerunner
165), não no que o case original supôs.

## 2. Ambiente de execução

O script roda em Docker (Dockerfile + docker-compose.yml), sem instalar nada
na máquina local.

**Problema encontrado e corrigido:** a build falhou inicialmente porque o
Dockerfile usava `python:3.11-slim`, e a lib `garminconnect` a partir da
versão 0.3.3 exige Python ≥3.12. Corrigido trocando a imagem base pra
`python:3.12-slim`.

**Contexto que motivou essa versão mais nova da lib:** em março/2026 a
Garmin mudou o fluxo de autenticação e quebrou o `garth` (dependência que a
maioria das libs Python usava pra login), que foi descontinuado pelo mantenedor.
A `garminconnect` (cyberjunky) migrou pra `curl_cffi` com TLS impersonation
pra continuar autenticando. Ou seja, o risco "lib não-oficial pode quebrar",
já listado no case original, se materializou entre a escrita do case e a
Fase 0 — e a versão usada aqui (`garminconnect>=0.3.6`) já é o fix pós-quebra.

Login funcionou com token de sessão salvo em `.garmin_tokens/` (gitignored),
reaproveitado nas próximas execuções sem precisar de senha.

## 3. Endpoints testados e campos reais retornados

### 3.1 Atividades (`get_activities`) → `atividades_recentes.json`

3 atividades retornadas. Campos relevantes pro case (dor × treino):

| Campo | Uso pretendido |
|---|---|
| `activityId`, `activityUUID` | chave da atividade |
| `startTimeLocal` / `startTimeGMT` | data/hora da corrida (chave de join com `stg_dor`) |
| `distance`, `duration`, `elapsedDuration` | km e tempo — base pra regra dos 10% |
| `averageSpeed`, `maxSpeed` | pace (derivado) |
| `averageHR`, `maxHR` | esforço cardiovascular |
| `averageRunningCadenceInStepsPerMinute`, `maxRunningCadenceInStepsPerMinute` | cadência |
| `elevationGain`, `elevationLoss`, `avgElevation` | perfil de terreno |
| `aerobicTrainingEffect`, `anaerobicTrainingEffect`, `trainingEffectLabel` | classificação de carga da Garmin — não estava no case original, mas é direto pra IA julgar sobrecarga |
| `hrTimeInZone_1..5` | tempo em cada zona de FC — não estava no case original |
| `avgGroundContactTime`, `avgStrideLength`, `avgVerticalOscillation`, `avgVerticalRatio` | biomecânica da passada — relevante pra canelite especificamente |
| `calories` | complementar |

Total de ~100 campos por atividade; a maioria (GPS, splits, dive info,
imagens, redes sociais) não é relevante pro caso de uso e deve ficar de fora
do `schema.sql`.

### 3.2 Sono (`get_sleep_data`) → `sono_hoje.json`

Estrutura aninhada em `dailySleepDTO`. Campos relevantes:

| Campo | Uso pretendido |
|---|---|
| `sleepTimeSeconds` | duração total de sono |
| `deepSleepSeconds`, `lightSleepSeconds`, `remSleepSeconds`, `awakeSleepSeconds` | composição do sono |
| `avgHeartRate` | FC média durante o sono |
| `avgSleepStress` | estresse durante o sono |
| `awakeCount` | despertares |
| `sleepScores.overall.value` | score 0-100 da Garmin — indicador único e já normalizado de recuperação |

Essa tabela **não estava prevista no case original** (só citava "sono" como
dado bruto do relógio, sem tabela própria). Recomendo criar `stg_sono`
separada de `stg_atividades`, com join por data.

### 3.3 Frequência cardíaca (`get_heart_rates`) → `fc_hoje.json`

| Campo | Uso pretendido |
|---|---|
| `restingHeartRate` | FC de repouso do dia |
| `lastSevenDaysAvgRestingHeartRate` | tendência — útil pra sinalizar overtraining |
| `minHeartRate`, `maxHeartRate` | faixa do dia |
| `heartRateValues` | série temporal minuto a minuto — **não recomendo guardar bruta** (ver seção 5) |

### 3.4 Resumo diário (`get_user_summary`) → `resumo_hoje.json`

Confirma dados de bem-estar geral que cruzam com o dia da corrida:
`totalSteps`, `totalDistanceMeters`, `totalKilocalories`, `restingHeartRate`,
`averageStressLevel`, `sleepingSeconds`, mais o bloco `bodyBattery*`
(`bodyBatteryAtWakeTime`, `bodyBatteryMostRecentValue`,
`bodyBatteryChargedValue`, `bodyBatteryDrainedValue`).

### 3.5 Body Battery / recuperação (`get_body_battery`) → `body_battery_semana.json`

8 dias retornados (um a mais que o range pedido — Garmin inclui o dia
seguinte parcial). Cada dia tem `charged`, `drained`, `date`, mais
`bodyBatteryValuesArray` (série temporal) — mesmo caso do heart rate: útil
pra explorar, não pra guardar bruta na tabela final.

## 4. Volume de dados

- JSONs de exploração: ~195 KB no total (o de sono é o maior, ~140 KB, por
  causa da série minuto a minuto). Esses arquivos são sobrescritos a cada
  execução — não acumulam.
- Imagem Docker (`python:3.12-slim` + garminconnect + curl_cffi): ~250-350 MB,
  baixada/construída uma vez e cacheada pelo Docker — não recria a cada run.
- Chamadas à API: payloads pequenos (as mesmas centenas de KB por chamada),
  desprezível mesmo com polling a cada poucas horas.
- Projeção pro `.duckdb` de produção (Fase 1+): guardando só agregados
  diários (não as séries minuto a minuto), o crescimento é de poucos MB por
  ano pro volume de uma pessoa correndo — não é um dado que preocupa espaço
  em disco.

## 5. Decisões em aberto antes do `schema.sql` (Fase 1)

1. **Séries temporais (heart rate minuto a minuto, body battery ao longo do
   dia): guardar ou não?** Recomendo não guardar no DuckDB versionado — só
   os agregados diários (`restingHeartRate`, `bodyBatteryChargedValue` etc.).
   Se um dia fizer falta análise minuto a minuto, dá pra buscar sob demanda
   direto da API, sem inflar o banco.
2. **Nova tabela `stg_sono`** — não prevista no case original, mas os dados
   justificam uma tabela própria (não cabe bem dentro de `stg_atividades`,
   já que sono não é por atividade, é por dia).
3. **Nome da chave de join**: atividades usam `startTimeLocal` (timestamp),
   enquanto sono/FC/resumo usam `calendarDate` (data). O `fct_sessoes` vai
   precisar normalizar pra uma coluna `data` (DATE) comum antes do join.
4. **Campos de biomecânica** (`avgGroundContactTime`, `avgStrideLength`,
   `avgVerticalOscillation`) — vale manter no schema mesmo sem uso imediato
   na Fase 1, porque são especificamente relevantes pra canelite (mudança de
   passada é um dos fatores de risco) e a IA pode usá-los mais adiante.

## 6. Próximo passo

Fase 1: `schema.sql` com `stg_atividades`, `stg_sono` (nova), `stg_dor`,
`fct_sessoes` e `vw_resumo_semanal`, usando os campos confirmados acima.
