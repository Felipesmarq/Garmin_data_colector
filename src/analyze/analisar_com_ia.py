"""Fase 7 — análise semanal com IA (Gemini) a partir de vw_sessoes_ia + vw_resumo_semanal.

`gerar_analise(prompt) -> texto` é a única função que fala com o provedor de
LLM -- trocar de Gemini pra outro provedor é reescrever só essa função (ver
CASE_DO_PROJETO_1.md seção 4).

Dois guardrails (seção 6.1) não dependem do modelo "perceber" risco
sozinho -- ambos calculados em Python e injetados como instrução
obrigatória no prompt, não uma aposta em o LLM notar um número no meio da
tabela: `_instrucao_guardrail_acwr` (carga semanal agregada, dispara fora
da zona segura 0.8-1.3) e `_instrucao_guardrail_dor` (progressão sessão a
sessão -- trava intensidade se a dor mais recente registrada foi >= 2,
seguindo protocolo de retomada pós-canelite). Regras de composição da
semana (REGRAS_COMPOSICAO_SEMANA -- distribuição polarizada 80/20 e
alternância hard/easy) existem especificamente pra reduzir variação
arbitrária entre gerações sucessivas do plano.

Janela de contexto: 4 semanas (JANELA_SEMANAS) -- mesma unidade que a carga
crônica do ACWR já usa (mesociclo padrão na ciência do esporte) e evita
estourar o prompt com histórico irrelevante conforme o projeto cresce
(degradação de raciocínio do LLM com contexto poluído, seção 6.1). Valor
fácil de ajustar/testar, não uma verdade definitiva.
"""

import os
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import gspread
import psycopg
from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel

from src.db import conectar
from src.email_util import enviar_email
from src.extract.garmin import conectar as conectar_garmin
from src.load.treino_garmin import enviar_plano as enviar_plano_garmin
from src.tempo import hoje_brt
from src.load.planilha_desempenho import (
    ABA_ATIVIDADES,
    CABECALHO_ATIVIDADES,
    COLUNAS_REAIS,
    COR_ATIVIDADES,
    STATUS_PLANEJADO,
)
from src.planilha import conectar as conectar_planilha, inserir_linhas, inserir_linhas_no_topo, obter_aba

ROOT = Path(__file__).resolve().parent.parent.parent

JANELA_SEMANAS = 4
MODELO_PADRAO = "gemini-3.5-flash"

# Fase 10 -- teto de tokens do prompt, bem abaixo do limite real do Gemini
# (até 1M) -- ver gerar_analise().
TETO_TOKENS = 200_000

# Fase 11 -- máximo de tentativas (gerar + revisar) antes de desistir e
# abortar, sem cair num plano de fallback fixo -- ver __main__.
MAX_TENTATIVAS_REVISAO = 3

DIAS_SEMANA_PT = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]

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


class TreinoDia(BaseModel):
    dia_semana: str  # "Segunda".."Domingo", só pra o modelo se situar -- a data real é atribuída em Python (_semana_alvo), nunca confiada ao LLM
    descanso: bool
    tipo_treino: str | None = None  # da lista TIPOS_DE_TREINO, ou None se descanso
    distancia_km: float | None = None
    duracao_min: float | None = None
    pace_alvo: str | None = None  # "10:52-12:29 min/km"
    fc_alvo: str | None = None  # "123-133 bpm"
    motivo: str


class EstimativaFutura(BaseModel):
    """Tipos de treino sem histórico do corredor ainda (ex. Intervalado,
    Tiro) -- referência pra quando ele for tentar, não faz parte do plano
    da semana em si."""
    tipo_treino: str
    pace_alvo: str
    fc_alvo: str
    prazo_estimado: str  # critério/tempo explícito pra chegar lá, não "no futuro"
    nota: str


class PlanoSemanal(BaseModel):
    dias: list[TreinoDia]
    estimativas_futuras: list[EstimativaFutura] = []
    logica_geral: str


class RevisaoCoerencia(BaseModel):
    """Fase 11 -- veredito estruturado da segunda chamada ao Gemini
    (revisar_coerencia), não texto livre -- mesmo padrão de saída
    estruturada da geração do plano."""
    coerente: bool
    motivo: str
    problemas: list[str] = []


