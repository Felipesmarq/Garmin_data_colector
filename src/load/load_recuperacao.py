"""Fase 3 — carrega recuperação diária extraída (Fase 2) no DuckDB.

Idempotente: reprocessar o mesmo dia atualiza a linha em vez de duplicar
(útil porque body battery/sono do dia atual mudam ao longo do dia — cada
sync reflete o valor mais recente).
"""

import psycopg

from src.db import conectar

COLUNAS = [
    "data", "sono_total_seg", "sono_profundo_seg", "sono_leve_seg",
    "sono_rem_seg", "sono_desperto_seg", "despertares", "sono_fc_media",
    "sono_estresse_medio", "sono_score", "fc_repouso", "fc_repouso_media_7d",
    "body_battery_ao_acordar", "body_battery_mais_recente",
    "body_battery_carregada", "body_battery_drenada", "estresse_medio_dia",
    "passos_totais",
]


def _sql_upsert() -> str:
    colunas_sql = ", ".join(COLUNAS)
    placeholders = ", ".join(["%s"] * len(COLUNAS))
    sets = ", ".join(f"{c} = excluded.{c}" for c in COLUNAS if c != "data")
    return (
        f"INSERT INTO stg_recuperacao_diaria ({colunas_sql}) VALUES ({placeholders}) "
        f"ON CONFLICT (data) DO UPDATE SET {sets}"
    )


def carregar(con: psycopg.Connection, dias: list[dict]) -> int:
    """Insere/atualiza uma lista de dias de recuperação (extract.garmin)."""
    if not dias:
        return 0
    sql = _sql_upsert()
    linhas = [tuple(d.get(c) for c in COLUNAS) for d in dias]
    with con.cursor() as cur:
        cur.executemany(sql, linhas)
    return len(linhas)


if __name__ == "__main__":
    # roda dentro do container com:
    #   docker compose run --rm garmin python -m src.load.load_recuperacao
    from datetime import timedelta

    from src.extract.garmin import conectar as conectar_garmin
    from src.extract.garmin import recuperacao_diaria
    from src.tempo import hoje_brt

    api = conectar_garmin()
    dias = []
    for i in range(7):
        # hoje_brt(), não date.today() -- esse job roda às 21h BRT = 00h
        # UTC (virada de dia), então date.today() no runner (UTC) já
        # devolveria amanhã em BRT em qualquer atraso de agendamento.
        dia = hoje_brt() - timedelta(days=i)
        r = recuperacao_diaria(api, dia)
        if r:
            dias.append(r)

    con = conectar()
    n = carregar(con, dias)
    print(f"{n} dia(s) de recuperação carregado(s)/atualizado(s) em stg_recuperacao_diaria.")
    con.close()
