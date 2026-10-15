"""Fase 7 — análise semanal com IA (Gemini): gera, confere, revisa e entrega
o plano da semana. Ponto de entrada do workflow `analise_semanal.yml`
(`python -m src.analyze.analisar_com_ia`); a lógica fica nos módulos:

- `semana`: datas da semana a planejar (domingo a sábado).
- `contexto`: tudo que o plano precisa, lido do banco e da planilha.
- `retomada` e `guardrails`: regras de carga e dor em Python, injetadas
  como instrução obrigatória no prompt e conferidas no plano devolvido
  (`violacoes_regras`) -- não uma aposta em o LLM perceber o risco sozinho.
  Dois regimes: em retomada, protocolo de Warden (um estágio por semana);
  fora dela, teto por sessão de Frandsen et al. 2025 + ACWR em km corridos.
- `prompt`: texto do prompt e da revisão de coerência.
- `gemini`: a única parte que fala com o provedor de LLM.
- `entrega`: planilha, texto do plano e e-mail (o relógio fica em
  src/load/treino_garmin.py).
"""

import os

from src.analyze.contexto import ContextoSemana, carregar_contexto
from src.analyze.entrega import (
    ABA_PLANO,
    apagar_plano_da_semana,
    enviar_email_resumo,
    escrever_dias_planejados,
    escrever_forca,
    escrever_plano,
    escrever_semana_suspensa,
    imprimir_plano,
    plano_ja_escrito,
    texto_plano,
)
from src.analyze.forca import aplicar_forca
from src.analyze.gemini import gerar_analise, revisar_coerencia
from src.analyze.guardrails import violacoes_regras
from src.analyze.modelos import PlanoSemanal, plano_semanal
from src.analyze.retomada import SessaoRetomada, aplicar_retomada
from src.analyze.semana import semana_alvo
from src.config import EM_CI
from src.db import conectar
from src.email_util import enviar_email
from src.extract.garmin import conectar as conectar_garmin
from src.load.planilha_desempenho import ABA_ATIVIDADES, STATUS_PLANEJADO, ordenar_atividades
from src.load.treino_garmin import enviar_plano as enviar_plano_garmin
from src.planilha import conectar as conectar_planilha
from src.sintomas import alertas_ativos, texto_alerta
from src.tempo import hoje_brt

# Fase 11 -- máximo de tentativas (gerar + revisar) antes de desistir e
# abortar, sem cair num plano de fallback fixo -- ver gerar_plano.
MAX_TENTATIVAS_REVISAO = 3


def _relatar(resumo: str, detalhe: str, itens: list[str]) -> None:
    """No GitHub Actions só o resumo (o log é público, ver EM_CI); rodando
    local, o detalhe e cada item."""
    if EM_CI:
        print(resumo)
        return
    print(detalhe)
    for item in itens:
        print(f"  - {item}")


def _imprimir_contexto(ctx: ContextoSemana) -> None:
    if EM_CI:
        print(f"[{'retomada' if ctx.retomada else 'regime normal'}: {len(ctx.instrucoes)} instrução(ões) de carga/dor]\n")
    elif ctx.retomada:
        print(f"[retomada: {ctx.retomada.decisao}; {ctx.sessoes_evolucao} sessão(ões) de evolução; dor na tíbia recente: {ctx.dor_recente}]\n")
    else:
        print(f"[regime normal -- ACWR: {ctx.acwr}, teto por sessão: {ctx.teto_km} km, dor na tíbia recente: {ctx.dor_recente}, alerta recente: {ctx.houve_alerta}]\n")


def gerar_plano(ctx: ContextoSemana) -> tuple[PlanoSemanal, SessaoRetomada | None]:
    """Gera até MAX_TENTATIVAS_REVISAO candidatos: cada um passa primeiro
    pelas regras determinísticas (`violacoes_regras`) e só depois pela
    revisão de coerência (outra chamada ao LLM) -- a revisão nunca vê, e
    portanto nunca aprova, um plano que o Python reprovou."""
    prompt = ctx.prompt()
    for tentativa in range(1, MAX_TENTATIVAS_REVISAO + 1):
        rotulo = f"tentativa {tentativa}/{MAX_TENTATIVAS_REVISAO}"
        candidato = plano_semanal(gerar_analise(prompt))
        problemas = violacoes_regras(candidato, ctx)
        if problemas:
            _relatar(f"[{rotulo} fora das regras de carga: {len(problemas)} problema(s)]",
                     f"[{rotulo} fora das regras de carga:]", problemas)
            continue
        sessao_retomada = aplicar_retomada(candidato, ctx.retomada) if ctx.retomada else None
        aplicar_forca(candidato, ctx.forca)
        revisao = revisar_coerencia(prompt, texto_plano(candidato, ctx.dias, ctx.forca))
        if revisao.coerente:
            if tentativa > 1:
                print(f"[plano aprovado pela revisão de coerência na {rotulo}]")
            return candidato, sessao_retomada
        _relatar(f"[revisão reprovou a {rotulo}: {len(revisao.problemas)} problema(s)]",
                 f"[revisão reprovou a {rotulo}: {revisao.motivo}]", revisao.problemas)
    raise RuntimeError(
        f"Plano reprovado pela revisão de coerência em {MAX_TENTATIVAS_REVISAO} "
        "tentativas seguidas -- abortando sem escrever na planilha nem enviar e-mail."
    )


