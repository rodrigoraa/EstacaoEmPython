# Auditoria de limpeza segura

Data da auditoria: 2026-07-19
Branch: `chore/cleanup-dead-code`

## Escopo e método

Esta auditoria foi feita antes das remoções. As conclusões combinam:

- inventário de arquivos versionados e da árvore até cinco níveis;
- grafo de imports e busca de referências em Python, Jinja, HTML, CSS e JavaScript;
- mapa de rotas obtido do `url_map` real do Flask;
- execução dos modos `--help` dos workers com CLI;
- leitura dos schemas e das migrações incrementais, sem abrir nenhum banco;
- inspeção de comandos documentados de Gunicorn/systemd e subprocessos de deploy;
- Ruff e Vulture como apoio, nunca como prova isolada;
- histórico Git dos candidatos principais;
- suíte de testes e compilação como linha de base.

Não foram lidos `.env`, bancos, arquivos JSON de estado, credenciais, tokens, telefones reais ou dados pessoais.

## Linha de base

- Estado inicial: branch criada a partir de uma árvore limpa.
- Arquivos: 57 versionados; a varredura equivalente a `find . -maxdepth 5 -type f | sort`, sem `.git`, encontrou 86 arquivos, incluindo caches ignorados.
- Testes: `python -m unittest discover -s tests -v` — **53 testes, todos aprovados**.
- Compilação: `python -m compileall estacao tests` — aprovada.
- A primeira tentativa de testes dentro do sandbox não foi uma falha do projeto: o SQLite não pôde criar arquivos no diretório temporário do Windows. A repetição autorizada fora dessa restrição aprovou os 53 testes.
- Não há falha funcional preexistente conhecida.

## Entradas operacionais comprovadas

Os comandos abaixo pressupõem o diretório de trabalho `estacao/`, como documentado e exigido pelos imports atuais.

| Processo | Comando | Evidência |
|---|---|---|
| Flask de desenvolvimento | `python app.py` | `app.py`, bloco `__main__` |
| Flask de produção | `gunicorn -w 2 -b 127.0.0.1:8080 app:app` | objeto WSGI `app` e exemplos de produção do README |
| Inicialização/migração incremental | `python init_db.py` | entrypoint versionado |
| Coleta e alertas | `python workers/updater.py` | loop contínuo com intervalo de 15 segundos |
| Envio WhatsApp | `python workers/whatsapp_sender.py` | contínuo por padrão; `--once`, `--limite`, `--intervalo`, `--retry-failed` confirmados pelo parser e por `--help` |
| Health check | `python workers/health_check.py` | `--no-whatsapp` e `--fail-on-issues` confirmados |
| Campanha única | `python workers/enviar_aviso_whatsapp_unico.py` | dry-run por padrão; `--confirmar`, `--campaign-id`, `--intervalo`, `--limite`, `--mensagem-arquivo`, `--retry-failed` confirmados |

Não existem no repositório unit files de systemd, cron/timers, Dockerfile, Compose, Procfile, configuração separada de Gunicorn ou scripts shell. Os webhooks chamam scripts externos em `/var/www/deploy/`; a presença deles só pode ser verificada no host de produção.

## Inventário Python e grafo de dependências

Há 20 módulos Python versionados:

- bootstrap: `app.py`, `extensions.py`, `init_db.py`;
- domínio/persistência: `database.py`, `persistence.py`, `acumulados.py`, `time_utils.py`, `unsubscribe_tokens.py`;
- HTTP: `routes/public.py`, `routes/api.py`, `routes/admin.py`, `routes/webhook.py`;
- serviços: `services/weather_service.py`, `services/whatsapp_service.py` e o marcador de pacote;
- processos: `workers/updater.py`, `workers/whatsapp_sender.py`, `workers/health_check.py`, `workers/enviar_aviso_whatsapp_unico.py` e o marcador de pacote.

Grafo principal:

