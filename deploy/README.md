# Backup da escola

O arquivo oficial é `deploy/backup_escola.sh`. Ele roda como `servidor`, com
PATH explícito e `set -Eeuo pipefail`. Não para a aplicação nem o updater.

`estacao/workers/backup_db.py` usa `sqlite3.Connection.backup()` em lotes de
1024 páginas (4 MiB com páginas de 4 KiB), sem sleep no callback. A origem é
aberta em modo somente leitura; não há `cp`, VACUUM, migrações ou alterações
de registros. O timeout das conexões é 30 segundos; retries por BUSY/LOCKED
esperam 0,1 segundo. O callback limita a cópia online a 7200 segundos
(`--max-segundos`), informa avanços a cada 25% e emite um sinal de atividade
a cada 60 segundos. Escritas concorrentes podem fazer o progresso retroceder.
O prazo do callback não é um timeout global de quick_check/compressão/upload.

Cada cópia usa uma pasta temporária privada no filesystem do destino. Depois
de finalizar a cópia, o worker configura DELETE **somente no backup**, executa
`PRAGMA quick_check`, fecha as conexões e publica o arquivo com hard link
atômico, sem sobrescrever um destino existente. A existência de auxiliares
antigos com o mesmo nome de destino também impede o backup, sem removê-los.
Falhas e interrupções tratáveis
removem apenas essa pasta e seus auxiliares. Erros na CLI retornam código 1.
Os diretórios de destino devem estar em filesystem local com suporte a hard links.

## Caminhos de produção

| Finalidade | Caminho |
| --- | --- |
| Python do backup | `/var/www/EstacaoEmPython/estacao/venv/bin/python` |
| Worker | `/var/www/EstacaoEmPython/estacao/workers/backup_db.py` |
| SQLite Secretaria | `/var/www/data/secretaria.db` |
| Uploads/PDFs | `/var/www/secretaria/sistema_escolar_root/sistema_escolar/public/uploads` |
| SQLite Estação | `/var/www/EstacaoEmPython/estacao/estacao.db` |
| Backups Secretaria e uploads | `/var/backups/escola/secretaria/` |
| Backups Estação | `/var/backups/escola/estacao/` |
| Logs | `/var/backups/escola/logs/backup_YYYY-MM-DD_HH-MM.log` |
| Executável rclone | `/usr/bin/rclone` |
| Configuração rclone | `/home/servidor/.config/rclone/rclone.conf` |
| Lock persistente | `/var/backups/escola/backup_escola.lock` |

Os caminhos foram informados pelo responsável pelo servidor. O script valida
origens, executáveis, configuração e diretórios antes da cópia. Não usa o
ambiente `.venv` do updater e não inclui credenciais no repositório.

Os nomes locais são `secretaria_YYYY-MM-DD_HH-MM.db.gz`,
`uploads_YYYY-MM-DD_HH-MM.tar.gz` e `estacao_YYYY-MM-DD_HH-MM.db.gz`.
Os dois bancos passam pelo mesmo worker SQLite. Os uploads são arquivados
com tar. Toda compressão usa `gzip -1`, com validação `gzip -t`.
As operações pesadas usam `ionice -c3` e `nice -n 19`.

Antes de criar temporários ou iniciar qualquer cópia, o script verifica o
espaço disponível para o usuário no filesystem de `/var/backups/escola`, com
`stat -f` (blocos disponíveis × tamanho do bloco). Os destinos de Secretaria
e Estação precisam estar nesse mesmo filesystem; uma montagem diferente
provoca erro antes das cópias.

A estimativa usa bytes, sem supor que os bancos continuarão com o tamanho atual:

- `E` e `S`: tamanhos reais dos bancos Estação e Secretaria por `stat -L`,
  somados aos respectivos WALs existentes, de forma conservadora.
