"""Fase 1 (revisão) — conexão e helpers do Postgres (Neon).

Centraliza a abertura da conexão, a aplicação do schema.sql e os helpers de
consulta/upsert, pra os scripts de carga e de análise não duplicarem essa
lógica.
"""

import os

import psycopg

from src.config import ROOT, carregar_env

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
    carregar_env()
    database_url = os.environ["DATABASE_URL"]
    con = psycopg.connect(database_url, autocommit=True)
    if aplicar_schema:
        con.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
    return con


def consultar(con: psycopg.Connection, sql: str, params: tuple = ()) -> tuple[list[str], list[tuple]]:
    """(nomes das colunas, linhas) de uma consulta."""
    cur = con.execute(sql, params)
    return [d.name for d in cur.description], cur.fetchall()


def sql_upsert(tabela: str, colunas: list[str], chave: str) -> str:
    """INSERT ... ON CONFLICT (chave) DO UPDATE de todas as outras colunas --
    reprocessar o mesmo registro atualiza em vez de duplicar."""
    sets = ", ".join(f"{c} = excluded.{c}" for c in colunas if c != chave)
    return (
        f"INSERT INTO {tabela} ({', '.join(colunas)}) VALUES ({', '.join(['%s'] * len(colunas))}) "
        f"ON CONFLICT ({chave}) DO UPDATE SET {sets}"
    )
