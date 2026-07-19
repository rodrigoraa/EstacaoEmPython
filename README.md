# Estação meteorológica em Flask

Aplicação web para coletar dados públicos da Ambient Weather, manter histórico em SQLite, exibir condições e previsões meteorológicas e distribuir alertas pelo WhatsApp por meio da Evolution API.

O sistema é composto por três processos principais:

1. a aplicação Flask atende páginas, APIs, administração e webhooks;
2. o `updater` coleta e persiste leituras, calcula acumulados e cria alertas na fila;
3. o `whatsapp_sender` consome a fila e envia mensagens.

Há também um health check operacional e uma ferramenta idempotente para campanhas pontuais. Todos os processos compartilham o mesmo SQLite e usam o timezone `America/Campo_Grande`.

## Estrutura atual

```text
.
├── estacao/
│   ├── app.py                         # aplicação Flask/WSGI
│   ├── init_db.py                     # criação e migração incremental
│   ├── database.py                    # schema, consultas e fila
│   ├── persistence.py                 # persistência das leituras
│   ├── acumulados.py                  # acumulados meteorológicos
│   ├── time_utils.py                  # UTC e America/Campo_Grande
│   ├── unsubscribe_tokens.py          # tokens e telefones
│   ├── extensions.py                  # Flask-Limiter
│   ├── routes/
│   │   ├── public.py
│   │   ├── api.py
│   │   ├── admin.py
│   │   └── webhook.py
│   ├── services/
│   │   ├── weather_service.py         # Ambient Weather e Open-Meteo
│   │   └── whatsapp_service.py        # Evolution API
│   ├── workers/
│   │   ├── updater.py
│   │   ├── whatsapp_sender.py
│   │   ├── health_check.py
│   │   └── enviar_aviso_whatsapp_unico.py
│   ├── templates/
│   ├── static/
│   └── requirements.txt
├── tests/
├── CLEANUP_REPORT.md
└── README.md
```

O repositório não contém Dockerfile, Compose, unit files do systemd, timers/cron ou os scripts externos de deploy. Esses componentes precisam ser conferidos no host de produção.

## Instalação

Requer Python 3 com suporte a `venv`. Na raiz do repositório:

### Windows/PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r estacao\requirements.txt
```

### Linux

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r estacao/requirements.txt
```

As dependências de produção são deliberadamente pequenas:

- Flask e Flask-Limiter para HTTP e limitação de requisições;
- bcrypt e itsdangerous para autenticação e tokens;
- requests para Ambient Weather, Open-Meteo e Evolution API;
- python-dotenv para carregar configuração local;
- gunicorn como servidor WSGI de produção;
- tzdata para garantir os dados IANA do timezone, inclusive no Windows;
- o extra Redis do Flask-Limiter para armazenamento compartilhado configurável em produção.

## Configuração

Use variáveis do processo ou um arquivo `.env` não versionado. Não grave credenciais, telefones, tokens ou o banco no Git.

### Aplicação e banco

| Variável | Uso | Padrão/comportamento |
|---|---|---|
| `SECRET_KEY` | assinatura de sessão e fallback dos tokens de cancelamento | deve ser definida em produção |
| `ESTACAO_DB` | caminho do SQLite compartilhado | `estacao/estacao.db` |
| `ADMIN_PASSWORD_HASH` | senha administrativa bcrypt | preferida quando definida |
| `ADMIN_PASSWORD` | senha administrativa em texto | fallback; uma das duas senhas é obrigatória |
| `SESSION_COOKIE_SECURE` | cookie apenas por HTTPS | `false` |
| `SESSION_TIMEOUT_MINUTES` | duração da sessão administrativa | `30` |
| `RATELIMIT_ENABLED` | liga o Flask-Limiter | `true` |
| `RATELIMIT_STORAGE_URI` | backend do limiter | `memory://` |
| `RATELIMIT_KEY_PREFIX` | prefixo das chaves do limiter | `estacao` |
| `PUBLIC_CADASTRO_RATE_LIMIT` | limite do cadastro público | `60 per hour` |

