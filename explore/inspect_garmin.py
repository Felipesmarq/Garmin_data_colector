#!/usr/bin/env python3
"""Fase 0 — exploração da Garmin Connect API.

Objetivo deste script (não faz parte do pipeline final):
  1. Autenticar na conta Garmin e guardar o token de sessão localmente
     (assim login/MFA só precisam ser feitos uma vez).
  2. Puxar uma amostra pequena de cada tipo de dado que o case cita
     (atividades, sono, FC de repouso) e mostrar os campos REAIS que a
     API devolve — pra fechar o schema.sql da Fase 1 com base na
     realidade, não em suposição.
  3. Salvar essa amostra bruta em explore/output/*.json (gitignored —
     contém dados de saúde reais, nunca commitar) pra consulta enquanto
     desenhamos o schema.

Uso:
    pip install -r requirements.txt
    cp .env.example .env   # preencha GARMIN_EMAIL e GARMIN_PASSWORD
    python explore/inspect_garmin.py

Sobre login por tentativa e erro:
    Se você não lembra a senha exata, o script permite tentar de novo
    sem precisar editar o .env toda vez. Mas a Garmin aplica rate
    limit (erro 429) e pode bloquear temporariamente a conta depois de
    muitas tentativas erradas seguidas — se isso acontecer, o caminho é
    esperar um pouco ou usar "Esqueci minha senha" no site/app da
    Garmin em vez de continuar tentando aqui.
"""

import json
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv
import os

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

logging.getLogger("garminconnect").setLevel(logging.CRITICAL)

ROOT = Path(__file__).resolve().parent.parent
TOKENSTORE = str(ROOT / ".garmin_tokens")
OUTPUT_DIR = ROOT / "explore" / "output"
MAX_LOGIN_ATTEMPTS = 5  # limite de segurança pra não tomar rate limit/bloqueio


def init_api() -> Garmin | None:
    """Restaura sessão salva ou faz login do zero (com retry de senha)."""

    # 1. tenta reaproveitar token salvo de uma execução anterior
    try:
        garmin = Garmin()
        garmin.login(TOKENSTORE)
        print("Login restaurado a partir do token salvo (sem precisar de senha).")
        return garmin
    except GarminConnectTooManyRequestsError as err:
        print(f"Rate limit da Garmin: {err}")
        sys.exit(1)
    except (GarminConnectAuthenticationError, GarminConnectConnectionError, FileNotFoundError):
        print("Nenhum token válido encontrado — login com email/senha.")

    # 2. login do zero, com espaço pra errar a senha algumas vezes
    load_dotenv(ROOT / ".env")
    email = os.getenv("GARMIN_EMAIL")

    for attempt in range(1, MAX_LOGIN_ATTEMPTS + 1):
        password = os.getenv("GARMIN_PASSWORD") if attempt == 1 else None
        if not password:
            import getpass
            password = getpass.getpass(
                f"Senha da Garmin (tentativa {attempt}/{MAX_LOGIN_ATTEMPTS}): "
            )
        if not email:
            email = input("Email da Garmin: ").strip()

        try:
            garmin = Garmin(
                email=email,
                password=password,
                prompt_mfa=lambda: input("Código MFA: ").strip(),
            )
            garmin.login(TOKENSTORE)
            print(f"Login bem-sucedido. Token salvo em: {TOKENSTORE}")
            return garmin

        except GarminConnectAuthenticationError:
            print("Senha incorreta — tente de novo.")
            continue
        except GarminConnectTooManyRequestsError as err:
            print(
                f"Rate limit da Garmin ({err}). Pare de tentar por um tempo "
                "para não arriscar bloqueio da conta."
            )
            sys.exit(1)
        except GarminConnectConnectionError as err:
            print(f"Erro de conexão: {err}")
            return None
        except KeyboardInterrupt:
            return None

    print(f"Limite de {MAX_LOGIN_ATTEMPTS} tentativas atingido. Pare e confira a senha "
          "com calma (ou use 'Esqueci minha senha' no site da Garmin) antes de tentar de novo.")
    return None


def dump(name: str, payload) -> None:
    """Imprime um resumo das chaves e salva o JSON bruto pra consulta depois."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"{name}.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\n--- {name} ---")
    if isinstance(payload, list):
        print(f"  lista com {len(payload)} item(ns)")
        if payload:
            print(f"  campos do 1º item: {sorted(payload[0].keys())}")
    elif isinstance(payload, dict):
        print(f"  campos: {sorted(payload.keys())}")
    else:
        print(f"  tipo inesperado: {type(payload)}")
    print(f"  salvo em: {path.relative_to(ROOT)}")


def safe_call(label, fn, *args, **kwargs):
    try:
        result = fn(*args, **kwargs)
        if result:
            dump(label, result)
        else:
            print(f"\n--- {label} --- (vazio, sem dado pra essa data/consulta)")
        return result
    except GarminConnectTooManyRequestsError as e:
        print(f"\n--- {label} --- rate limit: {e}")
    except Exception as e:
        print(f"\n--- {label} --- erro: {e}")
    return None


def main():
    api = init_api()
    if not api:
        sys.exit(1)

    today = date.today().isoformat()
    week_ago = (date.today() - timedelta(days=7)).isoformat()

    # Atividades (corridas) — o que o case chama de "distância, pace, FC, cadência"
    safe_call("atividades_recentes", api.get_activities, 0, 5)

    # Sono da última noite
    safe_call("sono_hoje", api.get_sleep_data, today)

    # FC de repouso / heart rate do dia
    safe_call("fc_hoje", api.get_heart_rates, today)

    # Resumo diário (passos, calorias, distância) — cruza com o resumo semanal depois
    safe_call("resumo_hoje", api.get_user_summary, today)

    # Body battery / recuperação, se a conta tiver esse dado
    safe_call("body_battery_semana", api.get_body_battery, week_ago, today)

    print(f"\nAmostras salvas em: {OUTPUT_DIR.relative_to(ROOT)}/ (não commitar — está no .gitignore)")
    print("Próximo passo: usar esses campos reais pra fechar o schema.sql da Fase 1.")


if __name__ == "__main__":
    main()
