"""Fase 6 (extensão) — dashboard de desempenho na planilha, abas "Atividades"
e "Resumo Semanal" -- somente leitura, sem upsert em nenhuma tabela do
Postgres (a planilha em si não alimenta o banco de volta).

"Resumo Semanal" continua sobrescrita inteira a cada execução -- poucas
linhas (1 por semana ISO), sem custo real de reescrever, e sem "linha
existente" que valha a pena preservar. "Atividades" faz upsert por
`activity_id` (Fase 9): só insere atividade nova, nunca reescreve a que já
está na planilha -- sobrescrever tudo a cada execução ficava mais caro a
cada atividade nova acumulada. Efeito colateral aceito: uma atividade
corrigida ou apagada direto no Postgres não se reflete mais sozinha na
planilha (caso raro, sem lógica de reconciliação -- ver CASE_DO_PROJETO_1
seção 11).

As views guardam `double precision`/`numeric` crus (ex. distância em km
como `3.00331005859375`), sem serventia pra leitura humana -- os
FORMATADORES abaixo arredondam cada coluna pro nível de precisão que faz
sentido pra ela antes de escrever na planilha.
"""

from functools import partial

import psycopg

from src.db import conectar
from src.planilha import conectar as conectar_planilha, inserir_linhas_no_topo, obter_aba, sobrescrever

ABA_ATIVIDADES = "Atividades"
COR_ATIVIDADES = (0.16, 0.42, 0.75)  # azul
COLUNAS_ATIVIDADES = [
    "activity_id", "data", "nome", "tipo", "distancia_km", "duracao_min", "pace_min_km",
    "velocidade_media_mps", "fc_media", "fc_maxima", "cadencia_media",
    "efeito_treino_aerobico", "classificacao_atividade",
]
# nome de exibição, quando difere da coluna real (ex.: escrevemos km/h na
# planilha, mas a coluna em vw_sessoes -- e o valor bruto da Garmin -- é m/s)
RENOMEAR_CABECALHO = {"velocidade_media_mps": "velocidade_media_kmh"}
CABECALHO_ATIVIDADES = [RENOMEAR_CABECALHO.get(c, c) for c in COLUNAS_ATIVIDADES]

ABA_RESUMO_SEMANAL = "Resumo Semanal"
COR_RESUMO_SEMANAL = (0.20, 0.55, 0.36)  # verde
CABECALHO_RESUMO_SEMANAL = [
    "semana_inicio", "km_total", "duracao_total_min", "num_atividades",
    "variacao_pct_km_vs_semana_anterior", "carga_cronica_km", "acwr",
    "sono_score_medio", "fc_repouso_media",
]


def _numero(v, casas: int, remover_zero_a_direita: bool = False) -> str:
    """Arredonda pra `casas` decimais; remover_zero_a_direita tira zero(s)
    sobrando -- 3.00 -> "3", 3.50 -> "3.5" (pra colunas onde precisão fixa
    só polui, tipo distância/duração).

    Devolve com vírgula decimal (locale BR da planilha) -- com ponto, o
    Sheets confunde "3.01" com data (dia.mês) em vez de número, e some o
    ponto/mostra zero à esquerda ("03.01").
    """
    if v is None:
        return ""
    texto = f"{float(v):.{casas}f}"
    if remover_zero_a_direita:
        texto = texto.rstrip("0").rstrip(".")
    return texto.replace(".", ",")


def _pace(v) -> str:
    """minutos por km em float (ex. 5.532) -> "M:SS" (ex. "5:32").

    Prefixo `'` força a célula a ficar como texto -- sem ele, o Sheets
    interpreta "5:32" como horário (USER_ENTERED) e mostra "05:32:00".
    """
    if v is None:
        return ""
    minutos = int(v)
    segundos = round((float(v) - minutos) * 60)
    if segundos == 60:
        minutos, segundos = minutos + 1, 0
    return f"'{minutos}:{segundos:02d}"


def _percentual(v) -> str:
    return "" if v is None else f"{float(v):.1f}%".replace(".", ",")


def _velocidade_kmh(v) -> str:
    """m/s (bruto da Garmin, ver averageSpeed em extract/garmin.py) -> km/h."""
    return "" if v is None else _numero(float(v) * 3.6, casas=1)


# Enums em inglês da Garmin (tipo de atividade, classificação de efeito de
# treino) traduzidos só pra exibição na planilha -- o dado bruto em
# stg_atividades/vw_sessoes continua em inglês, sem tradução, porque é o
# valor exato que a API devolve.
TRADUCAO_TIPO = {
    "running": "Corrida",
    "treadmill_running": "Corrida em esteira",
}

