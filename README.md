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

`load_dor.py` (carregaria a planilha de dor manual) foi adiado pra
feature futura — uma planilha Excel local não é acessível pro GitHub
Actions sem um passo manual de commit/push a cada anotação. Detalhes da
decisão na seção 8 do [`CASE_DO_PROJETO_1.md`](CASE_DO_PROJETO_1.md). Por
ora o pipeline segue só com dado objetivo do Garmin (atividades +
recuperação).

## Estrutura do projeto

```
├── explore/                    # scripts de investigação (Fase 0, fora do pipeline final)
│   ├── inspect_garmin.py
│   ├── output/                 # JSON bruto de amostra (gitignored)
│   └── RELATORIO_FASE0.md
├── src/
│   ├── db.py                   # conexão Postgres (Neon) + aplica schema.sql
│   ├── extract/garmin.py       # puxa atividades (+ splits run/walk) e recuperação diária da API
│   └── load/
│       ├── schema.sql          # DDL: staging -> vw_sessoes -> vw_sessoes_ia -> vw_resumo_semanal
│       ├── load_atividades.py  # upsert de atividades + splits
│       └── load_recuperacao.py
├── Dockerfile · docker-compose.yml · .dockerignore  # só pra dev local, não usado em produção
├── requirements.txt · .env.example · .gitignore
└── CASE_DO_PROJETO_1.md        # case completo do projeto
```

## Status

Fases 0 a 5 implementadas em código (infraestrutura Postgres/Neon,
extração e load com splits run/walk e campos de performance, `vw_sessoes`
e `vw_sessoes_ia` já no `schema.sql`) — falta só validar contra um banco
Neon real (nenhum projeto foi provisionado ainda). Faltam: Fase 6 (análise
com Gemini), Fase 7 (GitHub Actions). Roadmap completo e critérios de
aceite por fase na seção 7 do
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

`.env` e `.garmin_tokens/` guardam credenciais (incluindo `DATABASE_URL`
do Neon) e não vão pro Git (`.gitignore`). `explore/output/` contém dado
de saúde real de amostra, também gitignored. O próprio dado de treino/sono
vive só no Neon, fora do repositório — nada disso é commitado. O
repositório no GitHub deve ficar **privado** como camada extra de proteção
do código.
