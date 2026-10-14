"""Entrega do plano aprovado: aba "Plano da Semana", dias planejados na aba
"Atividades" (Fase 9), texto do plano e e-mail. O envio ao relógio fica em
src/load/treino_garmin.py."""

from datetime import date

from src.analyze.modelos import DIAS_SEMANA_PT, PlanoSemanal
from src.config import EM_CI
from src.email_util import enviar_email
from src.formato import numero_br
from src.load.planilha_desempenho import (
    ABA_ATIVIDADES,
    CABECALHO_ATIVIDADES,
    COLUNAS_REAIS,
    COR_ATIVIDADES,
    STATUS_PLANEJADO,
)
from src.planilha import inserir_linhas, inserir_linhas_no_topo, obter_aba

ABA_PLANO = "Plano da Semana"


COR_PLANO = (0.55, 0.32, 0.78)  # roxo


CABECALHO_PLANO = [
    "dia_semana", "data", "tipo_treino", "distancia_km", "duracao_min",
    "pace_alvo", "fc_alvo", "motivo",
]


def _linhas_plano(plano: PlanoSemanal, dias: list[date]) -> list[tuple]:
    # dia_semana vem de DIAS_SEMANA_PT (índice), não de treino.dia_semana --
    # esse campo é preenchido livremente pelo LLM e saiu inconsistente
    # ("sábado" minúsculo em vez de "Sábado"); nome do dia é dado
    # determinístico, não precisa confiar na IA pra isso.
    #
    # Só linhas de dia (1 dia = 1 linha, 7 por semana) -- estimativas_futuras
    # e logica_geral não têm dia específico e "não se encaixam" numa tabela
    # de histórico por dia; vão só pro e-mail (ver enviar_email_resumo).
    return [
        (
            nome_dia,
            str(data),
            "Descanso" if treino.descanso else treino.tipo_treino,
            treino.distancia_km,
            treino.duracao_min,
            treino.pace_alvo,
            treino.fc_alvo,
            treino.motivo,
        )
        for nome_dia, treino, data in zip(DIAS_SEMANA_PT, plano.dias, dias)
    ]


def plano_ja_escrito(spreadsheet, dias: list[date]) -> bool:
    """A semana já tem plano na aba "Plano da Semana"? Critério: os 7 dias
    dela estão na coluna `data` (o plano sempre escreve os 7 dias, inclusive
    descanso, num append só). Os 7, não só o primeiro: na troca da semana
    de segunda-domingo pra domingo-sábado (2026-10), o domingo 11/10 já
    estava no plano antigo e a semana nova seria tomada como escrita."""
    aba = obter_aba(spreadsheet, ABA_PLANO, CABECALHO_PLANO, cor=COR_PLANO)
    idx_data = CABECALHO_PLANO.index("data") + 1  # col_values é 1-based
    return {str(d) for d in dias} <= set(aba.col_values(idx_data)[1:])


def escrever_plano(spreadsheet, plano: PlanoSemanal, dias: list[date]) -> int:
    """Append-only: cada semana é um registro histórico, não um estado
    atual pra espelhar -- diferente de planilha_desempenho.py (dashboard),
    aqui NÃO se usa sobrescrever(). Idempotente: se a semana já tem plano
    (ver plano_ja_escrito), não insere de novo."""
    if plano_ja_escrito(spreadsheet, dias):
        return 0
    aba = obter_aba(spreadsheet, ABA_PLANO, CABECALHO_PLANO, cor=COR_PLANO)
    linhas = _linhas_plano(plano, dias)
    inserir_linhas(aba, [["" if v is None else v for v in linha] for linha in linhas])
    return len(linhas)


def apagar_plano_da_semana(spreadsheet, dias: list[date]) -> tuple[int, int]:
    """Regeração forçada -- apaga o plano antigo da semana: as linhas da aba
    "Plano da Semana" e as linhas PLANEJADO de "Atividades". Linhas já
    resolvidas (REALIZADO_*/NÃO_REALIZADO) e atividades reais não são
    tocadas: são histórico, não plano. Só deve rodar DEPOIS de o plano novo
    ser aprovado -- se a geração falhar, o plano atual continua intacto.
    Devolve (linhas apagadas do plano, linhas PLANEJADO apagadas)."""
    datas_semana = {str(d) for d in dias}

    aba_plano = obter_aba(spreadsheet, ABA_PLANO, CABECALHO_PLANO, cor=COR_PLANO)
    idx_data_plano = CABECALHO_PLANO.index("data")
    linhas_plano = [
        i + 1
        for i, linha in enumerate(aba_plano.get_all_values())
        if i > 0 and len(linha) > idx_data_plano and linha[idx_data_plano] in datas_semana
    ]

    aba_ativ = obter_aba(spreadsheet, ABA_ATIVIDADES, CABECALHO_ATIVIDADES, cor=COR_ATIVIDADES)
    idx_data = CABECALHO_ATIVIDADES.index("data")
    idx_status = CABECALHO_ATIVIDADES.index("status")
    linhas_ativ = [
        i + 1
        for i, linha in enumerate(aba_ativ.get_all_values())
        if i > 0 and len(linha) > idx_status and linha[idx_data] in datas_semana and linha[idx_status] == STATUS_PLANEJADO
    ]

    # de baixo pra cima, senão o índice das linhas seguintes muda a cada delete
    for r in sorted(linhas_plano, reverse=True):
        aba_plano.delete_rows(r)
    for r in sorted(linhas_ativ, reverse=True):
        aba_ativ.delete_rows(r)
    return len(linhas_plano), len(linhas_ativ)