TRADUCAO_CLASSIFICACAO = {
    "AEROBIC_BASE": "Base Aeróbica",
    "RECOVERY": "Recuperação",
    "TEMPO": "Tempo",
    "LACTATE_THRESHOLD": "Limiar de Lactato",
    "VO2MAX": "VO2max",
    "ANAEROBIC_CAPACITY": "Capacidade Anaeróbica",
    "SPRINT": "Sprint",
    "CAMINHADA": "Caminhada",
    "UNKNOWN": "Desconhecido",
}


def _traduzir(v, dicionario: dict[str, str]) -> str:
    """Fora do dicionário (enum novo que a Garmin ainda não tinha mandado
    até agora), mantém o valor original em vez de quebrar."""
    if v is None:
        return ""
    return dicionario.get(v, v)


FORMATADORES = {
    "tipo": partial(_traduzir, dicionario=TRADUCAO_TIPO),
    "distancia_km": partial(_numero, casas=2, remover_zero_a_direita=True),
    "duracao_min": partial(_numero, casas=1, remover_zero_a_direita=True),
    "pace_min_km": _pace,
    "velocidade_media_mps": _velocidade_kmh,
    "cadencia_media": partial(_numero, casas=0),
    "efeito_treino_aerobico": partial(_numero, casas=1),
    "classificacao_atividade": partial(_traduzir, dicionario=TRADUCAO_CLASSIFICACAO),
    "km_total": partial(_numero, casas=2, remover_zero_a_direita=True),
    "duracao_total_min": partial(_numero, casas=1, remover_zero_a_direita=True),
    "variacao_pct_km_vs_semana_anterior": _percentual,
    "carga_cronica_km": partial(_numero, casas=2, remover_zero_a_direita=True),
    "acwr": partial(_numero, casas=2),
    "sono_score_medio": partial(_numero, casas=0),
    "fc_repouso_media": partial(_numero, casas=1),
}


def _formatar_linhas(cabecalho: list[str], linhas: list[tuple]) -> list[tuple]:
    return [
        tuple(FORMATADORES[c](v) if c in FORMATADORES else v for c, v in zip(cabecalho, linha))
        for linha in linhas
    ]


def _activity_ids_na_planilha(aba) -> set[int]:
    valores = aba.col_values(1)[1:]  # pula cabeçalho (coluna A = activity_id)
    return {int(v) for v in valores if v}


def escrever_atividades(con: psycopg.Connection, spreadsheet) -> int:
    """Upsert por `activity_id`: só insere atividade que ainda não está na
    planilha, no topo (mais recente primeiro) -- nunca reescreve a aba
    inteira (ver módulo)."""
    aba = obter_aba(spreadsheet, ABA_ATIVIDADES, CABECALHO_ATIVIDADES, cor=COR_ATIVIDADES)
    if aba.row_values(1) != CABECALHO_ATIVIDADES:
        # aba criada antes de existir a coluna activity_id -- reconstrói o
        # cabeçalho uma vez; com a aba "vazia" a partir daqui, o upsert
        # abaixo já cuida de reinserir todo o histórico, sem código extra.
        aba.clear()
        aba.append_row(CABECALHO_ATIVIDADES)

    ja_na_planilha = _activity_ids_na_planilha(aba)
    filtro = f"WHERE activity_id NOT IN ({','.join(map(str, ja_na_planilha))})" if ja_na_planilha else ""
    linhas = con.execute(
        f"SELECT {', '.join(COLUNAS_ATIVIDADES)} FROM vw_sessoes {filtro} ORDER BY data DESC"
    ).fetchall()
    linhas_formatadas = [
        ["" if v is None else str(v) for v in linha]
        for linha in _formatar_linhas(COLUNAS_ATIVIDADES, linhas)
    ]
    inserir_linhas_no_topo(aba, linhas_formatadas)
    return len(linhas)


def escrever_resumo_semanal(con: psycopg.Connection, spreadsheet) -> int:
    aba = obter_aba(spreadsheet, ABA_RESUMO_SEMANAL, CABECALHO_RESUMO_SEMANAL, cor=COR_RESUMO_SEMANAL)
    linhas = con.execute(
        f"SELECT {', '.join(CABECALHO_RESUMO_SEMANAL)} FROM vw_resumo_semanal ORDER BY semana_inicio DESC"
    ).fetchall()
    sobrescrever(aba, CABECALHO_RESUMO_SEMANAL, _formatar_linhas(CABECALHO_RESUMO_SEMANAL, linhas))
    return len(linhas)


if __name__ == "__main__":
    con = conectar()
    spreadsheet = conectar_planilha()

    n_ativ = escrever_atividades(con, spreadsheet)
    print(f"{n_ativ} atividade(s) escrita(s) na aba '{ABA_ATIVIDADES}'.")

    n_sem = escrever_resumo_semanal(con, spreadsheet)
    print(f"{n_sem} semana(s) escrita(s) na aba '{ABA_RESUMO_SEMANAL}'.")

    con.close()
