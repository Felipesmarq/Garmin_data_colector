"""Fase 12 -- envia o plano da semana como treino estruturado pro Garmin Connect.

Cada dia de treino (dia de descanso não gera nada) vira um `RunningWorkout`
agendado na data do dia, que o relógio baixa na próxima sincronização
(Forerunner 165 não-Music: só Bluetooth/USB, sem Wi-Fi -- não é instantâneo).

Best-effort (`enviar_plano`): qualquer falha é registrada e ignorada, nunca
levantada -- a planilha e o e-mail da semana não ficam reféns de uma
integração sobre uma API não-oficial (ver CASE_DO_PROJETO_1.md, Fase 12).

Formato do valor de alvo (confirmado contra a API real em 2026-09-21,
decodificando o .fit que a própria Garmin gera do treino):
  - FC: `targetValueOne/Two` = bpm absolutos (mín, máx).
  - Pace: `targetValueOne/Two` = velocidade em m/s, `One` o limite LENTO
    (pace máximo em min/km) e `Two` o RÁPIDO (pace mínimo). Não é pace em
    min/km nem em segundos: 6:00 min/km -> 2.778 m/s.
"""

import re
from datetime import date

from garminconnect import Garmin
from garminconnect.workout import (
    RunningWorkout,
    TargetType,
    WorkoutSegment,
    create_cooldown_step,
    create_distance_interval_step,
    create_interval_step,
    create_warmup_step,
)

from src.load.planilha_desempenho import _parse_faixa_fc, _parse_faixa_pace

# Baixa intensidade -> alvo de FC (o que importa é não passar do esforço
# seguro); o resto -> alvo de pace (acertar o ritmo de prova). O Garmin só
# aceita um tipo de alvo por passo. Comparação por prefixo do nome em
# minúsculas, porque o LLM devolve o tipo como texto ("Rodagem/Recuperação").
TIPOS_ALVO_FC = ("longão", "fartlek")

# Rodagem/Recuperação: só a distância é a meta, sem alvo de FC nem de pace.
# Com alvo de FC, o relógio empurrava o corredor a acelerar quando a FC ficava
# abaixo da faixa, o contrário do objetivo de um treino de recuperação
# (observado no uso real, 2026-10-06).
TIPOS_SEM_ALVO = ("rodagem", "recuperação")

# Aquecimento/desaquecimento sem alvo só nos treinos de intensidade (alvo de
# pace) -- nos de baixa intensidade o treino inteiro já é leve.
AQUECIMENTO_SEG = 10 * 60
DESAQUECIMENTO_SEG = 5 * 60

# Só usado pra estimar a duração de um treino por distância sem pace_alvo.
PACE_FALLBACK_MIN_KM = 6.0

_SUFIXO_NOME = re.compile(r" -- (\d{2}/\d{2})$")


def _usa_alvo_fc(tipo: str | None) -> bool:
    return (tipo or "").strip().lower().startswith(TIPOS_ALVO_FC)


def _sem_alvo(tipo: str | None) -> bool:
    return (tipo or "").strip().lower().startswith(TIPOS_SEM_ALVO)


def _nome_treino(tipo: str, data: date) -> str:
    return f"{tipo} -- {data.strftime('%d/%m')}"


def _alvo_fc(fc_alvo: str | None) -> tuple[dict, dict] | None:
    faixa = _parse_faixa_fc(fc_alvo or "")
    if faixa is None:
        return None
    tipo = {"workoutTargetTypeId": TargetType.HEART_RATE_ZONE, "workoutTargetTypeKey": "heart.rate.zone", "displayOrder": 4}
    return tipo, {"targetValueOne": faixa[0], "targetValueTwo": faixa[1]}


def _alvo_pace(pace_alvo: str | None) -> tuple[dict, dict] | None:
    faixa = _parse_faixa_pace(pace_alvo or "")
    if faixa is None:
        return None
    rapido, lento = faixa  # min/km decimais: menor = mais rápido
    tipo = {"workoutTargetTypeId": TargetType.PACE_ZONE, "workoutTargetTypeKey": "pace.zone", "displayOrder": 6}
    return tipo, {
        "targetValueOne": round(1000 / (lento * 60), 4),
        "targetValueTwo": round(1000 / (rapido * 60), 4),
    }


def _duracao_estimada_seg(dia, extra_seg: int) -> int:
    if dia.duracao_min:
        return int(dia.duracao_min * 60) + extra_seg
    faixa = _parse_faixa_pace(dia.pace_alvo or "")
    pace = sum(faixa) / 2 if faixa else PACE_FALLBACK_MIN_KM
    return int(dia.distancia_km * pace * 60) + extra_seg


