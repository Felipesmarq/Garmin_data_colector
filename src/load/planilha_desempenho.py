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

import re
from datetime import date, timedelta
from functools import partial

import gspread
import psycopg

from src.db import conectar
from src.email_util import enviar_email
from src.planilha import conectar as conectar_planilha, inserir_linhas_no_topo, obter_aba, sobrescrever
from src.tempo import hoje_brt

ABA_ATIVIDADES = "Atividades"
COR_ATIVIDADES = (0.16, 0.42, 0.75)  # azul

# Colunas de atividade real (upsert por activity_id, ver escrever_atividades).
COLUNAS_REAIS = [
    "activity_id", "data", "nome", "tipo", "distancia_km", "duracao_min", "pace_min_km",
    "velocidade_media_mps", "fc_media", "fc_maxima", "cadencia_media",
    "efeito_treino_aerobico", "classificacao_atividade",
]
# Colunas de dia planejado (Fase 9 -- ver analisar_com_ia.escrever_dias_planejados
# e resolver_dias_planejados abaixo). Numa linha de atividade real, ficam em
# branco; numa linha de dia planejado, as colunas de COLUNAS_REAIS além de
# `data` ficam em branco até o dia ser resolvido -- aí `distancia_km`,
# `duracao_min`, `pace_min_km` e `fc_media` passam a guardar o agregado real
# do dia, lado a lado com o que foi planejado.
COLUNAS_PLANO = [
    "status", "tipo_planejado", "distancia_planejada_km", "duracao_planejada_min",
    "pace_planejado", "fc_planejado",
]
COLUNAS_ATIVIDADES = COLUNAS_REAIS + COLUNAS_PLANO

STATUS_PLANEJADO = "PLANEJADO"
STATUS_REALIZADO_DENTRO_DA_MARGEM = "REALIZADO_DENTRO_DA_MARGEM"
STATUS_REALIZADO_FORA_DA_MARGEM = "REALIZADO_FORA_DA_MARGEM"
STATUS_NAO_REALIZADO = "NÃO_REALIZADO"

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
    if "activity_id" not in aba.row_values(1):
        # aba criada antes de existir a coluna activity_id -- migração
        # única: reconstrói o cabeçalho; com a aba "vazia" a partir daqui, o
        # upsert abaixo já cuida de reinserir todo o histórico, sem código
        # extra. Checagem propositalmente estreita (só a ausência da coluna
        # chave, não "cabeçalho != esperado" inteiro) -- comparação ampla
        # dispararia de novo em qualquer divergência futura de schema e
        # apagaria os dias planejados/resolvidos já acumulados, que não têm
        # como ser reconstruídos a partir do Postgres (só vivem na planilha).
        aba.clear()
        aba.append_row(CABECALHO_ATIVIDADES)

    ja_na_planilha = _activity_ids_na_planilha(aba)
    filtro = f"WHERE activity_id NOT IN ({','.join(map(str, ja_na_planilha))})" if ja_na_planilha else ""
    linhas = con.execute(
        f"SELECT {', '.join(COLUNAS_REAIS)} FROM vw_sessoes {filtro} ORDER BY data DESC"
    ).fetchall()
    em_branco_plano = [""] * len(COLUNAS_PLANO)  # linha de atividade real não tem plano associado
    linhas_formatadas = [
        ["" if v is None else str(v) for v in linha] + em_branco_plano
        for linha in _formatar_linhas(COLUNAS_REAIS, linhas)
    ]
    inserir_linhas_no_topo(aba, linhas_formatadas)
    return len(linhas)


# Margens de tolerância pra considerar um dia "dentro do planejado" (Fase 9)
# -- ponto de partida, ajustável depois de ver algumas semanas de dado real
# (ver CASE_DO_PROJETO_1.md seção 7).
MARGEM_DISTANCIA_DURACAO = 0.20  # ±20%
MARGEM_PACE_MIN_KM = 30 / 60  # ±30s/km, em minutos decimais
MARGEM_FC_BPM = 10


