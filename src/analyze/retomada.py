"""Retomada pós-canelite: protocolo de Warden (um estágio por semana), em
Python -- o LLM escolhe os dias e escreve o motivo, nunca os números (ver
CASE_DO_PROJETO_1.md seção 6.1, "Retomada")."""

import re
from datetime import date, timedelta

import gspread
import psycopg
from pydantic import BaseModel

from src.analyze.entrega import ABA_PLANO, CABECALHO_PLANO, COR_PLANO
from src.analyze.modelos import PlanoSemanal, Sessao
from src.db import consultar
from src.formato import mmss, pace_txt
from src.planilha import obter_aba

# Retomada pós-canelite -- protocolo de Warden et al. puro: 3 blocos de 10
# min de caminhada + trote, em dias alternados, subindo um estágio por
# semana (todas as sessões da semana no mesmo estágio). A regra dos 10%
# (Frandsen) só vale depois da retomada: é estudo de corredor saudável em
# treino regular, não de readaptação (decisão do usuário, 2026-10-13 -- ver
# CASE_DO_PROJETO_1.md seção 6.1, "Retomada"). O número de sessões é
# decisão do LLM, até o que a regra de alternância permite.
RETOMADA_BLOCOS = 3


RETOMADA_BLOCO_SEG = 10 * 60


# Trote por bloco em cada estágio: 9/1, 8/2, 7/3, 6/4, 4/6, 2/8 min e, por
# fim, 30 min contínuos (600 = bloco inteiro de trote).
RETOMADA_ESTAGIOS_SEG = (60, 120, 180, 240, 360, 480, 600)


# Estágio cumprido = a maior sessão da semana teve pelo menos 90% do trote
# prescrito (folga pro erro de detecção corrida/caminhada do Garmin).
RETOMADA_CUMPRIDO_FRACAO = 0.9


# Referências do trote na retomada, do próprio corredor (não tabela genérica):
# pace = mediana do pace dos trechos de corrida (RWD_RUN) das sessões de
# Rodagem/Recuperação recentes, arredondada pra baixo em 15s, com 30s de
# folga pro lado lento; FC = teto de "Easy" do Daniels (78% da FC máxima já
# registrada). O pace vira alvo do trecho de trote no relógio; a FC fica só
# no plano (no trote de 1-2 min ela não chega a subir a tempo de avisar).
JANELA_REFERENCIA_TROTE_DIAS = 180


TROTE_MIN_SEG_REFERENCIA = 120  # sessão com menos trote que isso não serve de referência de pace


FOLGA_PACE_TROTE_MIN = 0.5


FC_TETO_FRACAO_MAX = 0.78


# Fim da retomada: alguma sessão com ~30 min de trote nos últimos 30 dias.
RETOMADA_FIM_TROTE_SEG = 0.95 * RETOMADA_BLOCOS * RETOMADA_BLOCO_SEG


# Regra de dor do protocolo: qualquer dor registrada volta um degrau.
LIMIAR_DOR_RETOMADA = 1


class SessaoRetomada(BaseModel):
    """Estrutura de cada sessão da semana em retomada (todas iguais) --
    calculada em Python, nunca pedida ao LLM."""
    blocos: int
    caminhada_seg: int  # por bloco
    trote_seg: int  # por bloco; 0 = sessão só de caminhada
    pace_trote: tuple[float, float] | None = None  # min/km decimais (rápido, lento)
    fc_teto: int | None = None

    def texto_pace(self) -> str | None:
        """'8:15-8:45 min/km no trote' -- o "no trote" faz a resolução do
        planejado x realizado ignorar o pace (o pace médio da sessão inclui
        a caminhada, ver planilha_desempenho._resolver_dia)."""
        if self.trote_seg == 0 or self.pace_trote is None:
            return None
        rapido, lento = self.pace_trote
        return f"{pace_txt(rapido)}-{pace_txt(lento)} min/km no trote"

    def texto_fc(self) -> str | None:
        return f"até {self.fc_teto} bpm" if self.fc_teto else None

    @property
    def duracao_min(self) -> float:
        return self.blocos * (self.caminhada_seg + self.trote_seg) / 60

    def descricao(self) -> str:
        if self.trote_seg == 0:
            return f"caminhada de {self.duracao_min:.0f} min, sem trote"
        if self.caminhada_seg == 0:
            return f"{self.duracao_min:.0f} min de trote contínuo"
        return f"{self.blocos}x (caminhe {mmss(self.caminhada_seg)} + trote {mmss(self.trote_seg)})"


def montar_sessao(trote_por_bloco: int) -> SessaoRetomada:
    total = RETOMADA_BLOCOS * RETOMADA_BLOCO_SEG
    if trote_por_bloco <= 0:
        return SessaoRetomada(blocos=1, caminhada_seg=total, trote_seg=0)
    if trote_por_bloco >= RETOMADA_BLOCO_SEG:
        return SessaoRetomada(blocos=1, caminhada_seg=0, trote_seg=total)
    return SessaoRetomada(
        blocos=RETOMADA_BLOCOS,
        caminhada_seg=RETOMADA_BLOCO_SEG - trote_por_bloco,
        trote_seg=trote_por_bloco,
    )


