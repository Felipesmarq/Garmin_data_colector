"""Fase 1 (revisão) — conexão e helpers do Postgres (Neon).

Centraliza a abertura da conexão e a aplicação do schema.sql, pra
load_atividades.py / load_recuperacao.py não duplicarem essa lógica.
"""

import os
from pathlib import Path

import psycopg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "src" / "load" / "schema.sql"


def conectar(aplicar_schema: bool = True) -> psycopg.Connection:
    """Abre a conexão com o Postgres (Neon) e garante que o schema está aplicado.

    `DATABASE_URL` vem do `.env` local ou de um Secret no GitHub Actions
    (string de conexão do Neon, com `sslmode=require`). Autocommit ligado —
    volume e concorrência do projeto não justificam controle manual de
    transação.

    `CREATE TABLE IF NOT EXISTS` / `CREATE OR REPLACE VIEW` no schema.sql
    tornam a aplicação segura de rodar toda vez, mesmo com o banco já
    populado.
    """
    load_dotenv(ROOT / ".env")
    database_url = os.environ["DATABASE_URL"]
    con = psycopg.connect(database_url, autocommit=True)
    if aplicar_schema:
        con.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
    return con
