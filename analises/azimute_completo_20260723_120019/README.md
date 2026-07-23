# Análise da volta azimutal completa

## Identificação

- Equipamento: montagem GoTo Cosmic Cruiser
- Movimento: azimute, sentido negativo do contador
- Velocidade: máxima
- Captura original: [`azimute_completo_20260723_120019.csv`](../../captures/azimute_completo_20260723_120019.csv)
- Data da captura: 23 de julho de 2026
- Interface de aquisição: Arduino Mega 2560 + LogicLab
- Entradas:
  - Fio A: D2 / INT4
  - Fio B: D3 / INT5

## Resumo

A captura representa praticamente uma volta azimutal completa. O protocolo
observado é UART binário a 57.600 baud, formato 8N1. O Fio B aparenta transportar
comandos e consultas do controlador para a montagem, enquanto o Fio A aparenta
transportar as respostas da montagem.

Os resultados indicam:

| Métrica | Resultado |
|---|---:|
| Duração total da captura | 206,606 s |
| Transições registradas | 84.234 |
| Transições no Fio A | 51.764 |
| Transições no Fio B | 32.462 |
| Velocidade UART | 57.600 baud |
| Formato UART | 8N1 |
| Frequência aproximada das consultas | 4 Hz |
| Atraso mediano entre consulta e resposta | 2,565 ms |
| Tempo estimado de uma volta | 200,02 s |
| Velocidade angular máxima | 1,80 °/s |
| Velocidade de rotação | 0,300 RPM |

## Direção dos fios

### Fio B — controlador para montagem

O pacote válido mais frequente foi:

```text
01 01 00 09 00 00 00 00 00 00 00 00 F3 E2
```

Esse pacote apareceu 710 vezes com CRC válido. A repetição aproximadamente a
cada 250 ms indica que provavelmente se trata de uma consulta periódica de
posição ou estado.

### Fio A — montagem para controlador

Exemplo de resposta:

```text
01 01 00 09 A7 42 0A 00 41 12 05 00 C5 9E
```

As respostas aparecem aproximadamente 2,565 ms depois das consultas do Fio B.

## Estrutura provável do quadro de posição

| Bytes | Conteúdo | Interpretação provável |
|---|---|---|
| 0–2 | `01 01 00` | Cabeçalho ou endereço |
| 3 | `09` | Código da consulta/resposta de posição |
| 4–6 | variável | Posição azimutal em 24 bits, little-endian |
| 7 | `00` | Reservado ou estado |
| 8–11 | `41 12 05 00` | Estado ou configuração fixa |
| 12–13 | variável | CRC-16/KERMIT, little-endian |

O campo de posição é calculado por:

```python
posicao = byte4 | (byte5 << 8) | (byte6 << 16)
```

Exemplos consecutivos:

| Bytes 4–6 | Decimal |
|---|---:|
| `A7 42 0A` | 672.423 |
| `23 3B 0A` | 670.499 |
| `9E 2A 0A` | 666.270 |
| `34 22 0A` | 664.116 |

O valor diminui continuamente durante o movimento. Isso indica que o sentido
capturado corresponde ao sentido negativo do contador de posição.

## Escala provável do azimute

A análise sugere uma escala de 1.673.280 contagens por volta:

```text
1 volta     ≈ 1.673.280 contagens
1 grau      ≈ 4.648 contagens
1 contagem  ≈ 0,000215146°
1 contagem  ≈ 0,7745 segundo de arco
```

Essa hipótese é especialmente consistente porque:

```text
1.673.280 / 360 = 4.648
```

Com uma velocidade observada próxima de 8.366 contagens por segundo:

```text
velocidade ≈ 8.366 / 4.648
velocidade ≈ 1,80 °/s
```

O tempo correspondente para uma volta é:

```text
1.673.280 / 8.366 ≈ 200,02 segundos
```

## Posição inicial e final

Considerando a escala provável:

| Ponto | Contagem | Ângulo aproximado |
|---|---:|---:|
| Primeira resposta válida | 672.423 | 144,67° |
| Última resposta válida | 680.211 | 146,34° |

