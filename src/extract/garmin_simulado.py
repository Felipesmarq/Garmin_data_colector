"""Branch SR1 -- Garmin simulada: emula o cliente `garminconnect.Garmin` pra
quem não tem relógio Garmin poder testar o projeto inteiro.

Devolve o mesmo JSON bruto que a API real devolve (mesmos nomes de campo e
mesmo formato -- ver `_mapear_atividade`/`recuperacao_diaria` em garmin.py),
então o resto do pipeline roda sem mudança: mapeamento, carga no Postgres,
views, classificação de tipo de treino, planilha, Gemini e aderência.

Perfil emulado: o mesmo do autor do projeto (iniciante em run-walk,
retomando de canelite, ~3 corridas por semana) -- é o perfil que o prompt do
Gemini descreve. Faixas calibradas em médias do histórico real (2026-10-07):
~3 km em ~30 min, ~78% do tempo caminhando, FC média ~138, sono ~6h20, FC de
repouso ~57.

Determinístico: cada dia usa um gerador aleatório com semente derivada da
data, então o mesmo dia gera sempre a mesma atividade, com o mesmo
activity_id. A carga continua idempotente, e cada sync "descobre" as
corridas novas como com um relógio de verdade.

Segue o plano: num dia que tem plano na aba "Plano da Semana", o corredor
emulado faz o treino planejado com variação -- na maioria das vezes dentro
da margem da verificação de aderência (Fase 9), às vezes fora, às vezes
pula o dia. Sem plano pro dia (histórico anterior ao primeiro plano), segue
uma agenda base de 3 corridas por semana.
"""

import random
import uuid
from datetime import date, datetime, timedelta

from src.load.planilha_desempenho import _parse_faixa_fc, _parse_faixa_pace
from src.tempo import FUSO_BRT, hoje_brt

SEMENTE = "SR1"

# Quantos dias de histórico a Garmin simulada "tem". Os loaders da SR1 pedem
# essa janela inteira a cada sync (é tudo local, não custa nada), então um
# banco vazio já começa com histórico suficiente pro ACWR (4 semanas de
# carga crônica) e pra janela de 4 semanas do prompt.
HISTORICO_DIAS = 70

# Agenda base, usada nos dias sem plano: segunda, quinta e sábado.
AGENDA_BASE = {0: "Rodagem/Recuperação", 3: "Rodagem/Recuperação", 5: "Longão"}

# Desfecho de um dia planejado (cumulativo): pula, faz fora da margem, ou
# faz dentro da margem.
PROB_PULAR = 0.15
PROB_FORA_DA_MARGEM = 0.25
PROB_ESTEIRA = 0.2
PROB_CAMINHADA_AVULSA = 0.06

# Fração do tempo em cada zona de FC (1 a 5) por tipo de treino. Escolhidas
# pra cair exatamente no tipo certo na classificação de vw_sessoes
# (schema.sql): a verificação de aderência só aprova se o tipo real for igual
# ao planejado. Sem variação aleatória aqui de propósito -- uma variação
# poderia cruzar um limiar e trocar o tipo.
ZONAS_POR_TIPO = {
    "Rodagem/Recuperação": (0.40, 0.30, 0.22, 0.08, 0.00),  # baixa 0.70 < 0.85
    "Longão": (0.55, 0.35, 0.08, 0.02, 0.00),  # baixa 0.90 e >= 35 min
    "Ritmo": (0.20, 0.25, 0.30, 0.22, 0.03),  # alta 0.25
    "Limiar": (0.10, 0.15, 0.25, 0.42, 0.08),  # alta 0.50
    "Intervalado": (0.15, 0.15, 0.20, 0.35, 0.15),  # alta 0.50 + anaeróbico >= 1.5
    "Tiro": (0.15, 0.15, 0.20, 0.35, 0.15),
    # vw_sessoes nunca classifica como Fartlek -- um Fartlek planejado sempre
    # sai "fora da margem", com relógio simulado ou de verdade.
    "Fartlek": (0.20, 0.25, 0.30, 0.22, 0.03),
    "Caminhada": (0.60, 0.40, 0.00, 0.00, 0.00),
}