def _semana_alvo() -> list[date]:
    """Segunda a domingo da semana a planejar -- calculado em Python, não
    pedido ao LLM (aritmética de data não é trabalho pra prompt).

    Por padrão é a próxima semana ISO (o cron roda no domingo à noite). A
    variável de ambiente SEMANA_PLANO=atual muda pra semana ISO corrente:
    serve pra reexecução manual quando o cron falhou e a semana já começou
    (ver workflow_dispatch em analise_semanal.yml).

    `hoje_brt()`, não `date.today()`: essa conta roda no domingo à noite,
    e um atraso de agendamento no cron (comum no GitHub Actions) pode
    empurrar a execução pra depois da meia-noite UTC -- `date.today()`
    já seria segunda-feira ali, e a conta pularia a semana inteira (bug
    real, ver CASE_DO_PROJETO_1.md seção 11)."""
    hoje = hoje_brt()
    if os.environ.get("SEMANA_PLANO") == "atual":
        segunda = hoje - timedelta(days=hoje.weekday())
    else:
        segunda = hoje + timedelta(days=7 - hoje.weekday())
    return [segunda + timedelta(days=i) for i in range(7)]


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


ABA_OBSERVACOES = "Observações"
COR_OBSERVACOES = (0.85, 0.60, 0.10)  # âmbar
CABECALHO_OBSERVACOES = ["semana_inicio", "observacao"]


def _observacoes_recentes(spreadsheet: gspread.Spreadsheet) -> str:
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


def _acwr_semana_atual(colunas: list[str], linhas_resumo: list[tuple]) -> float | None:
    """linhas_resumo já em ordem cronológica -- a última é a semana mais recente."""
    if not linhas_resumo:
        return None
    valor = linhas_resumo[-1][colunas.index("acwr")]
    return float(valor) if valor is not None else None


def _instrucao_guardrail_acwr(acwr: float | None) -> str:
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
        f"segura (0.8-1.3). Mencione isso explicitamente na resposta antes de sugerir qualquer "
        f"aumento de volume ou intensidade."
    )


LIMIAR_DOR_GUARDRAIL = 2


def _dor_mais_recente(colunas: list[str], linhas: list[tuple]) -> int | None:
    """Dor da atividade mais recente com dor registrada -- linhas já vêm em
    ordem cronológica ASC (ORDER BY data), percorre do fim pro início e
    pula quem ainda não tem dor preenchida (None != "sem dor")."""
    idx_dor = colunas.index("dor")
    for linha in reversed(linhas):
        if linha[idx_dor] is not None:
            return int(linha[idx_dor])
    return None


def _instrucao_guardrail_dor(dor: int | None) -> str:
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


REGRAS_COMPOSICAO_SEMANA = (
    "- Distribuição polarizada (Seiler & Kjerland, 2006 -- padrão observado em atletas de "
    "endurance de elite): no máximo 1 treino de intensidade (Ritmo, Limiar, Intervalado ou "
    "Tiro) por semana. Todos os outros dias de corrida têm que ser Rodagem/Recuperação ou "
    "Longão. Isso é regra fixa, não sugestão -- não decida o número de treinos fortes "
    "livremente a cada vez.\n"
    "- Alternância hard/easy: nunca dois dias de corrida seguidos sem pelo menos 1 dia de "
    "descanso entre eles."
)


