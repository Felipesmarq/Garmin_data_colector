"""Retomada pós-canelite: protocolo de Warden (um estágio por semana), em
Python -- o LLM escolhe os dias e escreve o motivo, nunca os números (ver
CASE_DO_PROJETO_1.md seção 6.1, "Retomada")."""

import re
from datetime import date, timedelta
from typing import NamedTuple

import gspread
import psycopg
from pydantic import BaseModel

from src.analyze.entrega import ABA_PLANO, CABECALHO_PLANO, COR_PLANO
from src.analyze.modelos import PlanoSemanal, Sessao
from src.db import consultar
from src.formato import mmss, pace_txt
from src.planilha import obter_aba
from src.sintomas import com_dor_tibia, dor_tibia, sem_dor_confirmada

# Retomada pós-canelite -- protocolo de Warden et al. puro: 3 blocos de 10
# min de caminhada + trote, em dias alternados, subindo um estágio por
# semana (todas as sessões da semana no mesmo estágio). A regra dos 10%
# (Frandsen) só vale depois da retomada: é estudo de corredor saudável em
# treino regular, não de readaptação (decisão do usuário, 2026-10-13 -- ver
# CASE_DO_PROJETO_1.md seção 6.1, "Retomada").
RETOMADA_BLOCOS = 3
RETOMADA_BLOCO_SEG = 10 * 60
# Trote por bloco em cada estágio: 9/1, 8/2, 7/3, 6/4, 4/6, 2/8 min e, por
# fim, 30 min contínuos (600 = bloco inteiro de trote).
RETOMADA_ESTAGIOS_SEG = (60, 120, 180, 240, 360, 480, 600)
# Sessões de evolução por semana, fixas (decisão do usuário, 2026-10-14):
# deixar o número variar muda o volume dentro do mesmo estágio. A IA pode
# acrescentar caminhadas leves, que não contam como sessão de corrida.
RETOMADA_SESSOES = 3
TIPO_CAMINHADA = "Caminhada"
CAMINHADA_MAX_MIN = 45
# Sessão cumprida = pelo menos 90% do trote prescrito (folga pro erro de
# detecção corrida/caminhada do Garmin). Só sobe de estágio com
# RETOMADA_SESSOES sessões cumpridas na semana.
RETOMADA_CUMPRIDO_FRACAO = 0.9
# Até quantos dias pra trás procurar o último estágio prescrito no plano --
# cobre semanas suspensas por sinal de alerta.
JANELA_PRESCRITO_DIAS = 56
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
    """Estágio da semana em retomada -- igual pra todas as sessões de
    evolução da semana."""
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


class Prescricao(NamedTuple):
    trote_bloco: int  # trote por bloco do último estágio prescrito
    inicio_semana: date  # domingo da semana em que foi prescrito


def _indice_estagio(trote_bloco: float) -> int:
    """Índice do maior estágio com trote <= trote_bloco (-1 se abaixo do 1º)."""
    return max((i for i, e in enumerate(RETOMADA_ESTAGIOS_SEG) if e <= trote_bloco), default=-1)


def _estagio(i: int) -> int:
    return RETOMADA_ESTAGIOS_SEG[min(max(i, 0), len(RETOMADA_ESTAGIOS_SEG) - 1)]


def _txt_estagio(seg: int) -> str:
    return "30 min contínuos" if seg >= RETOMADA_BLOCO_SEG else f"trote {mmss(seg)} por bloco"


