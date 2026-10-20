# START-AZ-MOUNT-2.sr — nova análise

O arquivo foi atualizado: agora possui 42.979 bytes compactados, contra
28.323 bytes na leitura anterior.
SHA-256: 637b820f4c38e0b97983e4cc7ec38f9c6ef2631d8de3178571b633543286e7e6

Metadados: 1 MHz, oito canais digitais, uma amostra por byte.
Cinco blocos totalizam 43.356.160 amostras (43,356160 segundos).
43.356.156 amostras são 0xFF; quatro são 0xFC.

D0 e D1 são idênticos durante toda a captura. Ambos ficam baixos por apenas
1 microssegundo em cada uma destas posições:

- 3,323659 s
- 3,323689 s
- 3,337310 s
- 3,337312 s

D2 a D7 permanecem altos durante toda a captura. Não há quadros UART 57600
8N1 identificáveis: o bit esperado dura aproximadamente 17,36 microssegundos.
Não há pulso inicial baixo de aproximadamente 288 ms nem sequência de
inicialização ou teste de azimute registrada. Isso descreve os dados do
arquivo; não prova que a montagem deixou de transmitir fora dos pontos
medidos ou do intervalo capturado.

A taxa está adequada. Conferir pontos de medição, GND comum e se o acionamento
da montagem ocorre durante a aquisição. A identidade perfeita entre D0 e D1
justifica conferir se as pontas estão realmente em fios distintos, mas não
comprova curto ou erro de ligação.

Nenhuma alteração feita no firmware nesta reanálise.