FC_POR_TIPO = {
    "Rodagem/Recuperação": (128, 140),
    "Longão": (125, 135),
    "Ritmo": (148, 158),
    "Limiar": (158, 168),
    "Intervalado": (160, 172),
    "Tiro": (160, 172),
    "Fartlek": (145, 160),
    "Caminhada": (112, 122),
}

# Pace (min/km) da atividade inteira, contando a caminhada do run-walk.
PACE_POR_TIPO = {
    "Rodagem/Recuperação": (9.5, 11.5),
    "Longão": (10.5, 11.5),
    "Ritmo": (8.5, 9.5),
    "Limiar": (8.0, 9.0),
    "Intervalado": (8.0, 9.0),
    "Tiro": (7.5, 8.5),
    "Fartlek": (8.5, 10.0),
    "Caminhada": (11.0, 12.5),
}

EFEITO_AEROBICO_POR_TIPO = {
    "Rodagem/Recuperação": (1.6, 2.4, "RECOVERY"),
    "Longão": (2.4, 3.0, "AEROBIC_BASE"),
    "Ritmo": (2.6, 3.2, "TEMPO"),
    "Limiar": (3.0, 3.6, "LACTATE_THRESHOLD"),
    "Intervalado": (3.0, 3.5, "VO2MAX"),
    "Tiro": (2.8, 3.3, "ANAEROBIC_CAPACITY"),
    "Fartlek": (2.6, 3.2, "TEMPO"),
    # UNKNOWN é o rótulo que a Garmin dá a atividade fraca demais pra ter
    # efeito de treino; vw_sessoes usa ele pra marcar "Caminhada".
    "Caminhada": (0.3, 0.5, "UNKNOWN"),
}

# Velocidade dos trechos de corrida e de caminhada do run-walk. A caminhada
# do histórico real é rápida (~1.47 m/s nos splits RWD_WALK), e é isso que
# deixa a maior parte do tempo caminhando com pace médio de ~10 min/km.
VEL_CORRIDA_MPS = 1.95
VEL_CAMINHADA_MPS = 1.45
DURACAO_MIN_LONGAO = 36  # vw_sessoes só classifica Longão a partir de 35 min

ABA_PLANO = "Plano da Semana"  # mesmo nome de analisar_com_ia.ABA_PLANO


def _rng(dia: date, assunto: str) -> random.Random:
    # semente em texto: estável entre execuções (não depende de PYTHONHASHSEED)
    return random.Random(f"{SEMENTE}-{assunto}-{dia.isoformat()}")


def _numero(texto: str) -> float | None:
    texto = (texto or "").strip().replace(",", ".")
    try:
        return float(texto) if texto else None
    except ValueError:
        return None


def _ler_plano() -> dict[date, dict]:
    """Aba "Plano da Semana" como {data: dia do plano}. Sem planilha
    configurada ou sem a aba ainda (antes do primeiro plano), devolve vazio
    e o corredor emulado segue a agenda base."""
    try:
        from src.planilha import conectar as conectar_planilha

        linhas = conectar_planilha().worksheet(ABA_PLANO).get_all_values()
    except Exception:  # noqa: BLE001 -- sem plano é um estado válido, não erro
        return {}
    if not linhas:
        return {}
    cabecalho = linhas[0]
    plano = {}
    for linha in linhas[1:]:
        registro = dict(zip(cabecalho, linha))
        try:
            dia = date.fromisoformat(registro.get("data", ""))
        except ValueError:
            continue
        plano[dia] = {
            "tipo": registro.get("tipo_treino") or "",
            "distancia_km": _numero(registro.get("distancia_km", "")),
            "duracao_min": _numero(registro.get("duracao_min", "")),
            "pace_alvo": registro.get("pace_alvo", ""),
            "fc_alvo": registro.get("fc_alvo", ""),
        }
    return plano


