"""Fase 2 — extração de dados da Garmin Connect API.

Diferente do explore/inspect_garmin.py (Fase 0, que só explora e imprime
o JSON bruto), este módulo já devolve os dados mapeados pras colunas do
`src/load/schema.sql` (Fase 1), prontos pro `load/*.py` (Fase 3) inserir
no Postgres (Neon). Mapeamento de campos documentado em
explore/RELATORIO_FASE0.md e em CASE_DO_PROJETO_1.md (seção 6.1).

Uso típico (de outro script):

    from src.extract.garmin import conectar, atividades_novas, recuperacao_diaria

    api = conectar()
    atividades = atividades_novas(api, dias=3)
    recuperacao = recuperacao_diaria(api, date.today())
"""

import getpass
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

ROOT = Path(__file__).resolve().parent.parent.parent
TOKENSTORE = str(ROOT / ".garmin_tokens")
MAX_LOGIN_ATTEMPTS = 5


# =====================================================================
# Autenticação — mesma lógica da Fase 0 (explore/inspect_garmin.py),
# extraída aqui pra ser reusada pelo pipeline de verdade.
# =====================================================================

def conectar() -> Garmin:
    """Autentica na Garmin, reaproveitando o token salvo quando existir.

    Levanta RuntimeError se o rate limit da Garmin for atingido ou se
    todas as tentativas de senha falharem — quem chama decide o que
    fazer (ex.: abortar o workflow do GitHub Actions).
    """
    try:
        api = Garmin()
        api.login(TOKENSTORE)
        return api
    except GarminConnectTooManyRequestsError as err:
        raise RuntimeError(f"Rate limit da Garmin: {err}") from err
    except (GarminConnectAuthenticationError, GarminConnectConnectionError, FileNotFoundError):
        pass  # sem token válido — segue pro login com email/senha

    load_dotenv(ROOT / ".env")
    email = os.getenv("GARMIN_EMAIL")
    senha_env = os.getenv("GARMIN_PASSWORD")

    if os.getenv("GITHUB_ACTIONS") and not senha_env:
        # Sem terminal no runner -- getpass/input travariam até o timeout do
        # job (horas) em vez de falhar rápido. Token salvo (GARMIN_TOKEN
        # Secret) é a via normal em CI; chegar aqui significa que ele
        # expirou ou é inválido e precisa ser regerado localmente.
        raise RuntimeError(
            "Token da Garmin inválido/expirado e sem GARMIN_PASSWORD disponível -- "
            "rode a autenticação localmente e atualize o Secret GARMIN_TOKEN."
        )

    for tentativa in range(1, MAX_LOGIN_ATTEMPTS + 1):
        senha = senha_env if (tentativa == 1 and senha_env) else getpass.getpass(
            f"Senha da Garmin (tentativa {tentativa}/{MAX_LOGIN_ATTEMPTS}): "
        )
        if not email:
            email = input("Email da Garmin: ").strip()
        try:
            api = Garmin(
                email=email,
                password=senha,
                prompt_mfa=lambda: input("Código MFA: ").strip(),
            )
            api.login(TOKENSTORE)
            return api
        except GarminConnectAuthenticationError:
            print("Senha incorreta — tente de novo.")
            continue
        except GarminConnectTooManyRequestsError as err:
            raise RuntimeError(f"Rate limit da Garmin: {err}") from err

    raise RuntimeError(f"Login falhou após {MAX_LOGIN_ATTEMPTS} tentativas.")


# =====================================================================
# Atividades → colunas de stg_atividades
# =====================================================================