def _construir_prompt(
    atividades_txt: str,
    resumo_txt: str,
    faixas_txt: str,
    observacoes_txt: str,
    instrucoes_guardrail: list[str],
    dias: list[date],
) -> str:
    dias_txt = ", ".join(f"{nome} {d.strftime('%d/%m')}" for nome, d in zip(DIAS_SEMANA_PT, dias))
    partes = [
        "Você é o treinador de um corredor recreacional em retomada gradual após um "
        "episódio de canelite (síndrome do estresse tibial medial). Analise os dados "
        "objetivos de treino (relógio Garmin) abaixo e monte o plano da próxima semana "
        "de treino, como um treinador de verdade faria -- não uma sugestão genérica.",
        "",
        f"A próxima semana é: {dias_txt}.",
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
        "Resumo semanal (km, ACWR = Acute:Chronic Workload Ratio, zona segura 0.8-1.3, "
        "risco alto acima de 1.5):",
        resumo_txt,
        "",
        "Observações do próprio corredor sobre as semanas recentes (texto livre, "
        "escrito por ele -- pode conter contexto que os números não capturam, tipo "
        "cansaço, viagem, mudança de rotina):",
        observacoes_txt,
    ]
    for instrucao in instrucoes_guardrail:
        partes += ["", instrucao]
    partes += [
        "",
        "Responda em português. Monte o plano dia a dia da próxima semana, "
        "**exatamente 7 dias, na ordem Segunda a Domingo, incluindo os dias de "
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


def _cliente_gemini() -> tuple[genai.Client, str]:
    load_dotenv(ROOT / ".env")
    cliente = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    modelo = os.environ.get("GEMINI_MODEL", MODELO_PADRAO)
    return cliente, modelo


def _verificar_teto_tokens(cliente: genai.Client, modelo: str, texto: str) -> None:
    """Fase 10 -- rede de segurança, não limite operacional: a janela de 4
    semanas já mantém o prompt bem abaixo disso; um teto bem abaixo do
    limite real do Gemini (até 1M) pega algo que fugiu do esperado (bug de
    janela, observação colada por engano) antes de gastar uma chamada de
    geração/revisão em cima de um prompt fora do normal."""
    contagem = cliente.models.count_tokens(model=modelo, contents=texto)
    if contagem.total_tokens > TETO_TOKENS:
        raise RuntimeError(
            f"Prompt com {contagem.total_tokens} tokens, acima do teto de {TETO_TOKENS} -- "
            "algo fugiu do esperado (não é limite normal de operação). Abortando."
        )


def gerar_analise(prompt: str) -> str:
    """Chamada ao LLM isolada nesta função -- ver docstring do módulo.

    Devolve texto (contrato genérico, ver seção 4 do case) -- só que aqui
    esse texto é um JSON validado contra PlanoSemanal (response_schema),
    não texto livre. `response_schema` é config específica do Gemini; fica
    contida nesta função, então trocar de provedor continua sendo só
    reescrever `gerar_analise`.
    """
    cliente, modelo = _cliente_gemini()
    _verificar_teto_tokens(cliente, modelo, prompt)

    resposta = cliente.models.generate_content(
        model=modelo,
        contents=prompt,
        config=types.GenerateContentConfig(
            # não usamos tools/function calling -- desliga o AFC padrão pra
            # não gerar o aviso "Direct use of AFC..." a cada chamada, sem
            # efeito nenhum no resultado.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            response_mime_type="application/json",
            response_schema=PlanoSemanal,
        ),
    )
    if not resposta.candidates:
        motivo = resposta.prompt_feedback.block_reason if resposta.prompt_feedback else "desconhecido"
        raise RuntimeError(f"Gemini bloqueou o prompt antes de gerar resposta (motivo: {motivo}).")
    finalizacao = resposta.candidates[0].finish_reason
    if finalizacao not in (types.FinishReason.STOP, None) or not resposta.text:
        raise RuntimeError(f"Gemini não completou a resposta (finish_reason={finalizacao}).")
    return resposta.text


def _construir_prompt_revisao(prompt_original: str, plano_texto: str) -> str:
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


def revisar_coerencia(prompt_original: str, plano_texto: str) -> RevisaoCoerencia:
    """Fase 11 -- segunda chamada ao Gemini, independente da que gerou o
    plano (não autoavaliação na mesma resposta, ver CASE_DO_PROJETO_1.md
    seção 11) -- avalia coerência interna, contradição com os dados de
    entrada e violação de guardrail que tenha escapado da restrição de
    vocabulário. Recebe o prompt original (que já contém os dados e as
    instruções de guardrail que dispararam, se algum) + o plano gerado."""
    cliente, modelo = _cliente_gemini()
    prompt_revisao = _construir_prompt_revisao(prompt_original, plano_texto)
    _verificar_teto_tokens(cliente, modelo, prompt_revisao)

    resposta = cliente.models.generate_content(
        model=modelo,
        contents=prompt_revisao,
        config=types.GenerateContentConfig(
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            response_mime_type="application/json",
            response_schema=RevisaoCoerencia,
        ),
    )
    if not resposta.candidates:
        motivo = resposta.prompt_feedback.block_reason if resposta.prompt_feedback else "desconhecido"
        raise RuntimeError(f"Gemini bloqueou o prompt de revisão (motivo: {motivo}).")
    finalizacao = resposta.candidates[0].finish_reason
    if finalizacao not in (types.FinishReason.STOP, None) or not resposta.text:
        raise RuntimeError(f"Gemini não completou a revisão (finish_reason={finalizacao}).")
    return RevisaoCoerencia.model_validate_json(resposta.text)


def _plano_semanal(texto_json: str) -> PlanoSemanal:
    """Parse + validação do JSON devolvido por gerar_analise -- separado da
    chamada em si, pra ficar provider-agnostic (qualquer LLM que devolva
    esse formato funciona aqui, não só Gemini)."""
    plano = PlanoSemanal.model_validate_json(texto_json)
    if len(plano.dias) != 7:
        raise ValueError(f"Esperava 7 dias no plano, veio {len(plano.dias)}.")
    return plano


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


def escrever_plano(spreadsheet, plano: PlanoSemanal, dias: list[date]) -> int:
    """Append-only: cada semana é um registro histórico, não um estado
    atual pra espelhar -- diferente de planilha_desempenho.py (dashboard),
    aqui NÃO se usa sobrescrever(). Idempotente: se a segunda-feira dessa
    semana já está na coluna `data`, não insere de novo."""
    aba = obter_aba(spreadsheet, ABA_PLANO, CABECALHO_PLANO, cor=COR_PLANO)
    idx_data = CABECALHO_PLANO.index("data") + 1  # col_values é 1-based
    datas_existentes = set(aba.col_values(idx_data)[1:])
    if str(dias[0]) in datas_existentes:
        return 0
    linhas = _linhas_plano(plano, dias)
    inserir_linhas(aba, [["" if v is None else v for v in linha] for linha in linhas])
    return len(linhas)


def _numero_planilha(v, casas: int) -> str:
    """Mesma convenção de vírgula decimal de planilha_desempenho._numero --
    sem isso, o Sheets confunde "4.57" com separador de milhar (locale
    pt-BR da planilha) e vira "4.570.000..." (ver histórico do projeto)."""
    if v is None:
        return ""
    return f"{float(v):.{casas}f}".rstrip("0").rstrip(".").replace(".", ",")


def escrever_dias_planejados(spreadsheet, plano: PlanoSemanal, dias: list[date]) -> int:
    """Fase 9 -- grava um placeholder (status PLANEJADO) por dia de treino
    (não descanso) na aba "Atividades", pro job diário resolver depois
    contra a atividade real (ver planilha_desempenho.resolver_dias_planejados).
    Upsert por `data`, não por `activity_id` (dia planejado não tem
    atividade associada ainda) -- idempotente: se qualquer dia dessa semana
    já tem linha de plano em "Atividades", não insere de novo. Checa
    qualquer dia da semana, não só a segunda-feira (diferente de
    escrever_plano) -- segunda pode ser dia de descanso, que nunca ganha
    linha própria, então checar só ela nunca encontraria a semana já
    escrita e duplicaria a cada execução."""
    aba = obter_aba(spreadsheet, ABA_ATIVIDADES, CABECALHO_ATIVIDADES, cor=COR_ATIVIDADES)
    idx_data = CABECALHO_ATIVIDADES.index("data")
    idx_status = CABECALHO_ATIVIDADES.index("status")
    datas_semana = {str(d) for d in dias}
    ja_planejada = any(
        len(linha) > idx_status and linha[idx_data] in datas_semana and linha[idx_status]
        for linha in aba.get_all_values()[1:]
    )
    if ja_planejada:
        return 0

    em_branco_real = [""] * len(COLUNAS_REAIS)
    idx_data_real = COLUNAS_REAIS.index("data")
    linhas = []
    # mais recente primeiro (mesma convenção do resto da aba, ver
    # escrever_atividades) -- dias já vem em ordem Segunda->Domingo, então
    # percorre de trás pra frente antes de inserir no topo.
    for treino, data in reversed(list(zip(plano.dias, dias))):
        if treino.descanso:
            continue
        linha_real = list(em_branco_real)
        linha_real[idx_data_real] = str(data)
        linha_plano = [
            STATUS_PLANEJADO,
            treino.tipo_treino or "",
            _numero_planilha(treino.distancia_km, casas=2),
            _numero_planilha(treino.duracao_min, casas=1),
            treino.pace_alvo or "",
            treino.fc_alvo or "",
        ]
        linhas.append([str(v) for v in linha_real + linha_plano])
    inserir_linhas_no_topo(aba, linhas)
    return len(linhas)


def _texto_plano(plano: PlanoSemanal, dias: list[date]) -> str:
    """Plano completo em texto -- dia a dia + estimativas futuras + lógica
    geral. Compartilhado entre o print do terminal e o corpo do e-mail
    (enviar_email_resumo), pra não duplicar a formatação em dois lugares."""
    linhas = []
    for nome_dia, treino, data in zip(DIAS_SEMANA_PT, plano.dias, dias):
        if treino.descanso:
            linhas.append(f"{nome_dia} ({data}): Descanso -- {treino.motivo}")
            continue
        alvo = f"{treino.distancia_km}km" if treino.distancia_km else f"{treino.duracao_min}min"
        linhas.append(
            f"{nome_dia} ({data}): {treino.tipo_treino} -- {alvo}, "
            f"pace {treino.pace_alvo}, FC {treino.fc_alvo} -- {treino.motivo}"
        )
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


def _imprimir_plano(plano: PlanoSemanal, dias: list[date]) -> None:
    print(_texto_plano(plano, dias))


def enviar_email_resumo(plano: PlanoSemanal, dias: list[date]) -> None:
    """Plano completo (dia a dia + estimativas + lógica geral) por e-mail --
    esse conteúdo "não se encaixa" na aba "Plano da Semana" (ver
    _linhas_plano, que só guarda 1 linha por dia como histórico), então vai
    só pro e-mail e pro print do terminal."""
    assunto = f"Plano de treino -- semana de {dias[0].strftime('%d/%m')} a {dias[-1].strftime('%d/%m')}"
    enviar_email(assunto, _texto_plano(plano, dias))


if __name__ == "__main__":
    con = conectar()

    # data em BRT, não CURRENT_DATE do Postgres (servidor roda em UTC) --
    # mesmo cuidado de _semana_alvo, ver hoje_brt().
    corte_janela = hoje_brt() - timedelta(days=JANELA_SEMANAS * 7)
    colunas_ativ, linhas_ativ = _consultar(
        con,
        "SELECT * FROM vw_sessoes_ia WHERE data >= %s ORDER BY data",
        (corte_janela,),
    )
    colunas_resumo, linhas_resumo_desc = _consultar(
        con, "SELECT * FROM vw_resumo_semanal ORDER BY semana_inicio DESC LIMIT %s", (JANELA_SEMANAS,)
    )
    linhas_resumo = list(reversed(linhas_resumo_desc))  # ordem cronológica pro prompt

    acwr_atual = _acwr_semana_atual(colunas_resumo, linhas_resumo)
    dor_recente = _dor_mais_recente(colunas_ativ, linhas_ativ)
    instrucoes = [
        i for i in (_instrucao_guardrail_acwr(acwr_atual), _instrucao_guardrail_dor(dor_recente)) if i
    ]
    if instrucoes:
        print(f"[guardrail(s) ativado(s) -- ACWR atual: {acwr_atual}, dor recente: {dor_recente}]\n")

    spreadsheet = conectar_planilha()
    observacoes = _observacoes_recentes(spreadsheet)

    dias = _semana_alvo()
    prompt = _construir_prompt(
        _formatar_tabela(colunas_ativ, linhas_ativ),
        _formatar_tabela(colunas_resumo, linhas_resumo),
        _faixas_por_tipo(colunas_ativ, linhas_ativ),
        observacoes,
        instrucoes,
        dias,
    )
    plano = None
    for tentativa in range(1, MAX_TENTATIVAS_REVISAO + 1):
        candidato = _plano_semanal(gerar_analise(prompt))
        revisao = revisar_coerencia(prompt, _texto_plano(candidato, dias))
        if revisao.coerente:
            plano = candidato
            if tentativa > 1:
                print(f"[plano aprovado pela revisão de coerência na tentativa {tentativa}/{MAX_TENTATIVAS_REVISAO}]")
            break
        print(f"[revisão reprovou a tentativa {tentativa}/{MAX_TENTATIVAS_REVISAO}: {revisao.motivo}]")
        for problema in revisao.problemas:
            print(f"  - {problema}")

    if plano is None:
        raise RuntimeError(
            f"Plano reprovado pela revisão de coerência em {MAX_TENTATIVAS_REVISAO} "
            "tentativas seguidas -- abortando sem escrever na planilha nem enviar e-mail."
        )

    _imprimir_plano(plano, dias)

    n = escrever_plano(spreadsheet, plano, dias)
    if n:
        print(f"\n[{n} linha(s) escrita(s) na aba '{ABA_PLANO}']")
    else:
        print(f"\n[plano da semana de {dias[0]} já estava na aba '{ABA_PLANO}', não duplicado]")

    n_plan = escrever_dias_planejados(spreadsheet, plano, dias)
    if n_plan:
        print(f"[{n_plan} dia(s) planejado(s) escrito(s) na aba '{ABA_ATIVIDADES}', status {STATUS_PLANEJADO}]")
    else:
        print(f"[dias planejados da semana de {dias[0]} já estavam na aba '{ABA_ATIVIDADES}', não duplicados]")

    enviar_email_resumo(plano, dias)
    print("[e-mail com o plano da semana enviado]")

    # Fase 12 -- só depois do plano aprovado (nunca por candidato) e da
    # entrega central (planilha + e-mail). Best-effort: a integração com o
    # Garmin é mais frágil que o resto, então nada aqui aborta a execução.
    try:
        enviar_plano_garmin(conectar_garmin(), list(zip(dias, plano.dias)), hoje_brt())
    except Exception as erro:  # noqa: BLE001 -- best-effort de propósito
        print(f"[Garmin: não foi possível enviar os treinos ao relógio: {type(erro).__name__}: {erro}]")

    con.close()