Entre a primeira e a última resposta UART válida foram observados
aproximadamente 99,54% de uma volta. A diferença residual de cerca de 1,68°
é compatível com o intervalo entre:

- o envio do comando de movimento;
- a primeira consulta válida de posição;
- o comando de parada;
- a última resposta válida registrada.

## Comandos de movimento

### Início

```text
6,534155 s  01 01 00 C0 00 00 00 00 00 00 00 00 70 F2
6,540389 s  01 01 00 80 01 01 09 00 00 00 00 00 0A 2F
```

Hipóteses:

- `0xC0`: configuração, habilitação ou seleção do modo do motor;
- `0x80` com `01 01 09...`: iniciar movimento azimutal no sentido e velocidade
  selecionados.

### Parada

```text
206,327418 s  01 01 00 80 00 00 00 00 00 00 00 00 13 14
```

O comando `0x80` com payload zerado é um forte candidato a comando de parada.

### Sequência intermediária

Em aproximadamente 120,49 segundos foi observada esta sequência:

```text
120,488336 s  01 01 00 80 00 00 00 00 00 00 00 00 13 14
120,494223 s  01 01 00 C0 00 00 00 00 00 00 00 00 70 F2
120,738747 s  01 01 00 C0 00 00 00 00 00 00 00 00 70 F2
120,744982 s  01 01 00 80 01 01 09 00 00 00 00 00 0A 2F
```

Isso aparenta ser uma parada seguida de configuração e reinício do movimento.
Pode ter ocorrido um reenvio do comando pelo controlador durante a volta.

## CRC

Os dois últimos bytes dos quadros são compatíveis com CRC-16/KERMIT armazenado
em ordem little-endian.

Parâmetros:

```text
width   = 16
poly    = 0x1021
init    = 0x0000
refin   = true
refout  = true
xorout  = 0x0000
```

O CRC deve ser calculado sobre os 12 primeiros bytes do quadro.

## Qualidade e integridade da captura

Foram encontrados três timestamps isolados com um dígito ausente:

| Linha | Valor registrado | Valor interpolado para análise |
|---:|---:|---:|
| 41.766 | 10.656.756 µs | 106.567.578 µs |
| 60.918 | 15.125.593 µs | 151.255.933 µs |
| 70.831 | 17.501.206 µs | 175.012.093 µs |

Esses pontos foram corrigidos somente durante a análise. O CSV original foi
preservado.

Resultados do decodificador:

| Canal | Bytes UART | Quadros detectados | Quadros completos com CRC válido | Confiança de framing |
|---|---:|---:|---:|---:|
| Fio A | 10.840 | 798 | 473 | 96,65% |
| Fio B | 11.274 | 813 | 717 | 99,87% |

O Fio A teve mais quadros incompletos ou corrompidos. Apesar disso, existem
amostras válidas suficientes para identificar a trajetória da posição, a
velocidade, o período das consultas e os principais comandos.

## Grau de confiança das conclusões

| Conclusão | Confiança |
|---|---|
| UART 57.600 baud, 8N1 | Alta |
| Fio B como controlador → montagem | Alta |
| Fio A como montagem → controlador | Alta |
| Bytes 12–13 como CRC-16/KERMIT | Alta |
| Código `0x09` relacionado a posição/estado | Alta |
| Bytes 4–6 como contador de posição | Alta |
| Escala de 1.673.280 contagens por volta | Alta, ainda requer uma segunda captura |
| `0x80` com payload zerado como parada | Alta |
| `0x80 01 01 09...` como movimento máximo | Média/alta |
| `0xC0` como habilitação ou configuração | Média |

## Próximos testes recomendados

Para confirmar completamente o protocolo:

1. Capturar uma volta completa no sentido oposto.
2. Capturar movimentos de 10°, 30°, 90° e 180°.
3. Capturar cada nível de velocidade disponível.
4. Registrar a posição inicial informada pelo aplicativo da montagem.
5. Fazer uma captura somente do comando de início e parada.
6. Repetir o teste sem interrupção intermediária.
7. Comparar o eixo azimutal com o eixo de altitude.

Esses testes permitirão separar definitivamente posição, direção, velocidade,
estado do motor e possíveis indicadores de limite ou erro.