def _mapear_atividade(raw: dict) -> dict:
    start_local = raw.get("startTimeLocal")  # ex: "2026-07-27 19:05:54"
    tipo = raw.get("activityType") or {}
    return {
        "activity_id": raw.get("activityId"),
        "activity_uuid": raw.get("activityUUID"),
        "data": start_local.split(" ")[0] if start_local else None,
        "hora_inicio": start_local,
        "nome": raw.get("activityName"),
        "tipo": tipo.get("typeKey"),
        "distancia_m": raw.get("distance"),
        "duracao_seg": raw.get("duration"),
        "duracao_movimento_seg": raw.get("movingDuration"),
        "velocidade_media_mps": raw.get("averageSpeed"),
        "velocidade_maxima_mps": raw.get("maxSpeed"),
        "velocidade_ajustada_grade_mps": raw.get("avgGradeAdjustedSpeed"),
        "fc_media": raw.get("averageHR"),
        "fc_maxima": raw.get("maxHR"),
        "cadencia_media": raw.get("averageRunningCadenceInStepsPerMinute"),
        "cadencia_maxima": raw.get("maxRunningCadenceInStepsPerMinute"),
        "ganho_elevacao": raw.get("elevationGain"),
        "perda_elevacao": raw.get("elevationLoss"),
        "elevacao_media": raw.get("avgElevation"),
        "elevacao_minima": raw.get("minElevation"),
        "elevacao_maxima": raw.get("maxElevation"),
        "potencia_media": raw.get("avgPower"),
        "potencia_maxima": raw.get("maxPower"),
        "potencia_normalizada": raw.get("normPower"),
        "efeito_treino_aerobico": raw.get("aerobicTrainingEffect"),
        "efeito_treino_anaerobico": raw.get("anaerobicTrainingEffect"),
        "efeito_treino_label": raw.get("trainingEffectLabel"),
        "tempo_zona_fc_1": raw.get("hrTimeInZone_1"),
        "tempo_zona_fc_2": raw.get("hrTimeInZone_2"),
        "tempo_zona_fc_3": raw.get("hrTimeInZone_3"),
        "tempo_zona_fc_4": raw.get("hrTimeInZone_4"),
        "tempo_zona_fc_5": raw.get("hrTimeInZone_5"),
        "tempo_contato_solo_medio": raw.get("avgGroundContactTime"),
        "comprimento_passada_medio": raw.get("avgStrideLength"),
        "oscilacao_vertical_media": raw.get("avgVerticalOscillation"),
        "razao_vertical_media": raw.get("avgVerticalRatio"),
        "fastest_split_1km_seg": raw.get("fastestSplit_1000"),
        "fastest_split_1milha_seg": raw.get("fastestSplit_1609"),
        "minutos_intensidade_moderada": raw.get("moderateIntensityMinutes"),
        "minutos_intensidade_vigorosa": raw.get("vigorousIntensityMinutes"),
        "body_battery_variacao": raw.get("differenceBodyBattery"),
        "calorias": raw.get("calories"),
    }


def _mapear_splits(raw: dict) -> list[dict]:
    """splitSummaries -> linhas de stg_atividade_splits.

    É aqui que o método run-walk aparece de verdade: splitType 'RWD_RUN' /
    'RWD_WALK' alternando dentro da mesma atividade. Atividades sem
    segmentação (a maioria dos tipos que não são run-walk) simplesmente não
    têm `splitSummaries` — devolve lista vazia nesse caso.
    """
    splits = raw.get("splitSummaries") or []
    return [
        {
            "split_index": i,
            "tipo": s.get("splitType"),
            "duracao_seg": s.get("duration"),
            "distancia_m": s.get("distance"),
            "velocidade_media_mps": s.get("averageSpeed"),
            "velocidade_maxima_mps": s.get("maxSpeed"),
            "ganho_elevacao": s.get("totalAscent"),
            "perda_elevacao": s.get("elevationLoss"),
        }
        for i, s in enumerate(splits)
    ]


def atividades_novas(api: Garmin, dias: int = 7, limite: int = 20) -> list[dict]:
    """Atividades dos últimos `dias` dias, já mapeadas pro schema.

    `dias`/`limite` servem só como corte de segurança — a deduplicação
    de verdade (não inserir de novo o que já está no banco) é
    responsabilidade do load/load_atividades.py (Fase 3), via
    activity_id (PRIMARY KEY em stg_atividades).
    """
    corte = date.today() - timedelta(days=dias)
    brutas = api.get_activities(0, limite)
    mapeadas = []
    for bruta in brutas:
        atividade = _mapear_atividade(bruta)
        if not atividade["data"] or date.fromisoformat(atividade["data"]) < corte:
            continue
        atividade["splits"] = _mapear_splits(bruta)
        mapeadas.append(atividade)
    return mapeadas