def trote_prescrito(spreadsheet: gspread.Spreadsheet, inicio_semana: date) -> Prescricao | None:
    """Último estágio de retomada prescrito antes da semana a planejar, lido
    do motivo gravado na aba "Plano da Semana" ("3x (caminhe 9:00 + trote
    1:00) ..." ou "30 min de trote contínuo"), até JANELA_PRESCRITO_DIAS pra
    trás -- semana suspensa por sinal de alerta não tem sessão, então vale
    a anterior a ela. None se não houver."""
    aba = obter_aba(spreadsheet, ABA_PLANO, CABECALHO_PLANO, cor=COR_PLANO)
    idx_data, idx_motivo = CABECALHO_PLANO.index("data"), CABECALHO_PLANO.index("motivo")
    corte = inicio_semana - timedelta(days=JANELA_PRESCRITO_DIAS)
    achados = []
    for linha in aba.get_all_values()[1:]:
        if len(linha) <= idx_motivo:
            continue
        try:
            data = date.fromisoformat(linha[idx_data])
        except ValueError:
            continue
        if not corte <= data < inicio_semana:
            continue
        motivo = linha[idx_motivo]
        if casou := re.match(r"\d+x \(caminhe \d+:\d{2} \+ trote (\d+):(\d{2})\)", motivo):
            achados.append((data, int(casou.group(1)) * 60 + int(casou.group(2))))
        elif re.match(r"\d+ min de trote contínuo", motivo):
            achados.append((data, RETOMADA_BLOCO_SEG))
    if not achados:
        return None
    data, trote = max(achados)
    return Prescricao(trote, data - timedelta(days=(data.weekday() + 1) % 7))


def estado_retomada(
    sessoes: list[Sessao], inicio_semana: date, prescrito: int | None, houve_alerta: bool = False
) -> Retomada | None:
    """None se o corredor já não está em retomada (fez sessão de ~30 min de
    trote nos últimos 30 dias). Senão, o estágio da semana a planejar, a
    partir do último estágio prescrito (`prescrito`, ver trote_prescrito) --
    não do trote detectado, que só confirma se cada sessão foi cumprida
    (trotar além do prescrito não pula estágio):
      - sinal de alerta desde o último plano -> volta um estágio;
      - dor na tíbia (>= 1, durante/depois, manhã seguinte ou palpação) em
        qualquer sessão da semana -> volta um estágio (regra do protocolo);
      - nenhuma sessão com trote -> repete o estágio (1º, se não havia);
      - RETOMADA_SESSOES sessões cumpridas, todas confirmadas sem dor (dor 0
        durante/depois e na manhã seguinte) -> sobe um;
      - senão -> repete.
    Sem plano anterior lido (`prescrito` None), usa o maior estágio que o
    trote detectado cumpre. Caminhada (tipo_treino) fica fora da conta de
    trote: o pouco de "trote" que o Garmin detecta nela não é sessão."""
    if any(float(s.trote_seg or 0) >= RETOMADA_FIM_TROTE_SEG for s in sessoes):
        return None

    semana = [s for s in sessoes if s.data >= inicio_semana - timedelta(days=7)]
    com_trote = [s for s in semana if s.tipo_treino != TIPO_CAMINHADA]
    if prescrito is not None:
        atual = _indice_estagio(prescrito)
    else:
        maior_bloco = max((float(s.trote_seg or 0) for s in com_trote), default=0.0) / RETOMADA_BLOCOS
        atual = _indice_estagio(maior_bloco / RETOMADA_CUMPRIDO_FRACAO)
    alvo = _estagio(atual) * RETOMADA_BLOCOS
    cumpridas = [s for s in com_trote if float(s.trote_seg or 0) >= RETOMADA_CUMPRIDO_FRACAO * alvo]
    voltar = _estagio(atual - 1) if atual >= 1 else 0

    if houve_alerta:
        return Retomada(trote_bloco=voltar, decisao="sinal de alerta registrado desde o último plano: recomeça um estágio abaixo")
    doloridas = [s for s in semana if com_dor_tibia(s)]
    if doloridas:
        pior = max(dor_tibia(s) for s in doloridas)
        return Retomada(trote_bloco=voltar, decisao=f"dor na tíbia nível {pior} registrada na semana anterior: o protocolo volta um estágio")
    if not com_trote:
        return Retomada(
            trote_bloco=_estagio(atual),
            decisao="nenhuma sessão com trote na semana anterior: repete o estágio (o 1º, se ainda não havia)",
        )
    confirmadas = all(sem_dor_confirmada(s) for s in com_trote)
    if len(cumpridas) >= RETOMADA_SESSOES and confirmadas and atual + 1 < len(RETOMADA_ESTAGIOS_SEG):
        return Retomada(
            trote_bloco=_estagio(atual + 1),
            decisao=f"estágio anterior ({_txt_estagio(_estagio(atual))}) cumprido nas {RETOMADA_SESSOES} sessões, sem dor: sobe um estágio",
        )
    if len(cumpridas) < RETOMADA_SESSOES:
        motivo = f"só {len(cumpridas)} de {RETOMADA_SESSOES} sessões cumpridas"
    else:
        motivo = "dor na tíbia (durante/depois e na manhã seguinte) não registrada em todas as sessões"
    return Retomada(trote_bloco=_estagio(atual), decisao=f"{motivo}: repete o estágio")


