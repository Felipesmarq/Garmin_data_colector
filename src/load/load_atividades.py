"""Fase 3 — carrega atividades extraídas (Fase 2) no Postgres (Neon).

Idempotente: reprocessar a mesma atividade (mesmo activity_id) atualiza a
linha em vez de duplicar — necessário porque o sync roda repetidamente
(polling, ver CASE_DO_PROJETO_1.md seção 8). Os splits de uma atividade
(stg_atividade_splits) são recarregados por completo a cada sync — não dá
pra fazer upsert linha a linha porque o índice de um split não é uma
identidade estável entre re-sincronizações.
"""

import psycopg

from src.db import conectar

COLUNAS = [
    "activity_id", "activity_uuid", "data", "hora_inicio", "nome", "tipo",
    "distancia_m", "duracao_seg", "duracao_movimento_seg",
    "velocidade_media_mps", "velocidade_maxima_mps",
    "velocidade_ajustada_grade_mps", "fc_media", "fc_maxima",
    "cadencia_media", "cadencia_maxima", "ganho_elevacao", "perda_elevacao",
    "elevacao_media", "elevacao_minima", "elevacao_maxima", "potencia_media",
    "potencia_maxima", "potencia_normalizada", "efeito_treino_aerobico",
    "efeito_treino_anaerobico", "efeito_treino_label", "tempo_zona_fc_1",
    "tempo_zona_fc_2", "tempo_zona_fc_3", "tempo_zona_fc_4",
    "tempo_zona_fc_5", "tempo_contato_solo_medio",
    "comprimento_passada_medio", "oscilacao_vertical_media",
    "razao_vertical_media", "fastest_split_1km_seg",
    "fastest_split_1milha_seg", "minutos_intensidade_moderada",
    "minutos_intensidade_vigorosa", "body_battery_variacao", "calorias",
]

COLUNAS_SPLITS = [
    "activity_id", "split_index", "tipo", "duracao_seg", "distancia_m",
    "velocidade_media_mps", "velocidade_maxima_mps", "ganho_elevacao",
    "perda_elevacao",
]


def _sql_upsert() -> str:
    colunas_sql = ", ".join(COLUNAS)
    placeholders = ", ".join(["%s"] * len(COLUNAS))
    sets = ", ".join(f"{c} = excluded.{c}" for c in COLUNAS if c != "activity_id")
    return (
        f"INSERT INTO stg_atividades ({colunas_sql}) VALUES ({placeholders}) "
        f"ON CONFLICT (activity_id) DO UPDATE SET {sets}"
    )


def _sql_insert_splits() -> str:
    colunas_sql = ", ".join(COLUNAS_SPLITS)
    placeholders = ", ".join(["%s"] * len(COLUNAS_SPLITS))
    return f"INSERT INTO stg_atividade_splits ({colunas_sql}) VALUES ({placeholders})"


def _carregar_splits(con: psycopg.Connection, atividades: list[dict]) -> None:
    with con.cursor() as cur:
        for atividade in atividades:
            activity_id = atividade["activity_id"]
            splits = atividade.get("splits") or []
            cur.execute(
                "DELETE FROM stg_atividade_splits WHERE activity_id = %s",
                (activity_id,),
            )
            if not splits:
                continue
            linhas = [
                tuple({**s, "activity_id": activity_id}.get(c) for c in COLUNAS_SPLITS)
                for s in splits
            ]
            cur.executemany(_sql_insert_splits(), linhas)


def carregar(con: psycopg.Connection, atividades: list[dict]) -> int:
    """Insere/atualiza uma lista de atividades já mapeadas (extract.garmin).

    Retorna quantas atividades foram processadas (inseridas ou atualizadas).
    """
    if not atividades:
        return 0
    sql = _sql_upsert()
    linhas = [tuple(a.get(c) for c in COLUNAS) for a in atividades]
    with con.cursor() as cur:
        cur.executemany(sql, linhas)
    _carregar_splits(con, atividades)
    return len(linhas)


if __name__ == "__main__":
    from src.extract.garmin import atividades_novas
    from src.extract.garmin import conectar as conectar_garmin
    from src.extract.garmin_simulado import HISTORICO_DIAS

    api = conectar_garmin()
    # SR1: a janela inteira da Garmin simulada a cada sync (local, sem custo),
    # pra um banco vazio já começar com histórico -- ver garmin_simulado.py.
    novas = atividades_novas(api, dias=HISTORICO_DIAS, limite=1000)

    con = conectar()
    n = carregar(con, novas)
    print(f"{n} atividade(s) carregada(s)/atualizada(s) em stg_atividades.")
    con.close()