def _sessao_do_dia(dia: date, plano: dict[date, dict]) -> dict | None:
    """O que o corredor emulado fez no dia: {tipo, distancia_km, pace_min_km,
    fc_media}, ou None se não correu."""
    rng = _rng(dia, "sessao")
    planejado = plano.get(dia)

    if planejado is not None:
        tipo = planejado["tipo"]
        if not tipo or tipo == "Descanso":
            return None
        sorteio = rng.random()
        if sorteio < PROB_PULAR:
            return None
        fora = sorteio < PROB_PULAR + PROB_FORA_DA_MARGEM

        faixa_pace = _parse_faixa_pace(planejado["pace_alvo"]) or PACE_POR_TIPO.get(tipo, (9.5, 11.5))
        pace = rng.uniform(*faixa_pace)
        faixa_fc = _parse_faixa_fc(planejado["fc_alvo"]) or FC_POR_TIPO.get(tipo, (128, 140))
        fc = rng.randint(*faixa_fc)

        # Fora da margem = volume fora dos ±20% da Fase 9; dentro = ±8%.
        fator = rng.choice([rng.uniform(0.55, 0.70), rng.uniform(1.30, 1.45)]) if fora else rng.uniform(0.92, 1.08)
        if planejado["distancia_km"]:
            distancia = planejado["distancia_km"] * fator
        elif planejado["duracao_min"]:
            distancia = planejado["duracao_min"] * fator / pace
        else:
            distancia = rng.uniform(2.2, 3.2)
        return {"tipo": tipo, "distancia_km": distancia, "pace_min_km": pace, "fc_media": fc}

    tipo = AGENDA_BASE.get(dia.weekday())
    if tipo is None:
        return None
    pace = rng.uniform(*PACE_POR_TIPO[tipo])
    if tipo == "Longão":
        distancia = rng.uniform(DURACAO_MIN_LONGAO + 2, DURACAO_MIN_LONGAO + 10) / pace
    else:
        distancia = rng.uniform(2.2, 3.2)
    return {"tipo": tipo, "distancia_km": distancia, "pace_min_km": pace, "fc_media": rng.randint(*FC_POR_TIPO[tipo])}


