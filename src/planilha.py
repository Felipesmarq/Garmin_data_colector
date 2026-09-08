"""Cliente genérico de Google Sheets -- conexão, abas e escrita de linhas.

Mecânica pura de planilha, sem conhecimento do domínio (dor, atividades,
etc.) -- isso fica em load_dor.py e planilha_desempenho.py, que chamam
essas funções. Espelha o papel de src/db.py pro lado do Postgres.
"""

import os
from pathlib import Path

import gspread
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials
from gspread.utils import ValidationConditionType

ROOT = Path(__file__).resolve().parent.parent

ESCOPOS = ["https://www.googleapis.com/auth/spreadsheets"]

Cor = tuple[float, float, float]  # RGB 0-1, formato da Sheets API

COR_PADRAO: Cor = (0.29, 0.29, 0.29)  # cinza-escuro, usada se a aba não pedir cor própria


def conectar() -> gspread.Spreadsheet:
    load_dotenv(ROOT / ".env")
    credenciais = Credentials.from_service_account_file(
        os.environ["GOOGLE_SHEETS_CREDENTIALS_PATH"], scopes=ESCOPOS
    )
    cliente = gspread.authorize(credenciais)
    return cliente.open_by_key(os.environ["GOOGLE_SHEET_ID"])


def _rgb(cor: Cor) -> dict[str, float]:
    r, g, b = cor
    return {"red": r, "green": g, "blue": b}


def _hex(cor: Cor) -> str:
    r, g, b = cor
    return "#{:02x}{:02x}{:02x}".format(round(r * 255), round(g * 255), round(b * 255))


def aplicar_tema(aba: gspread.Worksheet, cor: Cor) -> None:
    """Cabeçalho colorido (texto branco em negrito), linha 1 congelada,
    linhas intercaladas numa versão bem clara da mesma cor, e a cor da
    própria aba (aparece na guia lá embaixo). Só chamado na
    criação/migração da aba -- reaplicar toda execução seria chamada de
    API à toa, e `addBanding` duplicaria a faixa se rodado 2x na mesma aba.
    """
    cor_cabecalho = _rgb(cor)
    r, g, b = cor
    cor_faixa = _rgb((1 - (1 - r) * 0.12, 1 - (1 - g) * 0.12, 1 - (1 - b) * 0.12))

    aba.format("1:1", {
        "backgroundColor": cor_cabecalho,
        "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
    })
    aba.freeze(rows=1)
    aba.spreadsheet.batch_update({
        "requests": [{
            "addBanding": {
                "bandedRange": {
                    "range": {"sheetId": aba.id},
                    "rowProperties": {
                        "headerColor": cor_cabecalho,
                        "firstBandColor": {"red": 1, "green": 1, "blue": 1},
                        "secondBandColor": cor_faixa,
                    },
                }
            }
        }]
    })
    aba.update_tab_color(_hex(cor))


def adicionar_dropdown(
    aba: gspread.Worksheet, coluna: str, valores: list[str], linha_inicio: int = 2, linha_fim: int = 200
) -> None:
    """Restringe uma coluna (letra, ex. "E") a uma lista fixa de valores --
    aparece como dropdown na célula. Intervalo cobre linhas futuras (a aba
    cresce por append), não só as que já têm dado hoje."""
    intervalo = f"{coluna}{linha_inicio}:{coluna}{linha_fim}"
    aba.add_validation(intervalo, ValidationConditionType.one_of_list, valores, showCustomUi=True)


def obter_aba(
    spreadsheet: gspread.Spreadsheet, titulo: str, cabecalho: list[str], cor: Cor = COR_PADRAO
) -> gspread.Worksheet:
    """Retorna a aba pelo título, criando (com cabeçalho + tema) se não existir."""
    try:
        return spreadsheet.worksheet(titulo)
    except gspread.WorksheetNotFound:
        aba = spreadsheet.add_worksheet(title=titulo, rows=200, cols=len(cabecalho))
        aba.append_row(cabecalho)
        aplicar_tema(aba, cor)
        return aba


def inserir_linhas(aba: gspread.Worksheet, linhas: list[list]) -> None:
    """Acrescenta linhas ao final da aba, sem tocar no que já existe."""
    if linhas:
        aba.append_rows(linhas, value_input_option="USER_ENTERED")


def inserir_linhas_no_topo(aba: gspread.Worksheet, linhas: list[list]) -> None:
    """Insere linhas logo abaixo do cabeçalho (linha 2), empurrando o resto
    pra baixo -- sem tocar no conteúdo das linhas já existentes. Usado
    quando a aba lista o mais recente primeiro (`ORDER BY ... DESC`): passe
    `linhas` já nessa ordem, e o bloco inserido preserva a ordem entre si."""
    if linhas:
        aba.insert_rows(linhas, row=2, value_input_option="USER_ENTERED")


def sobrescrever(aba: gspread.Worksheet, cabecalho: list[str], linhas: list[tuple]) -> None:
    """Limpa a aba inteira e reescreve cabeçalho + linhas.

    Usado pelas abas somente-leitura (snapshot de uma view) -- não há
    "linha existente" pra preservar, então sobrescrever é mais simples e
    mais seguro que calcular um diff.
    """
    aba.clear()
    aba.append_row(cabecalho)
    inserir_linhas(aba, [[_texto(v) for v in linha] for linha in linhas])


def _texto(v) -> str:
    return "" if v is None else str(v)