`WEBHOOK_SECRET` também é obrigatório para importar a aplicação, pois as rotas de deploy são registradas no bootstrap. A aplicação deve receber ainda `ALLOWED_DEPLOY_REPO` e `ALLOWED_DEPLOY_BRANCH` quando os padrões versionados não corresponderem ao ambiente.

Com mais de um worker Gunicorn, `memory://` mantém limites separados por processo. Configure um URI Redis quando o limite precisar ser global.

### Fontes meteorológicas e URLs públicas

| Variável | Uso |
|---|---|
| `AMBIENT_PUBLIC_SLUG` | substitui o slug público da estação; valor vazio preserva o fallback interno |
| `PUBLIC_BASE_URL` | base dos links públicos enviados nas mensagens |
| `FORECAST_CITY` | cidade consultada no Open-Meteo |
| `FORECAST_STATE` | estado usado na geocodificação |
| `FORECAST_COUNTRY` | país usado na geocodificação |
| `FORECAST_LABEL` | rótulo mostrado na página de previsão |
| `FORECAST_LAT` / `FORECAST_LON` | coordenadas opcionais que evitam geocodificação |

A precedência do slug da Ambient Weather é: valor não vazio de `AMBIENT_PUBLIC_SLUG`, seguido pelo fallback versionado. Não remova o fallback sem garantir a variável em todos os processos.

### Evolution API e envio

| Variável | Uso | Padrão |
|---|---|---|
| `EVOLUTION_URL` | URL da Evolution API | obrigatória para envio |
| `EVOLUTION_API_KEY` | autenticação | obrigatória para envio |
| `EVOLUTION_INSTANCE` | instância de WhatsApp | obrigatória para envio |
| `INTERVALO_ENVIO_USUARIOS` | segundos entre envios | `20` |
| `INTERVALO_WHATSAPP_SEM_FILA` | espera quando a fila está vazia | `5` |
| `WHATSAPP_ENVIANDO_EXPIRADO_MINUTOS` | recuperação de item preso em envio | `10` |
| `WHATSAPP_WORKERS` | consumidores concorrentes | `3`, mínimo `1` |
| `WHATSAPP_MAX_TENTATIVAS` | tentativas máximas por item | `4`, mínimo `1` |
| `UNSUBSCRIBE_SECRET` | segredo específico dos tokens de cancelamento | fallback para `SECRET_KEY` |
| `UNSUBSCRIBE_TOKEN_MAX_AGE_DAYS` | validade do token | `90` |

O serviço de WhatsApp valida as três variáveis da Evolution API ao ser importado. Configure-as antes de iniciar o sender, a campanha ou qualquer fluxo que efetivamente envie mensagens.

### Limites dos alertas

Os padrões atuais são contratos operacionais e não devem ser alterados sem decisão de domínio:

| Família | Variáveis e padrões |
|---|---|
| calor | `ALERTA_CALOR_NIVEL_1=35`, `ALERTA_CALOR_NIVEL_2=40`, `ALERTA_CALOR_REARME=33` |
| frio | `ALERTA_FRIO_NIVEL_1=12.4`, `ALERTA_FRIO_NIVEL_2=5`, `ALERTA_FRIO_NIVEL_3=2`, `ALERTA_FRIO_REARME=15` |
| vento | `ALERTA_VENTO_NIVEL_1=40`, `ALERTA_VENTO_NIVEL_2=70`, `ALERTA_VENTO_NIVEL_3=100` |
| chuva | `ALERTA_CHUVA_NIVEL_1=50`, `ALERTA_CHUVA_NIVEL_2=70` |
| umidade | `ALERTA_UMIDADE_NIVEL_1=30`, `ALERTA_UMIDADE_NIVEL_2=20`, `ALERTA_UMIDADE_REARME=35` |
| confirmação | `ALERTA_CONFIRMACOES_NIVEL_1=2`, mínimo `1` |

### Health check e campanha

| Variável | Padrão |
|---|---|
| `ADMIN_UPDATER_ATRASO_MINUTOS` | `5` |
| `HEALTH_UPDATER_ATRASO_MINUTOS` | valor administrativo acima |
| `HEALTH_FILA_PENDENTE_MINUTOS` | `30` |
| `HEALTH_FALHAS_MINIMAS` | `1` |
| `HEALTH_ALERT_COOLDOWN_MINUTOS` | `60` |
| `ADMIN_ALERT_PHONE` | vazio; necessário para notificação administrativa |
| `INTERVALO_CAMPANHA_WHATSAPP` | `10` segundos |
| `WHATSAPP_CAMPAIGN_ID` | identificador padrão versionado |

