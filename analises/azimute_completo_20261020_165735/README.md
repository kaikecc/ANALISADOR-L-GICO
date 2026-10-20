# Comparação do controle original com cosmic_cruiser_controller

Origem confirmada pelo usuário: **controle original**, não o Arduino controlador.
CSV analisado integralmente, sem alterar amostras: 17.785 estados, última
transição em 86,248918 s. Esse tempo não necessariamente é o instante de término
da aquisição, pois o arquivo registra transições.

**Conclusão:** não falta um opcode observado nesta captura: todos os comandos
válidos transmitidos pelo original aparecem no firmware. Faltam partes do
sequenciamento, confirmação de estado e recuperação. A captura mostra uma
referência funcional; não demonstra a causa da falha intermitente do Arduino.

## Integridade da análise

O decodificador atual em `logic_analyzer.py:1340` bloqueia novas bordas até
9,8 bits após o start. Alguns starts registrados ficam antes desse limite,
embora o stop anterior já tenha sido validado em 9,5 bits. Uma análise separada
libera a busca após 9,5 bits, mantendo 57.600 baud, 8N1 e CRC estrito little-endian.
Não foram inseridos bytes, reparados timestamps ou escolhidos bits pelo CRC.

| Canal | Quadros | CRC válido no decodificador atual | CRC válido na análise separada |
|---|---:|---:|---:|
| A, montagem → controle | 172 | 96 | 153 |
| B, controle → montagem | 203 | 159 | 202 |

Restam 19 quadros inválidos em A e um em B. Isso pode refletir aquisição,
temporização, decodificação ou sinal elétrico; **não prova que o UART do
controlador recebeu esses mesmos bytes errados**. O CSV não contém o contador
`dropped` do analisador, portanto não é possível excluir perda de transições.

Foram encontradas 168 consultas de posição, todas com atividade de resposta
correspondente até 10 ms depois. Dessas respostas, 149 têm quadro completo,
cabeçalho e CRC válidos. O atraso início–início é 2,550–2,606 ms, mediana
2,564 ms. A última resposta, em 86,246504 s, tem CRC válido. Não há silêncio
persistente da montagem nessa referência.

## Diferenças medidas e implicações

### 1. Primeira consulta após iniciar movimento — prioridade alta

No original:

| Movimento | Comando 80 (s) | Primeira consulta 09 (s) | Intervalo |
|---|---:|---:|---:|
| Positivo 1 | 25,281942 | 25,530445 | 248,503 ms |
| Positivo 2 | 54,154581 | 54,403084 | 248,503 ms |
| Negativo | 65,602602 | 65,851106 | 248,504 ms |

`beginManual()` (linha 367) e `beginGoto()` (385) não reiniciam `lastQueryAt`
ao transmitir movimento. `serviceMotion()` (417) utiliza a data da consulta
anterior. Após um período parado, pode transmitir `09` imediatamente depois
do movimento. Isso não reproduz a pausa consistente do original.

Correção candidata: agendar a primeira consulta a partir do comando de
movimento. As consultas seguintes do original têm mediana de 285,075 ms;
o firmware usa 250 ms. A diferença é real, mas não demonstra que 250 ms
seja proibido nem que mudar esse valor isoladamente resolva o problema.

### 2. Parada incompleta em relação à sequência observada — prioridade alta

Três paradas, em 44,060330; 62,170117 e 85,921374 s, repetem:

```text
80 stop → C0 stop → 80 stop → 80 stop → C0 stop → 09 posição
```

Intervalos entre inícios dos cinco primeiros quadros: aproximadamente
5,887; 6,060; 5,887; 17,904–17,912 ms. A consulta final chega cerca de
284,3–286,8 ms após o início do último C0.

`stopAll()` (155) transmite apenas `80 C0 80 C0`, com delays de 6/8/6 ms
**após** `sendFrame()`/`Serial1.flush()`. Esses delays se somam ao tempo de
transmissão; não equivalem aos intervalos início–início medidos no original.
`stopOneAxis()` (170), chamado pela interface ao soltar o botão, envia somente
duas paradas para o eixo escolhido. Nenhuma das duas rotinas agenda a consulta
final de confirmação.

Falta verificar se a parada foi efetiva, em vez de considerar a transmissão
como sucesso. Uma sequência diferente pode explicar comportamento de transição,
mas o papel necessário de cada repetição ainda requer teste controlado.

### 3. Inicialização enviada não equivale a inicialização concluída

O original transmite:

```text
6,003004   0D
8,480763   0B
8,492658   80 stop
8,504553   0A → resposta
8,630628   0E → resposta
9,419431   80 stop
9,425318   80 stop
9,443222   C0 stop
9,449110   81 payload zero
9,455177   C1 F0 61 06 00 00 00 00 00
14,544525  0A → resposta
19,630875  0A → resposta
21,524382  81 payload zero
21,530442  C1 F0 61 06 00 00 00 00 00
```

A primeira sequência já está implementada, com tempos próximos. A segunda
dupla `81/C1`, antes do primeiro movimento, não existe como etapa/comando
posterior no Arduino. O significado exato dessa repetição não está provado;
pode refletir uma ação de referência/calibração do usuário. Não deve ser
repetida cegamente ao recuperar comunicação, pois pode redefinir coordenadas.

