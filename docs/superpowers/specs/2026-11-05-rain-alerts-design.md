# Alertas de chuva: escalada e substituição

O pedido é impedir que um aviso de chuva inferior ainda pendente seja enviado
depois de um superior, e emitir automaticamente o aviso de 100 mm que hoje não
existe. Os eventos reais mostraram a geração correta e a inversão na fila.

Manter os limites de 30 e 50 mm e acrescentar nível 3 configurável, padrão 100 mm,
imediato e idempotente por dia. Usar o valor acumulado efetivamente observado.

Ao criar um nível superior, encerrar a campanha inferior de chuva inteira para
aquela data de referência. Cada novo nível vai para todos os inscritos ativos,
inclusive quem já recebeu o anterior. Não retomar o inferior mesmo quando há
falha no enqueue ou envio do superior para algum destinatário. Preservar envios
concluídos e envios em andamento. Registrar `substituido`, sem contar como envio
ou falha, e mostrar essa contagem no evento administrativo.

Abordagens: FIFO atrasaria os avisos críticos; apagar avisos perderia auditoria.
A substituição transacional preserva prioridade e histórico e foi a solução
solicitada pelo usuário. Executar ao enfileirar o superior e antes de reivindicar
um envio antigo, incluindo retentativas e recuperação de envios expirados.

Não enviar mensagens reais, alterar dados de produção nem fazer deploy nesta
tarefa. Uma requisição WhatsApp já em andamento não pode ser desfeita. O novo
nível não conhece campanhas manuais anteriores, fora dos eventos automáticos.
