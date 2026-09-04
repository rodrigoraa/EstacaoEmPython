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
Ambient Weather --------> updater ------------------┐
REDEMET Jaraguari ------> radar_updater ------------┼--> SQLite
PIN-MS (6 estações) --> regional_stations_updater --┘      │
                                                           v
                                                  nowcasting_updater
                                                           │
                                                           v
                                                       snapshots
                                                           │
                                                           v
                                           Flask /admin/monitoramento

Open-Meteo ---------------------------------------> /previsao
updater + fila -----------------------------------> alertas atuais
```

O radar é uma fonte independente, complementar e experimental. O navegador nunca consulta a REDEMET: o worker baixa e analisa os PNGs, persiste o estado e a aplicação lê somente SQLite/arquivos locais. Sua visualização exige a sessão administrativa; visitantes não recebem o painel, a API nem as imagens. Nesta fase ele não envia alertas preventivos; `RADAR_ALERTS_ENABLED=false` é o default e a função de integração permanece deliberadamente inativa até validação meteorológica.

A rede PIN-MS e o nowcasting também são experimentais e visíveis somente no painel administrativo. Dourados A721, Caarapó S706, Juti A749, Naviraí S735, Ivinhema A709 e Culturama S708 complementam a observação local, mas não são sensores da escola e não produzem previsão ou alertas. Os três workers continuam executando em background sem depender de sessão ou navegador autenticado.

O acesso a `/previsao` nunca coleta uma nova leitura oficial nem grava em `leituras_brutas`; a página usa a última leitura persistida pelo updater. Os processos principais são:

- `app.py`: Flask, páginas, APIs, administração e webhooks;
- `workers/updater.py`: coleta, deduplicação, histórico, acumulados e motor de alertas;
- `workers/whatsapp_sender.py`: claim transacional e envio at-least-once da fila;
- `workers/health_check.py`: diagnóstico interno e notificação operacional;
- `workers/maintenance.py`: auditoria, índices e retenção manual;
- `workers/radar_updater.py`: coleta REDEMET, análise de ecos e tracking;
- `workers/regional_stations_updater.py`: coleta atual/horária e tendências PIN-MS;
- `workers/nowcasting_updater.py`: funde somente dados persistidos e grava snapshots;
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

### Radar REDEMET Jaraguari

| Variável | Default | Finalidade |
|---|---:|---|
| `REDEMET_API_KEY` | vazio | chave oficial, exigida somente pelo worker |
| `RADAR_ENABLED` | `false` | ativa a coleta independente |
| `RADAR_AREA` | `jr` | código do radar de Jaraguari/MS |
| `RADAR_PRODUCT` | `maxcappi` | produto inicial |
| `RADAR_ANIMA` | `15` | quantidade solicitada à API |
| `RADAR_TARGET_LAT` | `-22.4925326` | latitude da EE São José |
| `RADAR_TARGET_LON` | `-54.4610352` | longitude da EE São José |
| `RADAR_POLL_SECONDS` | `300` | intervalo mínimo entre ciclos |
| `RADAR_REQUEST_TIMEOUT_SECONDS` | `30` | timeout HTTP |
| `RADAR_MIN_CLUSTER_PIXELS` | `100` | filtro de componentes pequenos |
| `RADAR_MORPH_CLOSE_ITERATIONS` | `2` | fechamento morfológico 3x3 |
| `RADAR_DILATE_ITERATIONS` | `1` | dilatação 3x3 |
| `RADAR_CLUTTER_RADIUS_KM` | `50` | apenas marca possível eco fixo |
| `RADAR_TRACK_MIN_FRAMES` | `3` | mínimo para movimento/ETA |
| `RADAR_TRACK_MIN_DURATION_MINUTES` | `10` | duração mínima observada para tracking |
| `RADAR_TRACK_MAX_SPEED_KMH` | `150` | limite de associação plausível |
| `RADAR_TRACK_MAX_SIZE_RATIO` | `4` | gating de crescimento/redução entre frames |
| `RADAR_TRACK_MAX_DIRECTION_CHANGE_DEG` | `90` | mudança máxima de trajetória associável |
| `RADAR_TRACK_PREDICTION_WEIGHT` | `0.65` | peso da posição prevista no custo geométrico |
| `RADAR_TRACK_TIMEOUT_MINUTES` | `180` | tempo para manter track sem nova associação |
| `RADAR_INTERCEPT_RADIUS_KM` | `25` | raio da trajetória compatível |
| `RADAR_STALE_MINUTES` | `45` | idade para aviso visual |
| `RADAR_MAX_FUTURE_MINUTES` | `30` | tolerância antes de marcar timestamp futuro como `suspect` |
| `RADAR_DATA_DIR` | `estacao/data/radar` | originais e imagens anotadas |
| `RADAR_ALERTS_ENABLED` | `false` | reservado; não envia nesta versão |

Exemplo de `.env` não versionado:

```dotenv
REDEMET_API_KEY=coloque-a-chave-somente-no-servidor
RADAR_ENABLED=true
RADAR_AREA=jr
RADAR_PRODUCT=maxcappi
RADAR_ANIMA=15
RADAR_TARGET_LAT=-22.4925326
RADAR_TARGET_LON=-54.4610352
RADAR_ALERTS_ENABLED=false
```

A chave não aparece em HTML/JSON/logs e não é necessária para iniciar o Flask. Não a coloque no código, README, unit files versionados ou comandos registrados no histórico do shell; prefira arquivo de ambiente com permissão restrita ou o mecanismo de secrets do serviço.

### Rede regional PIN-MS

| Variável | Default | Finalidade |
|---|---:|---|
| `REGIONAL_STATIONS_ENABLED` | `false` | ativa o worker regional separado |
| `REGIONAL_STATIONS_POLL_SECONDS` | `300` | frequência de consulta |
| `REGIONAL_STATIONS_TIMEOUT_SECONDS` | `30` | timeout HTTP |
| `REGIONAL_STATIONS_BOOTSTRAP_HOURS` | `24` | janela inicial/recorrente limitada |
| `REGIONAL_LAYER2_MAX_AGE_HOURS` | `12` | idade máxima para a camada 2 servir de bootstrap |
| `REGIONAL_LAYER2_POLL_SECONDS` | `3600` | frequência independente e persistente de consulta da camada 2 |
| `REGIONAL_STATION_STALE_MINUTES` | `120` | início do status `ATRASADA` |
| `REGIONAL_STATION_VERY_STALE_MINUTES` | `240` | início de `MUITO_ATRASADA` |
| `REGIONAL_STATION_STAGNANT_MINUTES` | `180` | mesmo fingerprint antes de `DADOS_ESTAGNADOS` (mínimo 60 min) |
| `REGIONAL_STATIONS_ALERTS_ENABLED` | `false` | reservado; não envia alertas nesta versão |
| `REGIONAL_TARGET_LAT` | `-22.4925326` | latitude da EE São José |
| `REGIONAL_TARGET_LON` | `-54.4610352` | longitude da EE São José |

A fonte fixa é o serviço público `Estacoes_CEMADEN_INMET` do PIN-MS. A camada 0 fornece o estado operacional mais recente e forma um histórico próprio por hora. A camada 2 continua persistida para auditoria e bootstrap, mas é consultada em frequência independente, por padrão a cada hora, e só participa de tendências quando possui timestamps confiáveis dentro de `REGIONAL_LAYER2_MAX_AGE_HOURS`. O último poll fica no estado persistido por estação, de modo que reiniciar o worker não antecipa uma nova consulta; a fonte volta a ser usada automaticamente se passar a oferecer dados recentes. Em setembro de 2026 foram observados registros reais da camada 2 ainda em março de 2026; eles permanecem `STALE`, não utilizáveis, e não são corrigidos, reinterpretados nem apagados. A allowlist dos seis códigos impede usar entrada HTTP para construir endpoints ou cláusulas ArcGIS arbitrárias.

A metadata do ArcGIS informa os tipos dos campos, mas não registra unidades nos aliases. O esquema corresponde aos dados automáticos do INMET, cuja documentação oficial define temperatura em °C, umidade em %, pressão no nível da estação em hPa, vento e rajada em m/s, direção em graus, precipitação em mm e radiação global em kJ/m². Por isso vento/rajada são preservados em valor bruto e m/s, com uma coluna derivada em km/h (`m/s × 3,6`). Nenhuma correção de pressão ao nível do mar é feita.

`DT_MEDICAO` e `HR_MEDICAO` são sempre preservados como raw. Na camada 0, `DT_MEDICAO` sem `HR_MEDICAO` representa somente a data operacional, mesmo quando o epoch equivale a 04:00 UTC/00:00 local; portanto recebe `date_only` e nunca vira uma hora oficial inventada. Para o histórico recente, `coletado_em` é copiado para `sample_time` com o tipo explícito `collection_time_proxy`: sabemos quando o servidor recebeu os valores, não o minuto oficial da medição. O fingerprint completo da leitura atual mantém `first_seen` e `last_seen` mesmo quando a observação é deduplicada. Após 180 minutos exatamente iguais, `DADOS_ESTAGNADOS` indica apenas que os valores recebidos podem estar congelados; não afirma que a estação está quebrada. Uma mudança em qualquer valor relevante reinicia o contador. A disponibilidade HTTP continua separada dessa heurística. Na camada 2, datas com hora inequívoca produzem timestamps timezone-aware e uma data ArcGIS à meia-noite combinada com a hora recebe `reconciled`. Hora própria conflitante, futuro incompatível e formatos dia/mês ambíguos ficam `suspect`.

### Nowcasting observacional

| Variável | Default | Finalidade |
|---|---:|---|
| `NOWCASTING_ENABLED` | `false` | ativa o worker de fusão persistida |
| `NOWCASTING_POLL_SECONDS` | `300` | intervalo entre análises |
| `NOWCASTING_ALERTS_ENABLED` | `false` | reservado; sempre ignorado nesta versão |
| `NOWCASTING_TEST_ALERTS_ENABLED` | `false` | envia testes vermelhos elegíveis somente ao administrador |
| `NOWCASTING_TEST_ALERT_COOLDOWN_MINUTES` | `60` | proteção global mínima entre testes enviados |
| `NOWCASTING_TEST_ALERT_REARM_MINUTES` | `30` | tempo contínuo fora do vermelho para encerrar um episódio |
| `NOWCASTING_UPSTREAM_CORRIDOR_KM` | `50` | largura lateral do corredor a montante |
| `NOWCASTING_RADAR_MAX_AGE_MINUTES` | `45` | idade máxima do radar para evidência plena |
| `NOWCASTING_REGIONAL_MAX_AGE_MINUTES` | `180` | idade máxima de estação confirmadora |
| `NOWCASTING_REGIONAL_CONFIRM_MIN_SIGNALS` | `2` | sinais independentes mínimos para confirmação |
| `NOWCASTING_REGIONAL_CONFIRM_MIN_STATIONS` | `1` | estações a montante mínimas com sinais |
| `NOWCASTING_ALGORITHM_VERSION` | `1.4` | versão persistida junto ao snapshot |

Nowcasting aqui não é previsão numérica. É fusão observacional de curtíssimo prazo:
o radar acompanha ecos, as estações regionais confirmam alterações em superfície e
a estação local confirma a chegada à escola. O serviço não acessa REDEMET, PIN-MS,
Ambient Weather nem Open-Meteo; lê somente o SQLite preenchido pelos workers
independentes.

Estações com `DADOS_ESTAGNADOS` não fornecem confirmação regional, mesmo quando existe tendência histórica anterior. Os alertas preventivos de nowcasting continuam bloqueados nesta versão, inclusive se a variável reservada for habilitada.

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

O SQLite usa WAL, `synchronous=FULL`, `busy_timeout` e foreign keys. O schema atual é a versão `8`, registrada na pequena tabela `schema_version`.

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
- criação de `radar_frames`, `radar_clusters`, `radar_tracks` e `radar_track_points`, com chaves estrangeiras, índices pequenos e `UNIQUE(path_remoto)`.
- criação de `regional_stations`, `regional_station_observations`, `regional_station_samples` e `regional_station_state`, com catálogo idempotente, índices e `UNIQUE(fingerprint)`.
- adição dos estados separados da fonte atual e do histórico externo; criação dos buckets horários locais por proxy de coleta, sem payload JSON duplicado;
- adição do fingerprint atual com primeira/última observação contínua e do último poll da camada 2 ao estado regional;
- adição das colunas UTC/local, timestamp raw, coleta UTC/local, classes relativas de refletividade e diagnóstico de clutter às estruturas de radar;
- criação de `nowcasting_snapshots`, com `UNIQUE(input_fingerprint)` e versão do algoritmo.

Novos frames de radar são armazenados em `data_frame_utc` com offset `+00:00` e
em `data_frame_local` com `America/Campo_Grande`. A [página oficial da API REDEMET](https://ajuda.decea.mil.br/base-de-conhecimento/api-redemet-produtos-radar/)
documenta o formato de `data`, mas não explicita o fuso desse campo. O coletor
interpreta-o operacionalmente como UTC e marca novos frames `utc_assumed`, nunca
`utc_confirmed`, até existir confirmação documental inequívoca. O texto original é
preservado em `data_frame_raw`; futuros além de `RADAR_MAX_FUTURE_MINUTES` são
persistidos como `suspect`, mas não entram em tracking nem ETA.
Linhas antigas não recebem backfill: permanecem com `data_frame_utc` nulo e
`timestamp_status=legacy_unverified`; para cálculos, o valor legado é assumido UTC e
continua preservado byte a byte na coluna original.

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

O radar tem retenção separada, também bloqueada por padrão. O dry-run não remove nada:

```bash
python -m estacao.workers.maintenance --radar-dry-run
```

| Variável | Default |
|---|---:|
| `RADAR_RETENCAO_AUTOMATICA` | `false` |
| `RADAR_RETENCAO_IMAGENS_DIAS` | `7` |
| `RADAR_RETENCAO_FRAMES_DIAS` | `30` |

Para executar deliberadamente:

```bash
RADAR_RETENCAO_AUTOMATICA=true python -m estacao.workers.maintenance --radar-cleanup --batch-size 1000
```

A limpeza aceita apenas caminhos `.png` registrados no banco que permaneçam dentro de `RADAR_DATA_DIR`; caminhos externos são ignorados. Primeiro os arquivos antigos são removidos e seus campos anulados; depois frames além da retenção são excluídos em lotes, com cascade apenas sobre dados derivados do radar.

A retenção regional também é separada e opt-in:

```bash
python -m estacao.workers.maintenance --regional-dry-run
REGIONAL_STATIONS_RETENTION_ENABLED=true python -m estacao.workers.maintenance --regional-cleanup --batch-size 1000
```

`REGIONAL_STATIONS_RETENTION_DAYS` tem default de 730 dias. A limpeza remove primeiro buckets expirados e depois somente observações antigas que não estejam referenciadas por buckets recentes; catálogo e estado das estações são preservados.

## Execução

Da raiz, os entrypoints por módulo são:

```bash
python -m estacao.workers.updater
python -m estacao.workers.whatsapp_sender
python -m estacao.workers.health_check --no-whatsapp --fail-on-issues
RADAR_ENABLED=true python -m estacao.workers.radar_updater --once
RADAR_ENABLED=true python -m estacao.workers.radar_updater
python -m estacao.workers.radar_updater --diagnose-palette
python -m estacao.workers.radar_updater --diagnose-time
REGIONAL_STATIONS_ENABLED=true python -m estacao.workers.regional_stations_updater --once
REGIONAL_STATIONS_ENABLED=true python -m estacao.workers.regional_stations_updater
REGIONAL_STATIONS_ENABLED=true python -m estacao.workers.regional_stations_updater --once --verbose-time
REGIONAL_STATIONS_ENABLED=true python -m estacao.workers.regional_stations_updater --once --verbose-history
NOWCASTING_ENABLED=true python -m estacao.workers.nowcasting_updater --once
NOWCASTING_ENABLED=true python -m estacao.workers.nowcasting_updater
```

Os comandos legados continuam compatíveis:

```bash
cd estacao
python app.py
python workers/updater.py
python workers/whatsapp_sender.py
python workers/radar_updater.py --once
python workers/regional_stations_updater.py --once
python workers/nowcasting_updater.py --once
```

No radar, `--once` consulta, deduplica, baixa somente frames novos ou anteriormente
falhos, analisa, persiste, imprime um resumo e termina. Nas estações regionais,
`--once` consulta as camadas atual e horária, normaliza, deduplica, persiste, imprime
o resumo e termina. Para atualização permanente, execute cada worker sem `--once`
como um serviço separado do updater Ambient Weather; os intervalos são controlados
por `RADAR_POLL_SECONDS`, `REGIONAL_STATIONS_POLL_SECONDS` e
`NOWCASTING_POLL_SECONDS`. Execute o nowcasting depois que os coletores já tiverem
gravado dados; ele nunca substitui ou reúne os outros workers em um único processo.
O updater Ambient Weather legado permanece contínuo e não possui `--once`; os três
workers novos aceitam `--once` sem alterar os limites existentes de chuva de 30/50 mm.

Gunicorn continua usando:

```bash
cd estacao
gunicorn -w 2 -b 127.0.0.1:8080 app:app
```

## Rotas e saúde

As rotas públicas existentes da estação foram preservadas, incluindo páginas, APIs, cancelamento e `/deploy/python` e `/deploy/php`. As superfícies experimentais ficam sob autenticação administrativa:

- `GET /signup/confirm`: confirmação do double opt-in;
- `GET /health`: status externo sem PII/segredos;
- `GET /admin/radar`: último estado persistido, imagem, clusters e tracking;
- `GET /admin/api/radar/status`: JSON do radar sem chave nem paths internos;
- `GET /admin/radar/imagem/<frame_id>` e `/admin/radar/imagem/atual`: PNG local registrado, validado e protegido;
- `GET /admin/estacoes-regionais`: seis cards para diagnóstico da coleta;
- `GET /admin/api/regional-stations`: observações, geografia, freshness e tendências sem payload bruto;
- `GET /admin/monitoramento`: painel unificado do último snapshot observacional;
- `GET /admin/api/nowcasting/status`: último snapshot com `ameaca_principal`, `ameacas` e `confirmacao_regional`.

As URLs experimentais antigas fora de `/admin` não servem dados publicamente: páginas redirecionam ao login e APIs/imagens retornam `401` sem sessão. O menu público permanece equivalente ao da master, com Dashboard, Histórico, Previsão do tempo e Sobre a estação.

`/health` retorna `200` para banco e leitura recente; retorna `503` para banco indisponível, ausência de leitura ou leitura antiga. A resposta contém somente `status`, `database`, `last_reading_age_seconds`, `queue`, `radar`, `regional_stations` e `nowcasting`. Fontes auxiliares desabilitadas aparecem como `disabled`; ausência/stale quando ativas aparece como `warning`, mas não torna sozinha a saúde principal `DOWN`.

### Regras da observação regional

Slots sem nenhuma variável meteorológica são ignorados. A deduplicação das observações usa SHA-256 determinístico sobre código, camada, `DT_MEDICAO` raw, `HR_MEDICAO` raw, coordenadas e valores meteorológicos normalizados. A camada 0 atualiza um único bucket por estação/hora com a última coleta válida daquele período; polls idênticos referenciam a mesma observação e não geram eventos meteorológicos novos. O bucket não duplica `payload_json`.

Freshness operacional usa a última coleta válida da camada 0. Assim, uma coleta atual não vira `MUITO_ATRASADA` porque o histórico externo está antigo. `current_source` e `external_hourly_source` são expostos separadamente; este último pode ficar `STALE` sem degradar o status atual. Uma estação ausente ou com erro não impede persistir as demais.

As tendências usam os buckets locais e só aparecem quando existe uma referência aproximadamente 1h/3h/6h atrás, com tolerância de 45 minutos; caso contrário retornam `null`. `trend_quality` distingue `INSUFFICIENT`, `PARTIAL` e `GOOD`, e `trend_source` informa histórico local ou bootstrap externo. Temperatura, umidade e pressão têm deltas 1h/3h; vento e rajada, delta 1h; a direção usa diferença angular. Chuva usa o valor representativo do bucket e soma cada observação-fonte no máximo uma vez, portanto `CHUVA=2` repetido em vários polls não vira 6 mm. Pressão é comparada somente com a própria estação.

Essa rede é observacional. Não existe inferência de deslocamento meteorológico, frente, tempestade, probabilidade, IA ou previsão; latitude, longitude, bearing, distância e freshness apenas deixam uma futura integração com radar tecnicamente possível.

### Algoritmo e limitações do radar

Cada frame usa os próprios limites `lat_min`, `lat_max`, `lon_min`/`lon_max`, raio informado e dimensões reais do PNG. A paleta foi levantada em 42 PNGs MaxCAPPI reais de Jaraguari, todos 750×750 RGBA: foram observadas 49 cores opacas, agrupadas sem equivalência dBZ em `REFLETIVIDADE_BAIXA` (cinza/azul), `REFLETIVIDADE_MEDIA` (verde), `REFLETIVIDADE_ALTA` (amarelo/laranja) e `REFLETIVIDADE_MUITO_ALTA` (vermelho). A classificação aceita somente essas cores confirmadas.

A área meteorologicamente válida é a interseção entre pixels não transparentes e o círculo de cobertura geográfica do radar. Assim, barras/legendas nas margens e cores fora do raio não viram clusters. A máscara original recebe somente pixels reais da paleta; uma segunda máscara recebe `MORPH_CLOSE` 3×3 e dilatação para conectar fragmentos. Connected components usam a processada apenas para agrupar fragmentos; tamanho, centro, contorno/borda meteorológica, classe predominante, classe máxima e distribuição por classe usam exclusivamente a união dos pixels originais. A dilatação não aproxima artificialmente `distancia_borda_escola_km`.

`python -m estacao.workers.radar_updater --diagnose-palette [PNG]` usa o PNG original mais recente quando o caminho é omitido, mostra dimensões, RGB/HSV, contagens, grupos, descartes e percentual de eco, e grava `mask_original.png`/`mask_classes.png` sob `RADAR_DATA_DIR/diagnosticos` (diretório operacional ignorado pelo Git). `--diagnose-time` consulta somente os metadados atuais e mostra raw, UTC assumido, horário local, agora UTC, diferença e status sem exibir a chave.

O tracking faz matching 1:1 global por custo guloso ordenado. O custo usa 70% de distância (posição prevista com peso padrão 0,65 mais posição anterior), 20% de razão logarítmica de tamanho e 10% de continuidade direcional. Antes do custo, o gating rejeita tempo não positivo, timeout, velocidade impossível, razão de tamanho excessiva, distância prevista incompatível e mudança de direção excessiva. Tracks sem associação sobrevivem até o timeout; células sem associação criam um ID novo. ETA só existe com frames mínimos, movimento coerente/plausível, aproximação e interseção projetada com o raio da escola; `distância / velocidade` isolado não é usado.

O índice interno de persistência de clutter é calculado somente com pelo menos 12
frames históricos e quatro ocorrências próximas, numa janela de 30 dias e raio de
12 km. Combina frequência por frame (45%), persistência da amostra (25%), baixo
deslocamento médio (20%) e proximidade do centro do radar (10%). O valor 0–1 não é
probabilidade meteorológica, não exclui ecos e fica `null` quando faltam dados.

### Regras do nowcasting

Uma estação é a montante quando sua projeção fica atrás do track no vetor inverso
do movimento, até 300 km, e a distância perpendicular ao eixo é menor que o corredor.
Quando o alvo é fornecido, o vetor também precisa apontar para a região da escola.
Direção do vento de superfície nunca é usada como direção da célula.

Na versão 1.4 cada track atual gera uma ameaça independente; scores de células diferentes não são somados. A `ameaca_principal` continua sendo ordenada pela relevância meteorológica: status explícito (`ATENCAO_PREVENTIVA`, `EVIDENCIA_REGIONAL`, `TRAJETORIA_RELEVANTE`, `SISTEMA_SE_APROXIMANDO`, `SISTEMA_EM_MOVIMENTO`, `ECO_EM_MONITORAMENTO`), confirmação regional, trajetória, aproximação, tracking, distância, ETA e clutter. As demais ameaças permanecem em `ameacas` e no painel. Estações com valores estagnados continuam excluídas da confirmação regional.

O estado `alerta_preventivo` é somente visual e é independente da ameaça principal.
Ele seleciona o eco sem clutter forte com a menor distância válida da borda dentro de
100 km: `AMARELO` até 100 km, `LARANJA` até 50 km e `VERMELHO` até 25 km, com limites
inclusivos. Tracking não é obrigatório para mostrar a faixa. Sem eco confiável nessa
faixa, clutter forte dentro de 100 km permanece visível como `AMARELO` diagnóstico,
mesmo que exista eco confiável mais distante; clutter nunca produz laranja ou vermelho
sozinho. Sem nenhum retorno relevante na faixa, o estado é `NORMAL`. Se o radar estiver
stale ou sem frame operacional válido, o estado é `INDISPONIVEL`, nunca `NORMAL` verde.
Confirmação regional aumenta a confiança textual, mas não é exigida para mostrar uma
faixa. Se houver chuva local fresca, a mensagem passa a informar que a chuva já foi
observada na EE São José.

As telas administrativas só tratam o último snapshot como atual quando
`gerado_em_utc` é válido, o radar do snapshot não está stale e qualquer nível
operacional colorido declara radar operacional. A validade é o maior valor entre 10
minutos e dois ciclos de `NOWCASTING_POLL_SECONDS`; com o polling padrão de 300
segundos, a janela é de 10 minutos. Depois disso, o snapshot continua persistido e
pode ser mostrado como histórico/diagnóstico, mas o alerta atual passa a
`INDISPONIVEL`.

Frames processados com `timestamp_status='suspect'` continuam persistidos para
auditoria, porém são excluídos da seleção operacional, do tracking e do histórico
usado para clutter. A idade e o estado stale usam o último frame processado não
suspeito. O histórico curto da borda é carregado em uma consulta agrupada para todos
os tracks do frame, aproveitando o índice existente `(track_id, data_frame)` e sem
alteração de schema.

O `ETA da trajetória` continua sendo a entrada projetada do centro no raio-alvo. O
`ETA estimado da borda` é separado e usa até oito distâncias recentes do mesmo track:
a taxa de aproximação é a mediana das taxas entre pares, exige no mínimo três frames,
duração mínima, ao menos 75% dos passos convergentes, trajetória compatível e taxa
plausível. Ruído excessivo e estimativas acima de seis horas suprimem o valor.

O índice por ameaça é auditável: eco +8, tracking válido +12, aproximação +12,
trajetória compatível +15 e faixa de distância +2/+5/+8. O radar sozinho chega no
máximo a aproximadamente 55, abaixo da atenção preventiva. Cada estação fresca a
montante pode contribuir no máximo 25 pontos por chuva horária, aumento de rajada ou
vento, queda de temperatura, aumento de umidade e queda da pressão da própria
estação. Clutter persistente desconta 20 e
suspeita simples desconta 5. Radar stale limita o total a 24. As faixas são
`SEM_EVIDENCIA`, `BAIXA`, `MODERADA`, `ELEVADA` e `MUITO_ELEVADA`; nunca são
apresentadas como probabilidade.

`ATENCAO_PREVENTIVA` não depende apenas do score: exige radar fresco, tracking
suficiente, aproximação, trajetória compatível e confirmação regional. Por padrão,
essa confirmação requer ao menos dois sinais independentes distribuídos em uma ou
mais estações frescas a montante com `trend_quality=GOOD`. As tendências vêm do
histórico próprio da camada 0 ou, apenas durante bootstrap, de uma camada 2 recente.
Alteração fora do corredor, estação stale ou histórico ainda em formação não
confirma a ameaça. Chuva local gera a evidência separada “Evento já observado na
estação local”, sem aumentar o score preventivo. `confirmacao_regional`, `radar_only`,
`ameaca_principal` e `ameacas` ficam no snapshot/API para auditoria. Mesmo assim,
`NOWCASTING_ALERTS_ENABLED=true` continua retornando zero e não grava filas.
O campo `would_send` indica apenas se um nível vermelho confiável seria candidato em
uma versão futura. Nesta versão ele depende somente de radar fresco, nível vermelho e
ausência de clutter forte; tracking, aproximação e trajetória não bloqueiam a
simulação, para que uma célula confiável recém-detectada perto da escola não seja
ocultada. `preventive_sending` permanece `DESATIVADO` e não cria registros em
`alertas_fila` nem em `alertas_eventos`.

### Alertas preventivos de teste para o administrador

`NOWCASTING_TEST_ALERTS_ENABLED=false` mantém o modo experimental desligado. Quando
habilitado, somente alertas `VERMELHO` atuais, confiáveis e marcados como candidatos
podem ser enviados diretamente para `ADMIN_ALERT_PHONE`. Usuários cadastrados não
recebem essas mensagens, e o teste não usa `alertas_fila` nem `alertas_eventos`.
`NOWCASTING_ALERTS_ENABLED` permanece bloqueado e não habilita alertas públicos nesta
versão.

O episódio é persistido na chave isolada `nowcasting_test_alert` da estrutura genérica
`health_check_estado`, sem armazenar telefone, credenciais ou texto enviado. Um track
gera uma chave como `track:27`; sem tracking, usa-se um episódio vermelho global para
impedir spam por mudanças de cluster. A troca posterior para um track também não
repete o teste enquanto o episódio estiver ativo. O rearm padrão exige 30 minutos
contínuos fora do vermelho e o cooldown global padrão exige 60 minutos desde o último
envio. Falhas no WhatsApp não derrubam o worker e uma nova tentativa respeita o mesmo
cooldown de segurança.

O MaxCAPPI processado é imagem RGB, não volume bruto calibrado. Portanto o sistema mostra “eco de radar”/“área de refletividade detectada” e não inventa dBZ, mm/h, probabilidade, granizo, severidade ou “tempestade confirmada”. Estações externas são aproximadamente horárias, o radar pode conter clutter, o ETA depende da continuidade e células podem nascer ou desaparecer rapidamente. Nowcasting não substitui avisos oficiais. As regras precisam rodar em observação por dias/semanas e ser comparadas com a chegada real à escola antes de qualquer alerta preventivo.

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
