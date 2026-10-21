# Controlador experimental da Cosmic Cruiser

Este firmware transforma um Arduino Mega 2560 em controlador experimental dos
eixos de azimute e elevação da montagem Cosmic Cruiser GoTo. Ele utiliza os
pacotes confirmados nas capturas de volta completa dos dois motores.

## Segurança elétrica e mecânica

Não conecte o TX do Mega diretamente a uma linha de aproximadamente 3 V. Use um
level shifter ou buffer apropriado de 5 V para 3,3 V.

Desconecte o transmissor do controlador original antes de ligar o TX1 do Mega.
O analisador passivo em D2/D3 podia conviver com o controlador; este firmware
dirige a linha e não pode compartilhar o fio com outro transmissor.

Faça os primeiros testes sem telescópio ou carga, em uma posição afastada dos
limites mecânicos e com acesso imediato à alimentação. O firmware não movimenta
o motor durante a inicialização.

## Ligações no Mega

| Arduino Mega | Destino |
|---|---|
| D18 / TX1 | RX da montagem, anteriormente observado como Fio B |
| D19 / RX1 | TX da montagem, anteriormente observado como Fio A |
| GND | GND da montagem |

## Comunicação

O USB recebe comandos a 115.200 baud. A montagem utiliza `Serial1` a
57.600 baud, 8N1. BNO055 usa I²C de hardware em D20/D21. O RTC HW-84 **com chip DS3231**
usa um barramento I²C por software separado em D22/D23. Confira a marcação DS3231 no módulo: o código
não implementa DS1307 ou outros RTCs.

### Sensores em outros pinos

| Mega | Sensor |
|---|---|
| D20 | SDA do BNO055 |
| D21 | SCL do BNO055 |
| D22 | SDA do DS3231 |
| D23 | SCL do DS3231 |
| GND | GND dos dois módulos |

Os pinos do RTC podem ser alterados em `sensors.h` (`SENSOR_SDA_PIN` e
`SENSOR_SCL_PIN`). Não use D18/D19, reservados à montagem. D20/D21 são reservados ao BNO055.
O BNO055 usa a biblioteca Wire do core Arduino AVR. Para o RTC usamos **SoftwareWire 1.6.0**
(instale pelo Gerenciador de Bibliotecas). Não são necessárias Adafruit BNO055
ou RTClib. Os dois barramentos operam a 50 kHz com timeout de 5 ms; o Wire suporta
clock stretching do BNO055. Use Arduino AVR Boards 1.8.6 ou posterior.

Use conversor de nível I²C bidirecional entre o Mega (lado 5 V) e sensores
(lado 3,3 V), com pull-ups externos em ambos os lados. Os pull-ups internos
estão desabilitados. Confira a alimentação permitida no breakout BNO055;
o chip não tolera sinais de 5 V. Não permita que pull-ups do módulo RTC a 5 V
cheguem diretamente ao BNO055. Verifique também o circuito de carga do HW-84
antes de usar uma pilha CR2032 não recarregável.

O BNO é detectado em 0x28 ou 0x29 e opera em NDOF, com heading/roll/pitch em
graus e calibração 0–3. Sua orientação é auxiliar: não substitui o alinhamento
óptico, nem representa automaticamente o norte verdadeiro. Instale longe dos
motores e calibre o sensor. A telemetria é enviada uma vez por segundo.
Se não for detectado ao ligar, confira a fiação e reinicie o Mega.

O RTC em 0x68 usa UTC e informa OSF (perda de validade do relógio).
`RTC SET` valida a data e limpa OSF somente após ajuste. A interface sincroniza
o RTC com o relógio UTC do computador ao conectar. Mantenha o relógio do PC correto.

A interface também reconhece o sketch independente de RTC que imprime
`Blumenau | DD/MM/AAAA HH:MM:SS BRT`. Nesse modo, envia automaticamente
`D DD/MM/AAAA HH:MM:SS` com a hora local atual do PC e aguarda uma leitura
com diferença de até três segundos para confirmar o ajuste. O botão
**Ajustar RTC pelo PC** repete o envio. Não utiliza a hora da compilação.
Configure o PC no fuso local desejado; o rótulo BRT desse sketch é fixo.
Esse sketch não oferece controle dos motores ou telemetria BNO055.

### Acompanhar a Lua em montagem altazimutal

1. Grave o firmware para Mega 2560 e execute `iniciar_montagem.bat`.
2. Conecte a porta USB e confira RTC válido, encoders e os limites de elevação.
3. Confira `OBSERVER_LATITUDE` e `OBSERVER_LONGITUDE` em `mount_controller.py`:
   os valores existentes são **-27.085263, -48.919816** (graus, leste positivo).
