# Mensagens de radar sem falsa chegada Implementation Plan

**Goal:** distinguir presença de chuva na área monitorada, entrada futura e falta de estimativa confiável nas mensagens de radar.

**Architecture:** manter detecção, seleção, projeção e autorização meteorológica; corrigir o gerador compartilhado `montar_mensagem_preventiva(snapshot)`. Testar o texto no evento, na fila, na revalidação automática e no caminho administrador → autorização manual.

**Tech Stack:** Python, unittest, SQLite temporário; sem dependências novas.

**Spec:** solicitação do usuário de 11/11/2026, resumida nas restrições e critérios abaixo. Implementação, testes, commit e push diretamente para master já autorizados.

## Restrições e critérios

- Preservar alterações locais e histórico remoto; sem outra branch ou force push.
- Sem deploy ou mensagens reais no WhatsApp.
- Não alterar raio, limiares, georreferenciamento ou seleção de ecos sem reprodução de defeito.
- Distância é a menor distância de um pixel original do eco à estação; centro é outro campo.
- Projeção considera pixels MEDIUM+ entrando no raio monitorado; ETA zero pode indicar interseção já presente, sem confirmar chuva no solo na estação.
- Distâncias positivas abaixo de 1 km: “menos de 1 km”; distância zero: “sobre ou muito próxima da estação”. Ausentes, negativos, booleanos e não finitos: distância indisponível.
- ETA confiável zero: área já presente, sem “se aproximando” ou contagem futura. ETA positivo abaixo de 1 minuto: “menos de 1 min”. Demais ETAs válidos: minutos aproximados.
- ETA deve indicar entrada na área monitorada e explicitar que não prevê chegada exata à estação.
- Sem tracking/projeção/confiança suficientes ou ETA inválido: informar ausência de estimativa confiável.
- Cobrir OBSERVACAO (texto informativo), INFORMATIVO (nome canônico), ATENCAO e ALERTA.

## Investigação realizada

Fluxo: `radar_updater/processar_frame` → `detectar_clusters` → `radar_clusters`/`radar_track_points` → `nowcasting_repository/_tracks_frame_atual`/`projetar_footprint` → `analisar_nowcasting`/`criar_alerta_preventivo` → snapshot persistido → avaliação pública → evento/fila → revalidação e texto atualizado no sender.

Teste administrativo usa o mesmo gerador; após envio bem-sucedido guarda a mensagem e snapshot. A autorização manual enfileira exatamente essa mensagem e preserva sua integridade na entrega.

Checkout inicial: master limpa em `39b111c3a402895bc3defbc1d798640599a4e606`, igual ao remoto após fetch. Nenhum AGENTS.md em disco no repositório ou ancestrais consultados; aplicam-se as instruções fornecidas na conversa.

Banco local consultado somente em modo leitura: 48 frames de 03/09/2026, 147 clusters (distância mínima 143,687 km), 50 snapshots sem distância/ETA no alerta preventivo, nenhum evento/fila. Não contém o alerta recebido nem reproduz zeros recorrentes de produção. Testes existentes já reproduzem ETA zero em footprint parcialmente dentro do raio.

## Tarefas

- [x] Rastrear todos os caminhos e examinar dados locais sem expor dados pessoais.
- [x] Criar regressões do gerador e observar falhas no código original.
- [x] Reproduzir com PNGs → projeção → snapshot → evento/fila e verificar texto manual/revalidado, usando transporte simulado.
- [x] Corrigir somente a composição compartilhada e atualizar expectativas antigas.
- [x] Executar suíte completa, compileall, teste JavaScript e diff --check; revisar o diff.
- [ ] Commit/push na master e confirmar hash remoto.

## Foco da revisão

Distância zero não comprova chuva no solo. Distância de eco fraco pode ser menor que a distância do footprint projetado. ETA de aproximação de borda legado não substitui ETA de entrada no raio. Ausência/invalidade não vira zero. Mensagens manuais históricas já recebidas continuam preservadas, sem reescrita retroativa.

## Verificação

`python -m unittest discover -s tests -v`

`python -m compileall estacao tests`

`node tests/test_form_guard.js`

`git diff --check`

Limite: dados locais não comprovam causa de todos os zeros recebidos em produção, nem o raio efetivo do servidor.

## Registro da execução

Regressões observadas antes da mudança: sete testes do gerador falharam nos casos novos; dois testes com PNGs chegaram à fila com “0 km”/“Estimativa de chegada: 0 min”; testes de administrador/manual, valores pequenos na fila pública e revalidação do sender também falharam pelo texto antigo. Após a correção, os sete testes do gerador e os casos novos ponta a ponta passaram.

A execução inicial da suíte (código de produção original) executou 646 testes e apresentou quatro erros em `EvolutionReadinessWorkerTest`, todos `WinError 32` no cleanup dos bancos temporários; um teste de flock foi ignorado por requerer Linux. O arquivo isolado reproduziu os mesmos quatro erros. Decisão: fechar explicitamente as quatro conexões de teste com `contextlib.closing`, preservando a transação de inserção. Documentação CPython consultada via Context7 confirma que o context manager de sqlite3 faz commit/rollback, mas não fecha a conexão. Nenhuma alteração no serviço da Evolution.

Revisão independente do diff: nenhum problema crítico, importante ou menor encontrado. O revisor executou sete testes de mensagem, oito testes com PNGs e dezesseis testes do fluxo manual, todos aprovados. A revisão confirmou que distância descritiva zero não decide presença de pixels MEDIUM+; essa condição continua baseada na projeção. A precisão herdada de três casas decimais do ETA pode representar como zero uma entrada inferior a 0,03 segundo; foi preservada por ser inferior à resolução em minutos da mensagem e não justificar mudança de cálculo meteorológico. Configuração efetiva e alertas reais de produção continuam fora das evidências locais.

Checks já concluídos: `compileall`, teste JavaScript de proteção de formulários e `git diff --check` aprovados. Auditoria atual do CI, `python -m pip_audit -r estacao/requirements.txt --strict`, executada em ambiente virtual isolado: nenhuma vulnerabilidade conhecida. Nenhum pacote da aplicação foi modificado.

Verificação final: `python -m unittest discover -s tests -v` executou 658 testes em 148,597 segundos: `OK (skipped=1)`. O único teste ignorado exige flock real no Linux. Os quatro erros de cleanup da execução inicial foram eliminados pelo fechamento explícito das conexões de teste. O hash do commit e a confirmação do push serão informados na entrega final.
