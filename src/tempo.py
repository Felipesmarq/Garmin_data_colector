"""Data "de hoje" ancorada em horário de Brasília, não no fuso do host.

`date.today()` reflete o fuso do sistema onde o código roda -- localmente
é o fuso do computador (BRT), mas no GitHub Actions é UTC. Um atraso de
agendamento de pouco mais de 1h no cron de domingo (23h UTC = 20h BRT) já
é suficiente pra `date.today()` virar segunda-feira em UTC enquanto ainda
é domingo à noite no Brasil -- e isso já aconteceu em produção, fazendo o
plano semanal pular pra semana errada (ver CASE_DO_PROJETO_1.md seção
11). O sync diário roda às 21h BRT = 00h UTC, ainda mais perto da virada.

Qualquer lugar do pipeline que precise saber "que dia é hoje" (calcular a
próxima semana, cortar uma janela de N dias, decidir "ontem") deve usar
`hoje_brt()`, não `date.today()`.
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

FUSO_BRT = ZoneInfo("America/Sao_Paulo")


def hoje_brt() -> date:
    return datetime.now(FUSO_BRT).date()
