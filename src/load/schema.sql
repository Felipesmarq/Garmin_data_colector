-- Fase 1 (revisão Postgres/Neon) — modelo de dados: staging -> vw_sessoes -> vw_resumo_semanal.
-- Baseado nos campos reais confirmados na Fase 0 (ver explore/RELATORIO_FASE0.md,
-- os JSONs de amostra em explore/output/, e a revisão de campos relevantes pra
-- geração de treino feita na sessão de refatoração — ver CASE_DO_PROJETO_1.md
-- seções 6, 6.1 e 11).

-- =====================================================================
-- STAGING: stg_atividades — 1 linha por atividade (get_activities)
-- Captura tudo que é extraído, sem filtro — a curadoria acontece em
-- vw_sessoes_ia, não aqui.
-- =====================================================================
CREATE TABLE IF NOT EXISTS stg_atividades (
    activity_id                    BIGINT PRIMARY KEY,   -- activityId
    activity_uuid                   VARCHAR,              -- activityUUID
    data                             DATE NOT NULL,        -- derivado de startTimeLocal
    hora_inicio                     TIMESTAMP,             -- startTimeLocal
    nome                             VARCHAR,               -- activityName
    tipo                             VARCHAR,               -- activityType
    distancia_m                     DOUBLE PRECISION,      -- distance
    duracao_seg                     DOUBLE PRECISION,      -- duration
    duracao_movimento_seg           DOUBLE PRECISION,      -- movingDuration
    velocidade_media_mps            DOUBLE PRECISION,      -- averageSpeed (pace é derivado na view)
    velocidade_maxima_mps           DOUBLE PRECISION,      -- maxSpeed
    velocidade_ajustada_grade_mps   DOUBLE PRECISION,      -- avgGradeAdjustedSpeed (pace já corrigido por inclinação)
    fc_media                         INTEGER,               -- averageHR
    fc_maxima                        INTEGER,               -- maxHR
    cadencia_media                   DOUBLE PRECISION,      -- averageRunningCadenceInStepsPerMinute
    cadencia_maxima                  DOUBLE PRECISION,      -- maxRunningCadenceInStepsPerMinute
    ganho_elevacao                   DOUBLE PRECISION,      -- elevationGain
    perda_elevacao                   DOUBLE PRECISION,      -- elevationLoss
    elevacao_media                   DOUBLE PRECISION,      -- avgElevation
    elevacao_minima                  DOUBLE PRECISION,      -- minElevation
    elevacao_maxima                  DOUBLE PRECISION,      -- maxElevation
    potencia_media                   DOUBLE PRECISION,      -- avgPower
    potencia_maxima                  DOUBLE PRECISION,      -- maxPower
    potencia_normalizada             DOUBLE PRECISION,      -- normPower
    efeito_treino_aerobico           DOUBLE PRECISION,      -- aerobicTrainingEffect
    efeito_treino_anaerobico         DOUBLE PRECISION,      -- anaerobicTrainingEffect
    efeito_treino_label              VARCHAR,               -- trainingEffectLabel
    tempo_zona_fc_1                  DOUBLE PRECISION,      -- hrTimeInZone_1
    tempo_zona_fc_2                  DOUBLE PRECISION,      -- hrTimeInZone_2
    tempo_zona_fc_3                  DOUBLE PRECISION,      -- hrTimeInZone_3
    tempo_zona_fc_4                  DOUBLE PRECISION,      -- hrTimeInZone_4
    tempo_zona_fc_5                  DOUBLE PRECISION,      -- hrTimeInZone_5
    tempo_contato_solo_medio         DOUBLE PRECISION,      -- avgGroundContactTime (relevante pra canelite)
    comprimento_passada_medio        DOUBLE PRECISION,      -- avgStrideLength (relevante pra canelite)
    oscilacao_vertical_media         DOUBLE PRECISION,      -- avgVerticalOscillation
    razao_vertical_media             DOUBLE PRECISION,      -- avgVerticalRatio
    fastest_split_1km_seg            DOUBLE PRECISION,      -- fastestSplit_1000
    fastest_split_1milha_seg         DOUBLE PRECISION,      -- fastestSplit_1609
    minutos_intensidade_moderada     INTEGER,               -- moderateIntensityMinutes
    minutos_intensidade_vigorosa     INTEGER,               -- vigorousIntensityMinutes
    body_battery_variacao            INTEGER,               -- differenceBodyBattery (custo da sessão em si)
    calorias                         DOUBLE PRECISION,      -- calories
    carregado_em                     TIMESTAMP DEFAULT current_timestamp
);