- `T`: tamanho aparente dos uploads por `du`, contando hard links separadamente,
  mais 16 KiB por entrada para cabeçalhos, nomes longos e padding do tar,
  mais 10 KiB de fechamento do arquivo. Nenhum conteúdo é compactado nessa medição.
- `G(x) = x + ceil(x / 100) + 64 KiB`: estimativa gzip que não presume redução.
- `subtotal = E + S + G(E) + G(S) + G(T)`.
- `mínimo inicial = subtotal + ceil(subtotal / 5) + 1 GiB`.

Essa conta reserva simultaneamente bancos crus, os gzip e o tar.gz, com
**20% mais 1 GiB de margem adicional**. O log informa componentes, disponível
e mínimo necessário. Se faltar espaço, a execução termina com erro antes de
copiar, compactar, aplicar retenção ou chamar rclone. Não apaga backups para
abrir espaço. A medição é repetida antes do tar/gzip de uploads, da cópia da
estação e de cada gzip dos bancos, considerando os artefatos ainda por criar.
Na compactação dos bancos, usa os tamanhos efetivos das cópias SQLite concluídas.

O script remove backups completos e logs desses padrões com mais de 7 dias
(10080 minutos), sem recursão, depois de publicar os novos backups locais.
Isso ocorre mesmo se o upload posterior falhar. Arquivos de outros padrões,
parciais antigos e arquivos da aplicação nunca entram nessa limpeza.
Uma segunda execução no mesmo minuto falha se os destinos já existirem.

`flock` permite uma única execução por vez. Contenção é registrada e retorna
0; falhas de aquisição do lock retornam erro. Não exclua o arquivo de lock.
Todo stdout/stderr é redirecionado ao log, inclusive Python, gzip e rclone;
falhas anteriores à abertura do log aparecem no stderr do cron.

## Google Drive

Cada execução envia somente os três artefatos recém-criados, validados,
compactados e publicados localmente. São três chamadas sequenciais de
`rclone copyto`, com checksum, `--transfers 1` e `--checkers 2`, nesta ordem:

| Arquivo local atual | Destino remoto |
| --- | --- |
| `/var/backups/escola/secretaria/secretaria_YYYY-MM-DD_HH-MM.db.gz` | `gdrive:BackupsServidor/secretaria/secretaria_latest.db.gz` |
| `/var/backups/escola/secretaria/uploads_YYYY-MM-DD_HH-MM.tar.gz` | `gdrive:BackupsServidor/secretaria/uploads_latest.tar.gz` |
| `/var/backups/escola/estacao/estacao_YYYY-MM-DD_HH-MM.db.gz` | `gdrive:BackupsServidor/estacao/estacao_latest.db.gz` |

