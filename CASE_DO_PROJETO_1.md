# Case do Projeto — Pipeline Garmin → Análise com IA

## 1. Contexto

Felipe é um corredor construindo o hábito de correr depois de meses lidando com
canelite (síndrome do estresse tibial medial). O treino evoluiu com um plano de
retomada gradual (método run-walk, regra dos 10%, fortalecimento de tibial
anterior), registrado manualmente numa planilha com dados objetivos que faltavam:
o que o relógio Garmin Forerunner 165 já capta sozinho (distância, pace, FC,
sono, recuperação), mas que não estava sendo cruzado com o registro de dor.

## 2. Objetivo

**Funcional:** montar uma pipeline que colhe os dados do relógio automaticamente,
cruza com o registro manual de dor, e gera uma análise semanal via IA generativa
com recomendações concretas para os próximos treinos — priorizando não repetir o
padrão de sobrecarga que causou a canelite.

**De aprendizado:** aplicar, num projeto pessoal, os mesmos conceitos do projeto
acadêmico de ELT (staging → fato → view, DuckDB) e ganhar prática com automação
via CI/CD (GitHub Actions) e integração com uma API de LLM — coisas que reforçam
diretamente o que está sendo estudado na graduação.

## 3. Arquitetura geral

```
Relógio Garmin ──sync──▶ Garmin Connect
                              │
                     (polling periódico)
                              ▼
                        [ Extração ]  ── garminconnect (lib não-oficial)
                              │
                              ▼
                      [ Armazenamento ] ── DuckDB (arquivo versionado no repo)
                              │              + Planilha Excel (input manual de dor)
                              ▼
                     [ Transformação ] ── SQL: staging → fato → view semanal
                              │
                              ▼
                       [ Análise (IA) ] ── Anthropic API (Claude)
                              │
                              ▼
                       Relatório semanal
                              │
                              ▼
                    Você ajusta o treino ──┐
                              ▲             │
                              └─────────────┘ (vira dado da semana seguinte)
```

## 4. Stack tecnológico

| Camada | Tecnologia | Por quê |
|---|---|---|
| Linguagem | Python 3.11+ | Ecossistema maduro pras duas pontas (dados + API de IA) |
| Extração | `garminconnect` (PyPI) | Biblioteca open-source que autentica na conta Garmin e expõe atividades/bem-estar |
| Armazenamento | DuckDB | Mesma ferramenta do projeto de ELT da faculdade — SQL de verdade, arquivo único, sem servidor |
| Input manual | Excel (`openpyxl`) | Continua sendo a "tela" onde você anota dor/comentário — bom pra isso, ruim como banco de dados |
| Transformação | SQL (dentro do DuckDB) | Staging → fato → view, mesmo padrão do curso |
| Análise | Anthropic API (`anthropic` SDK) | Gera a análise em linguagem natural a partir dos dados tratados |
| Orquestração | GitHub Actions (2 workflows agendados) | Grátis, cron nativo, já versiona o código, Secrets pra credenciais |
| Versionamento | Git/GitHub (repositório **privado**) | O `.duckdb` commitado contém dados de saúde — não pode ser público |

## 5. Engenharia do projeto — estrutura de pastas

```
garmin-ia-pipeline/
├── .github/workflows/
│   ├── sync_atividades.yml     # roda a cada poucas horas: extrai corridas novas
│   └── analise_semanal.yml     # roda todo domingo: gera a análise da semana
├── src/
│   ├── db.py                   # conexão + helpers do DuckDB
│   ├── extract/garmin.py       # puxa atividades + sono/FC de repouso
│   ├── load/
│   │   ├── schema.sql          # DDL: stg_atividades, stg_dor, fct_sessoes, vw_resumo_semanal
│   │   ├── load_atividades.py  # INSERT incremental em stg_atividades
│   │   └── load_dor.py         # lê a planilha -> INSERT em stg_dor
│   ├── transform/build_fct_sessoes.py
│   └── analyze/analisar_com_ia.py
├── explore/                    # scripts de investigação (não fazem parte do pipeline final)
├── data/treino.duckdb          # banco versionado, commitado a cada run
├── planilha/planilha_corrida_canelite.xlsx
├── reports/analise_AAAA-MM-DD.md
├── requirements.txt · .env.example · .gitignore · README.md
```