-- =====================================================================
-- STAGING: stg_atividade_splits — 1 linha por segmento dentro de uma
-- atividade (splitSummaries). Existe porque uma atividade pode ter mais
-- de um segmento com tipos diferentes -- é aqui que o método run-walk
-- aparece de verdade: splitType 'RWD_RUN' / 'RWD_WALK' alternando dentro
-- da mesma corrida. Recarregada por completo (delete+insert) a cada sync
-- da atividade, não é upsert linha a linha.
-- =====================================================================
CREATE TABLE IF NOT EXISTS stg_atividade_splits (
    activity_id          BIGINT NOT NULL REFERENCES stg_atividades(activity_id),
    split_index          INTEGER NOT NULL,
    tipo                  VARCHAR,               -- splitType (RWD_RUN, RWD_WALK, ou outros)
    duracao_seg           DOUBLE PRECISION,      -- duration
    distancia_m           DOUBLE PRECISION,      -- distance
    velocidade_media_mps  DOUBLE PRECISION,      -- averageSpeed
    velocidade_maxima_mps DOUBLE PRECISION,      -- maxSpeed
    ganho_elevacao         DOUBLE PRECISION,      -- totalAscent
    perda_elevacao         DOUBLE PRECISION,      -- elevationLoss
    PRIMARY KEY (activity_id, split_index)
);

-- =====================================================================
-- STAGING: stg_recuperacao_diaria — 1 linha por dia
-- Combina sono (get_sleep_data) + FC de repouso (get_heart_rates) +
-- resumo diário/body battery (get_user_summary, get_body_battery).
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
    sono_fc_media                   DOUBLE PRECISION,     -- avgHeartRate (dentro do sono)
    sono_estresse_medio             DOUBLE PRECISION,     -- avgSleepStress
    sono_score                      INTEGER,               -- sleepScores.overall.value
    -- FC de repouso / tendência
    fc_repouso                       INTEGER,               -- restingHeartRate
    fc_repouso_media_7d              DOUBLE PRECISION,     -- lastSevenDaysAvgRestingHeartRate
    -- body battery / recuperação
    body_battery_ao_acordar          INTEGER,               -- bodyBatteryAtWakeTime
    body_battery_mais_recente         INTEGER,               -- bodyBatteryMostRecentValue
    body_battery_carregada            INTEGER,               -- bodyBatteryChargedValue
    body_battery_drenada              INTEGER,               -- bodyBatteryDrainedValue
    -- bem-estar geral do dia
    estresse_medio_dia                DOUBLE PRECISION,     -- averageStressLevel
    passos_totais                      INTEGER,               -- totalSteps
    carregado_em                       TIMESTAMP DEFAULT current_timestamp
);

-- =====================================================================
-- STAGING: stg_dor — 1 linha por atividade anotada (input manual via
-- planilha/Google Sheets, ver CASE_DO_PROJETO_1.md seção 9). Chave por
-- activity_id, não por data -- uma mesma data pode ter mais de uma
-- atividade (ver vw_sessoes), cada uma com dor própria.
-- =====================================================================
CREATE TABLE IF NOT EXISTS stg_dor (
    activity_id     BIGINT PRIMARY KEY REFERENCES stg_atividades(activity_id),
    data             DATE,
    dor              INTEGER CHECK (dor BETWEEN 0 AND 5),
    localizacao      VARCHAR,
    comentario       VARCHAR,
    superficie       VARCHAR,
    tenis            VARCHAR,
    carregado_em     TIMESTAMP DEFAULT current_timestamp
);

