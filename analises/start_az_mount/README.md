# START-AZ-MOUNT: comparação com o firmware

Arquivo analisado: `captures/START-AZ-MOUNT`, 717.312 bytes. Valores presentes:
0xFF (708.422), 0xFD (7.885), 0xFE (1.005). Interpretado como amostras digitais
de oito canais, um byte por amostra: somente os bits 0 e 1 apresentam atividade.
Não há cabeçalho ou metadados que confirmem a taxa de amostragem.

## Limite da análise

A 24 MHz o arquivo inteiro duraria 29,888 ms. Os blocos de cerca de 49 amostras
durariam aproximadamente 2 us, incompatíveis com quadros de 14 bytes UART
8N1 a 57.600 baud (aproximadamente 2,431 ms). A hipótese de 20 kHz explica
esses blocos, o pulso inicial e os intervalos periódicos de 5.000 amostras.
Isso é uma inferência, não confirmação da configuração do equipamento.
Nessa taxa haveria apenas 0,347 amostra por bit UART; não é possível validar
todos os bytes, CRCs, posições ou sentidos de movimento sem outra captura.

## Inicialização

O firmware contém a sequência em `initializeMountProtocol()`, chamada por
`setup()`: 0D, 0B, parada 80, 0A, 0E, parada 80 duas vezes, parada C0,
81 com dados zerados e C1 com dados F0 61 06 00. Os quadros têm prefixo
01 01 00 e CRC-16/KERMIT. Espera respostas 0A e 0E, mas libera os comandos
mesmo quando essas respostas não chegam.

Comparação temporal abaixo usa exclusivamente a hipótese de 20 kHz.
Os nomes dos comandos são associações pela ordem temporal do firmware,
não decodificação independente dos bytes capturados.

| Intervalo | Captura, hipótese 20 kHz | Código |
|---|---:|---:|
| Canal 1 baixo, amostras 120191–125924 | 286,65 ms | 288 ms |
| Subida até primeiro bloco | 1798,90 ms | 1799 ms |
| Primeiro até segundo bloco (candidatos 0D → 0B) | 2481,45 ms | 2475 ms |
| Segundo até terceiro bloco | 11,90 ms | 12 ms |
| Terceiro até quarto bloco | 11,95 ms | 12 ms |
| Quarto até quinto bloco (candidatos 0A → 0E) | 126,05 ms | 126 ms |
| Quinto até próxima transmissão (candidata parada) | 792,40 ms | 783 ms |
| Duas transmissões seguintes | 5,85 ms | 6 ms |
| Próximo intervalo (candidato 80 → C0) | 17,90 ms | 18 ms |
| Próximo intervalo (candidato C0 → 81) | 5,85 ms | 6 ms |
| Próximo intervalo (candidato 81 → C1) | 6,05 ms | 6 ms |

O canal 0 apresenta blocos nas amostras 212058 e 214579, logo após os
blocos do canal 1 nas amostras 212008 e 214529: compatível com duas
respostas, aproximadamente 2,5 ms após o início das requisições.

Há dois blocos adicionais no canal 1 nas amostras 231547 e 231669,
após o candidato C1 na amostra 231090. O firmware não envia dois
quadros nessa posição da sequência. Sua identidade permanece desconhecida.
Também não há tráfego periódico no intervalo até a amostra 493459;
isso não reproduz o keepalive 0A a cada 5080 ms previsto no firmware ocioso.

## Trecho indicado como teste de azimute

Há duas transmissões nas amostras 493459 e 493583 (24,67295 e 24,67915 s
na hipótese de 20 kHz), seguidas de 26 pares de requisição/resposta de
aproximadamente 24,928 a 31,180 s, com período próximo de 250 ms.
Isso é compatível com a estrutura do controle manual: parar o outro eixo,
iniciar movimento e consultar posição periodicamente. O intervalo inicial
entre as transmissões é 6,20 ms; no firmware `beginManual()` aguarda 8 ms
após concluir a transmissão da parada, aproximadamente 10,43 ms entre
inícios dos quadros. A identidade do eixo, velocidade, direção e deslocamento
não podem ser confirmados pelos bytes desta captura subamostrada.

Há novos grupos de transmissões perto de 31,288, 31,863 e 32,278 s,
com respostas posteriores. A captura contém atividade nos dois canais durante
esse trecho; isso não comprova movimento mecânico nem perda de comunicação.

## Conclusão

A sequência de inicialização existe no firmware, mas não é possível afirmar
que reproduz integralmente esta captura. Há diferenças temporais e blocos
adicionais a identificar. Não foram alterados os comandos ou tempos do código
com base em bytes incertos. Para comparação byte a byte, é necessário o projeto
original com taxa de amostragem ou exportação UART 57600 8N1 dos dois canais,
preferencialmente de uma captura efetivamente feita a 24 MHz.

`blocos.csv` lista índices das amostras e agrupamentos de transições, separados
quando o intervalo excede 35 amostras. Tempos são explicitamente condicionais.