def construir_treino(dia, data: date) -> RunningWorkout | None:
    """`dia` é um TreinoDia (duck typing -- não importa analisar_com_ia pra
    não criar dependência circular). None se não for treino enviável (descanso,
    ou sem distância nem duração pra definir o passo principal)."""
    if dia.descanso or not dia.tipo_treino:
        return None
    if not dia.distancia_km and not dia.duracao_min:
        return None

    sem_alvo = _sem_alvo(dia.tipo_treino)
    por_fc = _usa_alvo_fc(dia.tipo_treino)
    if sem_alvo:
        alvo = None
    else:
        alvo = _alvo_fc(dia.fc_alvo) if por_fc else _alvo_pace(dia.pace_alvo)
    tipo_alvo, valores = alvo if alvo else (None, {})
    leve = sem_alvo or por_fc  # treino de baixa intensidade: sem aquecimento/desaquecimento

    passos = []
    ordem = 1
    if not leve:
        passos.append(create_warmup_step(AQUECIMENTO_SEG, ordem))
        ordem += 1

    if dia.distancia_km:
        principal = create_distance_interval_step(dia.distancia_km * 1000, ordem, tipo_alvo)
    else:
        principal = create_interval_step(dia.duracao_min * 60, ordem, tipo_alvo)
    for campo, valor in valores.items():
        setattr(principal, campo, valor)
    passos.append(principal)
    ordem += 1

    if not leve:
        passos.append(create_cooldown_step(DESAQUECIMENTO_SEG, ordem))

    extra = 0 if leve else AQUECIMENTO_SEG + DESAQUECIMENTO_SEG
    return RunningWorkout(
        workoutName=_nome_treino(dia.tipo_treino, data),
        description=dia.motivo,
        estimatedDurationInSecs=_duracao_estimada_seg(dia, extra),
        workoutSegments=[
            WorkoutSegment(
                segmentOrder=1,
                sportType={"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1},
                workoutSteps=passos,
            )
        ],
    )


def _achar_workout_agendado(api: Garmin, data: date) -> dict | None:
    """Item do calendário de um treino NOSSO já agendado nessa data (tem
    `id` do agendamento e `workoutId` do treino), ou None. "Nosso" = nome no
    formato `<tipo> -- dd/mm` com o dd/mm da própria data: acha o treino da
    data mesmo se o tipo mudou entre execuções (plano regerado), sem
    depender do nome exato, e ignora treinos manuais do usuário."""
    calendario = api.get_scheduled_workouts(data.year, data.month)
    for item in calendario.get("calendarItems", []):
        if item.get("itemType") != "workout" or item.get("date") != data.isoformat():
            continue
        casou = _SUFIXO_NOME.search(item.get("title") or "")
        if casou and casou.group(1) == data.strftime("%d/%m"):
            return item
    return None


def enviar_treino(api: Garmin, dia, data: date) -> str | None:
    """Cria (ou atualiza, se a data já tem treino nosso) e agenda o treino do
    dia. Se o dia não gera mais treino (virou descanso num plano regerado) e
    havia um nosso agendado, remove -- senão o treino velho ficaria no relógio.
    Devolve "criado", "atualizado", "removido", ou None se não havia nada a
    fazer."""
    treino = construir_treino(dia, data)
    existente = _achar_workout_agendado(api, data)

    if treino is None:
        if existente:
            api.unschedule_workout(existente["id"])
            api.delete_workout(existente["workoutId"])
            return "removido"
        return None

    if existente:
        api.update_workout(existente["workoutId"], treino.to_dict())
        return "atualizado"

    criado = api.upload_running_workout(treino)
    api.schedule_workout(criado["workoutId"], data.isoformat())
    return "criado"


def enviar_plano(api: Garmin, dias_do_plano: list[tuple[date, object]], hoje: date) -> None:
    """Best-effort: uma falha num dia é registrada e o resto segue; nada é
    levantado pra fora. Só envia datas de hoje em diante -- agendar treino no
    passado não faz sentido (relógio nunca vai executá-lo)."""
    for data, dia in dias_do_plano:
        if data < hoje:
            continue
        try:
            resultado = enviar_treino(api, dia, data)
        except Exception as erro:  # noqa: BLE001 -- best-effort de propósito
            print(f"[Garmin: falha ao enviar o treino de {data}: {type(erro).__name__}: {erro}]")
            continue
        if resultado:
            print(f"[Garmin: treino de {data} {resultado} ({dia.tipo_treino or 'Descanso'})]")
        elif not dia.descanso:
            print(f"[Garmin: treino de {data} ({dia.tipo_treino}) sem distância nem duração, não enviado]")
