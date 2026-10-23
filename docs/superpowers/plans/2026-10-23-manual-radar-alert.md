# Envio manual de alerta de radar

**Objetivo:** com os alertas públicos automáticos desligados, permitir ao administrador enviar aos assinantes o mesmo alerta de radar que recebeu.

**Decisões:** preservar texto e observação somente após sucesso do envio administrativo; mostrar prévia e confirmação no monitoramento; manter o controle automático desligado. Usar estado persistente e fila existentes, sem migração ou dependências novas. A autorização manual tem identidade própria e não depende do episódio automático. Continuam valendo validade da observação, condição meteorológica atual e consentimento dos destinatários.

**Plano de implementação:**

- [x] Testar e implementar persistência do último alerta recebido, enfileiramento atômico, repetição e expiração em `services/nowcasting_manual_alerts.py`, integrando o sucesso de `nowcasting_test_alerts.py`.
- [x] Testar e implementar entrega manual em `workers/whatsapp_sender.py`, preservando o texto aprovado e as verificações de destinatário.
- [x] Testar e implementar POST administrativo com autenticação/CSRF e cartão com prévia, confirmação, horário e resultado em `routes/nowcasting.py` e `templates/monitoramento.html`.
- [x] Executar testes relevantes e suíte completa, revisar o diff com outro agente e documentar o uso.

**Verificação:** testes com SQLite temporário e WhatsApp simulado; sem envios reais. Cobrir alerta não recebido, falha administrativa, troca de alerta, controle público ativo, dados inválidos, expiração, duplicação concorrente, rollback e cancelamento de assinatura.

**Revisão:** 15 testes do serviço/fila e 15 testes administrativos; conferência visual em desktop e celular. A revisão independente identificou e confirmou a correção do bloqueio de entrega quando os controles persistidos estão inválidos. Uma expectativa antiga de ausência do link Radar foi reproduzida no código original e corrigida para a navegação existente.

**Resultado final:** `python -m unittest discover -s tests -q`: 636 testes, sucesso com 1 ignorado. `python -m compileall -q estacao tests` e `git diff --check`: sucesso.
