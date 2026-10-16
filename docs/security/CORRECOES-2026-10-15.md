# Correções e cobertura de segurança

O relatório [AUDITORIA-2026-10-15.md](AUDITORIA-2026-10-15.md) registra o estado
anterior às correções. Este documento registra o que foi implementado localmente.
O servidor ainda precisa receber código, dependências e configuração conforme
[deploy/security.md](../../deploy/security.md). Nenhum serviço de produção foi
alterado, banco real migrado ou mensagem WhatsApp enviada nesta execução.

| Ponto solicitado | Tratamento no projeto | Conferência no servidor |
|---|---|---|
| `.env` exposta | Segredos ignorados no Git; exemplo Nginx bloqueia arquivos ocultos; diagnóstico não imprime valores | Aplicar Nginx e permissões; conferir 404 em HTTPS funcional |
| Validação frontend | Tamanhos, formato de telefone e validação nativa; regex compatível com Unicode `v` | Publicar templates e JS juntos |
| Validação backend | Nome/endereço limitados; telefone brasileiro canônico, DDD e número validados; JSON webhook valida tipos | Migrar schema 10 |
| SQL injection | Consultas parametrizadas preservadas; payloads de teste não alteram o schema | Não adicionar interpolação de valores HTTP em SQL |
| Autenticação fraca | Produção exige bcrypt; chave de desenvolvimento aleatória; sessões revogáveis e limites absoluto/inatividade | Configurar segredo forte/hash e escolher senha forte |
| IDOR | Rotas privadas continuam exigindo sessão; tokens de cadastro vinculados ao ID e nonce atual | Conferir bloqueio sem login e novo login após atualização |
| Senhas no banco | Não existe senha de assinante; administrador usa hash bcrypt; banco guarda hash dos tokens de sessão | Remover ADMIN_PASSWORD em texto do ambiente |
| Força bruta | Login 5 tentativas/15 minutos/IP; proxy só aceita cabeçalhos de remetente confiável | Redis compartilhado e Gunicorn em loopback |
| Bloquear durante envio | Primeiro POST segue; repetição bloqueada, botões desativados e restaurados ao voltar; cooldown atômico por destinatário | Publicar JS novo; não ignorar cooldown no backend |
| CSRF | Token obrigatório em formulários públicos e privados; webhook usa sua assinatura HMAC | Nenhuma página com CSRF em cache compartilhado |
| Upload sem validação | Arquivos enviados às rotas são recusados; limites de corpo/formulário; PNG externo limitado/verificado | Limite 1 MiB também no Nginx |
| Revelação de informações | Erros externos sanitizados; no-store em páginas HTML/áreas privadas; CSP com nonce; HSTS | HTTPS, cabeçalhos e logs com acesso restrito |
| Dependências vulneráveis | pip 26.2.1, urllib3 2.8.0, Werkzeug 3.1.9 instalados localmente; mínimos corrigidos documentados; auditoria CI e Dependabot | Atualizar venv real e repetir consulta no inventário do servidor |
| Tokens | Salt v2, identidade aleatória por inscrição, validade limitada, cancelamento consumido; links antigos revogados | Informar necessidade de novos links; não reaproveitar tokens antigos |
| Rate limit | Limites globais e por rota; produção recusa memória e exige Redis; sem fallback silencioso | Redis acessível, protegido e compartilhado entre workers |
| Arquivos sensíveis | Nginx nega bancos, backups, chaves, código, logs e dotfiles; serviço com UMask 0077 | Aplicar permissões aos arquivos existentes e backups |
| Cookies | HttpOnly/Lax; produção exige Secure; no-store; cookie sozinho não autoriza sem sessão válida no servidor | HTTPS efetivo e reinício de todos os processos web |
| SSRF | Imagens somente no host HTTPS oficial DECEA, sem redirects; Evolution validada e sem redirects | Proteger DNS/TLS/egress; Evolution configurada pelo operador |
| CORS | Política de mesma origem preservada, sem permissões para origem hostil nos testes | Não liberar origens indiscriminadamente no proxy |

A migração é aditiva e repetível. Mantém registros e histórico; desativa números
legados inválidos e duplicatas, priorizando inscrição ativa com consentimento.
Cancelar explicitamente uma inscrição remove também suas duplicatas em quarentena,
registrando cada cancelamento no histórico. Isso evita que uma duplicata antiga
impeça nova inscrição futura. Tokens antigos não alcançam a inscrição nova.

O cooldown reserva o envio antes de chamar o provedor e confirma a transação antes
do HTTP. Conexões concorrentes obtêm uma única reserva; falhas mantêm o intervalo
de cinco minutos para reduzir repetição após timeout. A fila de alertas preserva
suas verificações de consentimento e claim. Uma queda após o provedor aceitar a
mensagem ainda depende da idempotência oferecida pelo provedor para garantir
entrega exatamente uma vez.

## Verificação das dependências

A [consulta original à OSV](dependency-audit.local.json) encontrou avisos em três
pacotes. Após atualização, a [nova consulta](dependency-audit.fixed.local.json)
avaliou os mesmos 29 nomes de pacotes com as versões instaladas e retornou zero
avisos conhecidos. Os dois inventários são locais, não representam o servidor.
O script [audit_dependencies.py](../../scripts/audit_dependencies.py) envia
somente nomes/versões quando executado explicitamente com `--osv`. A consulta
externa foi autorizada pelo responsável pelo projeto.

## Limites da validação

Resultado final local: **581 testes Python passaram**, além do teste JavaScript
de validação/submissão, `compileall` e `pip check`.

Testes usam bancos temporários e chamadas externas simuladas. Nenhuma exploração
ou tentativa de força bruta foi executada contra a produção. O teste Bash de
backup `test_backup_escola.py` precisa ser executado em Linux/CI; o Git Bash deste
sandbox Windows falha ao criar pipes de sinais. Os testes Python de backup SQLite
permanecem incluídos na suíte local. `compileall`, `pip check` e verificação
JavaScript também são executados.

As primeiras sondagens públicas devolveram 404 para arquivos sensíveis; uma
sondagem posterior recebeu HTTP 530. Esse estado não comprova uma proteção nova
nem permite validar a implantação. Depois da publicação é necessário confirmar
disponibilidade e repetir as verificações públicas descritas no roteiro.
