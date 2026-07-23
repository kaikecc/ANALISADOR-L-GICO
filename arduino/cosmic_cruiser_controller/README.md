# Controlador experimental da Cosmic Cruiser

Este firmware transforma um Arduino Mega 2560 em controlador experimental do
eixo azimutal da montagem Cosmic Cruiser GoTo. Ele utiliza os pacotes inferidos
nas duas capturas de volta completa.

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

O USB permanece conectado ao computador para receber comandos a 115.200 baud.
A comunicação com a montagem usa `Serial1` a 57.600 baud, 8N1.

## Comandos

Abra o Monitor Serial em 115.200 baud com final de linha habilitado:

```text
QUERY
POS
NEG
GOTO 180
STOP
HELP
```

- `QUERY`: consulta e imprime posição em contagens e graus;
- `POS`: movimento contínuo no sentido positivo;
- `NEG`: movimento contínuo no sentido negativo;
- `GOTO <graus>`: escolhe o caminho mais curto e para próximo do azimute;
- `STOP`: parada imediata;
- `!`: parada de emergência sem precisar enviar newline.

O `GOTO` usa velocidade máxima, consulta a posição a cada 250 ms e trabalha com
tolerância padrão de 0,75°. O resultado pode apresentar overshoot. Não use para
operação real do telescópio antes de validar os comandos `QUERY`, `POS`, `NEG` e
`STOP` separadamente.

## Pacotes implementados

```text
Configurar:
01 01 00 C0 00 00 00 00 00 00 00 00 CRC CRC

Consultar posição:
01 01 00 09 00 00 00 00 00 00 00 00 CRC CRC

Mover:
01 01 00 80 01 DIREÇÃO VELOCIDADE 00 00 00 00 00 CRC CRC

Parar:
01 01 00 80 00 00 00 00 00 00 00 00 CRC CRC
```

Valores atualmente confirmados:

| Campo | Valor |
|---|---|
| Direção positiva | `0x00` |
| Direção negativa | `0x01` |
| Velocidade máxima | `0x09` |
| CRC | CRC-16/KERMIT, little-endian |
| Escala azimutal | 1.673.280 contagens/volta |
| Contagens por grau | 4.648 |

Outras velocidades ainda precisam ser capturadas antes de serem usadas.
