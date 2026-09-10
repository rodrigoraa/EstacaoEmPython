#!/bin/bash
set -Eeuo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
umask 077

readonly PYTHON=/var/www/EstacaoEmPython/estacao/venv/bin/python
readonly WORKER=/var/www/EstacaoEmPython/estacao/workers/backup_db.py
readonly SECRETARIA_DB=/var/www/data/secretaria.db
readonly UPLOADS=/var/www/secretaria/sistema_escolar_root/sistema_escolar/public/uploads
readonly ESTACAO_DB=/var/www/EstacaoEmPython/estacao/estacao.db
readonly BACKUP_ROOT=/var/backups/escola
readonly BACKUP_SECRETARIA=/var/backups/escola/secretaria
readonly BACKUP_ESTACAO=/var/backups/escola/estacao
readonly LOG_DIR=/var/backups/escola/logs
readonly RCLONE=/usr/bin/rclone
readonly RCLONE_CONFIG=/home/servidor/.config/rclone/rclone.conf
readonly REMOTO_ESTACAO=gdrive:BackupsServidor/estacao
readonly REMOTO_SECRETARIA=gdrive:BackupsServidor/secretaria
readonly PADRAO_DATA='[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]_[0-9][0-9]-[0-9][0-9]'
readonly LOCK=/var/backups/escola/backup_escola.lock

ETAPA=inicializacao
TMP_SECRETARIA=
TMP_ESTACAO=

erro() {
    local codigo=$?
    printf 'ERRO na etapa "%s" (código %s, linha %s).\n' "$ETAPA" "$codigo" "$1" >&2
    exit "$codigo"
}

limpar() {
    local codigo=$?
    trap - EXIT
    # Somente diretórios privados criados por esta execução, nunca backups antigos.
    if [[ -n "$TMP_SECRETARIA" ]]; then rm -rf -- "$TMP_SECRETARIA" || codigo=1; fi
    if [[ -n "$TMP_ESTACAO" ]]; then rm -rf -- "$TMP_ESTACAO" || codigo=1; fi
    if [[ "$codigo" != 0 ]]; then
        printf 'Backup encerrado com erro na etapa "%s" (código %s).\n' "$ETAPA" "$codigo" >&2
    elif [[ "$ETAPA" == concluido ]]; then
        echo 'Backup finalizado com sucesso.'
    fi
    exit "$codigo"
}

trap 'erro "$LINENO"' ERR
trap limpar EXIT
trap 'printf "Backup interrompido na etapa %s.\n" "$ETAPA" >&2; exit 130' INT
trap 'printf "Backup interrompido na etapa %s.\n" "$ETAPA" >&2; exit 143' TERM

# Os diretórios e permissões são preparados na instalação, não na aplicação.
[[ -d "$LOG_DIR" && -w "$LOG_DIR" ]]
DATA=$(date +%F_%H-%M)
readonly DATA
exec >>"$LOG_DIR/backup_$DATA.log" 2>&1
printf '\n===== BACKUP ESCOLA %s =====\n' "$DATA"

ETAPA=bloqueio
# Não apagar o lock: remover o inode permitiria duas execuções simultâneas.
exec 9>>"$LOCK"
if /usr/bin/flock -n -E 75 9; then
    :
else
    codigo=$?
    if [[ "$codigo" == 75 ]]; then
        echo 'Outro backup ainda está em execução; nova execução ignorada.'
        exit 0
    fi
    echo "ERRO ao adquirir flock (código $codigo)." >&2
    exit "$codigo"
fi

ETAPA=validacao
for arquivo in "$SECRETARIA_DB" "$ESTACAO_DB" "$WORKER" "$RCLONE_CONFIG"; do
    [[ -f "$arquivo" && -r "$arquivo" ]] || { echo "Arquivo ausente/ilegível: $arquivo" >&2; exit 1; }
done
for programa in "$PYTHON" "$RCLONE" /usr/bin/ionice /usr/bin/nice; do
    [[ -x "$programa" ]] || { echo "Executável indisponível: $programa" >&2; exit 1; }
done
[[ -d "$UPLOADS" && -r "$UPLOADS" && -x "$UPLOADS" ]]
for diretorio in "$BACKUP_ROOT" "$BACKUP_SECRETARIA" "$BACKUP_ESTACAO"; do
    [[ -d "$diretorio" && -w "$diretorio" && -x "$diretorio" ]]
