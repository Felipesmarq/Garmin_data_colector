"""Chamadas ao Gemini -- a única parte que fala com o provedor de LLM:
trocar de provedor é reescrever este módulo (ver CASE_DO_PROJETO_1.md
seção 4)."""

import os

from google import genai
from google.genai import errors, types
from pydantic import BaseModel

from src.analyze.modelos import PlanoSemanal, RevisaoCoerencia
from src.analyze.prompt import construir_prompt_revisao
from src.config import carregar_env

MODELO_PADRAO = "gemini-3.8-flash"


# Sobrecarga do Gemini (503 "high demand") é por modelo e dura mais que as
# 5 tentativas que o SDK já faz sozinho -- então, esgotadas elas, tenta o
# próximo modelo da lista em vez de desistir (ver _gerar_json e
# CASE_DO_PROJETO_1.md, 2026-10). 429 entra junto: a cota também é por modelo.
MODELOS_RESERVA = ("gemini-3.7-flash", "gemini-3.6-flash")


CODIGOS_SOBRECARGA = (429, 500, 502, 503, 504)


# Fase 10 -- teto de tokens do prompt, bem abaixo do limite real do Gemini
# (até 1M) -- ver gerar_analise().
TETO_TOKENS = 200_000


def _modelos() -> list[str]:
    """Modelo principal (GEMINI_MODEL, ou MODELO_PADRAO) e depois os de
    reserva, sem repetir."""
    carregar_env()
    principal = os.environ.get("GEMINI_MODEL") or MODELO_PADRAO
    return [principal] + [m for m in MODELOS_RESERVA if m != principal]


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


def _gerar_json(conteudo: str, schema: type[BaseModel], o_que: str) -> str:
    """Chamada ao Gemini com saída estruturada (`response_schema`), passando
    pro próximo modelo de `_modelos()` quando o atual está sobrecarregado
    (CODIGOS_SOBRECARGA, depois das tentativas do próprio SDK). Bloqueio ou
    resposta incompleta não troca de modelo: é problema do prompt, não de
    capacidade."""
    cliente = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    ultimo_erro: errors.APIError | None = None
    for modelo in _modelos():
        try:
            _verificar_teto_tokens(cliente, modelo, conteudo)
            resposta = cliente.models.generate_content(
                model=modelo,
                contents=conteudo,
                config=types.GenerateContentConfig(
                    # não usamos tools/function calling -- desliga o AFC padrão pra
                    # não gerar o aviso "Direct use of AFC..." a cada chamada, sem
                    # efeito nenhum no resultado.
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                    response_mime_type="application/json",
                    response_schema=schema,
                ),
            )
        except errors.APIError as erro:
            if erro.code not in CODIGOS_SOBRECARGA:
                raise
            print(f"[Gemini: {modelo} indisponível ({erro.code}) para {o_que}, tentando o próximo modelo]")
            ultimo_erro = erro
            continue
        if not resposta.candidates:
            motivo = resposta.prompt_feedback.block_reason if resposta.prompt_feedback else "desconhecido"
            raise RuntimeError(f"Gemini bloqueou o prompt de {o_que} (motivo: {motivo}).")
        finalizacao = resposta.candidates[0].finish_reason
        if finalizacao not in (types.FinishReason.STOP, None) or not resposta.text:
            raise RuntimeError(f"Gemini não completou {o_que} (finish_reason={finalizacao}).")
        print(f"[{o_que}: {modelo}]")
        return resposta.text
    raise ultimo_erro


def gerar_analise(prompt: str) -> str:
    """Chamada ao LLM isolada nesta função -- ver docstring do módulo.

    Devolve texto (contrato genérico, ver seção 4 do case) -- só que aqui
    esse texto é um JSON validado contra PlanoSemanal (response_schema),
    não texto livre. `response_schema` e a troca de modelo por sobrecarga
    são específicos do Gemini; ficam contidos em `_gerar_json`, então trocar
    de provedor continua sendo só reescrever essas duas funções.
    """
    return _gerar_json(prompt, PlanoSemanal, "geração do plano")


def revisar_coerencia(prompt_original: str, plano_texto: str) -> RevisaoCoerencia:
    """Fase 11 -- segunda chamada ao Gemini, independente da que gerou o
    plano (não autoavaliação na mesma resposta, ver CASE_DO_PROJETO_1.md
    seção 11) -- avalia coerência interna, contradição com os dados de
    entrada e violação de guardrail que tenha escapado da restrição de
    vocabulário. Recebe o prompt original (que já contém os dados e as
    instruções de guardrail que dispararam, se algum) + o plano gerado."""
    prompt_revisao = construir_prompt_revisao(prompt_original, plano_texto)
    return RevisaoCoerencia.model_validate_json(_gerar_json(prompt_revisao, RevisaoCoerencia, "revisão de coerência"))