def entregar(spreadsheet, plano: PlanoSemanal, ctx: ContextoSemana, sessao_retomada: SessaoRetomada | None, forcar: bool) -> None:
    dias = ctx.dias
    imprimir_plano(plano, dias, ctx.forca)

    if forcar:
        apagadas_plano, apagadas_ativ, apagadas_forca = apagar_plano_da_semana(spreadsheet, dias)
        print(
            f"[plano antigo apagado: {apagadas_plano} linha(s) em '{ABA_PLANO}', "
            f"{apagadas_ativ} linha(s) {STATUS_PLANEJADO} em '{ABA_ATIVIDADES}', "
            f"{apagadas_forca} linha(s) de força não registrada]"
        )

    n = escrever_plano(spreadsheet, plano, dias)
    if n:
        print(f"\n[{n} linha(s) escrita(s) na aba '{ABA_PLANO}']")
    else:
        print(f"\n[plano da semana de {dias[0]} já estava na aba '{ABA_PLANO}', não duplicado]")

    n_plan = escrever_dias_planejados(spreadsheet, plano, dias, forcar=forcar)
    if n_plan:
        print(f"[{n_plan} dia(s) planejado(s) escrito(s) na aba '{ABA_ATIVIDADES}', status {STATUS_PLANEJADO}]")
    else:
        print(f"[dias planejados da semana de {dias[0]} já estavam na aba '{ABA_ATIVIDADES}', não duplicados]")
    ordenar_atividades(spreadsheet)

    n_forca = escrever_forca(spreadsheet, plano, dias, ctx.forca)
    if n_forca:
        print(f"[{n_forca} exercício(s) de força escrito(s) na aba de força]")

    enviar_email_resumo(plano, dias, ctx.forca)
    print("[e-mail com o plano da semana enviado]")

    # Fase 12 -- só depois do plano aprovado (nunca por candidato) e da
    # entrega central (planilha + e-mail). Best-effort: a integração com o
    # Garmin é mais frágil que o resto, então nada aqui aborta a execução.
    try:
        enviar_plano_garmin(conectar_garmin(), list(zip(dias, plano.dias)), hoje_brt(), sessao_retomada, ctx.forca)
    except Exception as erro:  # noqa: BLE001 -- best-effort de propósito
        print(f"[Garmin: não foi possível enviar os treinos ao relógio: {type(erro).__name__}: {erro}]")


if __name__ == "__main__":
    # Idempotência ponta a ponta: semana que já tem plano na planilha não é
    # regerada. Reprocessar (retry manual, cron + disparo manual) chamaria o
    # Gemini de novo e o LLM devolve OUTRO plano -- a planilha (idempotente)
    # ficaria com o primeiro enquanto e-mail e relógio receberiam o segundo.
    # Checa antes de tocar no banco ou no Gemini, pra não gastar nada.
    # FORCAR_REGERAR=true (input `forcar` do disparo manual) gera um plano
    # novo mesmo assim e substitui o antigo -- mas só depois de o novo ser
    # aprovado pela revisão, então uma falha na geração não apaga nada.
    spreadsheet = conectar_planilha()
    dias = semana_alvo()
    forcar = os.environ.get("FORCAR_REGERAR") == "true"
    if plano_ja_escrito(spreadsheet, dias):
        if not forcar:
            print(
                f"[plano da semana de {dias[0]} já existe em '{ABA_PLANO}': nada a gerar, "
                "nenhum e-mail nem treino novo enviado]"
            )
            raise SystemExit(0)
        print(f"[regeração forçada: o plano de {dias[0]} em '{ABA_PLANO}' será substituído se o novo for aprovado]")

    con = conectar()

    # Sinal de alerta sem liberação: não sai plano (ver src/sintomas.py).
    alertas = alertas_ativos(con)
    if alertas:
        semana_txt = f"{dias[0].strftime('%d/%m')} a {dias[-1].strftime('%d/%m')}"
        escrever_semana_suspensa(spreadsheet, dias, "Plano suspenso por sinal de alerta na tíbia -- ver e-mail")
        enviar_email(
            "Plano da semana suspenso: sinal de alerta",
            texto_alerta(alertas, f"O plano da semana de {semana_txt} não foi gerado."),
        )
        print(f"[sinal de alerta ativo: plano da semana de {dias[0]} suspenso, e-mail enviado]")
        con.close()
        raise SystemExit(0)

    ctx = carregar_contexto(con, spreadsheet, dias)
    _imprimir_contexto(ctx)
    plano, sessao_retomada = gerar_plano(ctx)
    entregar(spreadsheet, plano, ctx, sessao_retomada, forcar)
    con.close()
