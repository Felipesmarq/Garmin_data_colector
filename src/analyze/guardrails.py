"""Guardrails de carga e dor fora do protocolo de retomada, e a conferência
determinística do plano devolvido pelo LLM (`violacoes_regras`) -- regras
calculadas em Python e injetadas como instrução obrigatória no prompt, não
uma aposta em o modelo perceber o risco sozinho (seção 6.1 do case)."""

from datetime import date

from src.analyze.modelos import DIAS_SEMANA_PT, PlanoSemanal, Sessao
from src.analyze.retomada import Retomada

ZONA_SEGURA_MIN = 0.8


ZONA_SEGURA_MAX = 1.3


ZONA_ALERTA_REFORCADO = 1.5


# Fora da retomada -- Frandsen et al. 2025 (BJSM): sessão mais de 10% maior
# que a mais longa dos últimos 30 dias aumentou a taxa de lesão por
# sobrecarga; ACWR e variação semana a semana não mostraram relação.
JANELA_SESSAO_DIAS = 30


AUMENTO_SESSAO_MAX = 0.10


def acwr_semana_atual(colunas: list[str], linhas_resumo: list[tuple]) -> float | None:
    """linhas_resumo já em ordem cronológica -- a última é a semana mais recente."""
    if not linhas_resumo:
        return None
    valor = linhas_resumo[-1][colunas.index("acwr")]
    return float(valor) if valor is not None else None


def teto_sessao_km(sessoes: list[Sessao]) -> float | None:
    maior = max((float(s.km_corridos) for s in sessoes if s.km_corridos is not None), default=0.0)
    return round(maior * (1 + AUMENTO_SESSAO_MAX), 2) if maior > 0 else None


def instrucao_teto_sessao(teto_km: float | None) -> str:
    if teto_km is None:
        return ""
    return (
        f"INSTRUÇÃO OBRIGATÓRIA (carga por sessão): nenhuma sessão da semana pode passar de "
        f"{teto_km:.2f} km -- 10% acima da sessão mais longa dos últimos {JANELA_SESSAO_DIAS} "
        f"dias. Frandsen et al. (BJSM, 2025), com 5.205 corredores, mostraram que uma sessão "
        f"acima disso aumenta a taxa de lesão por sobrecarga, mesmo com a semana dentro do "
        f"normal. Use distancia_km em todo treino, pra o limite poder ser conferido."
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


LIMIAR_DOR_GUARDRAIL = 2


def dor_mais_recente(colunas: list[str], linhas: list[tuple]) -> int | None:
    """Dor da atividade mais recente com dor registrada -- linhas já vêm em
    ordem cronológica ASC (ORDER BY data), percorre do fim pro início e
    pula quem ainda não tem dor preenchida (None != "sem dor")."""
    idx_dor = colunas.index("dor")
    for linha in reversed(linhas):
        if linha[idx_dor] is not None:
            return int(linha[idx_dor])
    return None


def instrucao_guardrail_dor(dor: int | None) -> str:
    """Progressão de intensidade sessão a sessão, não só carga agregada da
    semana (isso já é o ACWR) -- protocolo de retomada pós-MTSS/canelite:
    só avança intensidade depois de sessões consecutivas sem dor; se a dor
    voltar, regride. Determinístico igual o guardrail de ACWR -- não é uma
    sugestão que o LLM pode ignorar."""
    if dor is None or dor < LIMIAR_DOR_GUARDRAIL:
        return ""
    return (
        f"ALERTA OBRIGATÓRIO (dor recente): a dor mais recente registrada foi nível {dor} "
        f"(escala 0-5). Protocolo de retomada pós-canelite: intensidade só avança depois de "
        f"sessões consecutivas sem dor. Como {dor} >= {LIMIAR_DOR_GUARDRAIL}, esta semana só "
        f"pode conter treinos de Rodagem/Recuperação ou Longão -- nenhum Ritmo, Limiar, "
        f"Intervalado, Tiro ou Fartlek, mesmo que o ACWR permita."
    )


def violacoes_regras(
    plano: "PlanoSemanal",
    dias: list[date],
    retomada: Retomada | None,
    teto_km: float | None,
    dias_bloqueados: list[date],
) -> list[str]:
    """Regras determinísticas conferidas no plano devolvido pelo LLM -- não
    dependem da revisão de coerência (outra chamada ao LLM) pegar.
    `dias_bloqueados`: dias passados + dia seguinte a uma corrida real (a
    alternância vale também na virada da semana)."""
    problemas = []
    corridas = [i for i, d in enumerate(plano.dias) if not d.descanso]
    for a, b in zip(corridas, corridas[1:]):
        if b == a + 1:
            problemas.append(f"{DIAS_SEMANA_PT[a]} e {DIAS_SEMANA_PT[b]}: sessões em dias seguidos")
    for i in corridas:
        if dias[i] in dias_bloqueados:
            problemas.append(f"{DIAS_SEMANA_PT[i]} ({dias[i]}): dia que tinha que ser descanso")
    if retomada:
        if not corridas:
            problemas.append("nenhuma sessão na semana")
        for i in corridas:
            if not (plano.dias[i].tipo_treino or "").lower().startswith("rodagem"):
                problemas.append(f"{DIAS_SEMANA_PT[i]}: {plano.dias[i].tipo_treino} durante a retomada (só Rodagem/Recuperação)")
    elif teto_km is not None:
        for i in corridas:
            km = plano.dias[i].distancia_km
            if km is None:
                problemas.append(f"{DIAS_SEMANA_PT[i]}: sem distancia_km, o teto por sessão não pode ser conferido")
            elif km > teto_km:
                problemas.append(f"{DIAS_SEMANA_PT[i]}: {km} km, acima do teto por sessão de {teto_km} km")
    return problemas