4. Nivele a base, centralize a Lua manualmente e clique em **Alinhar na Lua**.
5. Inicie **Rastrear Lua** e mantenha o computador conectado. Parar/emergência
   interrompe o acompanhamento. Após reconectar ou executar INIT, alinhe novamente.

O Python calcula azimute/altitude topocêntricos aproximados e corrige o eixo
com maior erro, alternando os eixos; não aplica uma velocidade equatorial fixa.
O firmware continua controlando um eixo por vez. O BNO serve aos instrumentos
da interface e o RTC fornece o instante do cálculo.

**Controle em minutos de arco:** avaliação a cada 1 s, limiar de correção 2′
(0,033333°) e tolerância de parada 1′ no comando `AZ/ALT TRACK`. O GOTO manual
continua com tolerância 0,75°. A velocidade inicial do rastreamento é 4, uma das
velocidades observadas nas capturas; não há taxa angular calibrada. A consulta
do firmware permanece em 285 ms e a parada também ocorre ao cruzar o alvo.
Esses limiares não garantem precisão física de 1′: folga, overshoot e velocidade
precisam ser medidos na montagem. Não há comando de taxa contínua calibrada.
A montagem altazimutal também apresenta rotação de campo. A perda de telemetria
RTC por mais de 65 s interrompe o rastreamento. O limite de elevação padrão
35–85° refere-se aos encoders, não necessariamente à altitude celeste.