Não é enviada a pasta de backups, logs, temporários, auxiliares SQLite ou
históricos locais. O histórico de 7 dias permanece exclusivamente local.
Todas as chamadas rclone usam também `--contimeout 1m --timeout 10m
--retries 3 --low-level-retries 3 --retries-sleep 30s`. O primeiro timeout limita
o estabelecimento de conexão; o segundo limita inatividade de I/O. Não há
timeout global de duração: um upload lento pode continuar enquanto transfere.
As tentativas são limitadas tanto por operação HTTP quanto por transferência.
Consulte a [documentação dos limites do rclone](https://rclone.org/docs/#timeout-duration).
Não há exclusão ou renomeação prévia de nenhum latest. Se qualquer upload
retornar erro, o log identifica o artefato, o script encerra com erro e não
executa nenhuma limpeza remota. Os três backups locais completos permanecem.
Os uploads são independentes: se um upload posterior falhar, os anteriores
já concluídos permanecem atualizados; não há rollback dos três arquivos.

Somente depois de **todos os três uploads** concluírem, dois comandos
`rclone delete` limpam os históricos reconhecidos no nível superior de cada
pasta, com `--max-depth 1` e filtros ordenados que protegem os latest:

- `estacao/`: `estacao_YYYY-MM-DD_HH-MM` seguido exclusivamente de `.db`,
  `.db.gz`, `.db-journal`, `.db-wal` ou `.db-shm`. Esses formatos legados
  foram confirmados pelo administrador e são removidos apenas no Drive.
- `secretaria/`: somente `secretaria_YYYY-MM-DD_HH-MM.db.gz` e
  `uploads_YYYY-MM-DD_HH-MM.tar.gz`.

A revisão do código e do histórico disponível no repositório não identificou
outros nomes legados da Secretaria. Por isso, seus filtros permanecem restritos
aos dois formatos comprovados acima; não se presume que ela tenha produzido
os mesmos legados da Estação. Outros formatos exigem confirmação antes de
entrar na limpeza automática. Os três latest são sempre preservados.

O padrão de data aceita apenas dígitos nas posições indicadas. Todo outro
nome é excluído da limpeza. Atalhos do Drive são ignorados na escrita/limpeza
para não percorrer outros destinos. Nenhum comando percorre subpastas, usa
purge ou opera sobre `BackupsServidor/` inteiro, logs ou qualquer outra pasta.
As listagens finais incluem atalhos e precisam corresponder exatamente a:

```text
BackupsServidor/
├── estacao/
│   └── estacao_latest.db.gz
└── secretaria/
    ├── secretaria_latest.db.gz
    └── uploads_latest.tar.gz
```

Objetos inesperados, arquivos de outros padrões, subpastas ou nomes duplicados
são preservados e provocam erro com as listagens no log para inspeção do
operador. Não há remoção automática de subpastas ou deduplicação. Revisões
internas e lixeira do Google Drive não são eliminadas pelo script.

### Inspeção de objetos inesperados na migração

`teste-rclone.txt` e `arquivo-desconhecido.txt`, por exemplo, não são backups
reconhecidos. Eles permanecem no Drive e fazem a verificação final retornar
erro. Para listar somente nomes não reconhecidos e todos os diretórios, sem
remover nada ou percorrer subpastas, execute no servidor:

```bash
(
set -Eeuo pipefail
for pasta in estacao secretaria; do
  sudo -u servidor /usr/bin/rclone lsjson "gdrive:BackupsServidor/$pasta/" \
    --max-depth 1 --drive-skip-shortcuts=false --drive-skip-dangling-shortcuts=false \
    --config /home/servidor/.config/rclone/rclone.conf \
    --contimeout 1m --timeout 10m --retries 3 --low-level-retries 3 |
  /var/www/EstacaoEmPython/estacao/venv/bin/python -c '
import json, re, sys
pasta = sys.argv[1]
data = r"[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{2}-[0-9]{2}"
padroes = {
    "estacao": rf"(?:estacao_latest\.db\.gz|estacao_{data}\.db(?:\.gz|-journal|-wal|-shm)?)",
    "secretaria": rf"(?:secretaria_latest\.db\.gz|uploads_latest\.tar\.gz|secretaria_{data}\.db\.gz|uploads_{data}\.tar\.gz)",
}
for item in json.load(sys.stdin):
    if item["IsDir"] or not re.fullmatch(padroes[pasta], item["Name"]):
        print(json.dumps({"pasta": pasta, "nome": item["Name"], "diretorio": item["IsDir"]}, ensure_ascii=False))
' "$pasta"
done
)
```

A saída é apenas uma lista para inspeção, nunca entrada para uma exclusão em
lote. Se o administrador decidir remover **somente** `teste-rclone.txt`, use
o caminho literal, primeiro em simulação e depois com confirmação interativa:

```bash
sudo -u servidor /usr/bin/rclone deletefile \
  'gdrive:BackupsServidor/estacao/teste-rclone.txt' \
  --config /home/servidor/.config/rclone/rclone.conf --drive-use-trash=true --dry-run

sudo -u servidor /usr/bin/rclone deletefile \
  'gdrive:BackupsServidor/estacao/teste-rclone.txt' \
  --config /home/servidor/.config/rclone/rclone.conf --drive-use-trash=true --interactive
```

Não use curingas, um caminho de diretório ou qualquer nome latest nesse comando.
`deletefile` opera sobre um único arquivo e não aplica filtros de proteção;
confira o caminho mostrado antes de confirmar. [Documentação do rclone](https://rclone.org/commands/rclone_deletefile/).

Referências: [API de backup do SQLite no Python](https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.backup),
[rclone copyto](https://rclone.org/commands/rclone_copyto/) e
[exclusão com filtros](https://rclone.org/commands/rclone_delete/).

## Instalação após o git pull

Execute no Ubuntu. A sessão atual de desenvolvimento não acessou o servidor;
as verificações abaixo confirmam os caminhos e permissões antes da instalação.
Na primeira instalação, aguarde o backup antigo terminar: uma versão anterior
sem flock não participa do novo bloqueio. Não encerre a aplicação ou updater.

```bash
pgrep -af '[b]ackup_escola.sh|[w]orkers/backup_db.py'

cd /var/www/EstacaoEmPython
/bin/bash -n deploy/backup_escola.sh
sudo -u servidor test -x /var/www/EstacaoEmPython/estacao/venv/bin/python
sudo -u servidor /var/www/EstacaoEmPython/estacao/venv/bin/python \
  /var/www/EstacaoEmPython/estacao/workers/backup_db.py --help
sudo -u servidor test -r /var/www/data/secretaria.db
sudo -u servidor test -r /var/www/EstacaoEmPython/estacao/estacao.db
sudo -u servidor test -r /var/www/secretaria/sistema_escolar_root/sistema_escolar/public/uploads
sudo -u servidor test -x /var/www/secretaria/sistema_escolar_root/sistema_escolar/public/uploads
sudo -u servidor test -r /home/servidor/.config/rclone/rclone.conf
sudo -u servidor /usr/bin/rclone version
command -v flock ionice nice gzip tar find mktemp stat du wc sort

sudo install -d -o servidor -g "$(id -gn servidor)" -m 0750 \
  /var/backups/escola /var/backups/escola/secretaria \
  /var/backups/escola/estacao /var/backups/escola/logs
sudo install -d -o root -g root -m 0755 /var/www/deploy
sudo install -o root -g root -m 0755 \
  /var/www/EstacaoEmPython/deploy/backup_escola.sh \
  /var/www/deploy/backup_escola.sh.new
sudo /bin/bash -n /var/www/deploy/backup_escola.sh.new
sudo mv -T /var/www/deploy/backup_escola.sh.new /var/www/deploy/backup_escola.sh
sudo -u servidor test -x /var/www/deploy/backup_escola.sh
sudo -u servidor crontab -l
```

Pare se alguma verificação falhar. Não altere permissões dos bancos para
contornar falhas: confira as permissões que a aplicação já utiliza. Bancos
em WAL exigem acesso aos auxiliares SQLite. A configuração do rclone e seu
diretório precisam permitir a renovação de tokens pelo usuário `servidor`.
Se o cron já chama `/var/www/deploy/backup_escola.sh` às 22h, ele permanece
válido; não acrescente outra entrada. A linha equivalente no crontab de
`servidor` é `0 22 * * * /var/www/deploy/backup_escola.sh`.

## Teste manual e conferência

O primeiro comando executa o fluxo real, incluindo upload e limpeza remota
limitada às pastas da Estação e da Secretaria:

```bash
sudo -u servidor /var/www/deploy/backup_escola.sh
echo "Código de saída: $?"
sudo -u servidor /bin/bash -c \
  'tail -n 100 "$(ls -1t /var/backups/escola/logs/backup_*.log | head -n 1)"'

sudo -u servidor /usr/bin/rclone lsf \
  gdrive:BackupsServidor/estacao/ --max-depth 1 \
  --config /home/servidor/.config/rclone/rclone.conf
sudo -u servidor /usr/bin/rclone lsf \
  gdrive:BackupsServidor/secretaria/ --max-depth 1 \
  --config /home/servidor/.config/rclone/rclone.conf
```

A primeira listagem deve mostrar `estacao_latest.db.gz`; a segunda,
`secretaria_latest.db.gz` e `uploads_latest.tar.gz`, sem outros objetos.
Para validar automaticamente, inclusive detectar diretórios/duplicatas:

```bash
(
set -Eeuo pipefail
estacao=$(sudo -u servidor /usr/bin/rclone lsf \
  gdrive:BackupsServidor/estacao/ --max-depth 1 \
  --config /home/servidor/.config/rclone/rclone.conf)
secretaria=$(sudo -u servidor /usr/bin/rclone lsf \
  gdrive:BackupsServidor/secretaria/ --max-depth 1 \
  --config /home/servidor/.config/rclone/rclone.conf | LC_ALL=C sort)
test "$estacao" = 'estacao_latest.db.gz'
test "$secretaria" = $'secretaria_latest.db.gz\nuploads_latest.tar.gz'
)
echo "Verificação do Drive: $?"
```

Para exercitar apenas o worker com a estação funcionando, sem Drive e sem
sobrescrever backups, use um novo destino:

```bash
sudo -u servidor /bin/bash <<'BASH'
set -Eeuo pipefail
teste=$(mktemp -d /var/backups/escola/estacao/.teste-backup-XXXXXXXX)
echo "Backup manual de teste: $teste/estacao.db"
/usr/bin/ionice -c3 /usr/bin/nice -n 19 \
  /var/www/EstacaoEmPython/estacao/venv/bin/python \
  /var/www/EstacaoEmPython/estacao/workers/backup_db.py \
  --origem /var/www/EstacaoEmPython/estacao/estacao.db "$teste/estacao.db"
# O próprio worker valida quick_check. O arquivo fica disponível para inspeção.
BASH
```

Esse teste isolado não adquire o lock do script; execute quando o backup
agendado não estiver ativo. O diretório de teste não entra na retenção diária.

## Validação automatizada e limites

Execute a suíte completa em uma cópia de desenvolvimento descartável, com as
dependências instaladas e **sem `.env` de produção**. Configurações locais de
senha/branch interferem nos testes de rotas e webhook existentes. Os comandos
abaixo assumem a raiz dessa cópia e o Python do seu ambiente de testes:

```bash
python -m unittest discover -s tests -p 'test_backup*.py' -v
python -m unittest discover -s tests -v
python -m py_compile estacao/workers/backup_db.py
/bin/bash -n deploy/backup_escola.sh
```

Os testes usam bancos temporários (inclusive escrita concorrente em WAL e
DELETE), falhas injetadas e shell real com rclone simulado. Não usam o Drive
real. O teste de flock real requer Linux; os testes portáveis simulam também
contenção e erro de aquisição.

Não há garantia de duração em hardware de produção. Escrita intensa pode
reiniciar etapas da cópia online; o prazo impede retries indefinidos. O
quick_check faz leitura integral do backup. A verificação de espaço não reserva
blocos: outros processos e o crescimento dos dados podem consumir espaço após
a medição; quotas, inodes e falhas de disco também podem impedir uma gravação.
Nesses casos, a execução falha preservando as origens. `ionice` depende
do escalonador de I/O e não elimina a carga de disco.

Os uploads não são um snapshot transacional: mudanças detectadas pelo tar
provocam erro e impedem a publicação de um arquivo possivelmente incompleto.
O backup dos dois bancos não representa uma transação conjunta entre sistemas.
SIGKILL, queda de energia ou falha do filesystem podem deixar temporários;
nenhum código consegue tratar SIGKILL. O script não busca nem tenta reparar
resíduos antigos. Falhas de rede são registradas e preservam os backups
locais; os testes simulados não verificam permissões, versão/configuração
do rclone ou comportamento real da conta Google Drive.