- `app -> extensions + routes.*`;
- `public -> database + weather_service + time_utils + unsubscribe_tokens`;
- `api -> database + acumulados + time_utils`;
- `admin -> database + extensions + time_utils + unsubscribe_tokens`;
- `updater -> database + persistence + acumulados + weather_service + time_utils`;
- `whatsapp_sender -> database + whatsapp_service + time_utils`;
- `health_check -> database + time_utils + unsubscribe_tokens`, com import tardio do serviço WhatsApp;
- campanha -> `database + whatsapp_service + unsubscribe_tokens`;
- `weather_service -> persistence -> database + time_utils`.

Ruff não encontrou import não utilizado. Vulture apontou funções de rota e processadores Jinja apenas em baixa confiança; o registro por decorators/context processor prova que esses resultados são falsos positivos. Não há classe de produção sem instanciação nem constante seguramente inalcançável.

## Contratos HTTP

O mapa real do Flask confirmou todas as entradas abaixo:

- públicas: `GET/POST /`, `POST /unsubscribe/request`, `GET/POST /unsubscribe`, `GET /sobre`, `GET /historico`, `GET /previsao`;
- JSON: `GET /api/clima`, `/api/historico`, `/api/ultimo`, `/api/historico_semana`, `/api/historico_mes`, `/api/recordes_mes`, `/api/historico_consulta`, `/api/anos_disponiveis`;
- administrativas: `GET/POST /admin`, `POST /admin/logout`, `POST /admin/deletar/<id>`, `POST /admin/usuarios/<id>/editar`;
- deploy: `POST /deploy/python`, `POST /deploy/php`;
- direta: `GET /favicon.ico`;
- além da rota estática nativa do Flask.

`/api/ultimo` não é chamado pela interface atual, mas é documentado e pode ter consumidores externos; portanto não é código morto.

## Templates e estáticos

Todos os 10 templates são alcançáveis:

| Template | Origem |
|---|---|
| `index.html` | rota `/`; estende `layout.html`; inclui Chart.js |
| `historico.html` | rota `/historico`; estende `layout.html`; inclui Chart.js |
| `previsao.html` | rota `/previsao`; estende `layout.html` |
| `sobre.html` | rota `/sobre`; estende `layout.html` |
| `unsubscribe.html` | fluxos `/unsubscribe/request` e `/unsubscribe` |
| `admin_login.html` | `/admin` sem autenticação |
| `admin_painel.html` | `/admin` autenticado |
| `layout.html` | base das quatro páginas públicas |
| `includes/chartjs.html` | incluído por index e histórico |
| `includes/common_head_assets.html` | incluído pelos quatro documentos-raiz |

Todos os estáticos versionados possuem uso comprovado:

- `background.jpg` é referenciado no CSS claro e escuro;
- `logo.png` é usado por páginas e por `/favicon.ico`;
- `style.css` é carregado por todas as páginas;
- Chart.js local é incluído nas duas páginas que instanciam gráficos;
- o CSS Font Awesome é incluído globalmente;
- os oito webfonts constam das URLs do bundle; TTF e v4compat são fallbacks de compatibilidade.

Todos os sete `fetch` apontam para endpoints existentes e consomem campos presentes nos JSON. Actions, métodos e nomes dos formulários correspondem aos handlers. Todos os IDs consultados pelo JavaScript existem e não há ID duplicado. Não foi encontrado código executável comentado.

## Variáveis de ambiente

Somente os nomes foram inventariados:

- aplicação/sessão/rate limit: `SECRET_KEY`, `RATELIMIT_ENABLED`, `RATELIMIT_STORAGE_URI`, `RATELIMIT_KEY_PREFIX`, `SESSION_COOKIE_SECURE`, `SESSION_TIMEOUT_MINUTES`;
- banco: `ESTACAO_DB`;
- admin: `ADMIN_PASSWORD`, `ADMIN_PASSWORD_HASH`, `ADMIN_UPDATER_ATRASO_MINUTOS`;
- Evolution: `EVOLUTION_URL`, `EVOLUTION_API_KEY`, `EVOLUTION_INSTANCE`;
- cadastro/cancelamento: `PUBLIC_CADASTRO_RATE_LIMIT`, `UNSUBSCRIBE_SECRET`, `UNSUBSCRIBE_TOKEN_MAX_AGE_DAYS`;
- previsão: `FORECAST_CITY`, `FORECAST_STATE`, `FORECAST_COUNTRY`, `FORECAST_LABEL`, `FORECAST_LAT`, `FORECAST_LON`;
- deploy: `WEBHOOK_SECRET`, `ALLOWED_DEPLOY_REPO`, `ALLOWED_DEPLOY_BRANCH`;
- Ambient/URLs: `AMBIENT_PUBLIC_SLUG`, `PUBLIC_BASE_URL`;
- alertas: famílias `ALERTA_CALOR_*`, `ALERTA_FRIO_*`, `ALERTA_VENTO_*`, `ALERTA_CHUVA_*`, `ALERTA_UMIDADE_*` e `ALERTA_CONFIRMACOES_NIVEL_1`;
- sender: `INTERVALO_ENVIO_USUARIOS`, `INTERVALO_WHATSAPP_SEM_FILA`, `WHATSAPP_ENVIANDO_EXPIRADO_MINUTOS`, `WHATSAPP_WORKERS`, `WHATSAPP_MAX_TENTATIVAS`;
- health: `HEALTH_UPDATER_ATRASO_MINUTOS`, `HEALTH_FILA_PENDENTE_MINUTOS`, `HEALTH_FALHAS_MINIMAS`, `HEALTH_ALERT_COOLDOWN_MINUTOS`, `ADMIN_ALERT_PHONE`;
- campanha: `INTERVALO_CAMPANHA_WHATSAPP`, `WHATSAPP_CAMPAIGN_ID`.

O slug Ambient mantém a precedência necessária: variável não vazia, senão fallback do código. Há três testes específicos. `load_dotenv` em app e serviço WhatsApp não é duplicação morta, pois os processos são independentes.

## SQLite, estado e concorrência

As 13 tabelas foram mapeadas sem abrir banco:

`historico_clima`, `leituras_brutas`, `historico_diario`, `estado_alertas`, `acumulados_diarios`, `usuarios`, `alertas_envios`, `alertas_fila`, `alertas_eventos`, `logs_persistencia`, `health_check_estado`, `campanhas_whatsapp_envios`, `cadastro_eventos`.

Devem ser mantidos:

- todas as chamadas de `garantir_coluna`, pois migram bancos existentes de forma incremental;
- colunas de timestamps legado/UTC/local e seus `COALESCE`;
- índices de histórico, leituras, prioridade/retry e unicidade de evento/usuário;
- WAL, `synchronous=FULL`, `busy_timeout`, transações e retry de `OperationalError`;
- `BEGIN IMMEDIATE` do sender e conexão separada por thread;
- banco como estado primário e JSON como recuperação/migração;
- busca de arquivos JSON legados pela API.

O teste existente cobre migração arquivo -> banco, WAL, retry, idempotência de evento/fila e retomada de envios. Ainda faltam testes completos de banco indisponível, falha de escrita JSON e reinício entre essas falhas. Isso impede remover qualquer camada de estado.

## Dependências

As oito entradas de `requirements.txt` são diretas ou operacionais; **nenhuma remoção é recomendada**:

| Dependência | Justificativa para manter |
|---|---|
| `bcrypt` | import direto no login admin |
| `Flask` | servidor, blueprints, Jinja e sessões |
| `Flask-Limiter[redis]` | import direto; o extra suporta `RATELIMIT_STORAGE_URI` Redis em deploy |
| `gunicorn` | comando WSGI de produção |
| `itsdangerous` | import direto para tokens de cancelamento |
| `python-dotenv` | bootstrap independente do app e serviço WhatsApp |
| `requests` | Ambient Weather, Open-Meteo, Evolution e status admin |
| `tzdata` | dados IANA para `America/Campo_Grande`, especialmente em Windows |