class Retomada(BaseModel):
    """Estágio da semana em retomada -- igual pra todas as sessões, seja
    quantas forem."""
    trote_bloco: int  # trote por bloco do estágio; 0 = só caminhada (voltou do 1º estágio)
    decisao: str  # por que o estágio subiu, repetiu ou voltou
    pace_trote: tuple[float, float] | None = None  # ver referencias_trote
    fc_teto: int | None = None

    def sessao(self) -> SessaoRetomada:
        return montar_sessao(self.trote_bloco).model_copy(update={"pace_trote": self.pace_trote, "fc_teto": self.fc_teto})


def referencias_trote(con: psycopg.Connection, hoje: date) -> tuple[tuple[float, float] | None, int | None]:
    """(faixa de pace do trote, teto de FC) a partir do histórico real --
    ver JANELA_REFERENCIA_TROTE_DIAS. None no que não houver dado."""
    _, linhas = consultar(
        con,
        "SELECT SUM(s.duracao_seg) / 60 / NULLIF(SUM(s.distancia_m) / 1000, 0) "
        "FROM stg_atividade_splits s JOIN vw_sessoes v USING (activity_id) "
        "WHERE s.tipo = 'RWD_RUN' AND v.tipo_treino = 'Rodagem/Recuperação' AND v.data >= %s "
        "GROUP BY s.activity_id HAVING SUM(s.duracao_seg) >= %s",
        (hoje - timedelta(days=JANELA_REFERENCIA_TROTE_DIAS), TROTE_MIN_SEG_REFERENCIA),
    )
    paces = sorted(float(l[0]) for l in linhas if l[0] is not None)
    pace = None
    if paces:
        mediana = paces[len(paces) // 2]
        rapido = int(mediana * 4) / 4  # arredonda pra baixo em 15s
        pace = (rapido, rapido + FOLGA_PACE_TROTE_MIN)
    _, linhas = consultar(con, "SELECT MAX(fc_maxima) FROM vw_sessoes WHERE data >= %s", (hoje - timedelta(days=365),))
    fc_max = linhas[0][0] if linhas else None
    return pace, (round(float(fc_max) * FC_TETO_FRACAO_MAX) if fc_max else None)


def _indice_estagio(trote_bloco: float) -> int:
    """Índice do maior estágio com trote <= trote_bloco (-1 se abaixo do 1º)."""
    return max((i for i, e in enumerate(RETOMADA_ESTAGIOS_SEG) if e <= trote_bloco), default=-1)


def trote_prescrito(spreadsheet: gspread.Spreadsheet, inicio_semana: date) -> int | None:
    """Trote por bloco prescrito na semana anterior, lido do motivo gravado
    na aba "Plano da Semana" ("3x (caminhe 9:00 + trote 1:00) ..." ou "30
    min de trote contínuo"). None se a semana anterior não teve sessão de
    retomada no plano."""
    datas = {str(inicio_semana - timedelta(days=i)) for i in range(1, 8)}
    aba = obter_aba(spreadsheet, ABA_PLANO, CABECALHO_PLANO, cor=COR_PLANO)
    idx_data, idx_motivo = CABECALHO_PLANO.index("data"), CABECALHO_PLANO.index("motivo")
    prescritos = []
    for linha in aba.get_all_values()[1:]:
        if len(linha) <= idx_motivo or linha[idx_data] not in datas:
            continue
        motivo = linha[idx_motivo]
        if casou := re.match(r"\d+x \(caminhe \d+:\d{2} \+ trote (\d+):(\d{2})\)", motivo):
            prescritos.append(int(casou.group(1)) * 60 + int(casou.group(2)))
        elif re.match(r"\d+ min de trote contínuo", motivo):
            prescritos.append(RETOMADA_BLOCO_SEG)
    return max(prescritos) if prescritos else None


def estado_retomada(sessoes: list[Sessao], inicio_semana: date, prescrito: int | None) -> Retomada | None:
    """None se o corredor já não está em retomada (fez sessão de ~30 min de
    trote nos últimos 30 dias). Senão, o estágio da semana a planejar, a
    partir do estágio prescrito na semana anterior (`prescrito`, ver
    trote_prescrito) -- não do trote detectado, que só confirma se ele foi
    cumprido (trotar além do prescrito não pula estágio):
      - qualquer dor registrada (>= 1) -> volta um estágio (regra do protocolo);
      - nenhuma sessão com trote -> repete o estágio (1º, se não havia);
      - estágio cumprido e todas as sessões com trote com dor 0 registrada -> sobe um;
      - senão (sessão sem dor registrada, ou trote abaixo do prescrito) -> repete.
    Sem plano anterior lido (`prescrito` None), usa o maior estágio que o
    trote detectado cumpre. Caminhada (tipo_treino) fica fora da conta de
    trote: o pouco de "trote" que o Garmin detecta nela não é sessão."""
    if any(float(s.trote_seg or 0) >= RETOMADA_FIM_TROTE_SEG for s in sessoes):
        return None

    semana = [s for s in sessoes if s.data >= inicio_semana - timedelta(days=7)]
    com_trote = [s for s in semana if s.tipo_treino != "Caminhada"]
    maior_bloco = max((float(s.trote_seg or 0) for s in com_trote), default=0.0) / RETOMADA_BLOCOS
    dores = [int(s.dor) for s in semana if s.dor is not None]

    if prescrito is not None:
        atual = _indice_estagio(prescrito)
        cumprido = maior_bloco >= RETOMADA_CUMPRIDO_FRACAO * prescrito
    else:
        atual = _indice_estagio(maior_bloco / RETOMADA_CUMPRIDO_FRACAO)
        cumprido = atual >= 0
    estagio = lambda i: RETOMADA_ESTAGIOS_SEG[min(max(i, 0), len(RETOMADA_ESTAGIOS_SEG) - 1)]
    txt = lambda seg: "30 min contínuos" if seg >= RETOMADA_BLOCO_SEG else f"trote {mmss(seg)} por bloco"

    if dores and max(dores) >= LIMIAR_DOR_RETOMADA:
        trote = estagio(atual - 1) if atual >= 1 else 0
        return Retomada(trote_bloco=trote, decisao=f"dor {max(dores)} registrada na semana anterior: o protocolo volta um estágio")
    if not com_trote:
        return Retomada(
            trote_bloco=estagio(atual),
            decisao="nenhuma sessão com trote na semana anterior: repete o estágio (o 1º, se ainda não havia)",
        )
    if cumprido and all(s.dor is not None for s in com_trote) and atual + 1 < len(RETOMADA_ESTAGIOS_SEG):
        return Retomada(
            trote_bloco=estagio(atual + 1),
            decisao=f"estágio anterior ({txt(estagio(atual))}) cumprido sem dor: sobe um estágio",
        )
    motivo = "trote abaixo do prescrito" if not cumprido else "alguma sessão com trote sem dor registrada (só sobe com dor 0 registrada em todas)"
    return Retomada(trote_bloco=estagio(atual), decisao=f"{motivo}: repete o estágio")


def instrucao_retomada(r: Retomada, max_sessoes: int) -> str:
    s = r.sessao()
    referencias = ", ".join(
        t for t in (s.texto_pace() and f"pace {s.texto_pace()}", s.texto_fc() and f"FC {s.texto_fc()}") if t
    ) or "sem histórico de trote suficiente"
    return (
        f"INSTRUÇÃO OBRIGATÓRIA (retomada pós-canelite -- protocolo de Warden et al., um "
        f"estágio por semana): o corredor ainda não sustenta 30 min de trote. Quantas sessões "
        f"fazer nesta semana é decisão sua, com base nos dados (de 1 a {max_sessoes}, em dias "
        f"alternados, nunca seguidos). Todas as sessões da semana são iguais: {s.descricao()} "
        f"({s.duracao_min:.0f} min). Estágio desta semana: {r.decisao}. Cada sessão é "
        f"Rodagem/Recuperação com duracao_min {s.duracao_min:.0f} e distancia_km nulo; "
        f"pace_alvo e fc_alvo são preenchidos depois, a partir do histórico dele ({referencias}). "
        f"O trote é bem leve, conversável. Nenhum outro tipo de treino (nem "
        f"Longão, nem intensidade). Explique no motivo que, se doer durante, depois ou no dia "
        f"seguinte, ele para, registra a dor e faz as sessões seguintes da semana só "
        f"caminhando. O ACWR da tabela não se aplica durante a retomada -- o controle de "
        f"carga é este protocolo."
    )


def aplicar_retomada(plano: "PlanoSemanal", r: Retomada) -> SessaoRetomada:
    """Fixa em Python a estrutura de cada sessão (o LLM escolhe quantas e
    em que dias, e escreve o motivo, não os números) -- vai igual pra
    planilha, e-mail e relógio."""
    sessao = r.sessao()
    for dia in plano.dias:
        if dia.descanso:
            continue
        dia.tipo_treino = "Rodagem/Recuperação"
        dia.distancia_km = None
        dia.duracao_min = sessao.duracao_min
        dia.pace_alvo = sessao.texto_pace()
        dia.fc_alvo = sessao.texto_fc()
        guia = ", ".join(t for t in (dia.pace_alvo, dia.fc_alvo and f"FC {dia.fc_alvo}") if t)
        dia.motivo = f"{sessao.descricao()}{f' ({guia})' if guia else ''} -- {dia.motivo}"
    return sessao