def _atividade_bruta(dia: date, indice: int, sessao: dict, rng: random.Random) -> dict:
    """Uma atividade no formato bruto de `get_activities` da API real."""
    tipo = sessao["tipo"]
    caminhada = tipo == "Caminhada"
    esteira = not caminhada and rng.random() < PROB_ESTEIRA

    distancia_m = sessao["distancia_km"] * 1000
    duracao = distancia_m / (1000 / (sessao["pace_min_km"] * 60))
    parado = 0.0 if caminhada else rng.uniform(5, 90)
    em_movimento = duracao - parado
    vel_movimento = distancia_m / em_movimento

    # Run-walk: divide o tempo em movimento entre correr e caminhar de forma
    # que a velocidade média bata com o pace da sessão.
    vel_corrida = max(VEL_CORRIDA_MPS * rng.uniform(0.95, 1.05), vel_movimento * 1.1)
    vel_caminhada = min(VEL_CAMINHADA_MPS, vel_movimento * 0.95)
    if caminhada:
        frac_caminhando = 1.0
    else:
        frac_caminhando = (vel_corrida - vel_movimento) / (vel_corrida - vel_caminhada)
    seg_caminhando = em_movimento * frac_caminhando
    seg_correndo = em_movimento - seg_caminhando
    dist_correndo = seg_correndo * vel_corrida
    dist_caminhando = distancia_m - dist_correndo

    splits = []
    if seg_correndo > 0:
        splits.append(("RWD_RUN", seg_correndo, dist_correndo, vel_corrida))
    splits.append(("RWD_WALK", seg_caminhando, dist_caminhando, dist_caminhando / seg_caminhando if seg_caminhando else 0.0))
    if parado:
        splits.append(("RWD_STAND", parado, 0.0, 0.0))

    zonas = ZONAS_POR_TIPO.get(tipo, ZONAS_POR_TIPO["Rodagem/Recuperação"])
    te_min, te_max, rotulo = EFEITO_AEROBICO_POR_TIPO.get(tipo, EFEITO_AEROBICO_POR_TIPO["Rodagem/Recuperação"])
    anaerobico = round(rng.uniform(2.0, 2.6), 1) if tipo in ("Intervalado", "Tiro") else round(rng.uniform(0.0, 0.1), 1)
    fc_media = sessao["fc_media"]
    cadencia = (seg_correndo * rng.uniform(158, 168) + seg_caminhando * rng.uniform(80, 90)) / em_movimento
    potencia = round(95 + 40 * vel_movimento)
    vel_media = distancia_m / duracao
    duracao_min = duracao / 60

    hora = datetime(dia.year, dia.month, dia.day, rng.randint(15, 19), rng.randint(0, 59), rng.randint(0, 59))
    activity_id = int(f"9{dia:%Y%m%d}{indice}")
    elevacao = rng.uniform(8, 20)
    ganho = 0.0 if esteira else round(rng.uniform(0, 12))

    if caminhada:
        nome = "Caminhada (simulada)"
    elif esteira:
        nome = "Corrida em esteira (simulada)"
    else:
        nome = "Corrida (simulada)"

    return {
        "activityId": activity_id,
        "activityUUID": str(uuid.uuid5(uuid.NAMESPACE_OID, str(activity_id))),
        "activityName": nome,
        "startTimeLocal": hora.strftime("%Y-%m-%d %H:%M:%S"),
        "activityType": {"typeKey": "treadmill_running" if esteira else "running"},
        "distance": round(distancia_m, 2),
        "duration": round(duracao, 2),
        "movingDuration": round(em_movimento, 2),
        "averageSpeed": round(vel_media, 3),
        "maxSpeed": round(vel_corrida * rng.uniform(1.3, 1.7), 3),
        "avgGradeAdjustedSpeed": round(vel_media * rng.uniform(0.99, 1.02), 3),
        "averageHR": fc_media,
        "maxHR": fc_media + rng.randint(15, 30),
        "averageRunningCadenceInStepsPerMinute": round(cadencia, 2),
        "maxRunningCadenceInStepsPerMinute": round(rng.uniform(165, 185)),
        "elevationGain": ganho,
        "elevationLoss": 0.0 if esteira else round(ganho + rng.uniform(-2, 2), 0) if ganho > 2 else ganho,
        "avgElevation": round(elevacao, 2),
        "minElevation": round(elevacao - rng.uniform(1, 4), 1),
        "maxElevation": round(elevacao + rng.uniform(1, 6), 1),
        "avgPower": potencia,
        "maxPower": round(potencia * rng.uniform(1.9, 2.3)),
        "normPower": round(potencia * rng.uniform(1.15, 1.25)),
        "aerobicTrainingEffect": round(rng.uniform(te_min, te_max), 1),
        "anaerobicTrainingEffect": anaerobico,
        "trainingEffectLabel": rotulo,
        **{f"hrTimeInZone_{i + 1}": round(duracao * frac, 2) for i, frac in enumerate(zonas)},
        "avgGroundContactTime": round(rng.uniform(330, 390), 1),
        "avgStrideLength": round(rng.uniform(80, 90), 2),
        "avgVerticalOscillation": round(rng.uniform(6.0, 7.5), 2),
        "avgVerticalRatio": round(rng.uniform(7.2, 8.8), 2),
        "fastestSplit_1000": round(1000 / (vel_movimento * 1.12), 2) if distancia_m >= 1000 and not caminhada else None,
        "fastestSplit_1609": round(1609 / (vel_movimento * 1.10), 2) if distancia_m >= 1609 and not caminhada else None,
        "moderateIntensityMinutes": round(duracao_min * (zonas[1] + zonas[2])),
        "vigorousIntensityMinutes": round(duracao_min * (zonas[3] + zonas[4])),
        "differenceBodyBattery": -max(1, round(duracao_min / 5)),
        "calories": round(duracao_min * rng.uniform(7.0, 8.5)),
        "splitSummaries": [
            {
                "splitType": tipo_split,
                "duration": round(seg, 2),
                "distance": round(dist, 2),
                "averageSpeed": round(vel, 3),
                "maxSpeed": round(vel * 1.4, 3),
                "totalAscent": 0.0,
                "elevationLoss": 0.0,
            }
            for tipo_split, seg, dist, vel in splits
        ],
    }


