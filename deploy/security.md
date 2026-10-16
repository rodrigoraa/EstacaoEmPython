# Aplicação das correções de segurança

As correções são feitas no projeto local. Copiar o código não altera o Nginx,
Redis, permissões ou o ambiente Python do servidor. A produção passa a recusar
configuração insegura, em vez de iniciar com proteções desativadas.

## Antes de publicar

1. Faça um backup SQLite consistente com o worker já existente e preserve o
   `.env` atual fora de qualquer diretório público. Não sobrescreva o banco,
   `.env`, venv ou imagens operacionais com arquivos do projeto local.
2. Atualize o código, incluindo os novos módulos, templates e arquivos estáticos.
   Pare temporariamente os processos da estação durante a migração e atualização
   do ambiente Python. Mantenha os nomes reais dos serviços que você já utiliza.
3. Na pasta `/var/www/EstacaoEmPython/estacao`, atualize as dependências:

   ```bash
   ./venv/bin/python -m pip install --upgrade 'pip>=26.2.0'
   ./venv/bin/python -m pip install --upgrade -r requirements.txt
   ./venv/bin/python -m pip check
   ```

4. Instale Redis no próprio servidor, caso ainda não exista:

   ```bash
   sudo apt-get install redis-server
   sudo systemctl enable --now redis-server
   redis-cli -h 127.0.0.1 ping
   ```

   A resposta deve ser `PONG`. Mantenha Redis em loopback com `protected-mode yes`;
   não exponha a porta 6379 na rede. Em servidor compartilhado com usuários não
   confiáveis, configure ACL/senha ou socket Unix privado e use a URI correspondente.
   Os contadores precisam ser compartilhados entre todos os workers web. Falha
   no Redis impede requisições limitadas; não há fallback silencioso para memória.

5. Mescle [security.env.example](security.env.example) no `.env` existente.
   Use `APP_ENV=production`, URL HTTPS, segredos aleatórios com pelo menos 32
   caracteres, somente `ADMIN_PASSWORD_HASH` bcrypt custo 12 a 16 e Redis.
   Remova a variável `ADMIN_PASSWORD` em texto. Preserve segredos atuais fortes;
   trocá-los invalida sessões e links. Para criar um hash sem exibir a senha,
   execute no terminal privado do servidor:

   ```bash
   ./venv/bin/python - <<'PY'
   import bcrypt, getpass, os
   from dotenv import set_key
   try:
       terminal = os.open('/dev/tty', os.O_RDWR)
   except OSError:
       raise SystemExit('Execute em um terminal privado interativo.')
   os.close(terminal)
   senha = getpass.getpass('Nova senha administrativa (mínimo 12 caracteres): ')
   if len(senha) < 12 or len(senha.encode('utf-8')) > 72:
       raise SystemExit('Use pelo menos 12 caracteres e no máximo 72 bytes UTF-8.')
   if senha != getpass.getpass('Repita a senha: '):
       raise SystemExit('As senhas não coincidem.')
   set_key('.env', 'ADMIN_PASSWORD_HASH', bcrypt.hashpw(senha.encode(), bcrypt.gensalt(rounds=12)).decode())
   print('Hash salvo no .env; nenhuma senha ou hash foi exibido.')
   PY
   ```

   Para preencher apenas segredos ausentes/fracos sem exibi-los:

   ```bash
   ./venv/bin/python - <<'PY'
   import secrets
   from dotenv import dotenv_values, set_key
   config = dotenv_values('.env')
   for nome in ('SECRET_KEY', 'WEBHOOK_SECRET'):
       valor = config.get(nome) or ''
       if len(valor) < 32 or len(set(valor)) < 8:
           set_key('.env', nome, secrets.token_urlsafe(48))
           print(nome + ': novo segredo salvo; atualizar também o emissor que usa este segredo.')
   PY
   ```

   Se `WEBHOOK_SECRET` mudar, atualize o emissor autorizado do webhook antes de
   reabrir a coleta. Nunca exiba `.env` completo. O servidor precisa de HTTPS
   funcional antes de ativar cookies Secure/HSTS.

