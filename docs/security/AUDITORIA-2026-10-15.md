# Atualização após a auditoria

Este relatório conserva o diagnóstico anterior às alterações. As correções
implementadas estão em [CORRECOES-2026-10-15.md](CORRECOES-2026-10-15.md) e o
roteiro para aplicar no servidor está em [deploy/security.md](../../deploy/security.md).

# Auditoria de segurança da estação — 15/10/2026

**Resultado:** há proteções efetivas e falhas reproduzidas. O projeto não deve ser
classificado integralmente como seguro enquanto os achados e as verificações de
produção abaixo estiverem pendentes. Esta rodada valida o estado encontrado; não
altera aplicação, credenciais, banco operacional ou serviços.

Foram examinados código, schema, testes, configurações locais e dependências da
`.venv-cleanup`. Reproduções usaram banco temporário e mensageria simulada. A
verificação pública usou somente HEAD em `meteo.eesjv.com.br`, sem login, força
bruta, leitura de arquivos sensíveis ou chamadas aos webhooks de deploy.

**Evidência pública e seus limites:** a primeira sondagem recebeu 404 nos sete
caminhos de `.env`, `.git` e SQLite testados. `/admin` retornou cookie com Secure,
HttpOnly e SameSite=Lax; `/api/clima` não permitiu a origem externa testada. HSTS
e Cache-Control estavam ausentes. Uma sondagem posterior recebeu HTTP 530,
inclusive na página inicial. Isso impede confirmar o estado atual e o
redirecionamento HTTP→HTTPS; não comprova exposição de arquivos nem identifica a
causa da indisponibilidade. Registros: [sondagem inicial](http-check.initial.json)
e [sondagem posterior](http-check.public.json).

| Item solicitado | Resultado | Evidência e limite |
|---|---|---|
| `.env` exposta | Não observada nos caminhos testados | 404 em três variantes; não versionada nem encontrada no histórico Git disponível. Permissões do servidor e outras cópias não foram inspecionadas. |
| Validação do frontend | Parcial | `required` e máscaras existem; não há limites completos de tamanho/formato e podem ser ignorados pelo cliente. |
| Validação do backend | Falha confirmada | Nome/endereço com 2.000 caracteres aceitos; telefone fictício de 13 dígitos aceito no cadastro e telefone de um dígito aceito na edição administrativa. |
| SQL injection | Não encontrada no escopo testado | Valores HTTP usam parâmetros SQL. Identificadores dinâmicos analisados vêm de constantes/listas internas. Payload sintético foi armazenado como texto sem alterar tabelas. |
| Autenticação fraca | Risco de configuração e sessão | Hash bcrypt preferido, CSRF e limitação de tentativas existem. O fallback público de SECRET_KEY permite forjar sessão se o segredo não estiver configurado; não demonstrado no servidor. Sessões capturadas não são revogadas no logout. |
| IDOR | Não encontrado nos fluxos analisados | Alteração/exclusão por ID exige administrador; anônimo não modifica registros. APIs/imagens privadas retornam 401 sem sessão. Token antigo de cancelamento é um problema separado de escopo/replay. |
| Senhas diretamente no banco | Não encontradas no schema da estação | Usuários não têm campo senha. Administrador usa configuração de ambiente; localmente há hash bcrypt, sem senha administrativa em texto. |
| Ataque de força bruta | Proteção parcial | Login limitado a 5 POSTs/15 minutos/IP; sexta tentativa bloqueada em teste. Armazenamento por processo e proxy mal configurado reduzem a proteção. Não houve ataque ao site real. |
| Bloquear durante envio | Falha confirmada | Formulários permitem submissões repetidas; duas tentativas de cadastro pendente geram duas confirmações simuladas. A fila de alertas possui claim transacional, mas não garante entrega exatamente uma vez. |
| CSRF | Protegido no admin; ausente no público | Login/edição/exclusão/logout/controles validam token. Cadastro e solicitação pública de cancelamento aceitam POST cross-site, permitindo abuso de mensageria. |
| Upload sem validação | Sem endpoint público de upload | Não há uso de request.files. O worker baixa PNG de fonte externa, com riscos de tamanho/dimensões e URL descritos abaixo. |
| Revelação de informações | Melhorias necessárias | Erro bruto de conexão Evolution aparece em painel autenticado; faltam no-store e HSTS na sondagem bem-sucedida. Não houve retorno público de segredo nos testes. |
| Dependências vulneráveis | Confirmadas no ambiente local | OSV apontou pip, urllib3 e Werkzeug. Versões do servidor ainda precisam de confirmação; correspondência de versão não comprova exploração. |
| Tokens mal otimizados | Falha confirmada de escopo/replay | Cancelamento vale 90 dias, contém apenas telefone e pode excluir nova inscrição. Assinatura/expiração existem; não há consumo/revogação por inscrição. |
| Rate limit | Parcial | Há limites em rotas sensíveis, mas memory:// não é compartilhado entre processos; APIs públicas não têm política global. Proxy e backend efetivos do servidor precisam de confirmação. |
| Arquivos sensíveis expostos | Não observados nos caminhos testados | `.env`, `.git` e bancos retornaram 404 na primeira sondagem. Arquivos de radar exigem sessão e têm confinamento de caminho. Outras cópias/backups e regras Nginx não foram inspecionados. |
| Cookies inseguros | Atributos corretos na primeira sondagem pública | Secure/HttpOnly/Lax presentes no cookie de login público. O código permite Secure=false por configuração; flags locais não representam necessariamente o servidor. |
| SSRF | Falha condicional no worker | URLs HTTP(S) arbitrárias e redirects são aceitos no download do radar; destinos privados aceitos em teste simulado. Exige controle/adulteração da resposta REDEMET, sem parâmetro público de URL encontrado. |
| CORS | Sem permissão indevida observada | Não há liberação permissiva no código ou na sondagem da API com origem hostil. Política adicionada pelo proxy para outros caminhos não foi inspecionada. |

