"""Formatação e leitura de valores de treino (pace, tempo, números, faixas
de pace/FC) -- compartilhado entre a planilha, o prompt, o plano e o
relógio, pra que todos escrevam e leiam do mesmo jeito."""

import re


def pace_txt(min_km: float) -> str:
    """Minutos por km em decimal (ex. 5.532) -> "M:SS" (ex. "5:32")."""
    minutos = int(min_km)
    segundos = round((float(min_km) - minutos) * 60)
    if segundos == 60:
        minutos, segundos = minutos + 1, 0
    return f"{minutos}:{segundos:02d}"


def mmss(segundos: int) -> str:
    """Segundos inteiros -> "M:SS" (ex. 90 -> "1:30")."""
    return f"{segundos // 60}:{segundos % 60:02d}"


def numero_br(v, casas: int, remover_zero_a_direita: bool = False) -> str:
    """Arredonda pra `casas` decimais; remover_zero_a_direita tira zero(s)
    sobrando -- 3.00 -> "3", 3.50 -> "3.5" (pra colunas onde precisão fixa
    só polui, tipo distância/duração).

    Devolve com vírgula decimal (locale BR da planilha) -- com ponto, o
    Sheets confunde "3.01" com data (dia.mês) em vez de número, e some o
    ponto/mostra zero à esquerda ("03.01"); "4.57" vira separador de milhar.
    """
    if v is None:
        return ""
    texto = f"{float(v):.{casas}f}"
    if remover_zero_a_direita:
        texto = texto.rstrip("0").rstrip(".")
    return texto.replace(".", ",")


def parse_faixa_pace(texto: str | None) -> tuple[float, float] | None:
    """'10:52-12:29 min/km' (ou só '10:52 min/km') -> (min, max) em minutos
    decimais. `pace_alvo`/`fc_alvo` do plano são texto livre (ver TreinoDia
    em analyze/modelos.py) -- parse tolerante, não confia em formato fixo."""
    pares = re.findall(r"(\d+):(\d{2})", texto or "")
    if not pares:
        return None
    valores = [int(m) + int(s) / 60 for m, s in pares]
    return (min(valores), max(valores))


def parse_faixa_fc(texto: str | None) -> tuple[int, int] | None:
    """'123-133 bpm' (ou só '130 bpm') -> (min, max). 'até 150 bpm' (teto da
    retomada) -> (0, 150): só reprova acima."""
    valores = [int(v) for v in re.findall(r"\d+", texto or "")]
    if not valores:
        return None
    if "até" in (texto or "").lower():
        return (0, max(valores))
    return (min(valores), max(valores))