## Banco, estado e concorrência

Inicialize ou atualize a estrutura a partir do diretório `estacao/`:

```bash
cd estacao
python init_db.py
```

O comando é incremental: cria estruturas ausentes e acrescenta colunas legadas necessárias. Não apaga dados. As 13 tabelas atuais são:

```text
historico_clima             leituras_brutas
historico_diario            estado_alertas
acumulados_diarios          usuarios
alertas_envios              alertas_fila
alertas_eventos             logs_persistencia
health_check_estado         campanhas_whatsapp_envios
cadastro_eventos
```

O acesso compartilhado preserva:

- WAL, `synchronous=FULL` e `busy_timeout`;
- retry para contenção transitória do SQLite;
- transações e reivindicação atômica da fila com `BEGIN IMMEDIATE`;
- conexão separada por thread do sender;
- chaves e índices de idempotência para alertas e campanhas;
- colunas de horário legadas, UTC e local, necessárias para bancos existentes.

O estado de alertas é primariamente persistido na tabela `estado_alertas`. O arquivo `alert_state.json` continua como fallback e fonte de migração/recuperação. As duas camadas são intencionais; não remova nenhuma sem cobrir reinício, migração e falha de banco.

Faça backup consistente do SQLite e dos arquivos de estado antes de manutenção no host. Com WAL ativo, prefira a API de backup do SQLite ou pare os processos antes de copiar o conjunto de arquivos do banco.

## Execução

Os imports dos entrypoints atuais pressupõem o diretório de trabalho `estacao/`:

```bash
cd estacao
```

### Aplicação Flask

Desenvolvimento:

```bash
python app.py
```

Produção:

```bash
gunicorn -w 2 -b 127.0.0.1:8080 app:app
```

### Coleta e criação de alertas

```bash
python workers/updater.py
```

O updater roda continuamente e consulta a estação a cada 15 segundos. Ele persiste primeiro a leitura e os acumulados, depois avalia alertas e cria itens idempotentes na fila.

### Envio da fila de WhatsApp

Contínuo:

```bash
python workers/whatsapp_sender.py
```

Modos operacionais preservados:

```bash
python workers/whatsapp_sender.py --once
python workers/whatsapp_sender.py --limite 10
python workers/whatsapp_sender.py --intervalo 5
python workers/whatsapp_sender.py --retry-failed --limite 10
```

### Health check

```bash
python workers/health_check.py
python workers/health_check.py --no-whatsapp --fail-on-issues
```

O segundo formato é adequado a cron ou timer: registra o diagnóstico, não envia mensagem administrativa e retorna código `1` quando encontra problemas.

### Campanha pontual

O padrão é simulação, sem envio:

```bash
python workers/enviar_aviso_whatsapp_unico.py
```

O envio exige confirmação explícita:

```bash
python workers/enviar_aviso_whatsapp_unico.py --confirmar
python workers/enviar_aviso_whatsapp_unico.py --confirmar --limite 10
python workers/enviar_aviso_whatsapp_unico.py --confirmar --campaign-id aviso-2026
python workers/enviar_aviso_whatsapp_unico.py --confirmar --mensagem-arquivo mensagem.txt
python workers/enviar_aviso_whatsapp_unico.py --confirmar --retry-failed
```

O `campaign-id` participa da idempotência. Reutilize-o somente quando a intenção for retomar a mesma campanha.

## Rotas HTTP

### Páginas e formulários

| Métodos | Rota | Finalidade |
|---|---|---|
| `GET`, `POST` | `/` | painel público e cadastro de alertas |
| `POST` | `/unsubscribe/request` | solicita link de cancelamento |
| `GET`, `POST` | `/unsubscribe` | valida token e cancela inscrição |
| `GET` | `/historico` | consulta histórica |
| `GET` | `/previsao` | previsão Open-Meteo |
| `GET` | `/sobre` | informações da estação |
| `GET`, `POST` | `/admin` | login e painel administrativo |
| `POST` | `/admin/logout` | encerra sessão |
| `POST` | `/admin/deletar/<id>` | exclui usuário cadastrado |
| `POST` | `/admin/usuarios/<id>/editar` | edita usuário cadastrado |
| `GET` | `/favicon.ico` | logo PNG |

