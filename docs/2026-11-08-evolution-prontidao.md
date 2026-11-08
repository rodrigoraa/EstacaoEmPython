# Proteção da fila durante inicialização da Evolution

## Evidência do incidente de 07/11

Horários locais UTC−4: Docker iniciou às 14:13:28, no mesmo segundo das primeiras
falhas de conexão dos envios. As respostas HTTP 500 apareceram às 14:13:48 e
14:14:08. A interface física perdeu carrier duas vezes durante a inicialização;
o DHCP atribuiu IPv4 somente às 14:14:20. O DNS local falhou em consultas do
Cloudflare até esse momento e o túnel conectou às 14:14:26.

A aplicação acessa a Evolution por localhost. Os registros demonstram envios
durante a inicialização da infraestrutura, mas não revelam o detalhe interno dos
HTTP 500 da Evolution. Os 141 itens consultados terminaram `enviado`: 132 na
primeira tentativa, nove na segunda. Esse estado indica aceitação pela API,
não confirmação de entrega no aparelho.

## Mudança

- Consultar `GET /instance/connectionState/{instanceName}` antes de cada lote
  do worker contínuo. Só liberar se a resposta documentada contiver
  `instance.state = open`. HTTP 200 sozinho não comprova prontidão.
- Em indisponibilidade, aguardar o intervalo sem fila (padrão cinco segundos)
  sem reivindicar mensagens ou incrementar tentativas. Registrar motivo apenas
  quando mudar, e registrar a retomada quando a instância conectar.
- Nos modos `--once` e `--limite`, adiar com código de saída 1 quando não pronta,
  mantendo a fila e o histórico intactos; executar novamente após recuperação.
- Preservar os métodos internos de envio e as retentativas de POST: uma queda
  depois da consulta de estado ainda pode ocorrer e deve seguir o backoff atual.
- Não registrar resposta bruta, chave, URL da instância, mensagem ou destinatário
  na verificação. Não seguir redirecionamentos com o cabeçalho de autenticação.
- Não alterar DNS, rede, contêineres, limites meteorológicos ou schema SQLite.

## Plano de validação

- [x] Reproduzir em testes a reivindicação prematura sem prontidão.
- [x] Validar endpoint somente leitura, estados não abertos, JSON inválido,
      códigos HTTP, destino inválido e ausência de vazamento de credenciais.
- [x] Exercitar CLI e loop contínuo com SQLite real e HTTP simulado: falha de
      conexão → conectado → desconectado → conectado, sem perder tentativas.
- [x] Confirmar que um POST que falha depois da prontidão mantém seu retry.
- [x] Executar suíte completa, compileall e revisão independente.

Validação sobre a `master`: 646 testes passaram; compileall, o teste JavaScript
`tests/test_form_guard.js` e git diff --check passaram. A adaptação preserva as
validações de URL e nome da instância já existentes nessa branch.
Revisão independente sem problemas críticos ou importantes; os dez testes de
prontidão também foram executados pelo revisor.

Referências oficiais consultadas: `EvolutionAPI/evolution-api`,
`src/api/routes/instance.router.ts` e `src/api/controllers/instance.controller.ts`.
Teste contra a instância de produção e atualização do servidor permanecem
separados; esta tarefa não dispara mensagens reais nem altera serviços remotos.
