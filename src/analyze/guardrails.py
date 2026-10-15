"""Guardrails de carga e dor fora do protocolo de retomada, e a conferência
determinística do plano devolvido pelo LLM (`violacoes_regras`) -- regras
calculadas em Python e injetadas como instrução obrigatória no prompt, não
uma aposta em o modelo perceber o risco sozinho (seção 6.1 do case)."""

from typing import TYPE_CHECKING

from src.analyze.modelos import DIAS_SEMANA_PT, PlanoSemanal, Sessao, TreinoDia
from src.analyze.retomada import CAMINHADA_MAX_MIN, RETOMADA_SESSOES
from src.sintomas import LIMIAR_DOR_TIBIA, dor_tibia

if TYPE_CHECKING:
    from src.analyze.contexto import ContextoSemana

ZONA_SEGURA_MIN = 0.8
ZONA_SEGURA_MAX = 1.3
ZONA_ALERTA_REFORCADO = 1.5

# Fora da retomada -- Frandsen et al. 2025 (BJSM): sessão mais de 10% maior
# que a mais longa dos últimos 30 dias aumentou a taxa de lesão por
# sobrecarga; ACWR e variação semana a semana não mostraram relação.
JANELA_SESSAO_DIAS = 30
AUMENTO_SESSAO_MAX = 0.10

TIPOS_INTENSIDADE = ("ritmo", "limiar", "intervalado", "tiro", "fartlek")


def eh_caminhada(dia: TreinoDia) -> bool:
    return (dia.tipo_treino or "").lower().startswith("caminhada")


def acwr_semana_atual(colunas: list[str], linhas_resumo: list[tuple]) -> float | None:
    """linhas_resumo já em ordem cronológica -- a última é a semana mais recente."""
    if not linhas_resumo:
        return None
    valor = linhas_resumo[-1][colunas.index("acwr")]
    return float(valor) if valor is not None else None


def teto_sessao_km(sessoes: list[Sessao], aumento: float = AUMENTO_SESSAO_MAX) -> float | None:
    maior = max((float(s.km_corridos) for s in sessoes if s.km_corridos is not None), default=0.0)
    return round(maior * (1 + aumento), 2) if maior > 0 else None


def instrucao_teto_sessao(teto_km: float | None, com_aumento: bool = True) -> str:
    if teto_km is None:
        return ""
    referencia = (
        f"10% acima da sessão mais longa dos últimos {JANELA_SESSAO_DIAS} dias"
        if com_aumento
        else f"a sessão mais longa dos últimos {JANELA_SESSAO_DIAS} dias, sem aumento (ver alerta de dor)"
    )
    return (
        f"INSTRUÇÃO OBRIGATÓRIA (carga por sessão): nenhuma sessão de corrida da semana pode "
        f"passar de {teto_km:.2f} km -- {referencia}. Frandsen et al. (BJSM, 2025), com 5.205 "
        f"corredores, mostraram que uma sessão mais de 10% acima da mais longa recente aumenta "
        f"a taxa de lesão por sobrecarga, mesmo com a semana dentro do normal. Use distancia_km "
        f"em todo treino de corrida, pra o limite poder ser conferido."
    )


def instrucao_guardrail_acwr(acwr: float | None) -> str:
    """Só dispara pra ACWR alto -- a pesquisa (seção 6.1 do case) documenta
    risco de lesão especificamente acima da zona segura. ACWR abaixo de 0.8
    é destreino, não risco de sobrecarga; não é caso de guardrail."""
    if acwr is None or acwr <= ZONA_SEGURA_MAX:
        return ""
    if acwr > ZONA_ALERTA_REFORCADO:
        return (
            f"ALERTA OBRIGATÓRIO (carga semanal): o ACWR desta semana é {acwr:.2f}, acima do "
            f"limiar de risco alto (1.5) -- pesquisa (Gabbett, 2016) mostra 2-4x mais chance de "
            f"lesão na semana seguinte nessa faixa. Comece a resposta com um alerta explícito de "
            f"risco de sobrecarga e recomende redução de volume/intensidade nesta semana, "
            f"priorizando recuperação em vez de qualquer aumento de carga."
        )
    return (
        f"ALERTA OBRIGATÓRIO (carga semanal): o ACWR desta semana é {acwr:.2f}, fora da zona "
        f"segura ({ZONA_SEGURA_MIN}-{ZONA_SEGURA_MAX}). Mencione isso explicitamente na resposta antes de sugerir qualquer "
        f"aumento de volume ou intensidade."
    )