def _parse_faixa_pace(texto: str) -> tuple[float, float] | None:
    """'10:52-12:29 min/km' (ou só '10:52 min/km') -> (min, max) em minutos
    decimais. `pace_alvo`/`fc_alvo` do plano são texto livre (ver TreinoDia
    em analyze/analisar_com_ia.py) -- parse tolerante, não confia em formato
    fixo."""
    pares = re.findall(r"(\d+):(\d{2})", texto or "")
    if not pares:
        return None
    valores = [int(m) + int(s) / 60 for m, s in pares]
    return (min(valores), max(valores))


def _parse_faixa_fc(texto: str) -> tuple[int, int] | None:
    """'123-133 bpm' (ou só '130 bpm') -> (min, max)."""
    valores = [int(v) for v in re.findall(r"\d+", texto or "")]
    if not valores:
        return None
    return (min(valores), max(valores))


def _dentro_margem_percentual(planejado: float, real: float, margem: float) -> bool:
    return abs(real - planejado) <= planejado * margem


def _dentro_faixa_com_margem(faixa: tuple[float, float] | None, real: float | None, margem: float) -> bool:
    """Sem alvo pra comparar (faixa não pérseável) ou sem valor real (ex.
    atividade sem FC registrada) não reprova por essa métrica -- a régua de
    aprovação só se aplica ao que dá pra comparar de fato."""
    if faixa is None or real is None:
        return True
    minimo, maximo = faixa
    return (minimo - margem) <= real <= (maximo + margem)


def _resolver_dia(
    tipo_planejado: str | None,
    distancia_planejada: float | None,
    duracao_planejada: float | None,
    pace_planejado_txt: str,
    fc_planejado_txt: str,
    atividades: list[tuple],  # (activity_id, tipo_treino, distancia_km, duracao_min, fc_media)
) -> dict:
    """Compara o planejado do dia contra as atividades reais daquele dia
    (0, 1 ou mais). Mais de uma atividade: soma distância/duração; pace
    ponderado pela duração total; FC média ponderada pela duração de cada
    atividade; tipo real (e `activity_id_principal`, usado pra fundir o
    veredito na linha da atividade real em vez de duplicar -- ver
    resolver_dias_planejados) vêm da atividade de maior duração (a
    "principal" do dia) -- ver critérios de aceite da Fase 9 no case do
    projeto."""
    if not atividades:
        return {"status": STATUS_NAO_REALIZADO}

    distancia_real = sum(a[2] or 0 for a in atividades) or None
    duracao_real = sum(a[3] or 0 for a in atividades) or None
    pace_real = (duracao_real / distancia_real) if distancia_real and duracao_real else None
    fc_real = (
        sum((a[4] or 0) * (a[3] or 0) for a in atividades) / duracao_real
        if duracao_real else None
    )
    principal = max(atividades, key=lambda a: a[3] or 0)
    activity_id_principal, tipo_real = principal[0], principal[1]

    aprovado = tipo_planejado is None or tipo_real == tipo_planejado
    if aprovado and distancia_planejada is not None and distancia_real is not None:
        aprovado = _dentro_margem_percentual(distancia_planejada, distancia_real, MARGEM_DISTANCIA_DURACAO)
    if aprovado and duracao_planejada is not None and duracao_real is not None:
        aprovado = _dentro_margem_percentual(duracao_planejada, duracao_real, MARGEM_DISTANCIA_DURACAO)
    if aprovado:
        aprovado = _dentro_faixa_com_margem(_parse_faixa_pace(pace_planejado_txt), pace_real, MARGEM_PACE_MIN_KM)
    if aprovado:
        aprovado = _dentro_faixa_com_margem(_parse_faixa_fc(fc_planejado_txt), fc_real, MARGEM_FC_BPM)

    status = STATUS_REALIZADO_DENTRO_DA_MARGEM if aprovado else STATUS_REALIZADO_FORA_DA_MARGEM
    return {
        "status": status,
        "tipo": tipo_real,
        "distancia_km": distancia_real,
        "duracao_min": duracao_real,
        "pace_min_km": pace_real,
        "fc_media": fc_real,
        "activity_id_principal": activity_id_principal,
    }