def instrucao_retomada(r: Retomada, n_sessoes: int, ultimo_dia: str) -> str:
    s = r.sessao()
    referencias = ", ".join(
        t for t in (s.texto_pace() and f"pace {s.texto_pace()}", s.texto_fc() and f"FC {s.texto_fc()}") if t
    ) or "sem histórico de trote suficiente"
    return (
        f"INSTRUÇÃO OBRIGATÓRIA (retomada pós-canelite -- protocolo de Warden et al., um "
        f"estágio por semana): o corredor ainda não sustenta 30 min de trote. A semana tem "
        f"exatamente {n_sessoes} sessões de evolução, em dias alternados (nunca seguidos) e "
        f"nunca no {ultimo_dia} (a dor na manhã seguinte precisa estar registrada antes do "
        f"plano da semana seguinte). Todas iguais: {s.descricao()} ({s.duracao_min:.0f} min). "
        f"Estágio desta semana: {r.decisao}. Cada sessão de evolução é Rodagem/Recuperação com "
        f"duracao_min {s.duracao_min:.0f} e distancia_km nulo; pace_alvo e fc_alvo são "
        f"preenchidos depois, a partir do histórico dele ({referencias}). O trote é bem leve, "
        f"conversável. Além delas, se fizer sentido pelos dados, pode incluir caminhadas leves "
        f"em outros dias (tipo {TIPO_CAMINHADA}, sem trote, duracao_min de até "
        f"{CAMINHADA_MAX_MIN}) -- caminhada não conta como dia de corrida. Nenhum outro tipo de "
        f"treino (nem Longão, nem intensidade). Explique no motivo das sessões de evolução que, "
        f"se a tíbia doer durante, depois ou na manhã seguinte, ele para, registra a dor e faz "
        f"as sessões seguintes da semana só caminhando. O ACWR da tabela não se aplica durante "
        f"a retomada -- o controle de carga é este protocolo."
    )


def aplicar_retomada(plano: "PlanoSemanal", r: Retomada) -> SessaoRetomada:
    """Fixa em Python a estrutura de cada sessão de evolução (o LLM escolhe
    os dias e escreve o motivo, não os números) -- vai igual pra planilha,
    e-mail e relógio. Caminhada extra fica com a duração da IA, limitada a
    CAMINHADA_MAX_MIN."""
    sessao = r.sessao()
    for dia in plano.dias:
        if dia.descanso:
            continue
        dia.distancia_km = None
        if (dia.tipo_treino or "").lower().startswith("caminhada"):
            dia.tipo_treino = TIPO_CAMINHADA
            dia.duracao_min = min(dia.duracao_min or CAMINHADA_MAX_MIN, CAMINHADA_MAX_MIN)
            dia.pace_alvo = None
            dia.fc_alvo = None
            continue
        dia.tipo_treino = "Rodagem/Recuperação"
        dia.duracao_min = sessao.duracao_min
        dia.pace_alvo = sessao.texto_pace()
        dia.fc_alvo = sessao.texto_fc()
        guia = ", ".join(t for t in (dia.pace_alvo, dia.fc_alvo and f"FC {dia.fc_alvo}") if t)
        dia.motivo = f"{sessao.descricao()}{f' ({guia})' if guia else ''} -- {dia.motivo}"
    return sessao
