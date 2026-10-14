"""Configuração compartilhada: raiz do projeto, leitura do `.env` e se o
código está rodando no GitHub Actions."""

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

# O repositório é público, e com ele os logs do GitHub Actions. No Actions, o
# log não mostra conteúdo de saúde (plano com os motivos, nível de dor,
# justificativa da revisão) -- isso já chega completo por e-mail, planilha e
# relógio. Rodando local, tudo continua sendo impresso.
EM_CI = bool(os.environ.get("GITHUB_ACTIONS"))


def carregar_env() -> None:
    """Lê o `.env` local (no GitHub Actions as variáveis vêm dos Secrets e o
    arquivo não existe -- aí não faz nada). Não sobrescreve variável já
    definida no ambiente."""
    load_dotenv(ROOT / ".env")
