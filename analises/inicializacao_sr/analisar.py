"""Decodifica D0/D1 em 57600 8N1, sem corrigir bits para forcar CRC.
Execute da raiz do repositorio: python analises/inicializacao_sr/analisar.py.
A unica falha de framing em D1 por captura e o pulso baixo de inicializacao.
"""
from pathlib import Path
import zipfile,re,sys,csv,json,importlib.util
from collections import Counter
spec=importlib.util.spec_from_file_location('decoder', 'analises/azimute_completo_20261020_165735/analisar.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
for p in Path('captures').glob('Inicial*.sr'):
 rows=[]; offset=0; previous=None
 with zipfile.ZipFile(p) as z:
  metadata=z.read('metadata').decode()
  assert 'samplerate=1 MHz' in metadata and 'unitsize=1' in metadata, metadata
  for name in sorted((n for n in z.namelist() if n.startswith('logic-1-')),key=lambda n:int(n.split('-')[-1])):
   data=z.read(name).translate(bytes(v&3 for v in range(256)))
   for r in re.finditer(rb'([\x00-\x03])\1*',data):
    v=r[1][0]
    if v!=previous: rows.append((offset+r.start(),v&1,(v>>1)&1));previous=v
   offset+=len(data)
 rows.append((offset-1,previous&1,previous>>1))
 events=[]
 print(p.name,'duration',offset/1e6,'first',rows[:5])
 for ch in (0,1):
  frames,stats=m.decode(rows,ch,9.5);events+=frames
  print(ch,stats,Counter(f['command'] for f in frames if f['valid']))
 assert all(f['valid'] for f in events), 'Quadro invalido: examine a captura'
 events.sort(key=lambda f:f['start_us'])
 Path('analises/inicializacao_sr/'+p.stem+'.json').write_text(json.dumps(events,indent=2))
 for f in events:
  if f['start_us']<15000000 or (f['wire']=='B' and f['command'] not in ('09','0A')):
   print(round(f['start_us']/1e6,6),f['wire'],f['hex'],f['valid'])