O default atual do limiter é em memória. Com múltiplos workers Gunicorn, um deploy que precise de limites compartilhados deve configurar Redis; por isso o extra não pode ser removido sem conhecer o ambiente real.

## Classificação dos candidatos

### REMOVER COM SEGURANÇA

| Arquivo/símbolo | Motivo e evidência além da análise estática | Risco | Proteção/validação | Ação |
|---|---|---|---|---|
| `workers/updater.py`, segundo `sys.path` | A linha recalcula e adiciona exatamente o mesmo diretório já inserido como `BASE_DIR` na linha anterior. | Muito baixo | suíte + novo smoke test de carregamento direto | remover somente a segunda inserção |
| `updater.executar`, seis locais meteorológicos | Os nomes `pressao`, `radiacao`, `vento`, `vento_dir`, `chuva_rate`, `chuva_evento` nunca são lidos; o produtor sempre inclui as chaves e o dicionário completo segue para persistência. | Baixo; retirar acessos deixa de validar implicitamente um payload interno malformado | caracterizar o fluxo de `executar`; persistência já testada | remover as atribuições, não os campos nem a persistência |
| `updater.py` e campanha, `conn.row_factory` | `database.get_db()` já define `sqlite3.Row` em toda conexão. Testes acessam linhas por nome. | Baixo | testes de fila e campanha | remover as duas redefinições; na campanha, remover também o import `sqlite3` que ficará morto |
| `routes/public.py`, `dias_chuva` | A variável só é criada e passada ao template; nenhum template, include, teste ou script a lê. Hash inicial de `GET /` registrado para comparação. | Muito baixo | testes de cadastro + hash de resposta antes/depois | remover bloco e imports exclusivos |
| CSS `.gauge-marks`, `.valor-grande`, seis `.card-*`, `.atualizando`/`pulseData` | Zero referência em HTML, Jinja ou JavaScript; não há JS first-party externo nem construção dinâmica desses nomes. | Baixo | smoke visual desktop/mobile/dark | remover regras e keyframe órfãos |
| CSS dark para classes exatas `bg-slate-50/50` e `bg-slate-50/80` | As classes exatas não existem. Os usos vivos são `hover:bg-slate-50/80` e têm seletor separado, que será mantido. | Baixo | busca cruzada + smoke dark | retirar apenas quatro itens das listas |
| `.gitignore`, entradas repetidas | `estacao.db`, segundo `alert_state.json`, segundo `venv/`, segundo `__pycache__/` e extensões individuais já são cobertos por padrões anteriores/geral, confirmado por `git check-ignore`. | Muito baixo | matriz `git check-ignore` após edição | remover duplicatas sem reduzir cobertura |

### CONSOLIDAR