def dor_mais_recente(sessoes: list[Sessao]) -> int | None:
    """Dor na tíbia da sessão mais recente com algum registro (durante/depois,
    manhã seguinte ou palpação) -- `sessoes` em ordem cronológica; pula quem
    ainda não tem nada preenchido (None != "sem dor")."""
    for sessao in reversed(sessoes):
        dor = dor_tibia(sessao)
        if dor is not None:
            return dor
    return None


def instrucao_guardrail_dor(dor: int | None, houve_alerta: bool = False) -> str:
    """Fora da retomada (na retomada, a dor já move o estágio): com dor na
    tíbia na sessão mais recente, ou sinal de alerta recente já liberado, a
    semana não tem intensidade nem aumento de carga. Determinístico igual o
    guardrail de ACWR -- não é uma sugestão que o LLM pode ignorar."""
    com_dor = dor is not None and dor >= LIMIAR_DOR_TIBIA
    if not (com_dor or houve_alerta):
        return ""
    motivo = (
        f"a dor na tíbia mais recente registrada foi nível {dor} (escala 0-5)"
        if com_dor
        else "houve sinal de alerta na tíbia na semana anterior (já liberado)"
    )
    return (
        f"ALERTA OBRIGATÓRIO (dor recente): {motivo}. Protocolo pós-canelite: só avança "
        f"carga ou intensidade depois de sessões sem dor na tíbia. Esta semana só pode conter "
        f"Rodagem/Recuperação, Longão ou Caminhada -- nenhum Ritmo, Limiar, Intervalado, Tiro "
        f"ou Fartlek -- e nenhuma sessão acima da mais longa dos últimos {JANELA_SESSAO_DIAS} "
        f"dias, mesmo que o ACWR permita."
    )


def violacoes_regras(plano: PlanoSemanal, ctx: "ContextoSemana") -> list[str]:
    """Regras determinísticas conferidas no plano devolvido pelo LLM -- não
    dependem da revisão de coerência (outra chamada ao LLM) pegar.
    Caminhada não conta como dia de corrida: não entra na alternância nem no
    teto por sessão."""
    dias = ctx.dias
    problemas = []
    treinos = [i for i, d in enumerate(plano.dias) if not d.descanso]
    corridas = [i for i in treinos if not eh_caminhada(plano.dias[i])]
    for a, b in zip(corridas, corridas[1:]):
        if b == a + 1:
            problemas.append(f"{DIAS_SEMANA_PT[a]} e {DIAS_SEMANA_PT[b]}: corridas em dias seguidos")
    for i in treinos:
        if dias[i] in ctx.dias_passados:
            problemas.append(f"{DIAS_SEMANA_PT[i]} ({dias[i]}): dia que já passou, tinha que ser descanso")
    for i in corridas:
        if dias[i] in ctx.dias_pos_corrida:
            problemas.append(f"{DIAS_SEMANA_PT[i]} ({dias[i]}): corrida logo depois de um dia de corrida")
    for i in treinos:
        dia = plano.dias[i]
        if eh_caminhada(dia) and dia.duracao_min and dia.duracao_min > CAMINHADA_MAX_MIN:
            problemas.append(f"{DIAS_SEMANA_PT[i]}: caminhada de {dia.duracao_min:g} min, acima de {CAMINHADA_MAX_MIN}")

    if ctx.retomada:
        if len(corridas) != ctx.sessoes_evolucao:
            problemas.append(
                f"retomada pede {ctx.sessoes_evolucao} sessões de evolução na semana "
                f"({RETOMADA_SESSOES} numa semana inteira), o plano tem {len(corridas)}"
            )
        for i in corridas:
            if not (plano.dias[i].tipo_treino or "").lower().startswith("rodagem"):
                problemas.append(f"{DIAS_SEMANA_PT[i]}: {plano.dias[i].tipo_treino} durante a retomada (só Rodagem/Recuperação ou Caminhada)")
            if i == len(dias) - 1:
                problemas.append(f"{DIAS_SEMANA_PT[i]}: sessão de evolução no último dia da semana (sem tempo de registrar a dor da manhã seguinte)")
        return problemas

    for i in corridas:
        dia = plano.dias[i]
        if ctx.restricao_dor and (dia.tipo_treino or "").lower().startswith(TIPOS_INTENSIDADE):
            problemas.append(f"{DIAS_SEMANA_PT[i]}: {dia.tipo_treino} com alerta de dor ativo (sem intensidade)")
        if ctx.teto_km is not None:
            if dia.distancia_km is None:
                problemas.append(f"{DIAS_SEMANA_PT[i]}: sem distancia_km, o teto por sessão não pode ser conferido")
            elif dia.distancia_km > ctx.teto_km:
                problemas.append(f"{DIAS_SEMANA_PT[i]}: {dia.distancia_km} km, acima do teto por sessão de {ctx.teto_km} km")
    return problemas