class GarminSimulado:
    """Mesma interface (dos métodos usados pelo projeto) que
    `garminconnect.Garmin`. Ver docstring do módulo."""

    def __init__(self, plano: dict[date, dict] | None = None):
        # plano injetável pra teste; None = lê da planilha na primeira vez
        self._plano = plano

    @property
    def plano(self) -> dict[date, dict]:
        if self._plano is None:
            self._plano = _ler_plano()
        return self._plano

    def _atividades_do_dia(self, dia: date) -> list[dict]:
        atividades = []
        sessao = _sessao_do_dia(dia, self.plano)
        if sessao:
            atividades.append(_atividade_bruta(dia, 0, sessao, _rng(dia, "atividade")))
        rng = _rng(dia, "caminhada")
        if rng.random() < PROB_CAMINHADA_AVULSA:
            pace = rng.uniform(*PACE_POR_TIPO["Caminhada"])
            caminhada = {"tipo": "Caminhada", "distancia_km": rng.uniform(0.6, 1.0), "pace_min_km": pace, "fc_media": rng.randint(*FC_POR_TIPO["Caminhada"])}
            atividades.append(_atividade_bruta(dia, 1, caminhada, rng))
        return atividades

    def get_activities(self, start: int = 0, limit: int = 20) -> list[dict]:
        """Mais recente primeiro, como a API real. Atividade de hoje só
        aparece depois do horário em que ela "aconteceu"."""
        agora = datetime.now(FUSO_BRT).replace(tzinfo=None)
        hoje = hoje_brt()
        todas = []
        for i in range(HISTORICO_DIAS + 1):
            dia = hoje - timedelta(days=i)
            for atividade in self._atividades_do_dia(dia):
                if datetime.strptime(atividade["startTimeLocal"], "%Y-%m-%d %H:%M:%S") <= agora:
                    todas.append(atividade)
        todas.sort(key=lambda a: a["startTimeLocal"], reverse=True)
        return todas[start:start + limit]

    def _fc_repouso(self, dia: date) -> int:
        return _rng(dia, "fc_repouso").randint(54, 60)

    def get_sleep_data(self, iso: str) -> dict:
        dia = date.fromisoformat(iso)
        rng = _rng(dia, "sono")
        if rng.random() < 0.15:  # relógio fora do pulso à noite: sem dado de sono
            return {"dailySleepDTO": {}}
        total = rng.randint(5 * 3600 + 1800, 7 * 3600 + 2400)
        profundo = round(total * rng.uniform(0.17, 0.25))
        rem = round(total * rng.uniform(0.10, 0.20))
        desperto = rng.randint(0, 900)
        return {
            "dailySleepDTO": {
                "sleepTimeSeconds": total,
                "deepSleepSeconds": profundo,
                "remSleepSeconds": rem,
                "lightSleepSeconds": total - profundo - rem,
                "awakeSleepSeconds": desperto,
                "awakeCount": rng.randint(0, 2),
                "avgHeartRate": rng.randint(58, 66),
                "avgSleepStress": round(rng.uniform(8, 25), 1),
                "sleepScores": {"overall": {"value": rng.randint(60, 90)}},
            }
        }

    def get_heart_rates(self, iso: str) -> dict:
        dia = date.fromisoformat(iso)
        ultimos_7 = [self._fc_repouso(dia - timedelta(days=i)) for i in range(7)]
        return {
            "restingHeartRate": self._fc_repouso(dia),
            "lastSevenDaysAvgRestingHeartRate": round(sum(ultimos_7) / 7),
        }

    def get_user_summary(self, iso: str) -> dict:
        rng = _rng(date.fromisoformat(iso), "resumo")
        return {
            "bodyBatteryAtWakeTime": rng.randint(60, 95),
            "bodyBatteryMostRecentValue": rng.randint(10, 40),
            "bodyBatteryChargedValue": rng.randint(40, 75),
            "bodyBatteryDrainedValue": rng.randint(45, 75),
            "averageStressLevel": rng.randint(22, 40),
            "totalSteps": rng.randint(3500, 9000),
        }

    def get_body_battery(self, inicio: str, fim: str | None = None) -> list[dict]:
        dia, ultimo = date.fromisoformat(inicio), date.fromisoformat(fim or inicio)
        dias = []
        while dia <= ultimo:
            rng = _rng(dia, "body_battery")
            dias.append({"date": dia.isoformat(), "charged": rng.randint(40, 75), "drained": rng.randint(45, 75)})
            dia += timedelta(days=1)
        return dias