| Arquivo/símbolo | Equivalência comprovada | Risco | Proteção/validação | Ação |
|---|---|---|---|---|
| `updater.telefone_alerta` | É idêntico a `unsubscribe_tokens.telefone_com_codigo_pais`: filtra dígitos e prefixa país quando necessário. | Baixo | teste de enfileiramento confere número normalizado | usar helper compartilhado |
| `updater.valor_float` | Mesmo fallback e mesmas exceções de `acumulados.valor_float`; updater já depende de `acumulados`. | Baixo/médio | testes de acumulados e virada diária | importar o helper existente |
| `public.registrar_evento_cadastro` e `admin.registrar_evento_admin` | Ambos apenas encaminham todos os argumentos a `database.registrar_cadastro_evento`, sem regra. | Baixo | testes de cadastro, cancelamento e edição admin | chamar helper central diretamente |
| `admin.minutos_desde` e `health_check.minutos_desde` | Mesma conversão local, diferença em minutos e clamp em zero. | Médio por mudar ponto de mock | novo teste direto + testes admin/health | mover uma implementação a `time_utils` |
| `index.html`, máscaras de telefone | Dois listeners têm corpo idêntico. | Médio/baixo | smoke no navegador | extrair helper mantendo os dois inputs |
| `index.html`, abrir/fechar modais | Os pares diferem apenas pelo ID do modal. | Médio por scroll/ESC/backdrop | smoke no navegador | criar helpers parametrizados e manter nomes públicos chamados pelo HTML |
| `style.css`, botões de tema/fechar | Declarações base, hover e media são idênticas. | Baixo | smoke visual | agrupar seletores |
| `style.css`, menu dark | O seletor dark repete posição/dimensões do seletor base; só cor/borda/backdrop e `right` são overrides. | Médio/baixo pela cascata responsiva | smoke desktop/mobile/dark | manter somente overrides reais |
| link de `style.css` nos quatro documentos-raiz | Todos incluem `common_head_assets.html` e repetem o mesmo link imediatamente depois. | Baixo | renderização + busca por um link por documento | centralizar no include comum |
| README | A mesma informação aparece em várias seções e há contradições objetivas com árvore, requirements, banco e workers atuais. | Baixo, documental | cruzar novamente com código e comandos | reescrever de forma concisa após a limpeza |

### MANTER

| Componente | Evidência/justificativa | Risco evitado | Teste/proteção |
|---|---|---|---|
| todas as rotas e templates | entradas HTTP e referências Jinja reais | quebra de clientes e páginas | mapa Flask e matriz de templates |
| todos os arquivos estáticos | todos referenciados; fontes são fallbacks do bundle | quebra visual/navegadores legados | matriz de referências + smoke visual |
| `services/__init__.py`, `workers/__init__.py` | marcadores de pacote; testes importam `workers.*` | diferenças entre execução/import/tooling | imports da suíte |
| inserção única de `BASE_DIR` em cada worker | necessária ao comando direto com imports top-level atuais | worker não iniciar em produção | smoke direto |
| slug Ambient e fallback | override configurável com fallback testado | perda de coleta no deploy sem variável | três testes de configuração |
| estado em banco + JSON e caminhos legados | recuperação, migração e compatibilidade | perda/repetição de alertas | teste de migração parcial |
| schemas, `garantir_coluna`, campos e índices | migrações incrementais e compatibilidade com bancos existentes | banco antigo deixar de abrir | suíte de persistência/admin/workers |
| modos CLI do sender | contratos operacionais confirmados | quebra de cron/operação | `--help` + testes do sender |
| catches amplos em limites de processo/fallback | mantêm worker vivo ou previsão degradável | queda do processo; alteração seria observabilidade/arquitetura | cobertura parcial; não alterar nesta limpeza |
| todas as dependências | uso direto ou de produção configurável | falha de instalação/runtime | venv limpo ao final |
| `.vscode/settings.json` | preferência de editor compartilhada pode ser intencional | remoção sem prova de inutilidade | não é runtime; manter |

### INCERTO

| Candidato | Por que a evidência é insuficiente | Risco | Decisão recomendada |
|---|---|---|---|
| parâmetro `uv` em `verificar_alertas` e validade UV | não participa de condição, mas há teste explícito de que UV não gera alerta e a assinatura pode ser usada externamente | apagar intenção/contrato interno | manter; decisão de domínio humana |
| inserção de `ESTACAO_DIR` repetida entre arquivos de teste | consolidável, porém cada teste hoje executa isoladamente | quebrar execução individual | manter até definir bootstrap oficial de testes |
| empty-state de `historico.html` | a API atual devolve dias zerados, então a condição não ocorre, mas ela expressa fallback de UX | remover recuperação futura | manter |
| variável opcional `titulo` em `layout.html` | nenhuma rota atual fornece, mas é um ponto de extensão barato | quebrar extensões externas | manter |
| base Jinja comum para três templates standalone | há boilerplate, mas o ganho é modesto e faltam snapshots completos | refatoração de layout fora do escopo | não fazer agora |
| `estado_alertas.json` no `.gitignore` | não há referência no código atual, mas pode ser nome de estado legado no host | rastrear acidentalmente estado operacional | manter o padrão ignorado |
| campanha única em produção | tem testes e persistência idempotente, mas não há cron/systemd versionado | apagar ferramenta operacional pontual | manter até confirmação do operador |
| unit files, cron/timers e scripts externos | estão fora do repositório | conclusão falsa sobre uso/dependências | validar no host sem expor EnvironmentFile |