def escrever_dias_planejados(spreadsheet, plano: PlanoSemanal, dias: list[date], forcar: bool = False) -> int:
    """Fase 9 -- grava um placeholder (status PLANEJADO) por dia de treino
    (não descanso) na aba "Atividades", pro job diário resolver depois
    contra a atividade real (ver planilha_desempenho.resolver_dias_planejados).
    Upsert por `data`, não por `activity_id` (dia planejado não tem
    atividade associada ainda) -- idempotente: se qualquer dia dessa semana
    já tem linha de plano em "Atividades", não insere de novo. Checa
    qualquer dia da semana, não só o domingo -- domingo pode ser dia de
    descanso, que nunca ganha
    linha própria, então checar só ela nunca encontraria a semana já
    escrita e duplicaria a cada execução.

    `forcar=True` (regeração forçada, ver apagar_plano_da_semana): não trata
    linha com status como "semana já escrita" -- só sobraram as já resolvidas
    (as PLANEJADO foram apagadas), e essas datas já têm desfecho, então não
    ganham placeholder novo; as demais ganham."""
    aba = obter_aba(spreadsheet, ABA_ATIVIDADES, CABECALHO_ATIVIDADES, cor=COR_ATIVIDADES)
    idx_data = CABECALHO_ATIVIDADES.index("data")
    idx_status = CABECALHO_ATIVIDADES.index("status")
    datas_semana = {str(d) for d in dias}
    datas_com_status = {
        linha[idx_data]
        for linha in aba.get_all_values()[1:]
        if len(linha) > idx_status and linha[idx_data] in datas_semana and linha[idx_status]
    }
    if datas_com_status and not forcar:
        return 0

    em_branco_real = [""] * len(COLUNAS_REAIS)
    idx_data_real = COLUNAS_REAIS.index("data")
    linhas = []
    # mais recente primeiro (mesma convenção do resto da aba, ver
    # escrever_atividades) -- dias já vem em ordem Domingo->Sábado, então
    # percorre de trás pra frente antes de inserir no topo.
    for treino, data in reversed(list(zip(plano.dias, dias))):
        if treino.descanso or str(data) in datas_com_status:
            continue
        linha_real = list(em_branco_real)
        linha_real[idx_data_real] = str(data)
        linha_plano = [
            STATUS_PLANEJADO,
            treino.tipo_treino or "",
            numero_br(treino.distancia_km, casas=2, remover_zero_a_direita=True),
            numero_br(treino.duracao_min, casas=1, remover_zero_a_direita=True),
            treino.pace_alvo or "",
            treino.fc_alvo or "",
        ]
        linhas.append([str(v) for v in linha_real + linha_plano])
    inserir_linhas_no_topo(aba, linhas)
    return len(linhas)


def texto_plano(plano: PlanoSemanal, dias: list[date]) -> str:
    """Plano completo em texto -- dia a dia + estimativas futuras + lógica
    geral. Compartilhado entre o print do terminal e o corpo do e-mail
    (enviar_email_resumo), pra não duplicar a formatação em dois lugares."""
    linhas = []
    for nome_dia, treino, data in zip(DIAS_SEMANA_PT, plano.dias, dias):
        if treino.descanso:
            linhas.append(f"{nome_dia} ({data}): Descanso -- {treino.motivo}")
            continue
        partes = [f"{treino.distancia_km}km" if treino.distancia_km else f"{treino.duracao_min:g}min"]
        if treino.pace_alvo:
            partes.append(f"pace {treino.pace_alvo}")
        if treino.fc_alvo:
            partes.append(f"FC {treino.fc_alvo}")
        linhas.append(f"{nome_dia} ({data}): {treino.tipo_treino} -- {', '.join(partes)} -- {treino.motivo}")
    if plano.estimativas_futuras:
        linhas.append("")
        linhas.append("Estimativas pra tipos ainda não tentados:")
        for est in plano.estimativas_futuras:
            linhas.append(f"  {est.tipo_treino}: pace {est.pace_alvo}, FC {est.fc_alvo}")
            linhas.append(f"    Prazo estimado: {est.prazo_estimado}")
            linhas.append(f"    {est.nota}")
    linhas.append("")
    linhas.append(f"Lógica da semana: {plano.logica_geral}")
    return "\n".join(linhas)


def imprimir_plano(plano: PlanoSemanal, dias: list[date]) -> None:
    if EM_CI:
        treinos = sum(1 for d in plano.dias if not d.descanso)
        print(
            f"[plano da semana de {dias[0]} gerado: {treinos} treino(s), {7 - treinos} descanso(s) "
            "-- conteúdo completo no e-mail, na planilha e no relógio]"
        )
        return
    print(texto_plano(plano, dias))


def enviar_email_resumo(plano: PlanoSemanal, dias: list[date]) -> None:
    """Plano completo (dia a dia + estimativas + lógica geral) por e-mail --
    esse conteúdo "não se encaixa" na aba "Plano da Semana" (ver
    _linhas_plano, que só guarda 1 linha por dia como histórico), então vai
    só pro e-mail e pro print do terminal."""
    assunto = f"Plano de treino -- semana de {dias[0].strftime('%d/%m')} a {dias[-1].strftime('%d/%m')}"
    enviar_email(assunto, texto_plano(plano, dias))
