-- Fase 1 — modelo de dados (staging -> fato -> view)
-- Baseado nos campos reais confirmados na Fase 0 (ver explore/RELATORIO_FASE0.md
-- e os JSONs de amostra em explore/output/).

-- =====================================================================
-- STAGING: stg_atividades — 1 linha por corrida (get_activities)
-- =====================================================================
CREATE TABLE IF NOT EXISTS stg_atividades (
    activity_id                 BIGINT PRIMARY KEY,   -- activityId
    activity_uuid                VARCHAR,              -- activityUUID
    data                          DATE NOT NULL,        -- derivado de startTimeLocal
    hora_inicio                  TIMESTAMP,             -- startTimeLocal
    nome                          VARCHAR,               -- activityName
    tipo                          VARCHAR,               -- activityType
    distancia_m                  DOUBLE,                -- distance
    duracao_seg                  DOUBLE,                -- duration
    duracao_movimento_seg        DOUBLE,                -- movingDuration
    velocidade_media_mps         DOUBLE,                -- averageSpeed (pace é derivado na view)
    velocidade_maxima_mps        DOUBLE,                -- maxSpeed
    fc_media                      INTEGER,               -- averageHR
    fc_maxima                     INTEGER,               -- maxHR
    cadencia_media                DOUBLE,                -- averageRunningCadenceInStepsPerMinute
    cadencia_maxima               DOUBLE,                -- maxRunningCadenceInStepsPerMinute
    ganho_elevacao                DOUBLE,                -- elevationGain
    perda_elevacao                DOUBLE,                -- elevationLoss
    elevacao_media                 DOUBLE,                -- avgElevation
    efeito_treino_aerobico        DOUBLE,                -- aerobicTrainingEffect
    efeito_treino_anaerobico      DOUBLE,                -- anaerobicTrainingEffect
    efeito_treino_label            VARCHAR,               -- trainingEffectLabel
    tempo_zona_fc_1                DOUBLE,                -- hrTimeInZone_1
    tempo_zona_fc_2                DOUBLE,                -- hrTimeInZone_2
    tempo_zona_fc_3                DOUBLE,                -- hrTimeInZone_3
    tempo_zona_fc_4                DOUBLE,                -- hrTimeInZone_4
    tempo_zona_fc_5                DOUBLE,                -- hrTimeInZone_5
    tempo_contato_solo_medio       DOUBLE,                -- avgGroundContactTime (relevante pra canelite)
    comprimento_passada_medio      DOUBLE,                -- avgStrideLength (relevante pra canelite)
    oscilacao_vertical_media       DOUBLE,                -- avgVerticalOscillation
    razao_vertical_media           DOUBLE,                -- avgVerticalRatio
    calorias                        DOUBLE,                -- calories
    carregado_em                    TIMESTAMP DEFAULT current_timestamp
);

-- =====================================================================
-- STAGING: stg_recuperacao_diaria — 1 linha por dia
-- Combina sono (get_sleep_data) + FC de repouso (get_heart_rates) +
-- resumo diário/body battery (get_user_summary, get_body_battery).
-- Tabela nova em relação ao case original (Fase 0 mostrou que esses três
-- endpoints são todos keyed por dia, não por atividade — ver relatório).
-- =====================================================================
CREATE TABLE IF NOT EXISTS stg_recuperacao_diaria (
    data                          DATE PRIMARY KEY,
    -- sono (dailySleepDTO)
    sono_total_seg                INTEGER,               -- sleepTimeSeconds
    sono_profundo_seg             INTEGER,               -- deepSleepSeconds
    sono_leve_seg                  INTEGER,               -- lightSleepSeconds
    sono_rem_seg                   INTEGER,               -- remSleepSeconds
    sono_desperto_seg              INTEGER,               -- awakeSleepSeconds
    despertares                     INTEGER,               -- awakeCount
    sono_fc_media                   DOUBLE,                -- avgHeartRate (dentro do sono)
    sono_estresse_medio             DOUBLE,                -- avgSleepStress
    sono_score                      INTEGER,               -- sleepScores.overall.value
    -- FC de repouso / tendência
    fc_repouso                       INTEGER,               -- restingHeartRate
    fc_repouso_media_7d              DOUBLE,                -- lastSevenDaysAvgRestingHeartRate
    -- body battery / recuperação
    body_battery_ao_acordar          INTEGER,               -- bodyBatteryAtWakeTime
    body_battery_mais_recente         INTEGER,               -- bodyBatteryMostRecentValue
    body_battery_carregada            INTEGER,               -- bodyBatteryChargedValue
    body_battery_drenada              INTEGER,               -- bodyBatteryDrainedValue
    -- bem-estar geral do dia
    estresse_medio_dia                DOUBLE,                -- averageStressLevel
    passos_totais                      INTEGER,               -- totalSteps
    carregado_em                       TIMESTAMP DEFAULT current_timestamp
);

