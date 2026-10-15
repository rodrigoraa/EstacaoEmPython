# Ativação dos alertas de chuva pelo radar

As alterações são preparadas no projeto local e ativadas no servidor depois da
transferência. Não é necessário iniciar workers nem enviar mensagens no computador
local. O fluxo é `radar_updater → SQLite → nowcasting_updater → alertas_fila →
whatsapp_sender → Evolution API`.

## Configuração

Mescle [radar-alerts.env.example](radar-alerts.env.example) ao `estacao/.env` do
servidor, sem substituir as opções existentes. É necessário obter uma chave
REDEMET e preencher `REDEMET_API_KEY`. Preserve `EVOLUTION_URL`,
`EVOLUTION_API_KEY` e `EVOLUTION_INSTANCE`. A instância Evolution precisa estar
conectada ao WhatsApp. O diagnóstico verifica presença das configurações; por
padrão não consulta a REDEMET. A opção `--check-source` consulta apenas metadados
do radar. Nenhum modo comprova conexão ou entrega da Evolution.

O bloco habilita coleta, análise e envio público. Os destinatários são os
assinantes ativos com opt-in confirmado. O modo de teste administrativo permanece
desabilitado. Não há envio de mensagens de teste aos assinantes na instalação.

`RADAR_ALERTS_ENABLED=true` serve como fallback para o motor e os alertas públicos.
As opções explícitas `NOWCASTING_ENABLED` e `NOWCASTING_ALERTS_ENABLED` prevalecem
inclusive quando estão em `false`. O controle salvo em `/admin/monitoramento`
prevalece sobre a configuração de envio do ambiente: confira que **Alertas
públicos por radar** está ativado. O painel não inicia os workers.

## Atualização no servidor

Os comandos abaixo usam os caminhos existentes deste projeto no servidor. Ajuste
o usuário e os caminhos se a instalação for diferente. Faça o backup habitual do
SQLite e reinicie os workers após transferir os arquivos e atualizar o `.env`.
Para aplicar o schema idempotente, sem coletar ou enviar mensagens:

```bash
cd /var/www/EstacaoEmPython/estacao
./venv/bin/python init_db.py
./venv/bin/python -m workers.radar_alert_status --json
```

O diagnóstico é somente leitura: não migra o banco, não coleta imagens e não
enfileira/envia alertas. `ready_to_start` indica que flags e credenciais permitem
iniciar. `operational` também exige schema e imagens/análises atuais. Antes da
primeira coleta, é esperado `operational=false` e código de saída 1. O campo
`config_blockers` lista pendências para iniciar; `operational_blockers` lista
pendências de dados. Os valores das chaves, telefones e mensagens não são exibidos.

### Aviso de radar desatualizado

A idade é contada do horário original da imagem, e não do horário da coleta ou
da análise. Enquanto superar 15 minutos, os preventivos de radar ficam bloqueados.
Uma imagem recebida com 11 minutos de idade pode vencer durante a espera de cinco
minutos do coletor antigo. O padrão agora é `RADAR_POLL_SECONDS=60`, contando o
tempo de processamento dentro do intervalo. Um valor `300` já presente no `.env`
ou no ambiente do serviço precisa ser alterado explicitamente e o coletor
reiniciado para usar a cadência nova.

Radar e monitoramento atualizam a tela automaticamente a cada minuto quando a
aba está visível e sem interação com formulários. A atualização da tela apenas
relê o estado persistido; não coleta nem envia alertas.

Depois de transferir os arquivos novos, compare a imagem local com a origem:

```bash
./venv/bin/python -m workers.radar_alert_status --check-source
```

Esse comando consulta somente os metadados REDEMET, sem baixar imagens, alterar
o banco ou enviar mensagens. Ele mostra horário da imagem, coleta, publicação
local e respectivos atrasos. `collector_behind_source` indica imagem mais recente
disponível na origem; `source_delayed` indica que até a imagem mais recente da
origem já ultrapassa a validade. Nesse segundo caso, consultas mais frequentes
não tornam a imagem atual: aguarde a origem publicar uma nova observação.

`No module named workers.radar_alert_status` indica que o módulo novo ainda não
está na cópia executada no servidor. Transfira também os arquivos novos do
projeto, além dos arquivos modificados, antes de executar o diagnóstico.

São necessários três processos contínuos:

```bash
./venv/bin/python -m workers.radar_updater
./venv/bin/python -m workers.nowcasting_updater
./venv/bin/python -m workers.whatsapp_sender
```

Cada comando ocupa seu processo; use os serviços existentes para mantê-los
executando. O sender atual também entrega os alertas observados da estação.
Reutilize esse serviço se ele já existir.

Se ainda não houver serviços, o modelo
[estacao-worker@.service.example](estacao-worker@.service.example) pode ser
instalado como `/etc/systemd/system/estacao-worker@.service`, após conferir `User`,
`WorkingDirectory` e `ExecStart`. Todos os processos devem usar o mesmo SQLite e
ter acesso ao `.env` e a `RADAR_DATA_DIR`. Se o Gunicorn executar com outro
usuário, configure um `Group` compartilhado e as permissões das pastas para que
ele consiga ler os PNGs. A máscara `0007` do modelo permite travessia das novas
pastas pelo grupo. Depois:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now estacao-worker@radar_updater
sudo systemctl enable --now estacao-worker@nowcasting_updater
sudo systemctl enable --now estacao-worker@whatsapp_sender
```

Se o sender já estiver em outro serviço, reinicie o serviço existente em vez de
habilitar uma segunda instância. Consulte o estado e os logs:

```bash
sudo systemctl status estacao-worker@radar_updater estacao-worker@nowcasting_updater
sudo journalctl -u estacao-worker@radar_updater -u estacao-worker@nowcasting_updater -n 100
./venv/bin/python -m workers.radar_alert_status --json
```

## Comportamento esperado

Uma área de chuva média ou superior precisa apresentar movimento consistente em
pelo menos quatro imagens distintas, cobrindo dez minutos. A área dos pixels
reais precisa ter passagem projetada sobre o raio de 5 km de São José, em até 120
minutos. O centro pode passar ao lado ou já estar afastando enquanto outra parte
da faixa ainda entra no distrito. Uma área inteira afastando, um eco lateral que
não atinge o alvo ou um eco fixo permanecem sem envio preventivo.

O envio público exige duas imagens novas elegíveis do mesmo track. Uma única
consulta inicial, mesmo contendo histórico, pode ainda ficar aguardando a
segunda confirmação. Não aumente o limite de idade para encobrir atrasos de
coleta: imagem e mensagem usam a validade operacional padrão de 15 minutos.

Antes do HTTP, o sender revalida a previsão, o episódio e o consentimento do
destinatário. Perda de trajetória, imagem vencida, chuva já observada localmente,
controle desligado ou cancelamento do cadastro impedem o envio. Há controle de
repetição por episódio e escalonamento por intensidade.

A projeção é uma extrapolação observacional. As mensagens continuam indicando
chegada provável ou possível: mudança de movimento, crescimento/dissipação e
evaporação da chuva podem mudar o resultado. HTTP 2xx da Evolution indica aceite
pela API, sem confirmar entrega ou leitura no telefone.
