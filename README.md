# Estação meteorológica EE São José

Aplicação Flask em produção para coletar dados públicos da Ambient Weather, preservar histórico em SQLite, mostrar condições e previsões e distribuir alertas pelo WhatsApp via Evolution API.

## Regras meteorológicas preservadas

Os limites oficiais de chuva são:

```text
CHUVA NÍVEL 1 = 30 mm
CHUVA NÍVEL 2 = 50 mm
```

Os demais padrões continuam configuráveis sem alterar esses contratos. A indicação visual de possível tempestade é independente dos alertas de WhatsApp: usa `chuva_rate` atual e rajada atual/recente na mesma janela temporal, com padrões conservadores de 10 mm/h, 60 km/h e 10 minutos.

## Arquitetura

```text
Ambient Weather -> updater -> SQLite -> Flask/APIs/páginas
                                     -> fila -> whatsapp_sender -> Evolution API
Open-Meteo ------------------------------> /previsao (somente previsão)
```

O acesso a `/previsao` nunca coleta uma nova leitura oficial nem grava em `leituras_brutas`; a página usa a última leitura persistida pelo updater. Os processos principais são:

- `app.py`: Flask, páginas, APIs, administração e webhooks;
- `workers/updater.py`: coleta, deduplicação, histórico, acumulados e motor de alertas;
- `workers/whatsapp_sender.py`: claim transacional e envio at-least-once da fila;
- `workers/health_check.py`: diagnóstico interno e notificação operacional;
- `workers/maintenance.py`: auditoria, índices e retenção manual;
- `workers/backup_db.py`: backup consistente via API do SQLite.

O estado de alertas mantém a precedência `SQLite -> alert_state.json legado -> default`.

### Topologia pública confiável

```text
Internet
  ↓
Cloudflare
  ↓
cloudflared
  ↓
Nginx
  ↓
Gunicorn
  ↓
Flask
```

Nessa topologia, o Gunicorn permanece em endereço local e não recebe conexões diretas da internet. Use `TRUST_PROXY=true` somente quando esse caminho estiver garantido e o proxy confiável sobrescrever os cabeçalhos `X-Forwarded-*`; caso contrário, mantenha `false` para impedir que o cliente forje esquema, host ou IP.

## Instalação

Requer Python 3.11 ou 3.12 em produção. Python 3.13 também é exercitado localmente, mas confirme a versão do servidor antes de mudá-la.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r estacao/requirements.txt
```

No PowerShell, ative com `./.venv/Scripts/Activate.ps1`.

## Configuração

Use variáveis do processo ou `.env` não versionado. Nenhum segredo real deve entrar no Git.

### Web, sessão e proxy

| Variável | Default | Obrigatória? | Finalidade |
|---|---:|---|---|
| `APP_ENV` | `development` | não | em `production`, habilita validação rígida |
| `SECRET_KEY` | chave somente de desenvolvimento | sim em produção | sessões e fallback de tokens |
| `ADMIN_PASSWORD_HASH` | vazio | uma senha em produção | hash bcrypt preferido |
| `ADMIN_PASSWORD` | vazio | uma senha em produção | fallback de senha administrativa |
| `WEBHOOK_SECRET` | vazio | sim em produção | HMAC SHA-256 dos webhooks |
| `PUBLIC_BASE_URL` | fallback HTTP apenas em desenvolvimento | sim em produção | origem pública absoluta dos links enviados |
| `SESSION_COOKIE_SECURE` | `false` | não | defina `true` somente com HTTPS |
| `SESSION_TIMEOUT_MINUTES` | `30` | não | expiração administrativa |
| `TRUST_PROXY` | `false` | não | confia em um salto de proxy reverso |
| `HSTS_ENABLED` | `false` | não | HSTS; habilite apenas após HTTPS validado |
| `LOG_LEVEL` | `INFO` | não | nível de logging |

Em produção, configure explicitamente `PUBLIC_BASE_URL=https://SEU_DOMINIO` depois de validar o certificado e todo o caminho HTTPS. A aplicação recusa iniciar sem essa variável. Configure também `SESSION_COOKIE_SECURE=true`; enquanto ela permanecer `false`, o startup registra um aviso, sem impedir uma transição operacional controlada.