## Contradições documentais a corrigir

- caminho local absoluto de uma estação de desenvolvimento;
- versão exata do Flask onde o requisito define uma faixa;
- afirmações de que `requirements.txt` é enorme e contém bibliotecas que já não constam nele;
- afirmação de que Gunicorn não está listado, embora esteja;
- árvore e seção de testes incompletas;
- descrição do slug como apenas hardcoded, sem precedência do ambiente;
- ausência do worker WhatsApp, health check, campanha e seus modos/variáveis;
- tabelas e índices atuais omitidos;
- estado descrito como apenas JSON, apesar de banco primário + fallback/migração;
- sugestão de criar health check e migração para banco que já existem;
- rota de edição admin e favicon omitidos;
- afirmações sobre arquivo de estado inexistente e apenas dois processos.

## Lacunas e riscos residuais antes da alteração

- não é possível provar a configuração real de systemd/cron/scripts fora do repositório;
- não há testes de webhook, Open-Meteo, todos os contratos JSON nem falhas completas de estado DB/JSON;
- JavaScript, tema, menu e modais não têm testes automatizados;
- SQLite + Evolution oferece entrega ao menos uma vez: uma queda após aceite externo e antes do commit pode duplicar mensagem;
- limiter em memória não é compartilhado entre múltiplos workers Gunicorn;
- imports top-level exigem que os comandos continuem partindo de `estacao/`.

As ações classificadas como `INCERTO` ou `MANTER` não serão removidas nesta limpeza.

## Resultado da implementação

### Resumo executivo

A limpeza foi aplicada somente aos candidatos classificados como seguros ou equivalentes. Não houve alteração de rota, contrato JSON, schema SQLite, regra meteorológica, limite de alerta, política de retry, mensagem enviada, timezone, variável de ambiente ou comando operacional.

Resultados objetivos:

- 53 testes na linha de base e 57 testes ao final, todos aprovados;
- 4 testes de caracterização acrescentados;
- 22 regras Flask ao final, sendo 21 rotas do projeto e a rota estática nativa, iguais ao inventário inicial;
- hash, tamanho e status de `GET /` idênticos antes e depois da remoção de `dias_chuva`;
- nenhuma dependência removida e nenhuma quebrada no ambiente limpo;
- nenhum arquivo de produção removido;
- aproximadamente 1.655 linhas versionadas removidas no diff, das quais 1.370 pertenciam ao README redundante/obsoleto; cerca de 285 remoções brutas ficaram em código, CSS, templates e configuração;
- revisão independente do diff sem bloqueadores.

### Arquivos modificados

- configuração e documentação: `.gitignore`, `README.md`;
- Python: `routes/admin.py`, `routes/public.py`, `time_utils.py`, `workers/updater.py`, `workers/health_check.py`, `workers/enviar_aviso_whatsapp_unico.py`;
- interface: `static/css/style.css`, `templates/index.html`, `templates/layout.html`, `templates/admin_login.html`, `templates/admin_painel.html`, `templates/unsubscribe.html`, `templates/includes/common_head_assets.html`;
- testes: `tests/test_updater_alerts.py`.

Arquivos criados:

- `CLEANUP_REPORT.md`;
- `tests/test_time_utils.py`;
- `tests/test_worker_entrypoints.py`.

