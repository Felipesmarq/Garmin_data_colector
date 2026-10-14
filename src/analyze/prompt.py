"""Texto que vai pro Gemini: o prompt do plano semanal e o da revisão de
coerência.

Janela de contexto: 4 semanas (JANELA_SEMANAS) -- mesma unidade que a carga
crônica do ACWR já usa (mesociclo padrão na ciência do esporte) e evita
estourar o prompt com histórico irrelevante conforme o projeto cresce
(degradação de raciocínio do LLM com contexto poluído, seção 6.1). Valor
fácil de ajustar/testar, não uma verdade definitiva."""

from datetime import date
from decimal import Decimal

from src.analyze.modelos import DIAS_SEMANA_PT
from src.formato import pace_txt

JANELA_SEMANAS = 4

# Vocabulário fechado de tipos de treino -- baseado no sistema de Jack
# Daniels (Daniels' Running Formula), referência padrão em ciência do
# esporte pra treino de corrida, com % de VO2max por tipo. Nomenclatura em
# português como usada no Brasil (rodagem, tiro, longão) mapeada 1:1 pro
# sistema E/M/T/I/R + fartlek. Fechado de propósito: sem isso, o modelo
# tende a recomendação vaga ("corra mais forte") em vez de um tipo de
# treino com protocolo e propósito fisiológico definidos.
TIPOS_DE_TREINO = """\
- Rodagem/Recuperação (Easy, 65-78% VO2max): ritmo confortável, conversável -- base aeróbica e recuperação entre treinos fortes.
- Longão (Easy estendido, 65-78% VO2max): mesma intensidade da rodagem, distância/duração maior -- resistência aeróbica e adaptação de volume.
- Ritmo/Tempo Run (Marathon, 80-84% VO2max): ritmo sustentável por prova longa.
- Limiar (Threshold, 88-92% VO2max): contínuo ou em blocos longos (20-40min totais) -- eleva o limiar de lactato.
- Intervalado (Interval, 95-100% VO2max): repetições de 3-5min com recuperação incompleta -- maximiza VO2max.
- Tiro (Repetition, acima de 100% VO2max, ritmo de milha): repetições curtas com recuperação completa -- potência anaeróbica, velocidade, economia de corrida.
- Fartlek (intensidade variável, sem estrutura fixa): variação livre de ritmo, recuperação em trote leve (não parado) -- mistura estímulo aeróbico/anaeróbico.\
"""


REGRAS_COMPOSICAO_SEMANA = (
    "- Distribuição polarizada (Seiler & Kjerland, 2006 -- padrão observado em atletas de "
    "endurance de elite): no máximo 1 treino de intensidade (Ritmo, Limiar, Intervalado ou "
    "Tiro) por semana. Todos os outros dias de corrida têm que ser Rodagem/Recuperação ou "
    "Longão. Isso é regra fixa, não sugestão -- não decida o número de treinos fortes "
    "livremente a cada vez.\n"
    "- Alternância hard/easy: nunca dois dias de corrida seguidos sem pelo menos 1 dia de "
    "descanso entre eles."
)


def _formatar_valor(v) -> str:
    """Arredonda float/Decimal pra 2 casas (sem zero à direita) -- números
    crus tipo 4.5746201171875 ou 91.0000000000000000 (AVG() do Postgres
    devolve Decimal, não float) são ruído puro pro prompt, só gastam token."""
    if v is None:
        return ""
    if isinstance(v, (float, Decimal)):
        return f"{float(v):.2f}".rstrip("0").rstrip(".")
    return str(v)


def formatar_tabela(colunas: list[str], linhas: list[tuple]) -> str:
    cabecalho = " | ".join(colunas)
    corpo = "\n".join(" | ".join(_formatar_valor(v) for v in linha) for linha in linhas)
    return f"{cabecalho}\n{corpo}" if linhas else f"{cabecalho}\n(sem atividades no período)"