-- =====================================================================
-- VIEW: vw_sessoes — 1 linha por atividade (não por dia -- múltiplas
-- atividades no mesmo dia entram todas). Substitui a antiga fct_sessoes
-- (tabela materializada): sempre live, sem script de transform/upsert
-- pra manter sincronizado. Pace e km são derivados aqui, não guardados
-- como coluna física.
-- =====================================================================
CREATE OR REPLACE VIEW vw_sessoes AS
WITH zonas AS (
    -- % de tempo por faixa de zona de FC, usado por tipo_treino abaixo.
    -- "baixa" = zonas 1-2, "media" = zona 3, "alta" = zonas 4-5.
    SELECT
        activity_id,
        COALESCE(tempo_zona_fc_1,0) + COALESCE(tempo_zona_fc_2,0)
            + COALESCE(tempo_zona_fc_3,0) + COALESCE(tempo_zona_fc_4,0)
            + COALESCE(tempo_zona_fc_5,0) AS total,
        COALESCE(tempo_zona_fc_1,0) + COALESCE(tempo_zona_fc_2,0) AS baixa,
        COALESCE(tempo_zona_fc_3,0) AS media,
        COALESCE(tempo_zona_fc_4,0) + COALESCE(tempo_zona_fc_5,0) AS alta
    FROM stg_atividades
)
SELECT
    a.activity_id,
    a.data,
    a.hora_inicio,
    a.nome,
    a.tipo,
    a.distancia_m / 1000.0 AS distancia_km,
    a.duracao_seg / 60.0 AS duracao_min,
    CASE WHEN a.distancia_m > 0
         THEN (a.duracao_seg / 60.0) / (a.distancia_m / 1000.0)
    END AS pace_min_km,
    a.velocidade_media_mps,
    a.velocidade_maxima_mps,
    a.velocidade_ajustada_grade_mps,
    a.fc_media,
    a.fc_maxima,
    a.cadencia_media,
    a.cadencia_maxima,
    a.ganho_elevacao,
    a.perda_elevacao,
    a.elevacao_media,
    a.elevacao_minima,
    a.elevacao_maxima,
    a.potencia_media,
    a.potencia_maxima,
    a.potencia_normalizada,
    a.efeito_treino_aerobico,
    a.efeito_treino_anaerobico,
    a.efeito_treino_label,
    a.tempo_zona_fc_1, a.tempo_zona_fc_2, a.tempo_zona_fc_3, a.tempo_zona_fc_4, a.tempo_zona_fc_5,
    a.tempo_contato_solo_medio,
    a.comprimento_passada_medio,
    a.oscilacao_vertical_media,
    a.razao_vertical_media,
    a.fastest_split_1km_seg,
    a.fastest_split_1milha_seg,
    a.minutos_intensidade_moderada,
    a.minutos_intensidade_vigorosa,
    a.body_battery_variacao,
    a.calorias,
    s.segundos_correndo,
    s.segundos_caminhando,
    r.sono_total_seg,
    r.sono_score,
    r.fc_repouso,
    r.fc_repouso_media_7d,
    r.body_battery_ao_acordar,
    r.body_battery_mais_recente,
    d.dor,
    d.localizacao AS localizacao_dor,
    d.comentario AS comentario_dor,
    d.superficie,
    d.tenis,
    -- efeito_treino_label 'UNKNOWN' da Garmin, na prática, marca atividades
    -- curtas demais/fracas demais pra ter efeito de treino relevante --
    -- tipicamente aquecimento ou deslocamento a pé. Não dá pra usar %
    -- de tempo em RWD_WALK como critério à parte: o método run-walk faz
    -- sessões de treino de verdade (RECOVERY, AEROBIC_BASE) também
    -- passarem 70-96% do tempo em RWD_WALK por estrutura, não por serem
    -- "só caminhada" -- então o label 'UNKNOWN' da própria Garmin já é o
    -- sinal mais confiável que existe pra esse caso.
    -- classificacao_atividade não sobrescreve o campo bruto acima; coluna
    -- no fim do SELECT porque Postgres não permite CREATE OR REPLACE VIEW
    -- inserir coluna no meio (só no fim).
    CASE
        WHEN a.efeito_treino_label = 'UNKNOWN' THEN 'CAMINHADA'
        ELSE a.efeito_treino_label
    END AS classificacao_atividade,
    -- tipo_treino: classifica a atividade no vocabulário de Daniels
    -- (Rodagem/Longão/Ritmo/Limiar/Intervalado/Tiro -- ver seção 6.1 do
    -- case pra fonte e racional) usando % de tempo por zona de FC +
    -- efeito anaeróbico como sinal de intensidade -- não a estrutura de
    -- splits (RWD_RUN/RWD_WALK). Validado contra dado real em 2026-08-14:
    -- treino run-walk de recuperação passa a maior parte do tempo em
    -- RWD_WALK só por estrutura, não por ser de baixa intensidade -- então
    -- estrutura de split classificaria errado; zona de FC não.
    CASE
        WHEN a.efeito_treino_label = 'UNKNOWN' THEN 'Caminhada'
        WHEN z.total IS NULL OR z.total = 0 THEN 'Rodagem/Recuperação'
        WHEN a.efeito_treino_anaerobico >= 1.5 AND z.alta / z.total >= 0.3
            THEN CASE WHEN a.duracao_seg / 60.0 < 20 THEN 'Tiro' ELSE 'Intervalado' END
        WHEN z.alta / z.total >= 0.40 THEN 'Limiar'
        WHEN z.alta / z.total >= 0.20 OR z.media / z.total >= 0.30 THEN 'Ritmo'
        WHEN z.baixa / z.total >= 0.85 AND a.duracao_seg / 60.0 >= 35 THEN 'Longão'
        ELSE 'Rodagem/Recuperação'
    END AS tipo_treino