Ative `TRUST_PROXY=true` somente quando a aplicação aceitar tráfego exclusivamente da cadeia confiável acima, que deve sobrescrever os cabeçalhos encaminhados. Não exponha o Gunicorn diretamente à internet nessa configuração.

### Banco e health

| Variável | Default | Obrigatória? | Finalidade |
|---|---:|---|---|
| `ESTACAO_DB` | `estacao/estacao.db` | não | caminho compartilhado do SQLite |
| `HEALTH_MAX_READING_AGE_SECONDS` | `300` | não | idade crítica da última leitura no `/health` |
| `ADMIN_UPDATER_ATRASO_MINUTOS` | `5` | não | painel administrativo |

### Rate limiter

| Variável | Default | Obrigatória? | Finalidade |
|---|---:|---|---|
| `RATELIMIT_ENABLED` | `true` | não | liga o limiter |
| `RATELIMIT_STORAGE_URI` | `memory://` | não | backend; use Redis se precisar de limite global |
| `RATELIMIT_KEY_PREFIX` | `estacao` | não | namespace das chaves |
| `PUBLIC_CADASTRO_RATE_LIMIT` | `60 per hour` | não | cadastro público |
| `SIGNUP_RESEND_RATE_LIMIT` | `5 per hour` | não | reenvios de confirmação pendente por IP |

`memory://` é adequado ao desenvolvimento. Em múltiplos workers, cada processo tem seu próprio contador; produção em escala deve apontar para Redis já administrado, sem tornar Redis requisito do projeto.

### Meteorologia

| Variável | Default |
|---|---:|
| `AMBIENT_PUBLIC_SLUG` | fallback público compatível |
| `FORECAST_CITY` | `Vicentina` |
| `FORECAST_STATE` | `Mato Grosso do Sul` |
| `FORECAST_COUNTRY` | `Brasil` |
| `FORECAST_LABEL` | `Distrito de São José, Vicentina/MS` |
| `FORECAST_LAT` / `FORECAST_LON` | vazio |
| `TEMPESTADE_CHUVA_RATE_MIN` | `10` mm/h |
| `TEMPESTADE_RAJADA_MIN` | `60` km/h |
| `TEMPESTADE_JANELA_MINUTOS` | `10` |
| `ALERTA_CHUVA_NIVEL_1` | `30` mm |
| `ALERTA_CHUVA_NIVEL_2` | `50` mm |

Também permanecem os limites `ALERTA_CALOR_*`, `ALERTA_FRIO_*`, `ALERTA_VENTO_*`, `ALERTA_UMIDADE_*` e `ALERTA_CONFIRMACOES_NIVEL_1` já usados pelo updater.

### WhatsApp e double opt-in

| Variável | Default | Obrigatória? | Finalidade |
|---|---:|---|---|
| `EVOLUTION_URL` | vazio | somente para envio | API Evolution |
| `EVOLUTION_API_KEY` | vazio | somente para envio | autenticação |
| `EVOLUTION_INSTANCE` | vazio | somente para envio | instância |
| `SIGNUP_CONFIRM_SECRET` | `SECRET_KEY` | não | segredo separado de confirmação |
| `SIGNUP_CONFIRM_TOKEN_MAX_AGE_HOURS` | `24` | não | validade do opt-in |
| `UNSUBSCRIBE_SECRET` | `SECRET_KEY` | não | segredo separado de cancelamento |
| `UNSUBSCRIBE_TOKEN_MAX_AGE_DAYS` | `90` | não | validade do cancelamento |
| `WHATSAPP_WORKERS` | `3` | não | consumidores concorrentes |
| `WHATSAPP_MAX_TENTATIVAS` | `4` | não | máximo de tentativas |

Novos cadastros que marcam WhatsApp entram como `pendente` e `receber_whatsapp=0`. O link assinado `/signup/confirm?token=...` torna o cadastro ativo de modo idempotente. Usuários legados permanecem ativos. O cancelamento mantém token assinado e confirmação em duas etapas.

Cada processo valida apenas a configuração que utiliza; importar a aplicação não exige Evolution configurada. Telefones e nomes são mascarados nos logs e detalhes de erro externos são limitados/sanitizados.

