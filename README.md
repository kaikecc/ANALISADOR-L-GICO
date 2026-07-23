# LogicLab

Interface visual em Python dedicada à leitura passiva de um protocolo proprietário de dois fios com Arduino Mega 2560. Inclui captura pelas interrupções externas INT4/INT5, visualização digital, zoom, análise UART, repetição e exportação CSV. O firmware usa acesso direto aos registradores do ATmega2560 e transmite as transições continuamente para permitir ciclos longos.

## Início rápido

1. Abra `arduino/logic_analyzer/logic_analyzer.ino` na Arduino IDE e grave na placa.
2. No computador, execute:

   ```bash
   python3 -m venv .venv
   .venv/bin/python -m pip install -r requirements.txt
   .venv/bin/python logic_analyzer.py
   ```

3. Selecione a porta, conecte e clique em **Capturar**. O aplicativo usa
   automaticamente `1.000.000 baud`.

No macOS, também é possível abrir `iniciar.command`. Feche o **Monitor Serial** e o **Plotter Serial** da Arduino IDE antes de conectar: somente um programa pode usar a porta USB de cada vez.

Sem Arduino, clique em **Modo demo** para testar toda a interface.

## Ligações

| Canal | Arduino | Uso sugerido |
|---|---|---|
| Fio A | D2 / PE4 / INT4 | Primeira via do protocolo |
| Fio B | D3 / PE5 / INT5 | Segunda via do protocolo |

Una o **GND do Arduino** ao GND do circuito analisado. A foto indica níveis próximos de 2,83–3,10 V. Isso é marginal para uma entrada do Mega alimentado em 5 V (o nível alto garantido começa em aproximadamente 3,0 V). Para leitura confiável, passe cada fio por um buffer 74HCT14/74HCT125 ou comparador apropriado, sempre mantendo a conexão passiva e o terra comum. Nunca aplique tensão acima da especificação da placa.

O firmware registra mudanças nas duas linhas usando `INT4` e `INT5`. Em cada interrupção, D2 e D3 são lidos simultaneamente pelo registrador `PINE`, com timestamps do Timer1 em resolução de 0,5 µs. Um buffer circular de 1.024 posições absorve os picos e o loop transmite continuamente a `1.000.000 baud`. O Python mantém a janela recente na RAM e grava a captura completa diretamente em `captures/azimute_completo_*.csv`.

O analisador não possui reprodução nem configura pinos de saída. O circuito observado nunca é dirigido pelo firmware.

## Diagnóstico da captura

Durante a captura, o LED integrado D13 apaga ao iniciar e acende na primeira transição detectada em D2 ou D3. As amostras aparecem no gráfico durante o movimento e são gravadas simultaneamente no CSV. Ao clicar em **Parar**, aguarde a mensagem `CSV completo salvo`.

Ao terminar, o firmware envia uma linha `#DEBUG` com contadores independentes, estados inicial/final e `dropped`. O valor `dropped=0` confirma que nenhuma transição foi perdida. Se for maior que zero, a taxa momentânea excedeu o escoamento serial.

Para sinais de aproximadamente 3 V, prefira um **74AHCT125** (a letra `T` é importante) alimentado em 5 V: pino 14 em 5 V, pino 7 em GND, `1OE`/`2OE` em GND, sinais nos pinos `1A`/`2A` e saídas `1Y`/`2Y` para D2/D3. Coloque um capacitor de 100 nF entre os pinos 14 e 7. O 74AHCT125 garante nível alto a partir de 2,0 V; um 74AHC125 comum não oferece essa mesma margem em 5 V.

O Mega possui apenas 8 kB de SRAM. A duração da captura agora é limitada principalmente pelo armazenamento do computador, mas surtos sustentados acima da capacidade da serial ainda podem produzir `dropped>0`. Nesse caso, uma placa com DMA, como RP2040/ESP32, será mais apropriada.

## Protocolo serial

- Comandos do app: `START`, `STOP`, `PING` seguidos de newline.
- Velocidade: `1.000.000 baud`.
- Amostra enviada durante a captura: `D,<tempo_desde_o_inicio_em_microssegundos>,<máscara_decimal>`.
- Mensagem de estado: `#<texto>`.

## Controlador experimental da montagem

O firmware separado
[`arduino/cosmic_cruiser_controller/cosmic_cruiser_controller.ino`](arduino/cosmic_cruiser_controller/cosmic_cruiser_controller.ino)
implementa consulta de posição, movimento positivo/negativo, parada e `GOTO` em
graus usando os pacotes inferidos nas capturas azimutais.

Leia primeiro as
[instruções específicas do controlador](arduino/cosmic_cruiser_controller/README.md).
Esse firmware dirige eletricamente a linha da montagem e exige desconectar o
transmissor original e utilizar conversão de nível adequada.
