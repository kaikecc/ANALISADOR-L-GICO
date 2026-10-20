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
57.600 baud, 8N1. Este firmware controla somente a montagem: não utiliza
BNO055, RTC ou barramento I²C. Os comandos `BNO`, `RTC` e `RTC SET` foram
removidos; `QUERY` retorna apenas as posições dos encoders.

A interface Python ainda possui recursos dependentes desses sensores.
O rastreamento lunar que exige RTC não está disponível com este firmware.

## Comandos

Abra o Monitor Serial em 115.200 baud com final de linha habilitado:

```text
QUERY
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
