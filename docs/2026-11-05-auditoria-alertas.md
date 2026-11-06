# Auditoria dos alertas

Revisão do código e testes locais, sem acesso ao banco de produção, envio real
de mensagens ou deploy. Estende a correção de chuva solicitada para os demais
alertas. Usa o checkout existente, sem worktree adicional.

## Fontes revisadas

| Tipo | Níveis padrão | Controle da ocorrência |
| --- | --- | --- |
| Chuva acumulada | 30 / 50 / 100 mm | Diário |
| Rajada de vento | 40 / 70 / 100 km/h | Diário; nível inicial observa janela configurada |
| Calor | 35 / 40 °C | Ciclo até recuperação abaixo de 33 °C |
| Frio | 12,4 / 5 / 2 °C | Ciclo até recuperação; nível inicial considera sensação válida |
| Baixa umidade | 30 / 20% | Ciclo até recuperação acima de 35% |

Confirmações, validade dos sensores e limites existentes foram mantidos.
Os novos testes exercitam todos os níveis do motor real, fila e estado SQLite.
Radar, estações regionais e nowcasting continuam observacionais e não enfileiram
avisos preventivos, mesmo quando a opção experimental de alertas está habilitada.
UV não produz alertas, conforme a regra existente. Links de cadastro/cancelamento
são transacionais, sem níveis meteorológicos; reenvio de confirmação continua
limitado pela política existente.

## Problemas reproduzidos e correções

- Calor, frio, vento e umidade também enviavam nível superior e depois retomavam
  o inferior. A substituição agora encerra a campanha inferior inteira da mesma
  ocorrência, e cada nível superior vai para todos os inscritos ativos.
- Datas sozinhas não identificavam episódios instantâneos: uma escalada de frio
  atravessa meia-noite; um novo ciclo após recuperação precisa poder alertar.
  O schema 10 persiste `ciclo` e migra IDs automáticos antigos com validação.
- Sensação térmica fria persistente com ar já acima do limite de recuperação
  rearmava o frio a cada leitura. O rearme agora respeita os dois campos válidos.
- Falhas parciais ou totais de enfileiramento eram consideradas duplicatas na
  próxima tentativa, deixando destinatários sem fila. Reparo do maior nível
  ativo tenta somente quem está ausente, usando os dados originais. Detecção e
  distribuição são controladas separadamente, preservando os novos ciclos.
- Concluir um envio apagava a contagem de destinatários ausentes da fila. Esses
  faltantes agora continuam contabilizados como falhas.
- Atualização do schema concorrente entre workers podia falhar no check/ALTER.
  Preparação de eventos e operações do produtor são protegidas por transação.
- Health check concorrente decidia cooldown com estado antigo. A leitura passou
  para dentro da seção já serializada de escrita.
- Duas campanhas manuais simultâneas enviavam a mesma mensagem antes de gravar
  o histórico. Cada campanha/usuário agora é reservado antes da chamada HTTP.
- `--retry-failed` podia recuperar imediatamente o mesmo erro terminal em loop,
  inclusive indefinidamente sem limite. A tentativa explícita agora é limitada
  a uma por item na execução ou sessão contínua, compartilhada pelos workers
  paralelos; as retentativas normais pendentes mantêm seu backoff.

## Limites operacionais

Uma mensagem já recebida pela API externa não pode ser recolhida. Timeout ou
interrupção entre entrega e gravação não permitem garantir exatamente uma
entrega sem suporte idempotente do provedor externo.

Uma campanha manual interrompida conserva `enviando`; antes de tentar novamente,
é necessário reconciliar o resultado com a API. `--retry-failed` não recupera
automaticamente essa reserva. Campanhas diferentes continuam independentes.

Executar `init_db.py` com o Python do projeto antes de reiniciar serviços no
deploy. As migrações são aditivas; nenhum arquivo de banco de produção foi aberto
ou alterado nesta tarefa. Campanhas manuais e eventos automáticos não compartilham
identificador, portanto não são deduplicados entre si.

## Validação final

