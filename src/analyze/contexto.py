"""Contexto da semana a planejar: tudo que o plano precisa, lido do banco e
da planilha num lugar só (`carregar_contexto`) -- atividades e resumo da
janela do prompt, sessões recentes, estado da retomada, guardrails, dias
bloqueados e observações do corredor. O resto do pipeline só consome o
`ContextoSemana`."""

from dataclasses import dataclass
from datetime import date, timedelta

import gspread
import psycopg

from src.analyze.guardrails import (
    JANELA_SESSAO_DIAS,
    acwr_semana_atual,
    dor_mais_recente,
    instrucao_guardrail_acwr,
    instrucao_guardrail_dor,
    instrucao_teto_sessao,
    teto_sessao_km,
)
from src.analyze.modelos import Sessao
from src.analyze.prompt import JANELA_SEMANAS, construir_prompt, faixas_por_tipo, formatar_tabela
from src.analyze.retomada import Retomada, estado_retomada, instrucao_retomada, referencias_trote, trote_prescrito
from src.analyze.semana import dias_passados, max_sessoes
from src.db import consultar
from src.planilha import obter_aba
from src.tempo import hoje_brt

ABA_OBSERVACOES = "Observações"
COR_OBSERVACOES = (0.85, 0.60, 0.10)  # âmbar
CABECALHO_OBSERVACOES = ["semana_inicio", "observacao"]


def observacoes_recentes(spreadsheet: gspread.Spreadsheet) -> str:
    """Aba "Observações" -- texto livre por semana, preenchido manualmente
    pelo usuário (nunca escrita pelo script, só lida). Cria a aba com
    cabeçalho na primeira vez se ainda não existir; não insere linha
    nenhuma -- é 100% input manual, mesmo espírito da aba "Dor" mas sem a
    parte de export."""
    aba = obter_aba(spreadsheet, ABA_OBSERVACOES, CABECALHO_OBSERVACOES, cor=COR_OBSERVACOES)
    corte = hoje_brt() - timedelta(weeks=JANELA_SEMANAS)
    recentes = []
    for registro in aba.get_all_records():
        semana_txt = str(registro.get("semana_inicio", "")).strip()
        observacao = str(registro.get("observacao", "")).strip()
        if not semana_txt or not observacao:
            continue
        try:
            semana = date.fromisoformat(semana_txt)
        except ValueError:
            continue
        if semana >= corte:
            recentes.append(f"- semana de {semana_txt}: {observacao}")
    return "\n".join(recentes) if recentes else "(nenhuma observação registrada nas últimas semanas)"


def sessoes_antes_de(con: psycopg.Connection, inicio_semana: date) -> list[Sessao]:
    """Sessões dos JANELA_SESSAO_DIAS dias antes da semana a planejar. Sem
    splits de run/walk, conta a duração inteira como trote (lado
    conservador)."""
    _, linhas = consultar(
        con,
        "SELECT data, tipo_treino, COALESCE(segundos_correndo, duracao_min * 60), km_corridos, dor "
        "FROM vw_sessoes WHERE data >= %s AND data < %s ORDER BY data",
        (inicio_semana - timedelta(days=JANELA_SESSAO_DIAS), inicio_semana),
    )
    return [Sessao(*linha) for linha in linhas]


def _dias_bloqueados(con: psycopg.Connection, dias: list[date]) -> list[date]:
    """Dias que têm que ser descanso: os que já passaram (SEMANA_PLANO=atual)
    e o dia seguinte a uma corrida real -- a alternância vale também na
    virada da semana, ou dentro da semana já começada."""
    _, linhas = consultar(
        con,
        "SELECT DISTINCT data FROM vw_sessoes WHERE tipo_treino <> 'Caminhada' AND data >= %s AND data < %s",
        (dias[0] - timedelta(days=1), hoje_brt()),
    )
    datas_corrida = {linha[0] for linha in linhas}
    passados = dias_passados(dias)
    return [d for d in dias if d in passados or d - timedelta(days=1) in datas_corrida]


@dataclass
class ContextoSemana:
    dias: list[date]
    colunas_ativ: list[str]
    linhas_ativ: list[tuple]
    colunas_resumo: list[str]
    linhas_resumo: list[tuple]  # ordem cronológica
    sessoes: list[Sessao]
    retomada: Retomada | None
    teto_km: float | None  # teto por sessão (só fora da retomada)
    acwr: float | None
    dor_recente: int | None
    dias_bloqueados: list[date]
    max_sessoes: int
    observacoes: str

    @property
    def instrucoes(self) -> list[str]:
        """Instruções obrigatórias do prompt: o protocolo de retomada, ou
        ACWR + teto por sessão fora dela; a de dor vale nos dois regimes."""
        if self.retomada:
            carga = [instrucao_retomada(self.retomada, self.max_sessoes)]
        else:
            carga = [instrucao_guardrail_acwr(self.acwr), instrucao_teto_sessao(self.teto_km)]
        return [i for i in carga + [instrucao_guardrail_dor(self.dor_recente)] if i]

    def prompt(self) -> str:
        return construir_prompt(
            formatar_tabela(self.colunas_ativ, self.linhas_ativ),
            formatar_tabela(self.colunas_resumo, self.linhas_resumo),
            faixas_por_tipo(self.colunas_ativ, self.linhas_ativ),
            self.observacoes,
            self.instrucoes,
            self.dias,
            self.dias_bloqueados,
        )


def carregar_contexto(con: psycopg.Connection, spreadsheet: gspread.Spreadsheet, dias: list[date]) -> ContextoSemana:
    # data em BRT, não CURRENT_DATE do Postgres (servidor roda em UTC) --
    # mesmo cuidado de semana_alvo, ver hoje_brt().
    corte_janela = hoje_brt() - timedelta(days=JANELA_SEMANAS * 7)
    colunas_ativ, linhas_ativ = consultar(
        con, "SELECT * FROM vw_sessoes_ia WHERE data >= %s ORDER BY data", (corte_janela,)
    )
    colunas_resumo, linhas_resumo_desc = consultar(
        con, "SELECT * FROM vw_resumo_semanal ORDER BY semana_inicio DESC LIMIT %s", (JANELA_SEMANAS,)
    )

    sessoes = sessoes_antes_de(con, dias[0])
    retomada = estado_retomada(sessoes, dias[0], trote_prescrito(spreadsheet, dias[0]))
    if retomada:
        pace_trote, fc_teto = referencias_trote(con, hoje_brt())
        retomada = retomada.model_copy(update={"pace_trote": pace_trote, "fc_teto": fc_teto})

    bloqueados = _dias_bloqueados(con, dias)
    livres = [d for d in dias if d not in bloqueados]
    linhas_resumo = list(reversed(linhas_resumo_desc))  # ordem cronológica pro prompt
    return ContextoSemana(
        dias=dias,
        colunas_ativ=colunas_ativ,
        linhas_ativ=linhas_ativ,
        colunas_resumo=colunas_resumo,
        linhas_resumo=linhas_resumo,
        sessoes=sessoes,
        retomada=retomada,
        teto_km=None if retomada else teto_sessao_km(sessoes),
        acwr=acwr_semana_atual(colunas_resumo, linhas_resumo),
        dor_recente=dor_mais_recente(colunas_ativ, linhas_ativ),
        dias_bloqueados=bloqueados,
        max_sessoes=max_sessoes((livres[-1] - livres[0]).days + 1) if livres else 0,
        observacoes=observacoes_recentes(spreadsheet),
    )
