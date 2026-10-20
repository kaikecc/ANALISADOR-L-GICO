# LogicLab

Interface visual em Python dedicada à leitura passiva de um protocolo proprietário de dois fios com Arduino Mega 2560. Inclui captura pelas interrupções externas INT4/INT5, visualização digital, zoom, análise UART, repetição e exportação CSV. O firmware usa acesso direto aos registradores do ATmega2560 e transmite as transições continuamente para permitir ciclos longos.

## Início rápido

### Windows — interface nativa

Dê dois cliques em `iniciar_logiclab.bat`. Na primeira execução, ele cria
`.venv-win` e instala as dependências de `requirements-win.txt` (requer internet).
É necessário Python 3.10 ou superior com pip e Tcl/Tk. O aplicativo abre
`logiclab_native.py`, que carrega `logiclab_viewer.html` com acesso à porta serial.
Abrir o HTML diretamente permite visualizar CSVs, mas não conectar ao Arduino.
`logiclab_graph.html` é uma prévia estática gerada pela interface clássica.

### Analisador USB de 8 canais / 24 MHz

As duas interfaces (`logic_analyzer.py` e `logiclab_native.py`, aberta pelo
`iniciar_logiclab.bat`) oferecem a fonte **USB Logic Analyzer 24MHz**. O suporte
é para dispositivos FX2 de **oito canais** reconhecidos pelo driver `fx2lafw`
do sigrok, como os clones do Saleae Logic. Dispositivos de 16 canais ou de
outros drivers não são selecionados por essa integração.

1. Instale o [sigrok-cli para Windows](https://sigrok.org/download/binary/sigrok-cli/).
   O PulseView sozinho não fornece necessariamente esse executável. O aplicativo
   procura no PATH, em `Program Files/sigrok/sigrok-cli`, em
   `Program Files (x86)/sigrok/sigrok-cli` e em
   `%LOCALAPPDATA%/LogicLab/sigrok-cli`. Para outro local, defina
   `LOGICLAB_SIGROK` com o caminho completo do executável (sem aspas no valor).
2. O dispositivo deve ter um driver USB compatível. No Windows, siga as
   [instruções do sigrok para WinUSB/Zadig](https://sigrok.org/wiki/Windows).
   Selecione somente o analisador ao configurar o driver. Feche PulseView ou
   outro programa que esteja usando o dispositivo antes de capturar.
3. Abra o LogicLab, selecione **USB Logic Analyzer 24MHz**, atualize a lista,
   escolha o dispositivo e clique em **Conectar**. Ele é USB, não uma porta COM.
4. Escolha taxa, duração (0,01–300 segundos) e dois canais diferentes entre D0
   e D7 para Fio A/B. O padrão é 1 MHz, 2 segundos, A=D0 e B=D1.
5. Clique em **Capturar**. A aquisição é gravada em disco; o gráfico acompanha os dados
   gravados, com pequena latência de leitura e conversão. Para uma medição completa, aguarde a duração
   escolhida. **Parar** interrompe a aquisição e a conversão. O bruto e o trecho de CSV
   já gravados são preservados como captura parcial; o buffer final pode ser perdido.

O analisador USB é um instrumento passivo. Use GND comum e respeite a tensão
de entrada especificada pelo fabricante do seu modelo.

Arquivos em `captures/`:

- `fx2_*.bin`: oito canais originais, um byte por amostra, D0 no bit 0.
- `fx2_*.json`: taxa, canais escolhidos, número de amostras e estado
  `complete`, `partial` ou `error`.
- `fx2_*.log`: diagnóstico do sigrok-cli.
- `usb_*.csv`: transições dos dois canais escolhidos, com `tempo_us` fracionário.
  Os cabeçalhos `fio_A_D2`/`fio_B_D3` são mantidos por compatibilidade; em USB
  representam os canais selecionados, não os pinos D2/D3 do Mega.

O bruto preserva a resolução de uma amostra (aproximadamente 41,67 ns em
24 MHz). O CSV mantém tempos fracionários em microssegundos. A janela em RAM
é limitada; o CSV automático e o bruto em disco devem ser usados para guardar
a aquisição completa. Em 24 MHz, o bruto usa aproximadamente 24 MB por segundo,
antes do espaço adicional para CSV. A taxa suportada na prática depende do
dispositivo, da USB e do disco; erros são apresentados pelo aplicativo.

Para reabrir o bruto no PulseView, importe como **Raw binary logic data**, com
oito canais e a taxa indicada no JSON. O CSV pode ser importado diretamente
no LogicLab. A visualização atual continua dedicada a dois fios; não exibe
os oito canais simultaneamente.

Testes offline: `python -m unittest discover -s tests -v`.

### Arduino Mega

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

### Interface de controle

Depois de gravar o firmware do controlador no Mega, abra
`iniciar_montagem.command` ou execute:

```bash
.venv/bin/python mount_controller.py
```

A interface usa a USB a 115.200 baud e oferece posição em tempo real, movimentos
manuais de azimute e altitude enquanto o botão estiver pressionado, `GOTO`,
parada individual e parada de emergência dos dois eixos.