- `python -m unittest discover -s tests -v`: 305 testes passaram.
- `python -m compileall -q estacao tests` e `git diff --check`: sem erros.
- Revisão independente das mudanças, incluindo fluxo de reparo, migração,
  concorrência e retentativas, sem problemas críticos ou importantes pendentes.
- Reproduções dos defeitos falharam antes das correções e passaram depois.
- Alterações locais, sem commit, push, envio real ou atualização do servidor.

## Investigação posterior: vento de 40,4 km/h

A conversão de 25,1 mph produz 40,4 km/h. Os três registros apresentados pelo
usuário são de dias distintos (06/09, 03/10 e 08/10/2026); o valor repetido não
comprova duplicação. O alerta usa a maior entre rajada atual, máximo da nuvem e
máximo diário preservado, e pode representar um pico anterior do mesmo dia.

Foi reproduzida e corrigida uma falha de referência na virada: máximo corrigido
de ontem 66,3, último máximo da nuvem 40,4 após reset intermediário, primeira
máxima de hoje ainda 40,4 e rajada atual 8. O sistema aceitava 40,4 como novo.
O estado agora distingue a última máxima válida da nuvem do máximo corrigido;
campo ausente/inválido não apaga essa referência nem confirma um reset diário.
Novas máximas legítimas continuam aceitas. IDs/eventos históricos não foram
alterados e nenhuma causa foi atribuída aos três casos sem as leituras reais.

Validação posterior: 310 testes passaram, incluindo cinco novos testes de
referência da nuvem, ausência do campo, dois dias sem máxima e preservação de
novas rajadas legítimas. Compileall e git diff --check passaram. As alterações
permanecem locais, sem commit ou push.

### Sequência real de 05 para 06/09

O histórico fornecido posteriormente mostra a rajada de 40,4 km/h às 23:53 de
05/09. O contador da nuvem manteve 40,4 após meia-noite; de 00:00 até 00:14 de
06/09, as rajadas registradas ficaram entre 18,3 e 36,7 km/h. O evento de 06/09
usou 40,4 com horário de ocorrência 00:00. A imagem também contém um alerta de
43,9 em 05/09, compatível com máximo corrigido superior ao contador ao fim do dia.

O teste `test_wind_historical_sequence.py` reproduz todas as 25 leituras com
SQLite, histórico, acumulados e motor de alertas reais, simulando apenas a fonte
externa e o relógio. Com a função de virada do HEAD antigo, ele produz o aviso
indevido no novo dia. Com a correção local, não gera esse aviso e preserva a
máxima real de 36,7 do novo dia. Um pico posterior de 42,2 ainda gera seu alerta
após a janela configurada, usando o horário da nova rajada.

Isso explica o caso de 06/09 sem atribuir automaticamente a mesma causa aos
eventos de 03/10 e 08/10 ou às demais ocorrências de 40,4 no histórico.

### Confirmação dos dois casos de outubro

As leituras adicionais diferenciam os eventos:

| Evento | Evolução observada da máxima | Conclusão |
| --- | --- | --- |
| 06/09 00:00 | Pico de 40,4 já registrado em 05/09 23:53 | Reaproveitamento do contador anterior na virada |
| 03/10 09:38 | 38,5 até 09:37; 40,4 a partir de 09:38 | Nova máxima informada pela estação, cruzando 40 |
| 08/10 07:12 | 33,2, depois 34,9 às 07:11; 40,4 às 07:12 | Nova máxima informada pela estação, cruzando 40 |

As séries fornecidas apresentam valores em degraus, como 34,9, 36,7, 38,5 e
40,4. Nessa sequência, 40,4 é o primeiro patamar acima do gatilho de 40 km/h,
o que ajuda a explicar sua recorrência nos alertas iniciais. Isso é compatível
com discretização das medições/relatos, sem estabelecer a resolução física do
sensor ou a frequência de todos os valores possíveis. Rajada corrente menor
não invalida uma nova máxima informada entre coletas de um minuto. Não foi
encontrada evidência de reaproveitamento pelo código nesses dois eventos de
outubro; não se atribui a falha de setembro às demais ocorrências sem análise.
