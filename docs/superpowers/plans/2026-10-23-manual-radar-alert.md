# Envio manual de alerta de radar

**Objetivo:** com os alertas públicos automáticos desligados, permitir ao administrador enviar aos assinantes o mesmo alerta de radar que recebeu.

**Decisões:** preservar texto e observação somente após sucesso do envio administrativo; mostrar prévia e confirmação no monitoramento; manter o controle automático desligado. Usar estado persistente e fila existentes, sem migração ou dependências novas. A autorização manual tem identidade própria e não depende do episódio automático. A validade da observação e a condição meteorológica atual são verificadas no momento da autorização administrativa; depois que o lote é autorizado e enfileirado, sua entrega não é invalidada por mudança de snapshot ou pelo vencimento do TTL durante o fan-out. Continuam valendo na entrega a integridade da autorização persistida, o fail-safe dos controles, o consentimento e o estado atual dos destinatários e as proteções de envio da Evolution.

**Plano de implementação:**

- [x] Testar e implementar persistência do último alerta recebido, enfileiramento atômico, repetição e expiração em `services/nowcasting_manual_alerts.py`, integrando o sucesso de `nowcasting_test_alerts.py`.
- [x] Testar e implementar entrega manual em `workers/whatsapp_sender.py`, preservando o texto aprovado e as verificações de destinatário.
- [x] Testar e implementar POST administrativo com autenticação/CSRF e cartão com prévia, confirmação, horário e resultado em `routes/nowcasting.py` e `templates/monitoramento.html`.
- [x] Executar testes relevantes e suíte completa, revisar o diff com outro agente e documentar o uso.
- [x] Corrigir a revalidação pós-autorização para que um lote manual já aprovado não seja cancelado parcialmente por `invalidated` ou `expired` enquanto percorre a fila.

**Verificação:** testes com SQLite temporário e WhatsApp simulado; sem envios reais. Cobrir alerta não recebido, falha administrativa, troca de alerta, controle público ativo, dados inválidos, expiração antes da autorização, duplicação concorrente, rollback, cancelamento de assinatura e conclusão do lote após expiração/mudança meteorológica posterior à autorização.

**Revisão:** a autorização continua recusando alertas expirados ou meteorologicamente invalidados antes do enfileiramento. A entrega de um evento já autorizado passa a revalidar somente a autorização persistida e as condições operacionais/destinatário, evitando broadcasts parciais causados pelo tempo de processamento da própria fila.

**Resultado final:** validar em CI a suíte completa, compilação e verificações estáticas antes do merge.
