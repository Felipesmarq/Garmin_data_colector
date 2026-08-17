"""Fase 7 — análise semanal com IA (Gemini) a partir de vw_sessoes_ia + vw_resumo_semanal.

`gerar_analise(prompt) -> texto` é a única função que fala com o provedor de
LLM -- trocar de Gemini pra outro provedor é reescrever só essa função (ver
CASE_DO_PROJETO_1.md seção 4).

O guardrail de ACWR (seção 6.1) não depende do modelo "perceber" o risco de
sobrecarga sozinho: `_instrucao_guardrail` calcula a partir do ACWR real
(vw_resumo_semanal) e injeta uma instrução obrigatória no prompt quando a
semana atual sai da zona segura 0.8-1.3 -- determinístico, não uma aposta em
o LLM notar um número no meio da tabela.

Janela de contexto: 4 semanas (JANELA_SEMANAS) -- mesma unidade que a carga
crônica do ACWR já usa (mesociclo padrão na ciência do esporte) e evita
estourar o prompt com histórico irrelevante conforme o projeto cresce
(degradação de raciocínio do LLM com contexto poluído, seção 6.1). Valor
fácil de ajustar/testar, não uma verdade definitiva.
"""

import os
from decimal import Decimal
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from google import genai

from src.db import conectar

ROOT = Path(__file__).resolve().parent.parent.parent

JANELA_SEMANAS = 4
MODELO_PADRAO = "gemini-2.5-flash"

ZONA_SEGURA_MIN = 0.8
ZONA_SEGURA_MAX = 1.3
ZONA_ALERTA_REFORCADO = 1.5

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


def _consultar(con: psycopg.Connection, sql: str, params: tuple = ()) -> tuple[list[str], list[tuple]]:
    cur = con.execute(sql, params)
    colunas = [d.name for d in cur.description]
    return colunas, cur.fetchall()


def _formatar_valor(v) -> str:
    """Arredonda float/Decimal pra 2 casas (sem zero à direita) -- números
    crus tipo 4.5746201171875 ou 91.0000000000000000 (AVG() do Postgres
    devolve Decimal, não float) são ruído puro pro prompt, só gastam token."""
    if v is None:
        return ""
    if isinstance(v, (float, Decimal)):
        return f"{float(v):.2f}".rstrip("0").rstrip(".")
    return str(v)


def _formatar_tabela(colunas: list[str], linhas: list[tuple]) -> str:
    cabecalho = " | ".join(colunas)
    corpo = "\n".join(" | ".join(_formatar_valor(v) for v in linha) for linha in linhas)
    return f"{cabecalho}\n{corpo}" if linhas else f"{cabecalho}\n(sem atividades no período)"


def _pace_txt(min_km: float) -> str:
    minutos = int(min_km)
    segundos = round((min_km - minutos) * 60)
    if segundos == 60:
        minutos, segundos = minutos + 1, 0
    return f"{minutos}:{segundos:02d}"


def _faixas_por_tipo(colunas: list[str], linhas: list[tuple]) -> str:
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
            f"- {tipo}: pace {_pace_txt(min(paces))}-{_pace_txt(max(paces))} min/km, "
            f"FC {round(min(fcs))}-{round(max(fcs))} bpm ({len(valores)} sessão(ões) na janela)"
        )
    return "\n".join(linhas_txt)


def _acwr_semana_atual(colunas: list[str], linhas_resumo: list[tuple]) -> float | None:
    """linhas_resumo já em ordem cronológica -- a última é a semana mais recente."""
    if not linhas_resumo:
        return None
    valor = linhas_resumo[-1][colunas.index("acwr")]
    return float(valor) if valor is not None else None