**Achados prioritários e ações propostas:**

1. **S01 — alta, condicional: fallback conhecido de chave de sessão.** Sem
   APP_ENV=production e SECRET_KEY, a aplicação usa `dev-only-change-me`. Em
   ambiente isolado, uma sessão fabricada foi aceita por API administrativa sem
   senha. O validador rígido depende de APP_ENV=production. Exigir segredo forte
   em qualquer instalação exposta, recusar o fallback fora de testes e verificar
   a configuração efetiva do serviço. Esta condição não foi comprovada na
   produção. Fontes: [app.py](../../estacao/app.py), linhas 23–24;
   [config.py](../../estacao/config.py), linha 261.

2. **S02 — alta/média conforme uso: dependências com avisos conhecidos.** A consulta
   autorizada à OSV cobriu 29 pacotes locais. Os avisos e versões corrigidas estão
   na tabela abaixo. Atualizar em ambiente isolado, testar e repetir a auditoria
   no ambiente exato do servidor. Não foram instaladas versões ou alterados
   requirements nesta validação.

3. **S03 — média: token antigo cancela uma inscrição nova.** Foi possível usar um
   token, recriar o cadastro com novo ID e mesmo telefone e reutilizar o token
   para excluir o novo registro. Vincular token ao ID e à geração da inscrição,
   reduzir validade conforme necessidade e consumir/revogar após uso. Referências:
   [unsubscribe_tokens.py](../../estacao/unsubscribe_tokens.py), linhas 7 e 45;
   [public.py](../../estacao/routes/public.py), linha 367.

4. **S04 — média: logout não revoga sessão capturada.** Cookie obtido antes do
   logout continuou aceito depois do logout em teste isolado. A autorização não
   consulta uma sessão revogável nem uma versão das credenciais. Usar sessão no
   servidor ou um identificador/epoch revogável, invalidando no logout e na troca
   de senha. Referências: [admin_auth.py](../../estacao/admin_auth.py), linha 9;
   [admin.py](../../estacao/routes/admin.py), linha 493.

