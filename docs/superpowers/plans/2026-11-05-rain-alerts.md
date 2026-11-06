# Rain Alerts Implementation Plan

Escopo original de chuva concluído. A extensão posteriormente solicitada para
todos os alertas está registrada em `docs/2026-11-05-auditoria-alertas.md` e
substitui a restrição original de não alterar os demais tipos.

> **For agentic workers:** Use TDD and verify the complete change together.

**Goal:** substituir chuva pendente superada e emitir automaticamente aos 100 mm.
**Architecture:** manter o motor atual, adicionar nível 3; compartilhar atualização
de eventos e substituição transacional entre enfileiramento e worker de envio.
**Tech Stack:** Python 3.12, SQLite, unittest, Flask/Jinja.
**Spec:** ../specs/2026-11-05-rain-alerts-design.md

## Global Constraints

- Preservar os limites padrão 30/50 mm; novo nível padrão 100 mm.
- Campanha inteira na mesma data de referência; não interferir em outros tipos de alerta.
- Preservar mensagens enviadas e em andamento; não enviar WhatsApp real.
- Trabalhar no checkout isolado existente; sem worktree adicional ou deploy.

## Review Focus

- Falha ao enfileirar/enviar superior não pode retomar a campanha inferior.
- Fila anterior ao deploy e retentativas não podem ressuscitar níveis inferiores.
- Estado reiniciado, leitura decrescente e virada de dia precisam manter deduplicação.
- Substituição não incrementa tentativas nem histórico de envios/falhas.
- Migração aditiva e painel distinguem substituições de entregas e falhas.

## Task 1: Estágio de 100 mm

Files: updater.py, test_updater_alerts.py, README.md.
- [x] Testar 99.9/100 mm, passagem direta, persistência, validade e próximo dia (RED).
- [x] Acrescentar `chuva_3` e ramo nível 3 antes do nível 2 (GREEN).
- [x] Documentar novo estágio e executar testes do updater.

## Task 2: Substituição auditável

Files: services/alert_queue.py, database.py, workers/{updater,whatsapp_sender}.py,
routes/admin.py, templates/admin_painel.html, tests/test_rain_alert_queue.py.
- [x] Reproduzir fila 30/50 e cobrir escopo, falhas, retries e migração (RED).
- [x] Adicionar contador `substituidos` e compartilhar `atualizar_evento(conn, evento_id)`.
- [x] Substituir pendentes após enqueue superior na mesma transação e, no sender,
      pular candidatos superados antes de incrementar tentativas (GREEN).
- [x] Exibir contador/status no painel e validar sua rota.

## Task 3: Verificação e revisão

- [x] Rodar `python -m unittest discover -s tests -v`, compileall e git diff --check.
- [x] Revisar a mudança com agente independente e resolver falhas relevantes.
- [x] Entregar alterações locais testadas e instruções operacionais, sem deploy.

## Resultado

- 267 testes passaram; compileall e git diff --check passaram.
- Testes observaram RED antes das correções de chuva, fila e painel.
- Schema atualizado de 8 para 9 com migração aditiva e teste de preservação.
- Revisão independente sem problemas críticos; corrida de migração identificada
  foi reproduzida e corrigida com trava de escrita antes da preparação do sender.
- Alterações locais, sem commit, envio real ou deploy no servidor.

## Esclarecimento do usuário: campanha inteira

- Cada nível superior é distribuído a todos os inscritos, inclusive quem já recebeu o anterior.
- Encerrar todos os pendentes do nível inferior do mesmo dia, sem condicionar ao enqueue individual do superior.
- Quatro regressões reproduzidas em RED; 16 testes de fila em GREEN e suíte completa 267/267.
- Revisão independente sem regressões importantes no escopo atualizado.
