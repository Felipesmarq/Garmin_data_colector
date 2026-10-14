"""Fase 6 — dor via planilha (Google Sheets), aba "Dor".

Lógica de banco (o que precisa ser exportado, o que fazer com o que foi
preenchido) fica aqui; a mecânica de planilha em si (conectar, abrir/criar
aba, inserir linhas) está em src/planilha.py, reaproveitada também por
planilha_desempenho.py.

Duas direções, na mesma execução (volume baixo -- poucas atividades por
semana -- não justifica separar em dois scripts/schedules):

1. `exportar_pendentes`: atividades sem dor registrada em `stg_dor` viram
   linha nova na aba "Dor" (activity_id, data, nome, tipo), com as colunas
   de dor em branco pro usuário preencher.
2. `importar_preenchidas`: linhas da aba "Dor" com a coluna `dor`
   preenchida são upsertadas em `stg_dor` por `activity_id`.
"""

import gspread
import psycopg

from src.db import conectar, sql_upsert
from src.planilha import adicionar_dropdown, aplicar_tema, ids_na_coluna, inserir_linhas
from src.planilha import conectar as conectar_planilha

ABA_DOR = "Dor"
COR_DOR = (0.82, 0.33, 0.30)  # vermelho-coral

CABECALHO = [
    "activity_id", "data", "nome", "tipo",
    "dor", "localizacao", "comentario", "superficie", "tenis",
]
COLUNA_DOR = chr(ord("A") + CABECALHO.index("dor"))  # "E"
COLUNAS_STG_DOR = ["activity_id", "data", "dor", "localizacao", "comentario", "superficie", "tenis"]


def obter_aba_dor(spreadsheet: gspread.Spreadsheet) -> gspread.Worksheet:
    """Igual a src.planilha.obter_aba, mas reaproveita a 1a aba da planilha
    (já existente, criada antes desta função existir) em vez de criar uma
    "Dor" vazia do zero."""
    try:
        return spreadsheet.worksheet(ABA_DOR)
    except gspread.WorksheetNotFound:
        aba = spreadsheet.sheet1
        aba.update_title(ABA_DOR)
        if not aba.row_values(1):
            aba.append_row(CABECALHO)
        aplicar_tema(aba, COR_DOR)
        adicionar_dropdown(aba, COLUNA_DOR, [str(n) for n in range(6)])
        return aba


def exportar_pendentes(con: psycopg.Connection, aba: gspread.Worksheet) -> int:
    """Atividades sem linha em stg_dor E sem linha na planilha viram linha nova."""
    ja_na_planilha = ids_na_coluna(aba)
    atividades = con.execute(
        "SELECT activity_id, data, nome, tipo FROM stg_atividades "
        "WHERE activity_id NOT IN (SELECT activity_id FROM stg_dor) "
        "ORDER BY data"
    ).fetchall()
    novas = [a for a in atividades if a[0] not in ja_na_planilha]
    if not novas:
        return 0
    linhas = [[aid, str(data), nome, tipo, "", "", "", "", ""] for aid, data, nome, tipo in novas]
    inserir_linhas(aba, linhas)
    return len(linhas)


def importar_preenchidas(con: psycopg.Connection, aba: gspread.Worksheet) -> int:
    """Linhas da planilha com a coluna `dor` preenchida -> upsert em stg_dor."""
    registros = aba.get_all_records()
    preenchidas = [r for r in registros if str(r.get("dor", "")).strip() != ""]
    if not preenchidas:
        return 0
    linhas = [
        (
            int(r["activity_id"]),
            r.get("data") or None,
            int(r["dor"]),
            r.get("localizacao") or None,
            r.get("comentario") or None,
            r.get("superficie") or None,
            r.get("tenis") or None,
        )
        for r in preenchidas
    ]
    with con.cursor() as cur:
        cur.executemany(sql_upsert("stg_dor", COLUNAS_STG_DOR, "activity_id"), linhas)
    return len(linhas)


if __name__ == "__main__":
    con = conectar()
    spreadsheet = conectar_planilha()
    aba = obter_aba_dor(spreadsheet)

    n_exportadas = exportar_pendentes(con, aba)
    print(f"{n_exportadas} atividade(s) nova(s) exportada(s) pra planilha.")

    n_importadas = importar_preenchidas(con, aba)
    print(f"{n_importadas} linha(s) de dor importada(s)/atualizada(s) em stg_dor.")

    con.close()