A fila mantém semântica at-least-once. Se a Evolution aceitar uma mensagem e a conexão cair antes da resposta, o sender não consegue provar a aceitação e uma retentativa pode duplicar o envio. Eliminar completamente essa ambiguidade depende de uma chave de idempotência aceita pelo provedor.

## Banco e migrations

O SQLite usa WAL, `synchronous=FULL`, `busy_timeout` e foreign keys. O schema atual é a versão `2`, registrada na pequena tabela `schema_version`.

Execute a migration leve explicitamente antes de reiniciar os serviços:

```bash
cd estacao
python init_db.py
```

As migrations automáticas são aditivas e idempotentes:

- criação de `schema_version`;
- adição de `usuarios.status_cadastro TEXT DEFAULT 'ativo'`;
- adição de `usuarios.confirmado_em TEXT`;
- criação de tabelas ausentes já suportadas pelo projeto.

Não há `DROP`, reconstrução, exclusão, `VACUUM` ou backfill massivo. O default `ativo` preserva o comportamento de linhas antigas sem executar `UPDATE` sobre a tabela. Um rollback de código pode ignorar as colunas e a tabela adicionais.

### Deduplicação

Para timestamps não nulos, novas leituras consultam pontualmente a chave lógica `(origem, station_timestamp_ms)` dentro de `BEGIN IMMEDIATE`. Se já existir, o ID é reutilizado e o updater não cria histórico/processamento duplicado. Duplicidades históricas não são apagadas nem modificadas.

Antes de habilitar a deduplicação em escala, audite as duplicidades em uma janela controlada. A auditoria é manual e pode ser pesada:

```bash
python -m estacao.workers.maintenance --audit-duplicates
```

O mesmo comando no formato legado, executado a partir do diretório `estacao`, é:

```bash
cd estacao
python workers/maintenance.py --audit-duplicates
```

### Índices

Bancos novos recebem índices durante a criação, quando as tabelas estão vazias. Em banco existente, índices de tabelas potencialmente grandes não são criados no startup. Para criar/verificar o índice composto de deduplicação e índices operacionais, agende uma janela separada:

```bash
python -m estacao.workers.maintenance --create-indexes
```

Esse comando pode manter lock de escrita enquanto o SQLite constrói cada índice. Não o inclua no deploy normal.

Formato legado, a partir do diretório `estacao`:

```bash
cd estacao
python workers/maintenance.py --create-indexes
```

Em banco de produção existente, primeiro execute a auditoria e revise o resultado; só depois crie os índices em uma janela controlada. Nenhum desses comandos é executado automaticamente pelo startup ou pelo deploy.

## Backup consistente

Com WAL ativo, não copie apenas o arquivo `.db` durante escrita. Antes do deploy, use um destino novo em diretório já existente:

```bash
python -m estacao.workers.backup_db /var/backups/estacao/estacao-2026-08-21.db
```

O comando usa `sqlite3.Connection.backup()`, valida `integrity_check`, cria o arquivo com permissão restrita e falha sem sobrescrever um backup existente. Ele não roda automaticamente.

## Integrity check, contagens e manutenção

```bash
python -m estacao.workers.maintenance --integrity-check
python -m estacao.workers.maintenance --counts
```

O resultado saudável do primeiro é `ok`. Contagens e auditorias existem apenas no CLI, nunca em requests recorrentes.

### Retenção opt-in

A retenção é desativada por padrão. Primeiro simule:

```bash
python -m estacao.workers.maintenance --dry-run
```

Variáveis:

| Variável | Default |
|---|---:|
| `RETENCAO_AUTOMATICA` | `false` |
| `RETENCAO_LEITURAS_BRUTAS_DIAS` | `365` |
| `RETENCAO_LOGS_DIAS` | `90` |
| `RETENCAO_ALERTAS_ENVIOS_DIAS` | `365` |
| `RETENCAO_CADASTRO_EVENTOS_DIAS` | `730` |
| `RETENCAO_DELETE_BATCH_SIZE` | `1000` |

Uma exclusão requer simultaneamente a variável de opt-in e o comando explícito:

```bash
RETENCAO_AUTOMATICA=true python -m estacao.workers.maintenance --cleanup --batch-size 1000
```

