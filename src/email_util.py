"""Envio de e-mail via Gmail SMTP -- mecânica pura, sem conhecimento do
conteúdo (assunto/corpo vêm de quem chama). Usado pelo e-mail semanal do
plano (analyze/analisar_com_ia.py) e pelo lembrete diário de treino não
realizado (load/planilha_desempenho.py, Fase 9).

Senha de app do Gmail (não a senha da conta -- exige verificação em 2
etapas ativada, gerada em myaccount.google.com/apppasswords), zero
dependência nova: só smtplib da biblioteca padrão.
"""

import os
import smtplib
from email.message import EmailMessage
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def enviar_email(assunto: str, corpo: str) -> None:
    load_dotenv(ROOT / ".env")
    remetente = os.environ["EMAIL_REMETENTE"]
    senha_app = os.environ["EMAIL_SENHA_APP"]
    destinatario = os.environ.get("EMAIL_DESTINATARIO") or remetente

    msg = EmailMessage()
    msg["Subject"] = assunto
    msg["From"] = remetente
    msg["To"] = destinatario
    msg.set_content(corpo)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(remetente, senha_app)
        smtp.send_message(msg)