-- =====================================================================
-- STAGING: stg_dor — 1 linha por data anotada na planilha (input manual)
-- =====================================================================
CREATE TABLE IF NOT EXISTS stg_dor (
    data           DATE PRIMARY KEY,
    dor             INTEGER CHECK (dor BETWEEN 0 AND 5),
    localizacao     VARCHAR,
    comentario      VARCHAR,
    superficie      VARCHAR,
    tenis           VARCHAR,
    carregado_em    TIMESTAMP DEFAULT current_timestamp
);

-- =====================================================================
-- FATO: fct_sessoes — junção por data (dado objetivo + subjetivo lado a lado)
--
-- Decisão de escopo (Fase 1): assume no máximo 1 atividade "principal" por
-- dia. Se um dia tiver mais de uma corrida, isso precisa ser resolvido no
-- transform (Fase 4) antes do INSERT aqui -- ainda não decidido como
-- (somar as corridas do dia? pegar a de maior distância?). Revisar quando
-- isso acontecer na prática.
-- =====================================================================
CREATE TABLE IF NOT EXISTS fct_sessoes (
    data                        DATE PRIMARY KEY,
    activity_id                 BIGINT,
    distancia_km                DOUBLE,
    duracao_min                 DOUBLE,
    pace_min_km                 DOUBLE,
    fc_media                    INTEGER,
    fc_maxima                   INTEGER,
    cadencia_media               DOUBLE,
    efeito_treino_aerobico       DOUBLE,
    tempo_contato_solo_medio     DOUBLE,
    comprimento_passada_medio    DOUBLE,
    sono_total_seg               INTEGER,
    sono_score                   INTEGER,
    fc_repouso                   INTEGER,
    body_battery_ao_acordar      INTEGER,
    dor                          INTEGER,
    localizacao_dor               VARCHAR,
    comentario_dor                VARCHAR,
    superficie                    VARCHAR,
    tenis                          VARCHAR,
    atualizado_em                  TIMESTAMP DEFAULT current_timestamp
);

-- =====================================================================
-- VIEW: vw_resumo_semanal — agregação por semana ISO (semana começa segunda)
-- =====================================================================
CREATE OR REPLACE VIEW vw_resumo_semanal AS
WITH semanal AS (
    SELECT
        date_trunc('week', data)   AS semana_inicio,
        SUM(distancia_km)          AS km_total,
        SUM(duracao_min)           AS duracao_total_min,
        COUNT(activity_id)         AS num_corridas,
        AVG(dor)                   AS dor_media,
        MAX(dor)                   AS dor_maxima,
        AVG(sono_score)            AS sono_score_medio,
        AVG(fc_repouso)            AS fc_repouso_media
    FROM fct_sessoes
    GROUP BY 1
)
SELECT
    semana_inicio,
    km_total,
    duracao_total_min,
    num_corridas,
    dor_media,
    dor_maxima,
    sono_score_medio,
    fc_repouso_media,
    ROUND(
        100.0 * (km_total - LAG(km_total) OVER (ORDER BY semana_inicio))
        / NULLIF(LAG(km_total) OVER (ORDER BY semana_inicio), 0),
        1
    ) AS variacao_pct_km_vs_semana_anterior   -- sinaliza a regra dos 10%
FROM semanal
ORDER BY semana_inicio;