Cada lote faz `COMMIT`; a operação pode ser interrompida e retomada. Não há `VACUUM` posterior. `historico_clima`, `historico_diario`, `usuarios`, fila pendente, estado e acumulados não fazem parte da limpeza configurada.

## Execução

Da raiz, os entrypoints por módulo são:

```bash
python -m estacao.workers.updater
python -m estacao.workers.whatsapp_sender
python -m estacao.workers.health_check --no-whatsapp --fail-on-issues
```

Os comandos legados continuam compatíveis:

```bash
cd estacao
python app.py
python workers/updater.py
python workers/whatsapp_sender.py
```

Gunicorn continua usando:

```bash
cd estacao
gunicorn -w 2 -b 127.0.0.1:8080 app:app
```

## Rotas e saúde

As rotas existentes foram preservadas, incluindo páginas, APIs, administração, cancelamento e `/deploy/python` e `/deploy/php`. Foram adicionadas:

- `GET /signup/confirm`: confirmação do double opt-in;
- `GET /health`: status externo sem PII/segredos.

`/health` retorna `200` para banco e leitura recente; retorna `503` para banco indisponível, ausência de leitura ou leitura antiga. A resposta contém somente `status`, `database`, `last_reading_age_seconds` e `queue`. O worker interno continua existindo porque o endpoint não substitui monitoramento fora do servidor.

Webhooks mantêm HMAC SHA-256, `compare_digest`, repositório, branch e comandos fixos. O default é `refs/heads/master`. O processo é destacado da request, stdout/stderr são descartados e `flock` evita execução simultânea no host.

## Segurança do frontend

- Tailwind não é carregado por CDN em runtime; `estacao/static/css/tailwind.css` é versionado;
- Chart.js e Font Awesome continuam locais;
- Google Fonts foi removido em favor de `Inter, system-ui, sans-serif`;
- tema/menu foram movidos para `static/js` e usam `addEventListener`;
- cookies preservam `HttpOnly` e `SameSite=Lax`;
- headers incluem nosniff, Referrer-Policy, Permissions-Policy, X-Frame-Options e CSP compatível;
- HSTS e proxy são opt-in para não quebrar HTTP local.

Para recompilar Tailwind somente no ambiente de build:

```bash
npx --yes tailwindcss@3.4.17 -c tailwind.config.js \
  -i estacao/static/css/tailwind.input.css \
  -o estacao/static/css/tailwind.css --minify
```

Node não é necessário no servidor Flask em runtime. Scripts grandes legados ainda inline exigem `'unsafe-inline'` na CSP; removê-los gradualmente é trabalho futuro para evitar uma mudança visual ampla nesta rodada. `unsafe-eval` não é permitido.

## Testes e CI

Todos os testes usam `TemporaryDirectory` e `ESTACAO_DB` temporário. APIs externas, envio e deploy são mockados.

```bash
python -m unittest discover -s tests -v
python -m compileall estacao tests
```

O workflow `.github/workflows/tests.yml` executa esses comandos em push e pull request com Python 3.11 e 3.12, sem segredos reais.

## Deploy seguro e rollback

Ordem recomendada no servidor:

1. confirmar branch `master` e worktree esperado;
2. executar o backup consistente;
3. executar `maintenance --integrity-check` e registrar contagens;
4. atualizar o código sem reescrever histórico;
5. instalar dependências, se o requirements mudou;
6. configurar as novas variáveis necessárias;
7. executar apenas `cd estacao && python init_db.py`;
8. reiniciar Flask/Gunicorn, updater, sender e health check;
9. verificar `/health`, páginas, fila e logs mascarados;
10. repetir `integrity-check`.

Não execute índices, auditoria de duplicidades ou limpeza junto do deploy normal. Agende-os posteriormente em janela de manutenção.

Para rollback, pare os processos, volte apenas o código para o commit anterior sem alterar o SQLite e reinicie. As mudanças de schema são aditivas e a versão anterior tende a ignorá-las. Se houver qualquer comportamento inesperado no banco, preserve o arquivo atual para investigação e restaure o backup consistente somente com os serviços parados e após conferir contagens/integridade; não use `VACUUM`, `DROP` ou deduplicação como tentativa de reparo.