Fontes: [Mega/I²C](https://docs.arduino.cc/learn/communication/wire),
[BNO055](https://www.bosch-sensortec.com/media/boschsensortec/downloads/datasheets/bst-bno055-ds000.pdf),
[DS3231](https://www.analog.com/media/en/technical-documentation/data-sheets/DS3231.pdf),
[SoftwareWire](https://github.com/Testato/SoftwareWire).

## Comandos

Abra o Monitor Serial em 115.200 baud com final de linha habilitado:

```text
QUERY
BNO
RTC
RTC SET 2026 10 21 0 0 0
AZ POS 9
AZ NEG 9
ALT POS 9
ALT NEG 9
AZ GOTO 180 9
ALT GOTO 45 9
AZ STOP
ALT STOP
STOP
HELP
```

- `QUERY`: consulta e imprime as duas posições;
- `AZ POS/NEG [1-9]`: movimento azimutal contínuo;
- `ALT POS/NEG [1-9]`: movimento de elevação contínuo;
- `AZ GOTO <graus> [1-9]`: posicionamento absoluto do azimute;
- `ALT GOTO <graus> [1-9]`: posicionamento absoluto da elevação;
- `AZ STOP` ou `ALT STOP`: para somente o eixo indicado;
- `STOP`: para imediatamente os dois eixos;
- `!`: parada de emergência sem precisar enviar newline.

Se a velocidade for omitida, o firmware usa `9`. O `GOTO` consulta as duas
posições a cada 285 ms e trabalha com tolerância padrão de 0,75°. O resultado
pode apresentar overshoot.

O valor `4` foi observado no movimento de azimute da captura
`Inicializacao-Controle-Montagem-Az.sr`; `9` permanece o padrão das capturas
anteriores. Os demais valores são experimentais; a escala não foi calibrada.

## Pacotes implementados

```text
Consultar posição:
01 01 00 09 00 00 00 00 00 00 00 00 CRC CRC

Mover azimute:
01 01 00 80 01 DIREÇÃO VELOCIDADE 00 00 00 00 00 CRC CRC

Parar azimute:
01 01 00 80 00 00 00 00 00 00 00 00 CRC CRC

Mover elevação:
01 01 00 C0 01 DIREÇÃO VELOCIDADE 00 00 00 00 00 CRC CRC

Parar elevação:
01 01 00 C0 00 00 00 00 00 00 00 00 CRC CRC
```

Valores atualmente confirmados:

| Campo | Valor |
|---|---|
| Direção positiva | `0x00` |
| Direção negativa | `0x01` |
| Velocidade máxima | `0x09` |
| CRC | CRC-16/KERMIT, little-endian |
| Comando do azimute | `0x80` |
| Comando da elevação | `0xC0` |
| Posição do azimute na resposta | bytes 4–6, uint24 LE |
| Posição da elevação na resposta | bytes 8–10, uint24 LE |
| Escala dos dois eixos | 1.673.280 contagens/volta |
| Contagens por grau | 4.648 |

Antes de usar `GOTO`, valide `QUERY`, movimentos curtos em cada sentido e as
paradas de cada eixo. Faça os primeiros testes sem telescópio ou carga.

## Diagnóstico de interrupção da comunicação

A recepção serial procura novamente o início do quadro após ruído ou CRC
inválido e ignora respostas de outros comandos até o prazo da consulta.
Três consultas de posição consecutivas sem resposta válida durante movimento
causam envio de parada aos dois eixos. Com a comunicação perdida, o envio não
garante que a montagem receba a parada.

Após gravar, mantenha consultas `QUERY` por alguns minutos, primeiro parado e
depois em movimentos curtos. Se falhar, registre as últimas linhas do console:
`RX1 recebeu 0 bytes` indica
ausência de resposta UART; bytes recebidos sem quadro válido indicam ruído,
quadro incompleto ou CRC incorreto. Verifique também se `HELP` ainda responde:
isso distingue falha da montagem de travamento do programa inteiro.

## Correção baseada nas três capturas de inicialização

Os arquivos `.sr` foram adquiridos a **1 MHz** (metadados), com o analisador
capaz de 24 MHz. D0 é a resposta da montagem e D1 é o comando do controle.
Os 133 quadros encontrados nos dois canais têm CRC válido. A análise
reproduzível e os quadros estão em `analises/inicializacao_sr/`.

No Mega de 16 MHz, o core Arduino AVR 1.8.6 usa uma exceção de compatibilidade
para `begin(57600)`: modo normal, UBRR=16, efetivamente 58.824 baud (+2,12%).
O firmware agora configura explicitamente **U2X1=1, UBRR1=34**, resultando em
57.143 baud (-0,79%). Isso reduz o erro em relação aos 57.600 nominais das
capturas; não demonstra, sozinho, a causa física do travamento relatado.

A sequência continua `0D → 0B → 80(stop) → 0A → 0E → 80 → 80 → C0 → 81 → C1`.
Foram ajustados os intervalos 0D/0B para 2.478 ms, 0E/stop para 789 ms,
consulta de posição para 285 ms e consulta 0A em repouso para 5.085 ms.
Há um intervalo mínimo global de 6 ms entre inícios de quadros, inclusive
quando comandos USB chegam juntos. A primeira consulta automática aguarda
285 ms após iniciar movimento, em vez de depender do horário da consulta anterior.

Movimentos só são liberados após respostas 0A e 0E com CRC válido. Se a
inicialização falhar, não são enviadas as referências 81/C1. Três falhas de
posição bloqueiam novos movimentos; se houver movimento, enviam parada.
`STOP` e `!` continuam disponíveis mesmo com o movimento bloqueado.

O novo comando **`INIT`** para os motores e repete a inicialização sem gerar
outro pulso baixo no TX. Ele redefine as referências dos encoders (81/C1);
não é uma recuperação transparente de posição. Não é executado automaticamente.

### Validação na montagem

Compile e grave para Arduino Mega 2560. Confira `0A OK | 0E OK` no console;
se houver `SEM RESPOSTA`, o firmware mantém o movimento bloqueado.
Teste `QUERY`, depois um movimento curto `AZ POS 4` seguido de `STOP`,
e novamente `QUERY`. Capture D18/D19 para comparar os quadros e verificar
se a comunicação permanece ativa. A compilação e análise offline não
substituem esse teste físico; o firmware não foi gravado automaticamente.

### Atualização do rastreamento em minutos de arco

Grave o firmware atualizado no Mega e reabra o LogicLab. O app consulta `HELP`
e exige a identificação `TRACK_ARCMIN=1` antes de iniciar o rastreamento.
Firmware antigo ou sketch independente de RTC não implementam esse comando.
Centralize a Lua, clique em Alinhar na Lua e depois em Rastrear Lua. O erro
mostrado está em minutos de arco; posições e alvos continuam em graus.

A referência das correções é o encoder de cada eixo, após alinhamento óptico.
O BNO055 continua como orientação auxiliar: a saída Euler usada tem 16 LSB/°,
ou 3,75′ por passo, e não confirma correções de 1′. Nenhum pitch/roll é assumido
como eixo óptico sem conhecer a instalação física do sensor.

Validação de bancada: conferir a confirmação de TRACK, o erro após a parada e
se ambos os eixos recebem correções. Se houver oscilação, a tolerância numérica
não resolve a dinâmica: medir velocidades, folga e distância de parada antes
de reduzir mais o limiar. A compilação e os testes simulados não substituem
esta validação física.