def _instrucao_guardrail(acwr: float | None) -> str:
    """Só dispara pra ACWR alto -- a pesquisa (seção 6.1 do case) documenta
    risco de lesão especificamente acima da zona segura. ACWR abaixo de 0.8
    é destreino, não risco de sobrecarga; não é caso de guardrail."""
    if acwr is None or acwr <= ZONA_SEGURA_MAX:
        return ""
    if acwr > ZONA_ALERTA_REFORCADO:
        return (
            f"ALERTA OBRIGATÓRIO: o ACWR desta semana é {acwr:.2f}, acima do limiar de risco "
            f"alto (1.5) -- pesquisa (Gabbett, 2016) mostra 2-4x mais chance de lesão na semana "
            f"seguinte nessa faixa. Comece a resposta com um alerta explícito de risco de "
            f"sobrecarga e recomende redução de volume/intensidade nesta semana, priorizando "
            f"recuperação em vez de qualquer aumento de carga."
        )
    return (
        f"ALERTA OBRIGATÓRIO: o ACWR desta semana é {acwr:.2f}, fora da zona segura "
        f"(0.8-1.3). Mencione isso explicitamente na resposta antes de sugerir qualquer "
        f"aumento de volume ou intensidade."
    )


def _construir_prompt(
    atividades_txt: str, resumo_txt: str, faixas_txt: str, instrucao_guardrail: str
) -> str:
    partes = [
        "Você é o treinador de um corredor recreacional em retomada gradual após um "
        "episódio de canelite (síndrome do estresse tibial medial). Analise os dados "
        "objetivos de treino (relógio Garmin) abaixo e monte o plano da próxima semana "
        "de treino, como um treinador de verdade faria -- não uma sugestão genérica.",
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
        "Pace e FC que o próprio corredor já demonstrou pra cada tipo de treino nas "
        "últimas semanas (dado real dele, não tabela genérica) -- use isso pra dar o "
        "alvo de cada treino em pace (min/km) e FC (bpm), que são os dois números que "
        "ele vê ao vivo no relógio durante a corrida:",
        faixas_txt,
        "",
        f"Atividades dos últimos {JANELA_SEMANAS} semanas (uma linha por atividade):",
        atividades_txt,
        "",
        "Resumo semanal (km, ACWR = Acute:Chronic Workload Ratio, zona segura 0.8-1.3, "
        "risco alto acima de 1.5):",
        resumo_txt,
    ]
    if instrucao_guardrail:
        partes += ["", instrucao_guardrail]
    partes += [
        "",
        "Responda em português. Monte o plano dia a dia da próxima semana (marque "
        "explicitamente os dias de descanso também), e pra cada treino informe: tipo "
        "(da lista acima), distância ou duração alvo, **pace alvo em min/km e FC alvo "
        "em bpm** (nunca %VO2max), e uma frase curta do porquê desse treino nesse "
        "momento, com base nos dados. Pra tipo de treino sem histórico do corredor "
        "ainda, estime a partir do que ele já demonstrou nos tipos mais próximos "
        "(ex. Limiar é mais rápido que Ritmo) e avise que é uma primeira estimativa, "
        "a ajustar conforme sensação real. Termine com uma frase resumindo a lógica "
        "geral da semana.",
    ]
    return "\n".join(partes)


def gerar_analise(prompt: str) -> str:
    """Chamada ao LLM isolada nesta função -- ver docstring do módulo."""
    load_dotenv(ROOT / ".env")
    cliente = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    modelo = os.environ.get("GEMINI_MODEL", MODELO_PADRAO)
    resposta = cliente.models.generate_content(model=modelo, contents=prompt)
    return resposta.text


if __name__ == "__main__":
    con = conectar()

    colunas_ativ, linhas_ativ = _consultar(
        con,
        "SELECT * FROM vw_sessoes_ia WHERE data >= CURRENT_DATE - (%s * INTERVAL '1 day') ORDER BY data",
        (JANELA_SEMANAS * 7,),
    )
    colunas_resumo, linhas_resumo_desc = _consultar(
        con, "SELECT * FROM vw_resumo_semanal ORDER BY semana_inicio DESC LIMIT %s", (JANELA_SEMANAS,)
    )
    linhas_resumo = list(reversed(linhas_resumo_desc))  # ordem cronológica pro prompt

    acwr_atual = _acwr_semana_atual(colunas_resumo, linhas_resumo)
    instrucao = _instrucao_guardrail(acwr_atual)
    if instrucao:
        print(f"[guardrail ativado -- ACWR atual: {acwr_atual:.2f}]\n")

    prompt = _construir_prompt(
        _formatar_tabela(colunas_ativ, linhas_ativ),
        _formatar_tabela(colunas_resumo, linhas_resumo),
        _faixas_por_tipo(colunas_ativ, linhas_ativ),
        instrucao,
    )
    print(gerar_analise(prompt))

    con.close()
