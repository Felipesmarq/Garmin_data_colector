# Garmin_data_colector

Pipeline pessoal que coleta dados do Garmin Forerunner 165 e vai gerar,
toda semana, recomendações de treino via IA que marquem evolução — com
cuidado pra não repetir o padrão de sobrecarga que já causou canelite uma
vez. Contexto completo, arquitetura e roadmap estão em
[`CASE_DO_PROJETO_1.md`](CASE_DO_PROJETO_1.md).

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
passo manual de commit — seção 9 do [`CASE_DO_PROJETO_1.md`](CASE_DO_PROJETO_1.md))
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

## Estrutura do projeto

```
├── explore/                    # scripts de investigação (Fase 0, fora do pipeline final)
│   ├── inspect_garmin.py
│   ├── output/                 # JSON bruto de amostra (gitignored)
│   └── RELATORIO_FASE0.md
├── src/
│   ├── db.py                   # conexão Postgres (Neon) + aplica schema.sql
│   ├── planilha.py              # conexão Google Sheets + abas/linhas (mecânica genérica, sem regra de negócio)
│   ├── extract/garmin.py       # puxa atividades (+ splits run/walk) e recuperação diária da API
│   └── load/
│       ├── schema.sql          # DDL: staging -> vw_sessoes -> vw_sessoes_ia -> vw_resumo_semanal
│       ├── load_atividades.py  # upsert de atividades + splits
│       ├── load_recuperacao.py
│       ├── load_dor.py         # aba "Dor": exporta pendentes + importa preenchidas em stg_dor
│       └── planilha_desempenho.py  # abas "Atividades"/"Resumo Semanal": snapshot somente-leitura
├── Dockerfile · docker-compose.yml · .dockerignore  # só pra dev local, não usado em produção
├── requirements.txt · .env.example · .gitignore
└── CASE_DO_PROJETO_1.md        # case completo do projeto
```

## Status

Fases 0 a 5 implementadas e **validadas contra um banco Neon real**
(schema aplicado, idempotência de `load_atividades`/`load_recuperacao`
testada). Fase 6 (dor via planilha) implementada, aguardando setup da
credencial do Google Sheets pra ser testada ponta a ponta. Faltam: Fase 7
(análise com Gemini), Fase 8 (GitHub Actions), Fase 9 (ajuste fino).
Roadmap completo e critérios de aceite por fase na seção 7 do
[`CASE_DO_PROJETO_1.md`](CASE_DO_PROJETO_1.md).

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

## Dados sensíveis

`.env`, `.garmin_tokens/` e `.google_sheets_credentials.json` guardam
credenciais (incluindo `DATABASE_URL` do Neon e a chave da service
account do Google) e não vão pro Git (`.gitignore`). `explore/output/` contém dado
de saúde real de amostra, também gitignored. O próprio dado de treino/sono
vive só no Neon, fora do repositório — nada disso é commitado. O
repositório no GitHub deve ficar **privado** como camada extra de proteção
do código.