5. **S05 — média: validação/canonicalização de telefone incompleta.** O mesmo
   destino com e sem 55 pode criar dois cadastros e receber duas mensagens. A
   normalização atual só remove caracteres não numéricos. Definir formato
   canônico antes de inserir/buscar, validar DDD/número e aplicar unicidade ao
   canônico, tratando duplicatas legadas antes da migração. Validar também
   tamanhos de nome/endereço no backend. Referências:
   [unsubscribe_tokens.py](../../estacao/unsubscribe_tokens.py), linha 18;
   [public.py](../../estacao/routes/public.py), linha 146;
   [admin.py](../../estacao/routes/admin.py), linha 440;
   [alert_queue_service.py](../../estacao/services/alert_queue_service.py), linha 85.

6. **S06 — média/condicional: limites por IP dependem da implantação.** Backend
   memory:// separa contadores entre workers. Com TRUST_PROXY=true e acesso direto
   à aplicação, variar X-Forwarded-For contornou o limite em teste isolado. Manter
   Gunicorn inacessível diretamente, sobrescrever cabeçalhos no proxy confiável,
   usar backend compartilhado e limitar também por destinatário nas rotas de
   mensageria. Não foi comprovada essa exposição de Gunicorn em produção.
   Referências: [app.py](../../estacao/app.py), linhas 26–55;
   [admin.py](../../estacao/routes/admin.py), linha 504.

7. **S07 — baixa/média: formulários públicos permitem abuso de mensagens.** POSTs
   anônimos cross-site foram aceitos sem CSRF; repetição de cadastro pendente
   acionou confirmação duas vezes. Isso não concede sessão administrativa.
   Adicionar CSRF/origem válida, bloquear botões durante envio, usar idempotência
   no backend e cooldown por telefone. Referências:
   [public.py](../../estacao/routes/public.py), linhas 135, 196 e 249;
   [index.html](../../estacao/templates/index.html), linhas 701, 781 e 834.

8. **S08 — média, condicional: SSRF e redirecionamentos externos.** O downloader de
   radar aceita host arbitrário, inclusive localhost/endereço de metadata em
   mocks. Validar HTTPS, hosts permitidos, resolução de endereço e cada redirect;
   bloquear destinos privados e limitar saída de rede. Chamadas Evolution com
   header `apikey` também seguem redirects: Requests preservou esse header para
   outra origem na reprodução sem rede. Bloquear redirects nas chamadas
   autenticadas. Referências: [radar_service.py](../../estacao/services/radar_service.py),
   linhas 127 e 233; [whatsapp_service.py](../../estacao/services/whatsapp_service.py),
   linha 48; [admin.py](../../estacao/routes/admin.py), linha 182.

