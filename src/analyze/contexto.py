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
    AUMENTO_SESSAO_MAX,
    JANELA_SESSAO_DIAS,
    acwr_semana_atual,
    dor_mais_recente,
    instrucao_guardrail_acwr,
    instrucao_guardrail_dor,
    instrucao_teto_sessao,
    teto_sessao_km,
)
from src.analyze.forca import SESSOES_POR_SEMANA as SESSOES_FORCA, PrescricaoForca, instrucao_forca, prescrever
from src.analyze.modelos import DIAS_SEMANA_PT, Sessao
from src.analyze.prompt import JANELA_SEMANAS, construir_prompt, faixas_por_tipo, formatar_tabela
from src.analyze.retomada import (
    RETOMADA_SESSOES,
    Retomada,
    estado_retomada,
    instrucao_retomada,
    referencias_trote,
    trote_prescrito,
)
from src.analyze.semana import dias_passados, max_corridas
from src.db import consultar
from src.planilha import obter_aba
from src.sintomas import LIMIAR_DOR_TIBIA, alertas_desde, com_dor_tibia
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
        "SELECT data, tipo_treino, COALESCE(segundos_correndo, duracao_min * 60), km_corridos, "
        "dor, dor_tibia_manha, palpacao_dor "
        "FROM vw_sessoes WHERE data >= %s AND data < %s ORDER BY data",
        (inicio_semana - timedelta(days=JANELA_SESSAO_DIAS), inicio_semana),
    )
    return [Sessao(*linha) for linha in linhas]


def _dias_pos_corrida(con: psycopg.Connection, dias: list[date]) -> list[date]:
    """Dias da semana que vêm logo depois de uma corrida real -- não podem
    ter corrida (caminhada pode): a alternância vale também na virada da
    semana, ou dentro da semana já começada."""
    _, linhas = consultar(
        con,
        "SELECT DISTINCT data FROM vw_sessoes WHERE tipo_treino NOT IN ('Caminhada', 'Força') AND data >= %s AND data < %s",
        (dias[0] - timedelta(days=1), hoje_brt()),
    )
    datas_corrida = {linha[0] for linha in linhas}
    return [d for d in dias if d - timedelta(days=1) in datas_corrida]


@dataclass
class ContextoSemana:
    dias: list[date]
    colunas_ativ: list[str]
    linhas_ativ: list[tuple]
    colunas_resumo: list[str]
    linhas_resumo: list[tuple]  # ordem cronológica
    sessoes: list[Sessao]
    retomada: Retomada | None
    houve_alerta: bool  # sinal de alerta desde o último plano (já liberado, senão nem chega aqui)
    teto_km: float | None  # teto por sessão (só fora da retomada)
    acwr: float | None
    dor_recente: int | None  # dor na tíbia da sessão mais recente com registro
    dias_passados: list[date]  # têm que ser descanso
    dias_pos_corrida: list[date]  # sem corrida (caminhada pode)
    sessoes_evolucao: int  # sessões de evolução da retomada nesta semana
    forca: PrescricaoForca
    observacoes: str

    @property
    def restricao_dor(self) -> bool:
        """Fora da retomada: dor na tíbia recente ou alerta recente tiram
        intensidade e aumento de carga da semana."""
        return (self.dor_recente or 0) >= LIMIAR_DOR_TIBIA or self.houve_alerta

    @property
    def instrucoes(self) -> list[str]:
        """Instruções obrigatórias do prompt: o protocolo de retomada (que já
        trata dor e alerta pelo estágio), ou ACWR + teto por sessão + dor
        fora dela."""
        instr_forca = instrucao_forca(
            min(SESSOES_FORCA, self.sessoes_evolucao) if self.retomada else None, self.forca.teste_calcanhar
        )
        if self.retomada:
            return [instrucao_retomada(self.retomada, self.sessoes_evolucao, DIAS_SEMANA_PT[len(self.dias) - 1]), instr_forca]
        carga = [
            instrucao_guardrail_acwr(self.acwr),
            instrucao_teto_sessao(self.teto_km, com_aumento=not self.restricao_dor),
            instrucao_guardrail_dor(self.dor_recente, self.houve_alerta),
            instr_forca,
        ]
        return [i for i in carga if i]

    def prompt(self) -> str:
        return construir_prompt(
            formatar_tabela(self.colunas_ativ, self.linhas_ativ),
            formatar_tabela(self.colunas_resumo, self.linhas_resumo),
            faixas_por_tipo(self.colunas_ativ, self.linhas_ativ),
            self.observacoes,
            self.instrucoes,
            self.dias,
            self.dias_passados,
            self.dias_pos_corrida,
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
    prescricao = trote_prescrito(spreadsheet, dias[0])
    # alerta desde o início da semana do último estágio prescrito (ou na
    # semana anterior, sem plano de retomada) -- a progressão volta um estágio
    desde_alerta = prescricao.inicio_semana if prescricao else dias[0] - timedelta(days=7)
    houve_alerta = bool(alertas_desde(con, desde_alerta))
    retomada = estado_retomada(sessoes, dias[0], prescricao.trote_bloco if prescricao else None, houve_alerta)
    if retomada:
        pace_trote, fc_teto = referencias_trote(con, hoje_brt())
        retomada = retomada.model_copy(update={"pace_trote": pace_trote, "fc_teto": fc_teto})

    passados = dias_passados(dias)
    pos_corrida = _dias_pos_corrida(con, dias)
    livres = [d for d in dias if d not in passados and d not in pos_corrida]
    # evolução da retomada nunca no último dia (a dor da manhã seguinte tem
    # que estar registrada antes do plano seguinte, que roda domingo às 6h)
    livres_evolucao = [d for d in livres if d != dias[-1]]
    dor_recente = dor_mais_recente(sessoes)
    restricao = (dor_recente or 0) >= LIMIAR_DOR_TIBIA or houve_alerta
    # dor na tíbia na semana anterior ou alerta seguram a progressão da força
    dor_semana = any(com_dor_tibia(s) for s in sessoes if s.data >= dias[0] - timedelta(days=7))
    linhas_resumo = list(reversed(linhas_resumo_desc))  # ordem cronológica pro prompt
    return ContextoSemana(
        dias=dias,
        colunas_ativ=colunas_ativ,
        linhas_ativ=linhas_ativ,
        colunas_resumo=colunas_resumo,
        linhas_resumo=linhas_resumo,
        sessoes=sessoes,
        retomada=retomada,
        houve_alerta=houve_alerta,
        teto_km=None if retomada else teto_sessao_km(sessoes, aumento=0.0 if restricao else AUMENTO_SESSAO_MAX),
        acwr=acwr_semana_atual(colunas_resumo, linhas_resumo),
        dor_recente=dor_recente,
        dias_passados=passados,
        dias_pos_corrida=pos_corrida,
        sessoes_evolucao=min(RETOMADA_SESSOES, max_corridas(livres_evolucao)),
        forca=prescrever(spreadsheet, dias[0], travar=dor_semana or houve_alerta),
        observacoes=observacoes_recentes(spreadsheet),
    )