# =====================================================================
# Recuperação diária → colunas de stg_recuperacao_diaria
# =====================================================================

def recuperacao_diaria(api: Garmin, dia: date) -> Optional[dict]:
    """Sono + FC de repouso + body battery + bem-estar de um dia específico.

    Combina 4 chamadas (get_sleep_data, get_heart_rates, get_user_summary,
    get_body_battery) porque na Fase 0 confirmamos que cada uma devolve
    só uma fatia do que vira uma linha de stg_recuperacao_diaria.
    """
    iso = dia.isoformat()
    resultado = {"data": iso}

    sono = api.get_sleep_data(iso) or {}
    dto = sono.get("dailySleepDTO") or {}
    scores = dto.get("sleepScores") or {}
    resultado.update({
        "sono_total_seg": dto.get("sleepTimeSeconds"),
        "sono_profundo_seg": dto.get("deepSleepSeconds"),
        "sono_leve_seg": dto.get("lightSleepSeconds"),
        "sono_rem_seg": dto.get("remSleepSeconds"),
        "sono_desperto_seg": dto.get("awakeSleepSeconds"),
        "despertares": dto.get("awakeCount"),
        "sono_fc_media": dto.get("avgHeartRate"),
        "sono_estresse_medio": dto.get("avgSleepStress"),
        "sono_score": (scores.get("overall") or {}).get("value"),
    })

    fc = api.get_heart_rates(iso) or {}
    resultado.update({
        "fc_repouso": fc.get("restingHeartRate"),
        "fc_repouso_media_7d": fc.get("lastSevenDaysAvgRestingHeartRate"),
    })

    resumo = api.get_user_summary(iso) or {}
    resultado.update({
        "body_battery_ao_acordar": resumo.get("bodyBatteryAtWakeTime"),
        "body_battery_mais_recente": resumo.get("bodyBatteryMostRecentValue"),
        "body_battery_carregada": resumo.get("bodyBatteryChargedValue"),
        "body_battery_drenada": resumo.get("bodyBatteryDrainedValue"),
        "estresse_medio_dia": resumo.get("averageStressLevel"),
        "passos_totais": resumo.get("totalSteps"),
        # fc_repouso via get_heart_rates é mais específico; usa resumo só se faltar
        "fc_repouso": resultado.get("fc_repouso") or resumo.get("restingHeartRate"),
    })

    bb_dias = api.get_body_battery(iso, iso) or []
    bb_hoje = next((d for d in bb_dias if d.get("date") == iso), None)
    if bb_hoje:
        # charged/drained do body_battery são mais completos que os do resumo
        # diário quando os dois existem — prioriza esses.
        resultado["body_battery_carregada"] = bb_hoje.get("charged") or resultado.get("body_battery_carregada")
        resultado["body_battery_drenada"] = bb_hoje.get("drained") or resultado.get("body_battery_drenada")

    # se nenhuma das 4 chamadas trouxe nada além da data, não vale a pena
    if all(v is None for k, v in resultado.items() if k != "data"):
        return None
    return resultado


if __name__ == "__main__":
    # teste manual: roda dentro do container com `docker compose run --rm
    # garmin python -m src.extract.garmin`
    api = conectar()

    print("--- atividades_novas(dias=7) ---")
    for a in atividades_novas(api, dias=7):
        print(f"  {a['data']} | {a['nome']} | {a['distancia_m']}m | fc_media={a['fc_media']}")

    print("\n--- recuperacao_diaria(hoje) ---")
    r = recuperacao_diaria(api, date.today())
    print(f"  {r}")