done
for programa in gzip tar mktemp ln find rm stat du wc sort; do command -v "$programa" >/dev/null; done

readonly SAIDA_SECRETARIA="$BACKUP_SECRETARIA/secretaria_$DATA.db.gz"
readonly SAIDA_UPLOADS="$BACKUP_SECRETARIA/uploads_$DATA.tar.gz"
readonly SAIDA_ESTACAO="$BACKUP_ESTACAO/estacao_$DATA.db.gz"
for arquivo in "$SAIDA_SECRETARIA" "$SAIDA_UPLOADS" "$SAIDA_ESTACAO"; do
    [[ ! -e "$arquivo" && ! -L "$arquivo" ]] || { echo "Destino já existe: $arquivo" >&2; exit 1; }
done

leve() { /usr/bin/ionice -c3 /usr/bin/nice -n 19 "$@"; }
drive() {
    leve "$RCLONE" "$@" --config "$RCLONE_CONFIG" \
        --transfers 1 --checkers 2 --log-level INFO --stats 1m \
        --contimeout 1m --timeout 10m --retries 3 --low-level-retries 3 --retries-sleep 30s
}

# Estimativa por metadados: não lê o conteúdo dos bancos nem compacta uploads.
tamanho_banco() {
    local tamanho wal=0
    tamanho=$(/usr/bin/stat -Lc %s -- "$1")
    if [[ -e "$1-wal" ]]; then wal=$(/usr/bin/stat -Lc %s -- "$1-wal"); fi
    echo "$((tamanho + wal))"
}

estimativa_gzip() {
    # Não presume ganho de compressão: reserva entrada + 1% + 64 KiB.
    echo "$(( $1 + ($1 + 99) / 100 + 65536 ))"
}

verificar_espaco() {
    local minimo=$1 dados blocos tamanho_bloco disponivel
    dados=$(/usr/bin/stat -f -c '%a %S' -- "$BACKUP_ROOT")
    read -r blocos tamanho_bloco <<<"$dados"
    [[ "$blocos" =~ ^[0-9]+$ && "$tamanho_bloco" =~ ^[0-9]+$ && "$tamanho_bloco" -gt 0 ]]
    disponivel=$((blocos * tamanho_bloco))
    printf 'Espaço em %s: disponível=%s bytes; mínimo necessário=%s bytes; etapa=%s.\n' \
        "$BACKUP_ROOT" "$disponivel" "$minimo" "$ETAPA"
    if (( disponivel < minimo )); then
        echo 'ERRO: espaço insuficiente; nenhuma nova cópia ou compactação será iniciada.' >&2
        exit 1
    fi
}

ETAPA='verificacao de espaco'
# A estimativa conjunta só é válida quando os destinos usam o mesmo filesystem.
DISPOSITIVO=$(/usr/bin/stat -Lc %d -- "$BACKUP_ROOT")
for diretorio in "$BACKUP_SECRETARIA" "$BACKUP_ESTACAO"; do
    DISPOSITIVO_DESTINO=$(/usr/bin/stat -Lc %d -- "$diretorio")
    [[ "$DISPOSITIVO_DESTINO" == "$DISPOSITIVO" ]] || {
        echo "ERRO: $diretorio não está no filesystem de $BACKUP_ROOT." >&2; exit 1;
    }
done
TAMANHO_ESTACAO=$(tamanho_banco "$ESTACAO_DB")
TAMANHO_SECRETARIA=$(tamanho_banco "$SECRETARIA_DB")
# Conta hard links separadamente; /. mede o conteúdo mesmo se UPLOADS for symlink.
DADOS_UPLOADS=$(leve du --apparent-size --block-size=1 --summarize --count-links -- "$UPLOADS/.")
read -r TAMANHO_UPLOADS _ <<<"$DADOS_UPLOADS"
[[ "$TAMANHO_UPLOADS" =~ ^[0-9]+$ ]]
ITENS_UPLOADS=$(leve find "$UPLOADS/." -printf . | wc -c)
# Reserva cabeçalhos, padding, nomes longos e links do tar por entrada.
TAMANHO_TAR=$((TAMANHO_UPLOADS + ITENS_UPLOADS * 16384 + 10240))
GZIP_ESTACAO=$(estimativa_gzip "$TAMANHO_ESTACAO")
GZIP_SECRETARIA=$(estimativa_gzip "$TAMANHO_SECRETARIA")
GZIP_UPLOADS=$(estimativa_gzip "$TAMANHO_TAR")
SUBTOTAL=$((TAMANHO_ESTACAO + TAMANHO_SECRETARIA + GZIP_ESTACAO + GZIP_SECRETARIA + GZIP_UPLOADS))
MARGEM=$((1073741824 + (SUBTOTAL + 4) / 5))  # 1 GiB adicional mais 20%.
printf 'Estimativa: estação+WAL=%s; secretaria+WAL=%s; gzip bancos=%s; tar.gz=%s; margem=%s bytes.\n' \
    "$TAMANHO_ESTACAO" "$TAMANHO_SECRETARIA" "$((GZIP_ESTACAO + GZIP_SECRETARIA))" "$GZIP_UPLOADS" "$MARGEM"
