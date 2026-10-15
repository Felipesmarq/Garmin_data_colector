"""Sintomas registrados na aba "Dor": dor na tíbia e sinais de alerta.

A dor na tíbia de uma sessão junta três registros -- `dor` (durante e depois
do treino), `dor_tibia_manha` (na manhã seguinte) e `palpacao_dor` (dor ao
apertar a borda interna da tíbia, critério diagnóstico da canelite no ensaio
de Moen et al.). Outros desconfortos (`outra_dor`) só ficam registrados.

O limiar é o mesmo nas duas fases (retomada e pós-retomada): qualquer dor
na tíbia conta -- os protocolos de lesão óssea (Warden et al.) pedem sessão
sem dor durante, depois e no dia seguinte.

Sinal de alerta (dor pontual na tíbia, dor ao caminhar, em repouso ou à
noite) é outra coisa: pode ser fratura por estresse, e suspende o plano até
o usuário preencher `liberado_em` (de preferência depois de avaliação).
"""

from datetime import date
from typing import NamedTuple

import psycopg

LIMIAR_DOR_TIBIA = 1

SEM_ALERTA = "nenhum"
SINAIS_ALERTA = [SEM_ALERTA, "dor pontual na tíbia", "dor ao caminhar", "dor em repouso", "dor à noite"]


def dor_tibia(sessao) -> int | None:
    """Maior dor na tíbia registrada pra sessão (durante/depois, manhã
    seguinte, palpação conta como 1); None se nada foi registrado.
    `sessao` é uma analyze.modelos.Sessao (duck typing)."""
    registros = [v for v in (sessao.dor, sessao.dor_manha) if v is not None]
    if sessao.palpacao is not None:
        registros.append(1 if sessao.palpacao else 0)
    return max(registros) if registros else None


def com_dor_tibia(sessao) -> bool:
    return (dor_tibia(sessao) or 0) >= LIMIAR_DOR_TIBIA


def sem_dor_confirmada(sessao) -> bool:
    """Sessão confirmada sem dor na tíbia: dor 0 durante/depois E na manhã
    seguinte, os dois registrados, e palpação sem dor (ou não feita)."""
    return sessao.dor == 0 and sessao.dor_manha == 0 and not sessao.palpacao


class Alerta(NamedTuple):
    data: date
    sinal: str


def _alertas(con: psycopg.Connection, filtro: str, params: tuple = ()) -> list[Alerta]:
    linhas = con.execute(
        "SELECT data, sinal_alerta FROM stg_dor "
        f"WHERE sinal_alerta IS NOT NULL AND sinal_alerta <> %s {filtro} ORDER BY data",
        (SEM_ALERTA, *params),
    ).fetchall()
    return [Alerta(*linha) for linha in linhas]


def alertas_ativos(con: psycopg.Connection) -> list[Alerta]:
    """Sinais de alerta ainda sem `liberado_em` -- enquanto houver um, não
    sai plano novo."""
    return _alertas(con, "AND liberado_em IS NULL")


def alertas_desde(con: psycopg.Connection, desde: date) -> list[Alerta]:
    """Sinais de alerta registrados a partir de `desde`, liberados ou não --
    a progressão volta um estágio depois de um alerta."""
    return _alertas(con, "AND data >= %s", (desde,))


def texto_alerta(alertas: list[Alerta], consequencia: str) -> str:
    sinais = "\n".join(f"- {a.data.strftime('%d/%m')}: {a.sinal}" for a in alertas)
    return (
        "Sinal de alerta registrado na aba Dor:\n\n"
        f"{sinais}\n\n"
        f"{consequencia}\n\n"
        "Dor pontual na tíbia, dor ao caminhar, em repouso ou à noite podem indicar fratura "
        "por estresse. Procure avaliação (médico ou fisioterapeuta esportivo) antes de "
        "voltar a correr.\n\n"
        "Para retomar: preencha a coluna liberado_em (data) na mesma linha do alerta. O "
        "plano seguinte recomeça um estágio abaixo."
    )
