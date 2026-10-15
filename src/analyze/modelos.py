"""Formato dos dados da análise: as sessões reais lidas do banco
(`Sessao`) e o plano semanal e a revisão -- o schema que o Gemini devolve
(`response_schema`) e que o resto do pipeline consome."""

from datetime import date
from typing import NamedTuple

from pydantic import BaseModel


class Sessao(NamedTuple):
    """Uma atividade real (vw_sessoes), com o que as regras de carga usam."""
    data: date
    tipo_treino: str | None
    trote_seg: float | None  # tempo correndo (RWD_RUN); sem splits, a duração inteira
    km_corridos: float | None
    dor: int | None  # dor na tíbia durante/depois (0-5)
    dor_manha: int | None  # dor na tíbia na manhã seguinte (0-5)
    palpacao: bool | None  # dor ao apertar a borda interna da tíbia

# A semana do plano vai de domingo a sábado (o cron roda domingo às 6h, ver
# analise_semanal.yml) -- não é a semana ISO.
DIAS_SEMANA_PT = ["Domingo", "Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado"]


class TreinoDia(BaseModel):
    dia_semana: str  # "Domingo".."Sábado", só pra o modelo se situar -- a data real é atribuída em Python (semana_alvo), nunca confiada ao LLM
    descanso: bool
    tipo_treino: str | None = None  # da lista TIPOS_DE_TREINO, ou None se descanso
    distancia_km: float | None = None
    duracao_min: float | None = None
    pace_alvo: str | None = None  # "10:52-12:29 min/km"
    fc_alvo: str | None = None  # "123-133 bpm"
    forca: bool = False  # sessão de força logo depois da corrida -- conteúdo calculado em Python (analyze/forca.py)
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


def plano_semanal(texto_json: str) -> PlanoSemanal:
    """Parse + validação do JSON devolvido por gerar_analise -- separado da
    chamada em si, pra ficar provider-agnostic (qualquer LLM que devolva
    esse formato funciona aqui, não só Gemini)."""
    plano = PlanoSemanal.model_validate_json(texto_json)
    if len(plano.dias) != 7:
        raise ValueError(f"Esperava 7 dias no plano, veio {len(plano.dias)}.")
    return plano