verificar_espaco "$((SUBTOTAL + MARGEM))"

TMP_SECRETARIA=$(mktemp -d "$BACKUP_SECRETARIA/.backup-exec-XXXXXXXX")
TMP_ESTACAO=$(mktemp -d "$BACKUP_ESTACAO/.backup-exec-XXXXXXXX")

ETAPA='backup secretaria'
echo 'Backup secretaria...'
leve "$PYTHON" "$WORKER" --origem "$SECRETARIA_DB" "$TMP_SECRETARIA/secretaria.db"

ETAPA='backup uploads'
verificar_espaco "$((TAMANHO_ESTACAO + GZIP_ESTACAO + GZIP_SECRETARIA + GZIP_UPLOADS + MARGEM))"
echo 'Backup uploads...'
# pipefail detecta também erros do tar, incluindo arquivos alterados durante leitura.
leve tar -C "$UPLOADS" -cf - . | leve gzip -1 >"$TMP_SECRETARIA/uploads.tar.gz"
leve gzip -t "$TMP_SECRETARIA/uploads.tar.gz"

ETAPA='backup estacao'
verificar_espaco "$((TAMANHO_ESTACAO + GZIP_ESTACAO + GZIP_SECRETARIA + MARGEM))"
echo 'Backup estação...'
leve "$PYTHON" "$WORKER" --origem "$ESTACAO_DB" "$TMP_ESTACAO/estacao.db"

ETAPA=compactacao
echo 'Compactando bancos...'
# Usa os tamanhos efetivamente copiados, incluindo crescimento desde a estimativa.
TAMANHO_COPIA_ESTACAO=$(/usr/bin/stat -Lc %s -- "$TMP_ESTACAO/estacao.db")
TAMANHO_COPIA_SECRETARIA=$(/usr/bin/stat -Lc %s -- "$TMP_SECRETARIA/secretaria.db")
GZIP_ESTACAO=$(estimativa_gzip "$TAMANHO_COPIA_ESTACAO")
GZIP_SECRETARIA=$(estimativa_gzip "$TAMANHO_COPIA_SECRETARIA")
verificar_espaco "$((GZIP_ESTACAO + GZIP_SECRETARIA + MARGEM))"
leve gzip -1 "$TMP_SECRETARIA/secretaria.db"
verificar_espaco "$((GZIP_ESTACAO + MARGEM))"
leve gzip -1 "$TMP_ESTACAO/estacao.db"
leve gzip -t "$TMP_SECRETARIA/secretaria.db.gz" "$TMP_ESTACAO/estacao.db.gz"
# Hard links publicam arquivos completos sem sobrescrever destinos existentes.
ln -T -- "$TMP_SECRETARIA/secretaria.db.gz" "$SAIDA_SECRETARIA"
ln -T -- "$TMP_SECRETARIA/uploads.tar.gz" "$SAIDA_UPLOADS"
ln -T -- "$TMP_ESTACAO/estacao.db.gz" "$SAIDA_ESTACAO"

ETAPA='retencao local'
echo 'Aplicando retenção local de 7 dias...'
# Sem recursão; não remove .db, journals ou resíduos de execuções anteriores.
leve find "$BACKUP_SECRETARIA" -maxdepth 1 -type f \
    \( -name 'secretaria_????-??-??_??-??.db.gz' -o -name 'uploads_????-??-??_??-??.tar.gz' \) \
    -mmin +10080 ! -path "$SAIDA_SECRETARIA" ! -path "$SAIDA_UPLOADS" -print -delete
leve find "$BACKUP_ESTACAO" -maxdepth 1 -type f -name 'estacao_????-??-??_??-??.db.gz' \
    -mmin +10080 ! -path "$SAIDA_ESTACAO" -print -delete