9. **S09 — média, condicional: limites de recursos tardios.** O download do radar
   verifica 25 MiB depois de carregar o corpo completo. Limitar durante streaming
   e verificar dimensões/pixels antes da conversão da imagem. O app não define
   MAX_CONTENT_LENGTH próprio; conferir também limites no proxy. Não há upload
   público comprovado. Referências: [radar_service.py](../../estacao/services/radar_service.py),
   linha 239; [radar_analysis.py](../../estacao/services/radar_analysis.py), linha 916;
   [app.py](../../estacao/app.py), linha 18. A documentação Flask recomenda limites
   explícitos de recursos: [Security Considerations](https://flask.palletsprojects.com/en/stable/web-security/).

10. **S10 — baixa/melhoria: cabeçalhos e informações.** Ativar HSTS após verificar
    HTTPS, usar Cache-Control: no-store nas páginas privadas e sanitizar erros
    apresentados ao administrador. CSP ainda permite unsafe-inline; remover
    gradualmente com scripts externos/nonces, sem classificar isso sozinho como
    XSS explorável. Referências: [app.py](../../estacao/app.py), linha 79;
    [admin.py](../../estacao/routes/admin.py), linha 188.

**Dependências: avisos confirmados na instalação local, não no servidor.**

| Pacote local | Versão | Versão corrigida indicada | Impacto e referência |
|---|---:|---:|---|
| pip | 26.1.2 | 26.2.0 | URLs duplamente codificadas em índices maliciosos podem gravar arquivos em locais indevidos durante download/instalação; depende do fluxo de instalação, não de uma rota Flask. [CVE-2026-13346](https://github.com/advisories/GHSA-qwm4-qh6w-59xr). |
| urllib3 | 2.7.0 | 2.8.0 | Três avisos: configuração TLS de proxy HTTPS, loop de descompressão Deflate e consumo de memória por cabeçalho chunked. A aplicação faz chamadas HTTP externas, mas não foi executado exploit. [TLS/proxy](https://github.com/urllib3/urllib3/security/advisories/GHSA-8988-9cw3-xx77), [Deflate](https://github.com/urllib3/urllib3/security/advisories/GHSA-gh4c-6fx4-qh6g), [chunked](https://github.com/urllib3/urllib3/security/advisories/GHSA-vxq7-64xx-v4gw). |
| Werkzeug | 3.1.8 | 3.1.9 | safe_join pode aceitar dispositivos especiais com ADS vazio e bloquear leitura em Windows/NTFS. Relevante ao ambiente local Windows; essa condição específica não se aplica ao servidor Linux informado. [CVE-2026-102598](https://github.com/pallets/werkzeug/security/advisories/GHSA-g6x2-hccm-hh4m). |

GHSA e PYSEC podem descrever o mesmo aviso: os nove identificadores retornados
não representam nove falhas diferentes. Há cinco avisos únicos nessa consulta,
em três pacotes. Os demais 26 pacotes não tiveram correspondência na OSV para as
versões consultadas; isso não garante ausência de vulnerabilidades desconhecidas
ou de problemas em bibliotecas nativas incluídas nos wheels. As dependências não
estão fixadas em lockfile, então uma instalação nova pode ser diferente.

Os assets locais também identificam Chart.js 4.4.9 e Font Awesome Free 6.4.0.
As páginas de advisories dos mantenedores não exibiam avisos publicados na
consulta, mas esses arquivos não fazem parte dos 29 pacotes PyPI auditados e não
foram submetidos a uma análise completa de dependências npm ou de integridade.
Referências: [Chart.js](https://github.com/chartjs/Chart.js/security/advisories)
e [Font Awesome](https://github.com/FortAwesome/Font-Awesome/security/advisories).

Inventário aprovado: [dependency-inventory.local.json](dependency-inventory.local.json).
Resultado bruto sem segredos: [dependency-audit.local.json](dependency-audit.local.json).
A consulta externa foi realizada após autorização explícita para enviar apenas
nomes e versões desses 29 pacotes.

**Verificações executadas:** 33 testes existentes de segurança, administração,
cancelamento e autorização passaram com banco temporário e transporte HTTP
bloqueado. Houve reproduções adicionais de sessão, replay de token, duplicidade,
validação, SQL, XSS, SSRF e encaminhamento de apikey com mocks. As sondagens
públicas foram HEAD, sem tentativa de senha, alteração de cadastro ou envio de
alerta. Passar nos testes existentes não elimina os achados novos.

**Ainda exige acesso/configuração do servidor:** versões instaladas em seu venv;
ambiente efetivo do serviço, especialmente APP_ENV/SECRET_KEY/backend do limiter;
restrição de acesso direto ao Gunicorn; regras Nginx/cloudflared; permissões de
`.env`, SQLite e backups; TLS/redirecionamento após o HTTP 530; armazenamento e
retenção de dados pessoais. Nome, telefone e endereço ficam em texto no SQLite e
histórico, mesmo após remover o cadastro; proteger arquivos/backups e revisar
retenção. Não publicar valores de `.env`, cookies ou tokens ao responder ao
diagnóstico.