def faixas_por_tipo(colunas: list[str], linhas: list[tuple]) -> str:
    """Pace/FC que o corredor já demonstrou de verdade em cada tipo_treino,
    na mesma janela do prompt -- %VO2max não é mensurável durante a
    corrida; pace e FC são (aparecem ao vivo no relógio). Referência
    pessoal, não uma tabela de VDOT genérica -- não exige VO2max/limiar
    calibrado, que não temos (ver limitação em CASE_DO_PROJETO_1.md
    seção 6.1)."""
    idx_tipo = colunas.index("tipo_treino")
    idx_pace = colunas.index("pace_min_km")
    idx_fc = colunas.index("fc_media")

    por_tipo: dict[str, list[tuple[float, float]]] = {}
    for linha in linhas:
        tipo, pace, fc = linha[idx_tipo], linha[idx_pace], linha[idx_fc]
        if tipo is None or tipo == "Caminhada" or pace is None or fc is None:
            continue
        por_tipo.setdefault(tipo, []).append((float(pace), float(fc)))

    if not por_tipo:
        return "(sem histórico suficiente ainda pra estimar faixas de pace/FC por tipo)"

    linhas_txt = []
    for tipo, valores in sorted(por_tipo.items()):
        paces = [v[0] for v in valores]
        fcs = [v[1] for v in valores]
        linhas_txt.append(
            f"- {tipo}: pace {pace_txt(min(paces))}-{pace_txt(max(paces))} min/km, "
            f"FC {round(min(fcs))}-{round(max(fcs))} bpm ({len(valores)} sessão(ões) na janela)"
        )
    return "\n".join(linhas_txt)


def construir_prompt(
    atividades_txt: str,
    resumo_txt: str,
    faixas_txt: str,
    observacoes_txt: str,
    instrucoes_guardrail: list[str],
    dias: list[date],
    dias_bloqueados: list[date],
) -> str:
    dias_txt = ", ".join(f"{nome} {d.strftime('%d/%m')}" for nome, d in zip(DIAS_SEMANA_PT, dias))
    partes = [
        "Você é o treinador de um corredor recreacional em retomada gradual após um "
        "episódio de canelite (síndrome do estresse tibial medial). Analise os dados "
        "objetivos de treino (relógio Garmin) abaixo e monte o plano da semana de "
        "treino, como um treinador de verdade faria -- não uma sugestão genérica.",
        "",
        f"A semana a planejar é: {dias_txt}.",
        "",
        "Objetivo: um plano que marque evolução -- sem repetir o padrão de sobrecarga "
        "que já causou lesão antes. Evolução é o objetivo; não-sobrecarga é uma "
        "restrição sobre esse objetivo, não um objetivo paralelo.",
        "",
        "Use exclusivamente os tipos de treino abaixo (nomenclatura + intensidade "
        "fisiológica de referência) -- não invente categoria fora dessa lista. O "
        "%VO2max é só pra você entender a relação de esforço ENTRE os tipos -- o "
        "corredor não consegue medir VO2max correndo, então NUNCA comunique a "
        "intensidade em %VO2max pra ele:",
        TIPOS_DE_TREINO,
        "",
        "Regras obrigatórias de composição da semana (baseadas em evidência, não "
        "opcionais -- servem justamente pra você não variar a estrutura da semana de "
        "forma arbitrária a cada geração):",
        REGRAS_COMPOSICAO_SEMANA,
        "",
        "Pace e FC que o próprio corredor já demonstrou pra cada tipo de treino nas "
        "últimas semanas (dado real dele, não tabela genérica) -- use isso pra dar o "
        "alvo de cada treino em pace (min/km) e FC (bpm), que são os dois números que "
        "ele vê ao vivo no relógio durante a corrida:",
        faixas_txt,
        "",
        f"Atividades dos últimos {JANELA_SEMANAS} semanas (uma linha por atividade):",
        atividades_txt,
        "",
        "Resumo semanal (ACWR = Acute:Chronic Workload Ratio, zona segura 0.8-1.3, risco "
        "alto acima de 1.5). A carga (km_corridos, variação %, carga_cronica_km e ACWR) é "
        "medida em km efetivamente corridos -- a distância ponderada pelo tempo correndo vs. "
        "caminhando; km_total inclui caminhada e é só informativo. Semana sem atividade "
        "aparece com 0:",
        resumo_txt,
        "",
        "Observações do próprio corredor sobre as semanas recentes (texto livre, "
        "escrito por ele -- pode conter contexto que os números não capturam, tipo "
        "cansaço, viagem, mudança de rotina):",
        observacoes_txt,
    ]
    for instrucao in instrucoes_guardrail:
        partes += ["", instrucao]
    if dias_bloqueados:
        bloqueados_txt = ", ".join(
            f"{DIAS_SEMANA_PT[dias.index(d)]} {d.strftime('%d/%m')}" for d in dias_bloqueados
        )
        partes += [
            "",
            f"Dias que obrigatoriamente são descanso (`descanso: true`): {bloqueados_txt} -- "
            "ou já passaram, ou vêm logo depois de um dia em que o corredor correu (a regra "
            "de alternância vale também na virada da semana).",
        ]
    partes += [
        "",
        "Quantos dias de treino a semana tem é decisão sua, com base nos dados -- não há "
        "número fixo, só as regras acima (a alternância permite no máximo 4 dias de corrida "
        "em 7).",
        "",
        "Responda em português. Monte o plano dia a dia da semana, "
        "**exatamente 7 dias, na ordem Domingo a Sábado, incluindo os dias de "
        "descanso** (um item por dia, `descanso: true` e o resto dos campos nulos "
        "nesse caso). Pra cada treino: tipo (da lista acima), distância ou duração "
        "alvo, **pace alvo em min/km e FC alvo em bpm** (nunca %VO2max), e uma frase "
        "curta do porquê desse treino nesse momento, com base nos dados. Pra tipo de "
        "treino sem histórico do corredor ainda, pode sugerir mesmo sem estar no plano "
        "da semana -- coloque em `estimativas_futuras`, estimando pace/FC a partir do "
        "que ele já demonstrou nos tipos mais próximos (ex. Limiar é mais rápido que "
        "Ritmo), nunca dentro do plano da semana em si. Em `prazo_estimado`, seja "
        "específico e explicativo sobre QUANDO -- não \"no futuro\" ou \"quando estiver "
        "pronto\": diga um critério concreto (ex. \"depois de 2 semanas seguidas de "
        "Limiar sem dor\") ou uma janela de tempo estimada (ex. \"provavelmente em "
        "3-4 semanas, no ritmo de evolução atual\"), baseado no protocolo de retomada "
        "pós-canelite (progressão só após sessões consecutivas sem dor) e no "
        "histórico real de progressão do corredor. Feche com uma frase em "
        "`logica_geral` resumindo a lógica geral da semana.",
    ]
    return "\n".join(partes)