Arquivos removidos: **nenhum**. Todos os templates, estáticos, módulos, entrypoints e artefatos de compatibilidade sem evidência conclusiva foram mantidos.

### Remoções e consolidações efetivadas

- retirada a segunda manipulação equivalente de `sys.path` do updater; a inserção necessária à execução direta foi mantida;
- retiradas seis atribuições locais não lidas em `updater.executar`, sem retirar qualquer campo do payload ou da persistência;
- retiradas duas redefinições redundantes de `row_factory` e o import `sqlite3` que ficou morto na campanha;
- retirados `public.registrar_evento_cadastro` e `admin.registrar_evento_admin`; as chamadas agora chegam diretamente ao helper existente do banco;
- retirados `updater.telefone_alerta` e `updater.valor_float`; foram reutilizados, respectivamente, `unsubscribe_tokens.telefone_com_codigo_pais` e `acumulados.valor_float`;
- consolidadas as duas implementações de `minutos_desde` em `time_utils.minutos_desde`;
- retirado o contexto Jinja morto `dias_chuva` e seus imports exclusivos; o HTML de `/` permaneceu byte a byte equivalente;
- removidos seletores/keyframes CSS sem nenhuma referência e overrides dark para classes exatas inexistentes;
- agrupadas declarações CSS idênticas de botões e retiradas repetições do menu dark que eram herdadas integralmente;
- centralizado o carregamento de `style.css` no include comum, permanecendo exatamente uma inclusão em cada documento-raiz;
- consolidados os pares equivalentes de abertura/fechamento de modal e os dois listeners equivalentes de máscara de telefone;
- consolidados padrões repetidos do `.gitignore`, com matriz de cobertura aprovada;
- reescrito o README a partir da árvore, dos parsers e das configurações atuais, reduzindo contradições e documentação duplicada.

### Testes criados e atualizados

`tests/test_updater_alerts.py` ganhou uma caracterização que comprova:

- persistência da leitura antes dos acumulados e alertas;
- preservação do dicionário meteorológico completo;
- parâmetros exatos enviados à avaliação de alertas.

`tests/test_time_utils.py` protege a implementação compartilhada de `minutos_desde`, incluindo offset local, entrada UTC ingênua, piso de minutos, clamp de valores futuros e entrada inválida.

`tests/test_worker_entrypoints.py` protege:

- carregamento direto de `workers/updater.py` a partir do diretório operacional;
- exposição de `--once`, `--limite`, `--intervalo` e `--retry-failed` pelo sender.

A suíte passou de 53 para 57 casos; nenhum teste foi excluído, relaxado ou silenciado.

### Validação executada

| Validação | Resultado |
|---|---|
| `python -m unittest discover -s tests -v` antes das mudanças | 53 testes, `OK` |
| `python -m compileall estacao tests` antes das mudanças | aprovado |
| suíte e `compileall` após cada grupo de mudanças | aprovados, salvo a verificação intermediária descrita abaixo |
| `python -m unittest discover -s tests -v` final | 57 testes, `OK` |
| `python -m compileall estacao tests` final | aprovado |
| instalação de `estacao/requirements.txt` em `.venv-cleanup` | aprovada |
| suíte com `.venv-cleanup` | 57 testes, `OK` |
| `pip check` em `.venv-cleanup` | nenhuma dependência quebrada |
| Ruff `F401,F841,F821,F822,F823` | aprovado |
| Vulture com confiança mínima de 80% | nenhum candidato reportado |
| `git check-ignore` para banco, estados, venvs, bytecode, logs e `.env` | cobertura preservada |
| mapa real de rotas | 21 rotas do projeto + `static`, sem mudança |
| `GET /` antes/depois | status `200`, 38.884 bytes e SHA-256 `dcffd020671b3772723c13826eb090da1a05ae350291133fb78294f325cba832` nos dois lados |
| renderização de páginas | respostas esperadas e um único link de `style.css` por documento-raiz |
| sintaxe do JavaScript inline via Node | aprovada |
| interações de modal/máscara em DOM simulado | aprovadas |
| `git diff --check` | aprovado |

