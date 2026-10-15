"""Fase A de segurança — sinal de alerta na tíbia (ver src/sintomas.py).

Roda no sync diário, logo depois de importar a aba "Dor": se há sinal de
alerta sem `liberado_em`, os dias planejados da semana que ainda não
passaram viram SUSPENSO na aba "Atividades", os treinos deles saem do
relógio e vai um e-mail de alerta. Idempotente: com os dias já suspensos,
a execução seguinte não acha nada pra suspender e não manda e-mail de novo
(o plano de domingo também não é gerado enquanto o alerta não for
liberado -- ver analyze/analisar_com_ia.py).
"""

from datetime import date

import gspread
from gspread.utils import rowcol_to_a1

from src.db import conectar
from src.email_util import enviar_email
from src.extract.garmin import conectar as conectar_garmin
from src.load.planilha_desempenho import (
    ABA_ATIVIDADES,
    CABECALHO_ATIVIDADES,
    COR_ATIVIDADES,
    STATUS_PLANEJADO,
    STATUS_SUSPENSO,
)
from src.load.treino_garmin import remover_treino
from src.planilha import conectar as conectar_planilha
from src.planilha import obter_aba
from src.sintomas import alertas_ativos, texto_alerta
from src.tempo import hoje_brt


def planejados_a_suspender(aba: gspread.Worksheet, hoje: date) -> dict[int, date]:
    """Linhas PLANEJADO de hoje em diante: {número da linha: data}."""
    idx_data, idx_status = CABECALHO_ATIVIDADES.index("data"), CABECALHO_ATIVIDADES.index("status")
    linhas = {}
    for num_linha, linha in enumerate(aba.get_all_values()[1:], start=2):
        if len(linha) <= idx_status or linha[idx_status] != STATUS_PLANEJADO:
            continue
        try:
            data = date.fromisoformat(linha[idx_data])
        except ValueError:
            continue
        if data >= hoje:
            linhas[num_linha] = data
    return linhas


def marcar_suspensos(aba: gspread.Worksheet, linhas: list[int]) -> None:
    coluna = CABECALHO_ATIVIDADES.index("status") + 1
    aba.batch_update(
        [{"range": rowcol_to_a1(n, coluna), "values": [[STATUS_SUSPENSO]]} for n in linhas],
        value_input_option="RAW",
    )


def _remover_do_relogio(datas: list[date]) -> None:
    """Best-effort, igual ao envio (treino_garmin.enviar_plano)."""
    try:
        api = conectar_garmin()
    except Exception as erro:  # noqa: BLE001 -- best-effort de propósito
        print(f"[Garmin: não foi possível conectar pra remover os treinos: {type(erro).__name__}: {erro}]")
        return
    for data in datas:
        try:
            if remover_treino(api, data):
                print(f"[Garmin: treino de {data} removido]")
        except Exception as erro:  # noqa: BLE001
            print(f"[Garmin: falha ao remover o treino de {data}: {type(erro).__name__}: {erro}]")


if __name__ == "__main__":
    con = conectar()
    alertas = alertas_ativos(con)
    con.close()
    if not alertas:
        print("[nenhum sinal de alerta ativo]")
        raise SystemExit(0)

    aba = obter_aba(conectar_planilha(), ABA_ATIVIDADES, CABECALHO_ATIVIDADES, cor=COR_ATIVIDADES)
    linhas = planejados_a_suspender(aba, hoje_brt())
    if not linhas:
        print("[sinal de alerta ativo, sem dia planejado a suspender -- e-mail já enviado antes]")
        raise SystemExit(0)

    # Ordem de propósito: relógio e e-mail antes de marcar SUSPENSO na
    # planilha. Se algo falhar no meio, a nova tentativa do workflow ainda
    # acha os dias PLANEJADO e repete -- no pior caso o e-mail chega duas
    # vezes, mas nunca deixa de chegar.
    datas = sorted(set(linhas.values()))
    print(f"[sinal de alerta ativo: {len(datas)} dia(s) planejado(s) a suspender]")
    _remover_do_relogio(datas)
    dias_txt = ", ".join(d.strftime("%d/%m") for d in datas)
    enviar_email(
        "Treinos suspensos: sinal de alerta",
        texto_alerta(alertas, f"Os treinos planejados de {dias_txt} foram suspensos e tirados do relógio."),
    )
    print("[e-mail de alerta enviado]")
    marcar_suspensos(aba, list(linhas))