def construir_prompt_revisao(prompt_original: str, plano_texto: str) -> str:
    return "\n".join([
        "Você é um revisor cético, independente de quem gerou o plano abaixo -- sua única "
        "função é encontrar problemas, não elogiar o trabalho. Considere o contexto e as "
        "instruções originais que levaram a esse plano:",
        "",
        prompt_original,
        "",
        "=" * 40,
        "PLANO GERADO A PARTIR DESSE CONTEXTO:",
        "=" * 40,
        plano_texto,
        "",
        "Avalie o plano contra três critérios -- qualquer um deles, sozinho, reprova o "
        "plano:",
        "1. Inconsistência interna: o `motivo` de algum dia contradiz o tipo de treino, "
        "distância, duração, pace ou FC escolhidos pra aquele dia.",
        "2. Contradição com os dados de entrada: o plano ignora um sinal óbvio dos dados "
        "(atividades, resumo semanal, observações do corredor) sem justificativa no motivo.",
        "3. Violação de guardrail: se havia alguma instrução obrigatória de guardrail no "
        "contexto acima (ACWR fora da zona segura, ou dor recente exigindo restrição de "
        "intensidade), confira se o plano realmente a respeitou -- inclusive de forma "
        "disfarçada (ex. um treino rotulado como Rodagem/Recuperação mas com distância, "
        "duração, pace ou FC incompatíveis com baixa intensidade).",
        "",
        "Responda `coerente: true` só se o plano passar nos três critérios. Se reprovar, "
        "`coerente: false` e liste em `problemas` cada item específico encontrado (não "
        "genérico -- diga qual dia, qual campo, qual contradição), e em `motivo` um resumo "
        "de uma frase.",
    ])
