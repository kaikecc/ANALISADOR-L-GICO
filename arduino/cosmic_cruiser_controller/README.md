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

## Sensor BNO055 via I²C

Não é necessário instalar nenhuma biblioteca externa. O firmware acessa
diretamente os registradores do BNO055 via I²C e utiliza apenas `Wire`, incluída
no núcleo padrão do Arduino. No Mega 2560, conecte:

| BNO055 | Arduino Mega |
|---|---|
| VIN | 5 V somente em breakout compatível; caso contrário, 3,3 V |
| GND | GND |
| SDA | D20 / SDA |
| SCL | D21 / SCL |

O endereço utilizado é `0x28`. A interface recebe rumo, roll, pitch e os quatro
níveis de calibração (`sistema/giroscópio/acelerômetro/magnetômetro`) cinco vezes
por segundo. Um nível `3` indica calibração completa daquele componente.

O USB permanece conectado ao computador para receber comandos a 115.200 baud.
A comunicação com a montagem usa `Serial1` a 57.600 baud, 8N1.

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
posições a cada 250 ms e trabalha com tolerância padrão de 0,75°. O resultado
pode apresentar overshoot.

Somente o valor `9` foi confirmado como velocidade máxima. Os valores `1–8`
estão disponíveis para investigação, mas são experimentais: ainda não foi
confirmado se todos são aceitos nem se a ordem é linear.

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