def resolver_dias_planejados(con: psycopg.Connection, spreadsheet) -> list[tuple[date, str]]:
    """Resolve todo dia planejado (status PLANEJADO) na aba "Atividades" cuja
    data já **passou** (estritamente anterior a hoje -- hoje nunca é
    resolvido, porque o treino ainda pode acontecer mais tarde nesse mesmo
    dia), comparando contra `vw_sessoes`. Quando existe atividade real pro
    dia, o veredito é **fundido na própria linha da atividade** (só as
    colunas de plano, sem tocar nas colunas de atividade real que já
    estavam lá) e a linha-placeholder (sem `activity_id`) é removida --
    sem isso, o dia aparecia duas vezes na aba (a atividade real e o
    placeholder resolvido lado a lado, mesmo dado repetido). Sem
    atividade real (`NÃO_REALIZADO`), não há o que fundir -- só atualiza o
    placeholder no lugar. Nunca reescreve a aba inteira. Devolve as datas
    resolvidas nesta execução + status final, pro chamador decidir se
    manda o lembrete diário (ver __main__)."""
    aba = obter_aba(spreadsheet, ABA_ATIVIDADES, CABECALHO_ATIVIDADES, cor=COR_ATIVIDADES)
    idx = {c: i for i, c in enumerate(CABECALHO_ATIVIDADES)}
    hoje = hoje_brt()

    todas_linhas = aba.get_all_values()[1:]
    linha_por_activity_id: dict[int, int] = {}
    for num_linha, linha in enumerate(todas_linhas, start=2):
        if len(linha) > idx["activity_id"] and linha[idx["activity_id"]]:
            try:
                linha_por_activity_id[int(linha[idx["activity_id"]])] = num_linha
            except ValueError:
                pass

    atualizacoes = []
    linhas_pra_remover = []
    resolvidos = []
    for num_linha, linha in enumerate(todas_linhas, start=2):
        if len(linha) <= idx["status"] or linha[idx["status"]] != STATUS_PLANEJADO:
            continue
        try:
            data_planejada = date.fromisoformat(linha[idx["data"]])
        except ValueError:
            continue
        if data_planejada >= hoje:
            # dia de hoje ainda não terminou (o treino pode acontecer mais
            # tarde) ou é dia futuro -- em ambos os casos, mantém PLANEJADO.
            # Bug real corrigido em 2026-09-16: usar `>` em vez de `>=` aqui
            # resolvia o dia de hoje mesmo antes de ele terminar, marcando
            # NÃO_REALIZADO num treino que só ainda não tinha acontecido.
            continue

        atividades = con.execute(
            "SELECT activity_id, tipo_treino, distancia_km, duracao_min, fc_media FROM vw_sessoes WHERE data = %s",
            (data_planejada,),
        ).fetchall()
        resultado = _resolver_dia(
            linha[idx["tipo_planejado"]] or None,
            float(linha[idx["distancia_planejada_km"]].replace(",", ".")) if linha[idx["distancia_planejada_km"]] else None,
            float(linha[idx["duracao_planejada_min"]].replace(",", ".")) if linha[idx["duracao_planejada_min"]] else None,
            linha[idx["pace_planejado"]],
            linha[idx["fc_planejado"]],
            [(a[0], a[1], float(a[2]) if a[2] is not None else None, float(a[3]) if a[3] is not None else None,
              float(a[4]) if a[4] is not None else None) for a in atividades],
        )
        resolvidos.append((data_planejada, resultado["status"]))

        linha_real = linha_por_activity_id.get(resultado.get("activity_id_principal"))
        if resultado["status"] != STATUS_NAO_REALIZADO and linha_real is not None:
            valores_plano = [linha[idx[c]] for c in COLUNAS_PLANO]
            valores_plano[COLUNAS_PLANO.index("status")] = resultado["status"]
            col_inicio = idx["status"] + 1  # 1-based
            faixa = (
                f"{gspread.utils.rowcol_to_a1(linha_real, col_inicio)}:"
                f"{gspread.utils.rowcol_to_a1(linha_real, len(CABECALHO_ATIVIDADES))}"
            )
            atualizacoes.append({"range": faixa, "values": [valores_plano]})
            linhas_pra_remover.append(num_linha)
            continue

        # NÃO_REALIZADO (sem atividade real pra fundir), ou fallback
        # defensivo se a atividade principal não foi achada na aba por
        # algum motivo -- atualiza o próprio placeholder no lugar.
        nova_linha = list(linha) + [""] * (len(CABECALHO_ATIVIDADES) - len(linha))
        nova_linha[idx["status"]] = resultado["status"]
        if resultado["status"] != STATUS_NAO_REALIZADO:
            nova_linha[idx["tipo"]] = resultado["tipo"]
            nova_linha[idx["distancia_km"]] = _numero(resultado["distancia_km"], casas=2, remover_zero_a_direita=True)
            nova_linha[idx["duracao_min"]] = _numero(resultado["duracao_min"], casas=1, remover_zero_a_direita=True)
            nova_linha[idx["pace_min_km"]] = _pace(resultado["pace_min_km"])
            nova_linha[idx["fc_media"]] = _numero(resultado["fc_media"], casas=0)
        faixa = f"A{num_linha}:{gspread.utils.rowcol_to_a1(num_linha, len(CABECALHO_ATIVIDADES))}"
        atualizacoes.append({"range": faixa, "values": [nova_linha]})

    if atualizacoes:
        aba.batch_update(atualizacoes, value_input_option="USER_ENTERED")
    # remove os placeholders fundidos só depois de todo write -- em ordem
    # decrescente pra um delete não invalidar o índice do próximo.
    for num_linha in sorted(linhas_pra_remover, reverse=True):
        aba.delete_rows(num_linha)
    return resolvidos