leve find "$LOG_DIR" -maxdepth 1 -type f -name 'backup_????-??-??_??-??.log' \
    -mmin +10080 ! -path "$LOG_DIR/backup_$DATA.log" -print -delete

ETAPA='upload Google Drive: secretaria_latest.db.gz'
echo 'Enviando secretaria_latest.db.gz para Google Drive...'
# Somente artefatos completos publicados nesta execução, sem exclusão prévia.
drive copyto "$SAIDA_SECRETARIA" "$REMOTO_SECRETARIA/secretaria_latest.db.gz" --checksum \
    --drive-skip-shortcuts --drive-skip-dangling-shortcuts
echo 'Upload de secretaria_latest.db.gz concluído.'

ETAPA='upload Google Drive: uploads_latest.tar.gz'
echo 'Enviando uploads_latest.tar.gz para Google Drive...'
drive copyto "$SAIDA_UPLOADS" "$REMOTO_SECRETARIA/uploads_latest.tar.gz" --checksum \
    --drive-skip-shortcuts --drive-skip-dangling-shortcuts
echo 'Upload de uploads_latest.tar.gz concluído.'

ETAPA='upload Google Drive: estacao_latest.db.gz'
echo 'Enviando estacao_latest.db.gz para Google Drive...'
drive copyto "$SAIDA_ESTACAO" "$REMOTO_ESTACAO/estacao_latest.db.gz" --checksum \
    --drive-skip-shortcuts --drive-skip-dangling-shortcuts
echo 'Upload de estacao_latest.db.gz concluído.'

# Só alcançada se TODOS os uploads retornaram sucesso. Nunca percorre subpastas.
# A estação inclui os formatos legados confirmados; desconhecidos são preservados.
ETAPA='limpeza Google Drive: estacao'
echo 'Removendo backups históricos reconhecidos da pasta remota estacao...'
drive delete "$REMOTO_ESTACAO/" --max-depth 1 \
    --filter '- /estacao_latest.db.gz' \
    --filter "+ /estacao_${PADRAO_DATA}.db" \
    --filter "+ /estacao_${PADRAO_DATA}.db.gz" \
    --filter "+ /estacao_${PADRAO_DATA}.db-journal" \
    --filter "+ /estacao_${PADRAO_DATA}.db-wal" \
    --filter "+ /estacao_${PADRAO_DATA}.db-shm" --filter '- **' \
    --drive-skip-shortcuts --drive-skip-dangling-shortcuts

ETAPA='limpeza Google Drive: secretaria'
# Sem evidência de outros formatos legados da Secretaria, não ampliar os filtros.
echo 'Removendo backups históricos reconhecidos da pasta remota secretaria...'
drive delete "$REMOTO_SECRETARIA/" --max-depth 1 \
    --filter '- /secretaria_latest.db.gz' --filter '- /uploads_latest.tar.gz' \
    --filter "+ /secretaria_${PADRAO_DATA}.db.gz" \
    --filter "+ /uploads_${PADRAO_DATA}.tar.gz" --filter '- **' \
    --drive-skip-shortcuts --drive-skip-dangling-shortcuts

ETAPA='verificacao Google Drive'
LISTAGEM_ESTACAO=$(drive lsf "$REMOTO_ESTACAO/" --max-depth 1 \
    --drive-skip-shortcuts=false --drive-skip-dangling-shortcuts=false)
LISTAGEM_SECRETARIA=$(drive lsf "$REMOTO_SECRETARIA/" --max-depth 1 \
    --drive-skip-shortcuts=false --drive-skip-dangling-shortcuts=false | LC_ALL=C sort)
if [[ "$LISTAGEM_ESTACAO" != 'estacao_latest.db.gz' || \
      "$LISTAGEM_SECRETARIA" != $'secretaria_latest.db.gz\nuploads_latest.tar.gz' ]]; then
    echo 'ERRO: estado remoto inesperado; verifique objetos, duplicatas e subpastas.' >&2
    printf 'Listagem de %s/:\n%s\n' "$REMOTO_ESTACAO" "$LISTAGEM_ESTACAO" >&2
    printf 'Listagem de %s/:\n%s\n' "$REMOTO_SECRETARIA" "$LISTAGEM_SECRETARIA" >&2
    echo 'Objetos não reconhecidos foram preservados; nenhuma subpasta será removida automaticamente.' >&2
    exit 1
fi

ETAPA=concluido
