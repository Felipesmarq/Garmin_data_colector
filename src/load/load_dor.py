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
2. `importar_preenchidas`: linhas da aba "Dor" com algum sintoma
   preenchido (dor na tíbia, manhã seguinte, palpação, sinal de alerta ou
   liberação -- ver src/sintomas.py) são upsertadas em `stg_dor` por
   `activity_id`.
"""

from datetime import date, datetime

import gspread
import psycopg

from src.db import conectar, sql_upsert
from src.planilha import adicionar_dropdown, aplicar_tema, formatar_cabecalho, ids_na_coluna, inserir_linhas
from src.planilha import conectar as conectar_planilha
from src.sintomas import SINAIS_ALERTA

ABA_DOR = "Dor"
COR_DOR = (0.82, 0.33, 0.30)  # vermelho-coral

# `dor` = dor na tíbia durante/depois do treino (0-5). As colunas depois de
# `tenis` são da Fase A de segurança (2026-10, ver src/sintomas.py) e ficam
# no fim pra não deslocar o que já estava preenchido na aba.
CABECALHO = [
    "activity_id", "data", "nome", "tipo",
    "dor", "localizacao", "comentario", "superficie", "tenis",
    "dor_tibia_manha", "palpacao_dor", "outra_dor", "sinal_alerta", "liberado_em",
]
COLUNAS_STG_DOR = [
    "activity_id", "data", "dor", "localizacao", "comentario", "superficie", "tenis",
    "dor_tibia_manha", "palpacao_dor", "outra_dor", "sinal_alerta", "liberado_em",
]
NIVEIS_DOR = [str(n) for n in range(6)]
SIM, NAO = "sim", "não"
DROPDOWNS = {
    "dor": NIVEIS_DOR,
    "dor_tibia_manha": NIVEIS_DOR,
    "palpacao_dor": [SIM, NAO],
    "sinal_alerta": SINAIS_ALERTA,
}


def _letra(coluna: str) -> str:
    return chr(ord("A") + CABECALHO.index(coluna))


def _aplicar_dropdowns(aba: gspread.Worksheet, colunas: list[str]) -> None:
    for coluna in colunas:
        if coluna in DROPDOWNS:
            adicionar_dropdown(aba, _letra(coluna), DROPDOWNS[coluna])


def _migrar_cabecalho(aba: gspread.Worksheet) -> None:
    """Aba criada antes das colunas novas: acrescenta as que faltam no fim do
    cabeçalho (com as listas suspensas), sem mexer no que já está preenchido."""
    atual = aba.row_values(1)
    if atual == CABECALHO[: len(atual)] and len(atual) < len(CABECALHO):
        if aba.col_count < len(CABECALHO):
            aba.add_cols(len(CABECALHO) - aba.col_count)
        faltando = CABECALHO[len(atual):]
        intervalo = f"{_letra(faltando[0])}1:{_letra(faltando[-1])}1"
        aba.update([faltando], intervalo)
        formatar_cabecalho(aba, intervalo, COR_DOR)
        _aplicar_dropdowns(aba, faltando)


def obter_aba_dor(spreadsheet: gspread.Spreadsheet) -> gspread.Worksheet:
    """Igual a src.planilha.obter_aba, mas reaproveita a 1a aba da planilha
    (já existente, criada antes desta função existir) em vez de criar uma
    "Dor" vazia do zero; e migra o cabeçalho de uma aba antiga."""
    try:
        aba = spreadsheet.worksheet(ABA_DOR)
    except gspread.WorksheetNotFound:
        aba = spreadsheet.sheet1
        aba.update_title(ABA_DOR)
        if not aba.row_values(1):
            aba.append_row(CABECALHO)
        aplicar_tema(aba, COR_DOR)
        _aplicar_dropdowns(aba, CABECALHO)
        return aba
    _migrar_cabecalho(aba)
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
    em_branco = [""] * (len(CABECALHO) - 4)
    inserir_linhas(aba, [[aid, str(data), nome, tipo, *em_branco] for aid, data, nome, tipo in novas])
    return len(novas)


def _texto(v) -> str:
    return str(v if v is not None else "").strip()


def _inteiro(v) -> int | None:
    return int(_texto(v)) if _texto(v) else None


def _sim_nao(v) -> bool | None:
    t = _texto(v).lower()
    return True if t == SIM else False if t in (NAO, "nao") else None


def _data(v) -> date | None:
    """Data da planilha: ISO (2026-10-20) ou como o Sheets mostra no locale
    pt-BR (20/10/2026)."""
    t = _texto(v)
    for formato in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(t, formato).date()
        except ValueError:
            continue
    return None


def importar_preenchidas(con: psycopg.Connection, aba: gspread.Worksheet) -> int:
    """Linhas da planilha com algum sintoma preenchido (dor, manhã seguinte,
    palpação, sinal de alerta ou liberação) -> upsert em stg_dor."""
    registros = aba.get_all_records()
    campos = ("dor", "dor_tibia_manha", "palpacao_dor", "sinal_alerta", "liberado_em")
    preenchidas = [r for r in registros if any(_texto(r.get(c)) for c in campos)]
    if not preenchidas:
        return 0
    linhas = [
        (
            int(r["activity_id"]),
            r.get("data") or None,
            _inteiro(r.get("dor")),
            r.get("localizacao") or None,
            r.get("comentario") or None,
            r.get("superficie") or None,
            r.get("tenis") or None,
            _inteiro(r.get("dor_tibia_manha")),
            _sim_nao(r.get("palpacao_dor")),
            r.get("outra_dor") or None,
            _texto(r.get("sinal_alerta")) or None,
            _data(r.get("liberado_em")),
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