def _texto_lembrete(pendencias: list[tuple[date, str]]) -> str:
    linhas = ["O sync de hoje encontrou treino(s) planejado(s) que não bateram com o executado:", ""]
    for data, status in pendencias:
        rotulo = "não foi realizado" if status == STATUS_NAO_REALIZADO else "foi realizado fora da margem planejada"
        linhas.append(f"- {data.strftime('%d/%m')}: {rotulo}")
    linhas += ["", "Considere repor no próximo treino disponível."]
    return "\n".join(linhas)


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

    resolvidos = resolver_dias_planejados(con, spreadsheet)
    if resolvidos:
        detalhe = ", ".join(f"{d.strftime('%d/%m')}={s}" for d, s in resolvidos)
        print(f"{len(resolvidos)} dia(s) planejado(s) resolvido(s): {detalhe}")

    ontem = hoje_brt() - timedelta(days=1)
    pendencias_ontem = [
        (d, s) for d, s in resolvidos
        if d == ontem and s in (STATUS_NAO_REALIZADO, STATUS_REALIZADO_FORA_DA_MARGEM)
    ]
    if pendencias_ontem:
        enviar_email("Treino de ontem ficou pendente", _texto_lembrete(pendencias_ontem))
        print(f"[e-mail de lembrete enviado -- {len(pendencias_ontem)} pendência(s) de ontem]")

    n_sem = escrever_resumo_semanal(con, spreadsheet)
    print(f"{n_sem} semana(s) escrita(s) na aba '{ABA_RESUMO_SEMANAL}'.")

    con.close()
