# Garmin_data_colector

Pipeline pessoal que coleta dados do Garmin Forerunner 165 e vai gerar,
toda semana, recomendações de treino via IA que marquem evolução — com
cuidado pra não repetir o padrão de sobrecarga que já causou canelite uma
vez. Documentação completa (contexto, arquitetura, roadmap e entendimento
do negócio) está em [`docs/`](docs/index.md).

Tudo roda em Docker — não precisa instalar Python nem nenhuma
dependência na sua máquina.

## Pré-requisitos

- Docker + Docker Compose (opcional — só pra rodar/testar local sem instalar Python; produção roda em GitHub Actions, sem Docker)
- Uma conta gratuita no [Neon](https://neon.tech) (Postgres gerenciado, sem cartão) — o banco vive lá, não no repositório

## Configuração inicial

```bash
cp .env.example .env
docker compose build
```

Abra o `.env` e preencha `GARMIN_EMAIL` e `DATABASE_URL` (string de conexão
do Neon — pegue em console.neon.tech, aba "Connect", com `sslmode=require`).
Pode deixar `GARMIN_PASSWORD` em branco — nesse caso o script pede a senha
no terminal a cada execução (via `getpass`, não aparece na tela).

No primeiro login bem-sucedido, o token de sessão fica salvo em
`.garmin_tokens/` (gitignored) e é reaproveitado nas execuções seguintes
— você não precisa digitar a senha de novo, só se o token expirar.

## Como rodar cada fase

### Fase 0 — explorar a API (`explore/inspect_garmin.py`)

```bash
docker compose run --rm garmin
```

Autentica, puxa uma amostra de atividades/sono/FC/recuperação e salva o
JSON bruto em `explore/output/` (gitignored — contém dado de saúde real).
Achados documentados em [`explore/RELATORIO_FASE0.md`](explore/RELATORIO_FASE0.md).

### Fase 1 — schema do banco (`src/load/schema.sql`)

Não precisa rodar manualmente: `src/db.py` aplica o schema automaticamente
toda vez que qualquer script de load/extração abre a conexão. O banco é o
Postgres do Neon apontado por `DATABASE_URL` — nenhum arquivo de banco é
criado ou versionado localmente.

### Fase 2 — extrair dados (`src/extract/garmin.py`)

Teste manual (imprime no terminal, não grava no banco):

```bash
docker compose run --rm garmin python -m src.extract.garmin
```

### Fase 3 — carregar no Postgres (`src/load/*.py`)

```bash
docker compose run --rm garmin python -m src.load.load_atividades
docker compose run --rm garmin python -m src.load.load_recuperacao
```

Ambos são idempotentes: rodar de novo atualiza as linhas existentes
(por `activity_id` / `data`) em vez de duplicar — pode rodar quantas
vezes quiser.

### Fase 6 — planilha (Google Sheets): dor + dashboard de desempenho

Substitui a ideia original de Excel local (inviável de sincronizar sem
passo manual de commit — seção 9 do [`docs/CASE_DO_PROJETO_1.md`](docs/CASE_DO_PROJETO_1.md))
por Google Sheets, acessível do celular. Três abas na mesma planilha:
**Dor** (input manual, `load_dor.py`), **Atividades** e **Resumo Semanal**
(dashboard somente-leitura, `planilha_desempenho.py`).

**Setup único (console.cloud.google.com):**
1. Crie/reaproveite um projeto no Google Cloud.
2. **APIs e serviços → Biblioteca** → busque "Google Sheets API" → **Ativar**.
3. **APIs e serviços → Credenciais** → **Criar credenciais** → **Conta de
   serviço**. Dê um nome (ex. `garmin-dor-sheets`).
4. Na conta de serviço, aba **Chaves** → **Adicionar chave** → **Criar
   nova chave** → tipo **JSON**. Salve o arquivo baixado como
   `.google_sheets_credentials.json` na raiz do projeto (gitignored).
5. Crie a planilha no Google Sheets. Copie o e-mail da service account
   (campo `client_email` no JSON) e **compartilhe a planilha** com esse
   e-mail, permissão **Editor**.
6. Preencha `GOOGLE_SHEETS_CREDENTIALS_PATH` e `GOOGLE_SHEET_ID` (o ID é a
   string na URL entre `/d/` e `/edit`) no `.env`.

**Uso:**

```bash
docker compose run --rm garmin python -m src.load.load_dor
docker compose run --rm garmin python -m src.load.planilha_desempenho
```

`load_dor` roda em duas direções na mesma execução: primeiro exporta pra
aba "Dor" as atividades que ainda não têm dor registrada (`activity_id`,
`data`, `nome`, `tipo` — colunas de dor ficam em branco pra você
preencher); depois importa de volta pra `stg_dor` (upsert por
`activity_id`) as linhas que já têm a coluna `dor` preenchida.

`planilha_desempenho` sobrescreve as abas "Atividades" (snapshot de
`vw_sessoes`: pace, velocidade, FC, efeito de treino) e "Resumo Semanal"
(snapshot de `vw_resumo_semanal`: km, variação %, ACWR) a cada execução —
não acumula histórico duplicado, sempre reflete o estado atual do banco.

### Fase 7 — análise semanal com IA (`analyze/analisar_com_ia.py`)

Gera o plano de treino da próxima semana via Gemini a partir de
`vw_sessoes_ia` + `vw_resumo_semanal` + a aba "Observações" (texto livre,
ver abaixo), com dois guardrails determinísticos (ACWR e dor recente) e a
regra 80/20 injetados no prompt — ver `docs/CASE_DO_PROJETO_1.md` seção 6.1.
Precisa de `GEMINI_API_KEY` (gere em
[aistudio.google.com/apikey](https://aistudio.google.com/apikey)) e das
variáveis de e-mail (`EMAIL_REMETENTE`, `EMAIL_SENHA_APP` — senha de app
do Gmail, gere em
[myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords),
exige verificação em 2 etapas ativada) no `.env`.

```bash
docker compose run --rm garmin python -m src.analyze.analisar_com_ia
```

Imprime o plano no terminal, escreve na aba "Plano da Semana" da mesma
planilha do Fase 6 -- **append-only**: cada semana gerada é um registro
novo (idempotente por data da segunda-feira, 1 linha por dia, não
sobrescreve o histórico) -- e manda o plano completo por e-mail
(`EMAIL_DESTINATARIO`, opcional -- por padrão manda pro próprio
`EMAIL_REMETENTE`).

Estimativas pra tipos de treino ainda não tentados (ex. Intervalado, Tiro)
e a frase-resumo da semana **não vão pra planilha** (não são "1 linha por
dia", não fazem sentido no histórico tabular) -- ficam só no terminal e
no e-mail.

**Aba "Observações"**: texto livre, 100% preenchido por você (o script só
lê, nunca escreve) -- colunas `semana_inicio` e `observacao`. Contexto que
os números não capturam (viagem, imprevisto, como a semana realmente foi)
entra no prompt da próxima geração, mesma janela de 4 semanas dos outros
dados.

### Fase 8 — automação (GitHub Actions)

Dois workflows em `.github/workflows/`, sem Docker (roda com
`actions/setup-python`, Python 3.12 direto no runner):

- **`sync_atividades.yml`** — todo dia, 21h BRT (`0 0 * * *` UTC): carrega
  atividades, recuperação, sincroniza dor e atualiza o dashboard da
  planilha.
- **`analise_semanal.yml`** — domingo, 20h BRT (`0 23 * * 0` UTC): resincroniza
  atividades/recuperação/dor (não depende do horário do sync diário ter
  rodado antes) e gera o plano da semana.

Cada passo tenta de novo (até 3x) antes de falhar de vez -- só aí o
GitHub manda o e-mail padrão de workflow agendado que falhou. Os dois
também têm `workflow_dispatch` (botão "Run workflow" na aba Actions do
GitHub, ou `gh workflow run <nome>.yml`), pra rodar manualmente sem
esperar o horário.

**Setup dos Secrets** (Settings → Secrets and variables → Actions no
GitHub, ou via `gh` CLI autenticado -- roda isso localmente, na raiz do
projeto, com o `.env` já preenchido):

```bash
gh secret set GARMIN_EMAIL --body "$(grep '^GARMIN_EMAIL=' .env | cut -d= -f2-)"
gh secret set GARMIN_PASSWORD --body "$(grep '^GARMIN_PASSWORD=' .env | cut -d= -f2-)"
gh secret set DATABASE_URL --body "$(grep '^DATABASE_URL=' .env | cut -d= -f2-)"
gh secret set GOOGLE_SHEET_ID --body "$(grep '^GOOGLE_SHEET_ID=' .env | cut -d= -f2-)"
gh secret set GEMINI_API_KEY --body "$(grep '^GEMINI_API_KEY=' .env | cut -d= -f2-)"
gh secret set GEMINI_MODEL --body "$(grep '^GEMINI_MODEL=' .env | cut -d= -f2- | tr -d '\"')"
gh secret set EMAIL_REMETENTE --body "$(grep '^EMAIL_REMETENTE=' .env | cut -d= -f2-)"
gh secret set EMAIL_SENHA_APP --body "$(grep '^EMAIL_SENHA_APP=' .env | cut -d= -f2-)"
gh secret set GARMIN_TOKEN < .garmin_tokens/garmin_tokens.json
gh secret set GOOGLE_SHEETS_CREDENTIALS_JSON < .google_sheets_credentials.json
```

`EMAIL_DESTINATARIO` é opcional (`gh secret set EMAIL_DESTINATARIO --body "..."`)
-- sem ele, o plano é enviado pro próprio `EMAIL_REMETENTE`.

`GARMIN_TOKEN` é o conteúdo do token de sessão já autenticado localmente
(dura ~1 ano, se renova sozinho sem senha/MFA -- ver seção 6.1 do case).
`GARMIN_PASSWORD` é só um fallback: se o token falhar em produção, sem
terminal interativo o workflow falha rápido com uma mensagem clara em vez
de travar esperando input (`src/extract/garmin.py`, `conectar()`) --
nesse caso, refaça o login local e rode o bloco de `gh secret set`
de novo pra atualizar o `GARMIN_TOKEN`.

### Fase 9 — verificação de aderência (plano vs. treino real)

Sem comando próprio -- roda dentro do fluxo já existente. Todo domingo,
`analisar_com_ia.py` grava um placeholder por dia de treino da semana
(status `PLANEJADO`) na aba "Atividades", ao lado das atividades reais.
Todo dia, `planilha_desempenho.py` resolve os dias planejados cuja data já
chegou -- comparando contra `vw_sessoes` com margem de tolerância (±20%
distância/duração, ±30s/km pace, ±10bpm FC) -- e atualiza o status pra
`REALIZADO_DENTRO_DA_MARGEM`, `REALIZADO_FORA_DA_MARGEM` ou
`NÃO_REALIZADO`. Quando o dia anterior fica com um desses dois últimos
status, um e-mail curto de lembrete é enviado (mesmo mecanismo do e-mail
semanal, `EMAIL_REMETENTE`/`EMAIL_SENHA_APP`). Puramente informativo por
enquanto -- não influencia a geração do plano seguinte.

## Estrutura do projeto

```
├── .github/workflows/
│   ├── sync_atividades.yml     # diário, 21h BRT -- atividades + recuperação + dor + dashboard
│   └── analise_semanal.yml     # domingo, 20h BRT -- resync + plano da semana via Gemini
├── explore/                    # scripts de investigação (Fase 0, fora do pipeline final)
│   ├── inspect_garmin.py
│   ├── output/                 # JSON bruto de amostra (gitignored)
│   └── RELATORIO_FASE0.md
├── src/
│   ├── db.py                   # conexão Postgres (Neon) + aplica schema.sql
│   ├── planilha.py              # conexão Google Sheets + abas/linhas (mecânica genérica, sem regra de negócio)
│   ├── email_util.py           # envio de e-mail via Gmail SMTP (mecânica genérica)
│   ├── extract/garmin.py       # puxa atividades (+ splits run/walk) e recuperação diária da API
│   ├── load/
│   │   ├── schema.sql          # DDL: staging -> vw_sessoes -> vw_sessoes_ia -> vw_resumo_semanal
│   │   ├── load_atividades.py  # upsert de atividades + splits
│   │   ├── load_recuperacao.py
│   │   ├── load_dor.py         # aba "Dor": exporta pendentes + importa preenchidas em stg_dor
│   │   └── planilha_desempenho.py  # "Atividades" (upsert) + "Resumo Semanal" (snapshot) + verificação de aderência (Fase 9)
│   └── analyze/analisar_com_ia.py  # plano semanal via Gemini + guardrails, aba "Plano da Semana"
├── Dockerfile · docker-compose.yml · .dockerignore  # só pra dev local, não usado em produção
├── requirements.txt · .env.example · .gitignore
└── docs/
    ├── index.md                 # página de documentação, com link pra tudo abaixo
    ├── CASE_DO_PROJETO_1.md     # case completo do projeto
    ├── ENTENDIMENTO_DO_NEGOCIO.md   # entendimento do negócio (CRISP-DM)
    └── ENTENDIMENTO_DO_NEGOCIO.pdf  # mesmo documento, em PDF
```

## Status

Fases 0 a 7 implementadas e **validadas contra o Neon e o Gemini reais**
(schema aplicado, idempotência dos loaders testada, plano semanal gerado
e escrito na planilha ponta a ponta, guardrails de ACWR/dor confirmados
disparando). Fase 8 (GitHub Actions) com os workflows escritos e
validados sintaticamente, pendente de: você configurar os Secrets
(comandos acima) e deixar rodar pelo menos 2 semanas sem intervenção
manual pra fechar o critério de aceite. Fase 9 (verificação de aderência)
implementada e **validada contra o Neon e a planilha reais** (comparação
com margem, soma de múltiplas atividades no dia, upsert idempotente,
mecanismo de e-mail de lembrete testado). Fases 10 e 11 (filtro de
tokens, revisão de coerência) desenhadas em sessão de grilling, ainda não
implementadas. Falta: Fase 12 (ajuste fino). Roadmap completo e critérios
de aceite por fase na seção 7 do
[`docs/CASE_DO_PROJETO_1.md`](docs/CASE_DO_PROJETO_1.md).

## Solução de problemas

- **Build falha com "requires a different python version"**: o
  Dockerfile já usa `python:3.12-slim` (a lib `garminconnect` exige
  Python ≥3.12 a partir da versão 0.3.3). Se você alterar a imagem base,
  o build volta a falhar.
- **"Senha incorreta" repetido**: a Garmin aplica rate limit e pode
  bloquear a conta temporariamente após várias tentativas seguidas. O
  script para sozinho depois de 5 tentativas — se isso acontecer, espere
  um pouco ou use "Esqueci minha senha" no app da Garmin.
- **Perdeu o login salvo**: apague `.garmin_tokens/` e rode de novo — ele
  pede email/senha e recria o token.
- **Workflow do GitHub Actions falha com "Token da Garmin inválido/expirado"**:
  o `GARMIN_TOKEN` (Secret) expirou ou foi revogado. Rode `docker compose
  run --rm garmin python -m src.extract.garmin` localmente pra reautenticar
  (recria `.garmin_tokens/garmin_tokens.json`), depois rode de novo o bloco
  de `gh secret set` (seção Fase 8) pra atualizar o Secret.

## Dados sensíveis

`.env`, `.garmin_tokens/` e `.google_sheets_credentials.json` guardam
credenciais (incluindo `DATABASE_URL` do Neon e a chave da service
account do Google) e não vão pro Git (`.gitignore`). `explore/output/` contém dado
de saúde real de amostra, também gitignored. O próprio dado de treino/sono
vive só no Neon, fora do repositório — nada disso é commitado. O
repositório no GitHub deve ficar **privado** como camada extra de proteção
do código.