### APIs JSON

Todas usam `GET`:

```text
/api/clima
/api/historico
/api/ultimo
/api/historico_semana
/api/historico_mes
/api/recordes_mes
/api/historico_consulta
/api/anos_disponiveis
```

Essas rotas são contratos externos mesmo quando não são chamadas por outra função Python. Preserve nomes, parâmetros e formatos JSON.

### Webhooks

```text
POST /deploy/python
POST /deploy/php
```

Os handlers validam evento, assinatura HMAC, repositório e branch. Depois iniciam scripts externos em `/var/www/deploy/`. Esses scripts e as permissões de `sudo` não fazem parte deste repositório.

## Produção

Mantenha a aplicação, o updater e o sender como processos independentes, todos com o mesmo `WorkingDirectory`, `ESTACAO_DB` e conjunto coerente de variáveis. Um unit file típico precisa equivaler conceitualmente a:

```ini
[Service]
WorkingDirectory=/caminho/do/projeto/estacao
EnvironmentFile=/caminho/seguro/estacao.env
ExecStart=/caminho/venv/bin/gunicorn -w 2 -b 127.0.0.1:8080 app:app
Restart=always
```

Para os workers, troque apenas `ExecStart` por:

```ini
ExecStart=/caminho/venv/bin/python workers/updater.py
ExecStart=/caminho/venv/bin/python workers/whatsapp_sender.py
```

Esses trechos são exemplos; nomes de usuário, diretórios, hardening, proxy reverso, certificados, units e timers reais pertencem ao ambiente de deploy. As variáveis devem existir antes da importação dos módulos.

## Testes e validação

Na raiz do repositório:

```bash
python -m unittest discover -s tests -v
python -m compileall estacao tests
```

A suíte cobre persistência, WAL e retry, acumulados, regras e idempotência de alertas, fila/retentativas do WhatsApp, usuários administrativos, cancelamento, health check, campanha, timezone, configuração Ambient e execução direta dos workers. Integrações externas são isoladas nos testes; a suíte não deve usar o banco ou credenciais de produção.

Para reproduzir a validação de instalação em um ambiente limpo:

### Windows/PowerShell

```powershell
python -m venv .venv-cleanup
.\.venv-cleanup\Scripts\python.exe -m pip install --upgrade pip
.\.venv-cleanup\Scripts\python.exe -m pip install -r estacao\requirements.txt
.\.venv-cleanup\Scripts\python.exe -m unittest discover -s tests -v
```

### Linux

```bash
python -m venv .venv-cleanup
.venv-cleanup/bin/python -m pip install --upgrade pip
.venv-cleanup/bin/python -m pip install -r estacao/requirements.txt
.venv-cleanup/bin/python -m unittest discover -s tests -v
```

## Diagnóstico rápido

- Falha ao importar `app`: confirme `WEBHOOK_SECRET` e uma das variáveis de senha administrativa; configure também `SECRET_KEY` antes de servir sessões.
- Sender ou campanha não inicia: confirme as três variáveis `EVOLUTION_*` no ambiente do processo.
- Banco diferente entre processos: compare o caminho absoluto efetivo de `ESTACAO_DB` e o `WorkingDirectory`.
- Erros de bloqueio SQLite: confirme que todos os processos usam o mesmo banco local e preserve WAL, timeout e transações existentes.
- Previsão indisponível: valide rede, coordenadas ou os campos `FORECAST_*`; a página trata indisponibilidade sem alterar a coleta Ambient.
- Rate limit inconsistente entre workers web: use armazenamento Redis compartilhado.
- Deploy não dispara: valide os headers do GitHub, repositório/branch permitidos, script externo e permissão de execução no host.

## Auditoria de manutenção

As evidências, decisões de remoção, itens mantidos por compatibilidade e riscos residuais da limpeza de código estão em [`CLEANUP_REPORT.md`](CLEANUP_REPORT.md).
