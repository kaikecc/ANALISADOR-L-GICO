# Inicialização e azimute — evidência offline

Fontes: os três arquivos `captures/Inicializacao-Controle-Montagem*.sr`.
Metadados: 1 MHz, 8 canais, 1 byte/amostra. Decodificação: D0/A recebe as
respostas, D1/B contém os pedidos; 57600 8N1, CRC-16/KERMIT little-endian.
O script reutiliza o decodificador da análise anterior, sem reparar bytes.

| Captura | D0 quadros válidos | D1 quadros válidos |
|---|---:|---:|
| Montagem | 15 | 23 |
| Montagem2 | 10 | 18 |
| Montagem-Az | 25 | 42 |

Zero erros de framing em D0; um em D1 por captura, correspondente ao
pulso de aproximadamente 288 ms baixo antes dos dados. Nenhum quadro
com CRC inválido. O primeiro 0D começa cerca de 1.798,9 ms após subir D1.

Intervalos entre **inícios** de quadros, nas três capturas:

- 0D → 0B: 2.477,439 a 2.477,768 ms.
- 0B → 80 e 80 → 0A: aproximadamente 11,893 ms.
- 0A → 0E: aproximadamente 126,044 ms.
- 0E → primeiro stop: 788,920 a 789,198 ms.
- Stops/configuração: aproximadamente 5,885–6,060 ms; segundo 80 → C0: 17,900 ms.
- 0A em repouso: aproximadamente 5.085 ms.

Na captura Az, `C0 stop` em 61,041914 s precede
`80 01 00 04` em 61,048147 s. A primeira consulta 09 ocorre em
61,296588 s; as seguintes têm período de aproximadamente 285,005 ms.
As 20 consultas 09 têm respostas válidas, iniciadas cerca de 2,56 ms
após o início do pedido. A posição sobe de 60 até 3.323 contagens,
confirmando o sentido positivo e a velocidade 4 como comando aceito.

Há repetição de 81/C1 em 25,085969/25,092028 s e silêncio até o movimento
em 61 s: estas capturas não demonstram necessidade de keepalive contínuo
nem mostram o travamento do Arduino. Não se pode atribuir a falha física
unicamente a intervalos ou baud a partir delas.

Os JSON preservam horário, canal, bytes e validação de todos os quadros.
Reproduzir: `python analises/inicializacao_sr/analisar.py` na raiz do projeto.