FROM stg_atividades a
LEFT JOIN zonas z ON z.activity_id = a.activity_id
LEFT JOIN (
    SELECT
        activity_id,
        SUM(duracao_seg) FILTER (WHERE tipo = 'RWD_RUN')  AS segundos_correndo,
        SUM(duracao_seg) FILTER (WHERE tipo = 'RWD_WALK') AS segundos_caminhando
    FROM stg_atividade_splits
    GROUP BY activity_id
) s ON s.activity_id = a.activity_id
LEFT JOIN stg_recuperacao_diaria r ON r.data = a.data
LEFT JOIN stg_dor d ON d.activity_id = a.activity_id;

-- =====================================================================
-- VIEW: vw_sessoes_ia — subconjunto curado de vw_sessoes, é o que
-- efetivamente vira prompt da IA (Fase 7). Contexto irrelevante mede-se
-- em degradação de raciocínio do LLM (ver CASE_DO_PROJETO_1.md seção
-- 6.1), então aqui só entra o que tem sinal pra geração de treino ou pro
-- guardrail de sobrecarga -- não é "menos dado", é dado com propósito.
--
-- Excluído de propósito (mas disponível em vw_sessoes/staging se algum
-- dia precisar): calorias (sem sinal além do que duração+distância+FC já
-- dão); elevação mínima/máxima (ganho/perda já resume o desafio de
-- terreno); velocidade máxima e cadência máxima (picos instantâneos e
-- ruidosos -- fastest_split_* e cadência média são versões mais robustas
-- do mesmo sinal); potência por zona (redundante com zona de FC pra
-- quem não usa power meter dedicado).
-- =====================================================================
CREATE OR REPLACE VIEW vw_sessoes_ia AS
SELECT
    activity_id,
    data,
    tipo,
    distancia_km,
    duracao_min,
    pace_min_km,
    velocidade_ajustada_grade_mps,
    fc_media,
    fc_maxima,
    cadencia_media,
    ganho_elevacao,
    perda_elevacao,
    potencia_media,
    potencia_normalizada,
    efeito_treino_aerobico,
    efeito_treino_anaerobico,
    -- tipo_treino (Rodagem/Ritmo/Limiar/Intervalado/Tiro/Longão/Caminhada,
    -- ver vw_sessoes e case seção 6.1), não classificacao_atividade nem
    -- efeito_treino_label bruto -- é o vocabulário que o prompt da Fase 7
    -- usa pra recomendar a semana, então usar o mesmo vocabulário pra
    -- descrever o que já foi feito fecha o elo entre histórico e
    -- recomendação. Repetir classificacao_atividade aqui também seria
    -- sinal redundante, contra o princípio de curadoria desta view.
    tipo_treino,
    tempo_zona_fc_1, tempo_zona_fc_2, tempo_zona_fc_3, tempo_zona_fc_4, tempo_zona_fc_5,
    tempo_contato_solo_medio,
    comprimento_passada_medio,
    oscilacao_vertical_media,
    razao_vertical_media,
    fastest_split_1km_seg,
    fastest_split_1milha_seg,
    minutos_intensidade_moderada,
    minutos_intensidade_vigorosa,
    segundos_correndo,
    segundos_caminhando,
    body_battery_variacao,
    sono_total_seg,
    sono_score,
    fc_repouso,
    body_battery_ao_acordar,
    dor,
    localizacao_dor,
    comentario_dor,
    superficie,
    tenis