## 6. Modelo de dados (staging → fato → view)

- **`stg_atividades`** — 1 linha por corrida: data, distância_km, duração_min, pace,
  fc_média, fc_máxima, cadência (campos exatos a confirmar na Fase 0/exploração)
- **`stg_dor`** — 1 linha por data anotada na planilha: dor (0-5), localização,
  comentário, superfície, tênis
- **`fct_sessoes`** — junção das duas por data: dado objetivo + subjetivo lado a lado
- **`vw_resumo_semanal`** — agregação por semana ISO: km total, tempo total, dor
  média, variação % de km vs. semana anterior (sinaliza a regra dos 10%)

## 7. Roadmap de implementação

| Fase | Entrega | Status |
|---|---|---|
| 0 | Explorar formato real dos dados do Garmin | 🔄 em andamento |
| 1 | `schema.sql` com base nos campos reais confirmados | ⏳ |
| 2 | `extract/garmin.py` — puxa atividades novas | ⏳ |
| 3 | `load/*.py` — carrega atividades + dor da planilha no DuckDB | ⏳ |
| 4 | `transform/build_fct_sessoes.py` — junção e view semanal | ⏳ |
| 5 | `analyze/analisar_com_ia.py` — prompt + chamada à API + relatório | ⏳ |
| 6 | Dois workflows de GitHub Actions + `.duckdb` versionado | ⏳ |
| 7 | Ajuste fino (frequência de polling, prompt, extras) | ⏳ |

## 8. Riscos e decisões a monitorar

- **Lib não-oficial do Garmin**: pode quebrar se o Garmin mudar algo no site.
  Mitigação: fixar versão no `requirements.txt`, atualizar sob demanda.
- **Sem webhook real**: "toda vez que sincroniza" na prática é polling
  periódico — não é instantâneo. Confirmado na prática (2026-07-28): a
  Garmin não oferece push/webhook pra contas de usuário comum (só via
  programa de parceiro aprovado) e não tem integração oficial com IFTTT
  pra isso, apesar de pedido há mais de 10 anos no fórum. **Decisão**:
  `sync_atividades.yml` (Fase 6) vai aceitar dois gatilhos — `schedule`
  (polling espaçado, ex. a cada 2-3h, como rede de segurança) e
  `repository_dispatch` (disparo manual instantâneo via atalho no
  celular logo depois que o relógio sincroniza com o app Garmin
  Connect, chamando a API do GitHub). Detalhar a implementação do
  atalho quando chegarmos na Fase 6.
- **MFA + login em CI headless**: o primeiro login deve ser feito localmente pra
  gerar o token de sessão; o GitHub Actions reaproveita esse token (guardado como
  Secret), não faz login interativo do zero.
- **Dados de saúde no repositório**: repositório **privado**, `.env` nunca commitado,
  `.duckdb` versionado com consciência de que contém dados sensíveis (FC, sono, dor).
- **Custo**: chamadas à API da Anthropic têm custo por token, mas baixo pro volume
  (1 análise por semana, prompt pequeno).

## 9. Critérios de sucesso

- Pipeline roda ponta a ponta sem intervenção manual (exceto anotar a dor)
- Relatório semanal chega automaticamente todo domingo
- Consegue manter constância de treino por 4+ semanas sem recidiva de dor ≥3
- Histórico fica consultável via SQL no DuckDB pra qualquer análise futura

## 10. Próximo passo imediato

Estamos na **Fase 0**: rodar `explore/inspect_garmin.py` pra ver os campos reais
que o Garmin devolve, e a partir disso fechar o `schema.sql` da Fase 1.