6. Execute uma vez a migração com o usuário do serviço:

   ```bash
   ./venv/bin/python - <<'PY'
   from dotenv import load_dotenv
   load_dotenv('.env')
   from database import init_db
   init_db()
   PY
   ```

   O schema 10 adiciona sessões revogáveis, identidade aleatória por inscrição,
   telefone canônico, unicidade e cooldown de mensagens. Preserva os registros e
   históricos. Duplicatas do mesmo destino ficam desativadas; prioriza-se um
   registro já ativo e com consentimento. Telefones legados inválidos também
   ficam desativados para revisão no painel. Não há envio nessa migração.
   Ela é repetível. Links antigos de confirmação/cancelamento deixam de valer;
   o usuário pode solicitar novos links. Sessões administrativas antigas exigem
   novo login. Cancelamento vale 7 dias por padrão, confirmação 24 horas; ambos
   ficam vinculados à inscrição atual. Solicitações por telefone respeitam
   intervalo mínimo de 5 minutos, inclusive após timeout do provedor.

7. Ajuste Nginx com [nginx-estacao.conf.example](nginx-estacao.conf.example),
   aproveitando os certificados existentes. O exemplo encaminha tudo ao Flask,
   bloqueia arquivos ocultos, bancos, backups, chaves e código; não publica a raiz
   do projeto. Sobrescreve cabeçalhos de proxy e mantém o Gunicorn em
   `127.0.0.1:8080`, como no [serviço web](estacao-web.service.example).
   Confira conflito de portas e adapte o serviço existente antes de instalar
   outro com o mesmo bind. Execute `sudo nginx -t` antes de recarregar.
   Os webhooks existentes executam scripts de deploy via `sudo -u servidor`.
   Por isso, este exemplo não ativa `NoNewPrivileges`, que bloquearia esse fluxo.
   Restrinja sudoers aos comandos realmente necessários e mantenha scripts de
   deploy fora de caminhos graváveis pelo público; não conceda `NOPASSWD: ALL`.
   Se houver Cloudflare/proxy anterior, configure `real_ip` somente com suas
   redes oficiais confiáveis, para os limites contarem o IP real. Não aceite
   livremente `CF-Connecting-IP` ou `X-Forwarded-For` enviados pela internet.

8. Restrinja `.env`, bancos, WAL/SHM, logs e backups ao usuário do serviço.
   O exemplo systemd define `UMask=0077`. Para os arquivos existentes, após
   confirmar que web e workers rodam como `servidor`:

   ```bash
   chmod 600 /var/www/EstacaoEmPython/estacao/.env
   chmod 600 /var/www/EstacaoEmPython/estacao/estacao.db
   ```

   Aplique o mesmo controle aos auxiliares e backups existentes, sem publicar
   esses diretórios. Código e venv devem ser legíveis pelo serviço; somente
   diretórios de dados precisam de escrita. Não compartilhe cópias de produção
   com o projeto local. Revise permissões/ACLs e retenção dos backups separadamente.

9. Reinicie os serviços existentes da estação e execute:

   ```bash
   ./venv/bin/python -m workers.security_status --json
   ./venv/bin/python -m workers.radar_alert_status --json
   ```

   Ambos são diagnósticos sem envio de mensagens. O primeiro não consulta a
   internet, não exibe segredos nem cria/altera o banco. Também não substitui a
   inspeção do ambiente efetivo de cada processo systemd. Corrija toda pendência
   e confirme os serviços ativos e Redis acessível antes de liberar o site.

## Conferência pública após a publicação

Confirme que a página inicial/login funcionam em HTTPS, que HTTP redireciona
para o domínio fixo e que estas requisições retornam 404: `/.env`, `/.git/config`,
`/estacao/.env`, `/estacao.db`, `/estacao/estacao.db` e caminhos reais de backup.
Uma resposta 5xx/530 não valida essa proteção; resolva a disponibilidade e repita.
Confirme `Secure; HttpOnly; SameSite=Lax`, HSTS, `Cache-Control: no-store, private`
nas páginas com formulários e CSP com nonce. Origem externa não deve receber
`Access-Control-Allow-Origin` permissivo. Não adicione `Access-Control-Allow-Origin: *`
ao proxy. Logout deve exigir POST+CSRF e inutilizar o cookie capturado anteriormente.

O projeto não oferece upload público e recusa arquivos enviados a suas rotas.
Imagens de radar são PNGs limitados por bytes/dimensões, obtidos exclusivamente
do host HTTPS oficial da REDEMET, sem redirects. A URL Evolution vem da
configuração privada do operador e pode apontar à rede local; restrinja também
o acesso de rede ao serviço. Certificados e DNS da origem continuam sendo
fronteiras de confiança da infraestrutura.

A CI verifica testes e consulta vulnerabilidades das dependências; Dependabot
propõe atualizações semanais. A aprovação local não garante ausência de novas
vulnerabilidades nem confirma as configurações do servidor que ainda não foi atualizado.