O firmware ignora o conteúdo das respostas `0A/0E` e libera movimentos mesmo
sem recebê-las (linha 294). Não tem estado de negociação confirmada, tentativa
de recuperação comandada nem diagnóstico detalhado dessas respostas.

O início baixo de B vai de 0 a 4,203705 s, mas já estava baixo no início do CSV.
Não é uma medição segura da duração de um pulso de reset. Não justifica trocar
automaticamente o pulso de 288 ms do Arduino por 4,2 s.

### 4. Keepalive com ciclo de vida diferente — hipótese a testar

O original só transmite `0A` em 8,504553; 14,544525 e 19,630875 s. Após a
dupla `81/C1` em 21,52 s, não aparece outro `0A`, inclusive durante a pausa
de aproximadamente 9,77 s entre a consulta de 44,380415 s e o próximo C0.

`serviceStartupKeepalive()` (297) envia `0A` a cada 5,08 s sempre que está
IDLE. Como seu relógio não é atualizado durante movimento, um movimento
mais longo pode ser seguido imediatamente por `0A` ao parar. O original
mostra consulta `09` após a parada, não esse comportamento.

Isso sugere distinguir fase de configuração e fase operacional. A captura
não permite afirmar que `0A` causa travamento nem que é um keepalive obrigatório.

### 5. Perda de comunicação é tratada com parada, sem recuperação

`updatePositions()` (318) invalida a posição e, após três falhas, envia parada.
Não há recuperação de sessão, confirmação da parada ou estado explícito de
falha que impeça novos movimentos até a comunicação ser verificada.
`beginManual()` não verifica conexão com a montagem e não limpa o contador
de falhas: se ele já estiver em três, a primeira consulta malsucedida do novo
movimento gera nova parada. Isso pode parecer que os botões deixaram de funcionar.

O leitor usa janela deslizante e CRC, portanto **ressincronização básica já
existe**. Porém seu estado parcial é local a cada chamada; `updatePositions()`
descarta todo RX pendente antes da consulta, e respostas de outros comandos
são descartadas. Falta um receptor contínuo que registre quadros, atrasos e
erros e entregue respostas à transação correspondente.

Os timeouts de 80/100 ms são muito maiores que as respostas desta captura.
Aumentá-los sem outra evidência não é uma solução sustentada pelos dados.
As esperas bloqueiam o processamento do console; `!` só é tratado quando
`pollConsole()` volta a executar, e a inicialização bloqueia por vários segundos.

## Outros resultados

- A velocidade `04` está confirmada nesta captura **para azimute nos dois
  sentidos**, com CRC válido e mudança dos encoders. Já é aceita pelo firmware;
  o aviso de que somente `09` foi confirmado está desatualizado. A velocidade
  mediana medida foi aproximadamente +0,135/+0,138 e −0,136 °/s, usando a escala
  existente de 4.648 contagens/grau. Isso não valida as velocidades 1–3/5–8,
  a escala absoluta nem movimentos de altitude.
- Não há ACK observado para `80`, `C0`, `81` ou `C1`. Não implementar espera
  por ACK inexistente; confirmação de movimento/parada precisa usar posição
  e critérios adequados, considerando folga mecânica e aceleração.
- No Arduino AVR core local 1.8.6, `HardwareSerial.cpp:117`, `begin(57600)`
  em 16 MHz seleciona modo normal, divisor 16: taxa nominal resultante de
  58.823,5 baud (erro +2,12%). É uma diferença a medir no TX do Arduino;
  não é prova de falha. Não se deve concluir que o controle original usa essa
  taxa a partir dos erros do decodificador.
- A interface Python ainda envia `RTC SET`/`RTC`, ausentes neste firmware.
  Isso gera erro de comando no console; não prova travamento da UART da montagem.

## Ordem sugerida para correção e validação

1. Registrar estado, TX/RX hex, CRC, falhas consecutivas e tempo desde a última
   resposta válida. Preservar o processamento prioritário de parada.
2. Corrigir o agendamento da primeira consulta e confirmar parada por consultas
   posteriores; testar a sequência de parada observada sem reinicializar referências.
3. Separar configuração, operação e falha, com recuperação explícita e sem
   retomar movimento automaticamente. Determinar quando `0A` e `81/C1` são necessários.
4. Capturar o Arduino no mesmo cenário até a falha, junto com o console USB.
   Se `HELP` responde mas RX da montagem silencia, o problema é diferente de
   um Arduino cujo console também parou. Comparar o último TX, o último RX,
   a taxa real e os intervalos com esta referência.

Nenhum firmware foi modificado ou gravado nesta análise. O CSV original foi
preservado. As diferenças acima são candidatas fundamentadas, não uma causa
raiz confirmada sem captura do Arduino durante a falha.

## Artefatos reproduzíveis

Execute da raiz: `.venv-win\Scripts\python.exe analises\azimute_completo_20261020_165735\analisar.py`.

- `quadros.csv`: sequência completa, com tempo, direção, bytes e validação estrita.
- `resumo.json`: contagens, latências e hash SHA-256 do CSV original.
- `decoded.json`: saída inicial do decodificador do aplicativo.
- `decoded_tolerant.json`: saída exploratória com liberação da busca em 9,5 bits.
- `analisar.py`: verificação independente e regeneração dos dois primeiros artefatos.
