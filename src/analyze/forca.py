"""Força (Fase B, 2026-10): prescrição calculada em Python a partir do
catálogo (`forca_catalogo.json`) e do que o corredor registrou na aba
"Força" -- o LLM só escolhe em quais dias de corrida ela entra (logo depois
da corrida), nunca os exercícios nem os números.

Decisões do usuário: 2 sessões por semana, cada uma com o bloco A
(panturrilha e tíbia) e o B (quadril e coxa); registro por exercício
("atingi o topo da faixa em todas as séries?" + RIR da última série);
progressão dupla por exercício. Treino de força reduz lesão por sobrecarga
e treino neuromuscular previne canelite (Lauersen 2014/2018; revisão de
2025 em Gait & Posture) -- ver CASE_DO_PROJETO_1.md, Fase B.
"""

import json
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

import gspread
from pydantic import BaseModel

from src.formato import parse_data
from src.planilha import adicionar_dropdown, obter_aba

CATALOGO_PATH = Path(__file__).with_name("forca_catalogo.json")
SESSOES_POR_SEMANA = 2
RIR_MINIMO_PROGRESSAO = 2  # progressão dupla: topo da faixa com RIR >= 2 em todas as séries
INTERVALO_TESTE_DIAS = 28  # teste de elevação de calcanhar a cada ~4 semanas

ABA_FORCA = "Força"
COR_FORCA = (0.42, 0.30, 0.66)  # violeta
CABECALHO_FORCA = [
    "data", "dia_semana", "bloco", "exercicio_id", "exercicio", "series", "faixa",
    "topo_atingido", "rir", "observacao",
]
SIM, NAO = "sim", "não"
OPCOES_RIR = ["0", "1", "2", "3", "4", "5+"]

ABA_TESTE = "Teste Calcanhar"
COR_TESTE = (0.13, 0.55, 0.60)  # azul-petróleo
CABECALHO_TESTE = ["data", "reps_esquerda", "reps_direita", "observacao"]
PROTOCOLO_TESTE = (
    "Teste de elevação de calcanhar (antes da força): uma perna de cada vez, descalço, mão na "
    "parede só para equilíbrio, ~1 repetição a cada 2 s, até a altura cair, o joelho dobrar ou "
    "o ritmo quebrar. Registre as repetições de cada perna na aba \"Teste Calcanhar\"."
)


class Exercicio(BaseModel):
    id: str
    nome: str
    bloco: str  # "A" panturrilha e tíbia, "B" quadril e coxa
    cadeia: str
    nivel: int
    series: int
    faixa: tuple[int, int]
    unidade: str  # "reps" ou "seg"
    por_lado: bool
    impacto: bool
    descanso_seg: int
    dicas: list[str]
    garmin: tuple[str, str]  # (categoria, exercício) do catálogo do Garmin

    def faixa_txt(self) -> str:
        minimo, maximo = self.faixa
        valor = f"{minimo}" if minimo == maximo else f"{minimo}-{maximo}"
        lado = " por perna/lado" if self.por_lado else ""
        return f"{valor} {'reps' if self.unidade == 'reps' else 's'}{lado}"

    def descricao(self) -> str:
        return f"{self.nome}: {self.series}x {self.faixa_txt()} ({'; '.join(self.dicas)})"


@lru_cache(maxsize=1)
def catalogo() -> dict[str, Exercicio]:
    dados = json.loads(CATALOGO_PATH.read_text(encoding="utf-8"))
    return {e["id"]: Exercicio(**e) for e in dados["exercicios"]}


def _cadeias() -> dict[str, list[Exercicio]]:
    cadeias: dict[str, list[Exercicio]] = {}
    for exercicio in catalogo().values():
        cadeias.setdefault(exercicio.cadeia, []).append(exercicio)
    return {c: sorted(es, key=lambda e: e.nivel) for c, es in cadeias.items()}


class PrescricaoForca(BaseModel):
    exercicios: list[Exercicio]
    decisoes: list[str]  # o que subiu ou por que repetiu, por cadeia
    teste_calcanhar: bool

    def texto(self) -> str:
        linhas = []
        for bloco, titulo in (("A", "Bloco A (panturrilha e tíbia)"), ("B", "Bloco B (quadril e coxa)")):
            linhas.append(f"  {titulo}:")
            linhas += [f"    - {e.descricao()}" for e in self.exercicios if e.bloco == bloco]
        linhas.append(
            f"  Cada exercício: pare com 2 repetições sobrando (RIR 2). Registre na aba \"{ABA_FORCA}\" se "
            "atingiu o topo da faixa em todas as séries e o RIR da última série -- atingiu com RIR 2 ou "
            "mais, sobe a variação na semana seguinte."
        )
        return "\n".join(linhas)


def _obter_aba_com_dropdowns(spreadsheet, titulo, cabecalho, cor, dropdowns: dict[str, list[str]]) -> gspread.Worksheet:
    """obter_aba + listas suspensas, só quando a aba é criada agora."""
    try:
        return spreadsheet.worksheet(titulo)
    except gspread.WorksheetNotFound:
        aba = obter_aba(spreadsheet, titulo, cabecalho, cor=cor)
        for coluna, valores in dropdowns.items():
            adicionar_dropdown(aba, chr(ord("A") + cabecalho.index(coluna)), valores)
        return aba


