"""Datas da semana a planejar (domingo a sábado) -- aritmética de data
fica em Python, nunca no prompt."""

import os
from datetime import date, timedelta

from src.tempo import hoje_brt

def semana_alvo() -> list[date]:
    """Domingo a sábado da semana a planejar -- calculado em Python, não
    pedido ao LLM (aritmética de data não é trabalho pra prompt).

    Por padrão é a semana que começa no próximo domingo -- o próprio dia,
    quando roda no domingo (o cron roda domingo às 6h, planejando a semana
    que começa ali). A variável de ambiente SEMANA_PLANO=atual muda pra
    semana já começada (domingo mais recente): serve pra reexecução manual
    quando o cron falhou (ver workflow_dispatch em analise_semanal.yml); os
    dias que já passaram viram descanso (ver dias_passados).

    `hoje_brt()`, não `date.today()`: o servidor do GitHub Actions roda em
    UTC, e um atraso de agendamento no cron (comum no GitHub Actions) já
    pulou a semana inteira uma vez (bug real, ver CASE_DO_PROJETO_1.md
    seção 11)."""
    hoje = hoje_brt()
    if os.environ.get("SEMANA_PLANO") == "atual":
        domingo = hoje - timedelta(days=(hoje.weekday() + 1) % 7)
    else:
        domingo = hoje + timedelta(days=(6 - hoje.weekday()) % 7)
    return [domingo + timedelta(days=i) for i in range(7)]


def dias_passados(dias: list[date]) -> list[date]:
    """Dias da semana a planejar anteriores a hoje (só acontece com
    SEMANA_PLANO=atual) -- têm que ser descanso no plano."""
    hoje = hoje_brt()
    return [d for d in dias if d < hoje]


def max_corridas(livres: list[date]) -> int:
    """Quantas corridas cabem nos dias livres (em ordem) sem duas em dias
    seguidos -- a regra de alternância. 7 dias livres -> 4."""
    total, ultimo = 0, None
    for d in livres:
        if ultimo is None or (d - ultimo).days > 1:
            total, ultimo = total + 1, d
    return total