FROM vw_sessoes;

-- =====================================================================
-- VIEW: vw_resumo_semanal — agregação por semana ISO (semana começa
-- segunda) + ACWR (Acute:Chronic Workload Ratio -- ver CASE_DO_PROJETO_1.md
-- seção 6.1). Granularidade semanal (não diária) porque o relatório roda
-- 1x/semana: carga aguda = km da semana corrente, carga crônica = média
-- das últimas 4 semanas (semana corrente + 3 anteriores). Zona segura
-- 0.8-1.3; risco alto acima de 1.5.
-- =====================================================================
CREATE OR REPLACE VIEW vw_resumo_semanal AS
WITH semanal AS (
    SELECT
        date_trunc('week', data)::date AS semana_inicio,
        SUM(distancia_km)              AS km_total,
        SUM(duracao_min)               AS duracao_total_min,
        COUNT(activity_id)             AS num_atividades,
        AVG(dor)                       AS dor_media,
        MAX(dor)                       AS dor_maxima,
        AVG(sono_score)                AS sono_score_medio,
        AVG(fc_repouso)                AS fc_repouso_media
    FROM vw_sessoes
    GROUP BY 1
)
SELECT
    semana_inicio,
    km_total,
    duracao_total_min,
    num_atividades,
    dor_media,
    dor_maxima,
    sono_score_medio,
    fc_repouso_media,
    ROUND(
        (100.0 * (km_total - LAG(km_total) OVER (ORDER BY semana_inicio))
        / NULLIF(LAG(km_total) OVER (ORDER BY semana_inicio), 0))::numeric,
        1
    ) AS variacao_pct_km_vs_semana_anterior,
    ROUND(
        AVG(km_total) OVER (ORDER BY semana_inicio ROWS BETWEEN 3 PRECEDING AND CURRENT ROW)::numeric,
        2
    ) AS carga_cronica_km,
    ROUND(
        (km_total / NULLIF(AVG(km_total) OVER (ORDER BY semana_inicio ROWS BETWEEN 3 PRECEDING AND CURRENT ROW), 0))::numeric,
        2
    ) AS acwr
FROM semanal
ORDER BY semana_inicio;