def obter_aba_forca(spreadsheet: gspread.Spreadsheet) -> gspread.Worksheet:
    return _obter_aba_com_dropdowns(
        spreadsheet, ABA_FORCA, CABECALHO_FORCA, COR_FORCA, {"topo_atingido": [SIM, NAO], "rir": OPCOES_RIR}
    )


def obter_aba_teste(spreadsheet: gspread.Spreadsheet) -> gspread.Worksheet:
    return _obter_aba_com_dropdowns(spreadsheet, ABA_TESTE, CABECALHO_TESTE, COR_TESTE, {})


def _rir(valor) -> int | None:
    texto = str(valor).strip().rstrip("+")
    return int(texto) if texto.isdigit() else None


def prescrever(spreadsheet: gspread.Spreadsheet, inicio_semana: date, travar: bool) -> PrescricaoForca:
    """Exercício de cada cadeia pra semana: o último prescrito na aba "Força"
    (o 1º nível, se nunca houve), subindo um nível quando o registro mais
    recente dele diz topo atingido com RIR >= 2. `travar` (dor na tíbia ou
    sinal de alerta recente) segura a progressão. Cadeia sem próximo nível
    na fase de retomada fica onde está -- carga e pliometria entram na
    Fase C."""
    registros = []
    for r in obter_aba_forca(spreadsheet).get_all_records():
        data = parse_data(r.get("data"))
        if data and data < inicio_semana and r.get("exercicio_id") in catalogo():
            registros.append((data, r))
    registros.sort(key=lambda x: x[0])

    exercicios, decisoes = [], []
    for cadeia, niveis in _cadeias().items():
        da_cadeia = [(d, r) for d, r in registros if catalogo()[r["exercicio_id"]].cadeia == cadeia]
        if not da_cadeia:
            exercicios.append(niveis[0])
            continue
        atual = catalogo()[da_cadeia[-1][1]["exercicio_id"]]
        preenchidos = [r for _, r in da_cadeia if r["exercicio_id"] == atual.id and str(r.get("topo_atingido")).strip()]
        proximo = next((e for e in niveis if e.nivel == atual.nivel + 1), None)
        ultimo = preenchidos[-1] if preenchidos else None
        cumpriu = (
            ultimo is not None
            and str(ultimo.get("topo_atingido")).strip().lower() == SIM
            and (_rir(ultimo.get("rir")) or 0) >= RIR_MINIMO_PROGRESSAO
        )
        if cumpriu and proximo and not travar:
            exercicios.append(proximo)
            decisoes.append(f"{atual.nome} -> {proximo.nome}")
        else:
            exercicios.append(atual)
            if cumpriu and proximo and travar:
                decisoes.append(f"{atual.nome}: topo atingido, mas segura por dor/alerta na tíbia")

    exercicios.sort(key=lambda e: (e.bloco, list(_cadeias()).index(e.cadeia)))
    assert not any(e.impacto for e in exercicios), "exercício de impacto na prescrição"
    return PrescricaoForca(exercicios=exercicios, decisoes=decisoes, teste_calcanhar=_teste_devido(spreadsheet, inicio_semana))


def _teste_devido(spreadsheet: gspread.Spreadsheet, inicio_semana: date) -> bool:
    """Teste de calcanhar se não há teste preenchido nos últimos
    INTERVALO_TESTE_DIAS dias."""
    feitos = [
        parse_data(r.get("data"))
        for r in obter_aba_teste(spreadsheet).get_all_records()
        if str(r.get("reps_esquerda")).strip() and str(r.get("reps_direita")).strip()
    ]
    feitos = [d for d in feitos if d and d < inicio_semana]
    return not feitos or max(feitos) < inicio_semana - timedelta(days=INTERVALO_TESTE_DIAS)


def instrucao_forca(n_dias: int | None, teste: bool) -> str:
    """`n_dias` None = fora da retomada, onde o número de dias de corrida é
    livre: força em 2 deles (ou em todos, se houver menos)."""
    quantos = f"exatamente {n_dias} dias" if n_dias is not None else f"{SESSOES_POR_SEMANA} dias de corrida (ou todos, se houver menos)"
    return (
        f"INSTRUÇÃO OBRIGATÓRIA (força): marque `forca: true` em {quantos} -- na retomada, dias de "
        f"sessão de evolução -- de preferência espaçados (ex. a 1ª e a última corrida da semana). "
        f"A sessão de força (bloco A, panturrilha e tíbia, e bloco B, quadril e coxa, uns 40 min) "
        f"é feita logo depois da corrida desses dias, pra panturrilha nunca chegar cansada à "
        f"corrida seguinte. O conteúdo da força é calculado pelo sistema: não liste exercícios. "
        f"Nunca marque força em descanso ou caminhada."
        + (" Nesta semana o primeiro dia de força começa com o teste de elevação de calcanhar." if teste else "")
    )


def aplicar_forca(plano, prescricao: PrescricaoForca) -> None:
    """Acrescenta a força ao motivo dos dias marcados -- vai assim pra
    planilha, e-mail e descrição do treino de corrida no relógio."""
    primeiro = True
    for dia in plano.dias:
        if not dia.forca or dia.descanso:
            continue
        teste = " + teste de elevação de calcanhar antes" if prescricao.teste_calcanhar and primeiro else ""
        dia.motivo = f"{dia.motivo} + força A+B depois da corrida (aba \"{ABA_FORCA}\"){teste}"
        primeiro = False