A primeira execução da linha de base dentro do sandbox não conseguiu criar bancos SQLite no diretório temporário do Windows. A mesma suíte foi repetida fora dessa restrição e passou; isso foi uma limitação do ambiente, não uma falha preexistente.

Durante a consolidação de `minutos_desde`, uma execução intermediária detectou que `tests/test_admin_users.py` acessa `routes.admin.agora_local` como atributo do módulo. O teste não foi alterado para esconder a incompatibilidade: o símbolo foi restaurado como reexportação explícita, e as suítes direcionada e completa passaram depois disso.

O navegador integrado foi selecionado para o smoke visual, mas seu bootstrap falhou antes de abrir a aplicação por ausência de metadado de sandbox (`sandboxPolicy`). Portanto, não houve inspeção visual real. A validação disponível foi renderização Flask, busca cruzada de referências, sintaxe JavaScript e DOM simulado. Isso é registrado abaixo como risco residual, sem apresentar o smoke como concluído.

O venv auxiliar das ferramentas estáticas, o banco descartável do smoke e o servidor local foram encerrados/removidos. `.venv-cleanup` foi mantido ignorado para reproduzir a instalação limpa solicitada; nenhum banco ou arquivo de estado operacional foi removido.

### Dependências

Nenhuma entrada de `requirements.txt` foi removida. A instalação limpa confirmou que todas as oito entradas são instaláveis e coerentes. Gunicorn, tzdata e o extra Redis do Flask-Limiter foram mantidos por uso operacional/configurável, mesmo sem import direto equivalente em todos os casos.

### Compatibilidade mantida deliberadamente

- fallback do slug da Ambient Weather e precedência da variável de ambiente;
- banco e arquivo JSON no estado dos alertas;
- todas as tabelas, colunas, índices e migrações incrementais;
- WAL, durabilidade, timeouts, retry, transações e idempotência;
- timezone `America/Campo_Grande` e campos de horário legados;
- todas as rotas, templates, estáticos e formatos JSON;
- todos os modos CLI dos workers;
- import/reexportação `routes.admin.agora_local` observado pela suíte;
- catches de fronteira de processo e fallback sem evidência suficiente para estreitamento;
- todas as variáveis e comandos de produção inventariados.

### Itens incertos e riscos residuais

Permanecem os candidatos já classificados como `INCERTO`, além destes limites da validação:

- é necessário um smoke manual desktop/mobile, em tema claro/escuro, antes do merge, especialmente para menu e modais;
- scripts externos poderiam importar os helpers internos removidos, embora não exista evidência disso no repositório, README anterior, testes ou comandos versionados;
- a consolidação de `minutos_desde` muda o ponto de mock interno: para controlar o relógio do helper compartilhado é necessário mockar `time_utils.agora_local`;
- units, timers, cron e scripts de deploy externos ainda precisam ser conferidos no host;
- faltam testes completos dos webhooks, de todos os contratos JSON e das combinações de falha banco/JSON;
- a entrega Evolution continua sendo ao menos uma vez e pode duplicar uma mensagem em uma queda entre o aceite externo e o commit local;
- o limiter em memória não é compartilhado entre workers Gunicorn;
- os entrypoints continuam dependendo do diretório de trabalho `estacao/`, compatibilidade que foi preservada em vez de virar uma refatoração arquitetural.

### Estado do diff na conclusão

O `git diff --stat` cobre os 16 arquivos versionados modificados:

```text
16 files changed, 376 insertions(+), 1655 deletions(-)
```

Como `CLEANUP_REPORT.md` e os dois novos módulos de teste ainda são arquivos não rastreados, eles não aparecem nesse resumo até serem adicionados ao índice. A branch permanece apenas local, sem commit, push, merge ou pull request.
