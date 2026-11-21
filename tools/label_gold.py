"""Bounded resumable full-suite labeling; labels never enter the selector image."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import subprocess
import time

def main():
 p=argparse.ArgumentParser();p.add_argument('corpus',type=Path);p.add_argument('--jobs',type=int,choices=[1,2],default=2);p.add_argument('--split',choices=['development','holdout','all'],default='all');p.add_argument('--validate-serial',type=Path);args=p.parse_args()
 corpus=args.corpus.resolve();manifest=json.loads((corpus/'manifest.json').read_text());oracle=Path(__file__).with_name('gold_oracle.py').resolve()
 jobs=[]
 for split,items in manifest['partitions'].items():
  if args.split!='all' and split!=args.split:continue
  folder=corpus/split;(folder/'labels').mkdir(exist_ok=True)
  for item in items:
   patch=folder/'commits'/(item['name']+'.patch')
   assert hashlib.sha256(patch.read_bytes()).hexdigest()==item['sha256']
   jobs.append((split,item,folder))
 def label(job):
  split,item,folder=job;dest=folder/'labels'/(item['name']+'.json')
  if dest.exists():
   data=json.loads(dest.read_text());assert data['patch_sha256']==item['sha256'];return job,data
  name='gold-'+split+'-'+hashlib.sha256(item['name'].encode()).hexdigest()[:12]
  command=['docker','run','--rm','--name',name,'--platform','linux/amd64','--network','none','--cpus','4','--memory','3500m','--memory-swap','3500m','--pids-limit','256','--read-only','--tmpfs','/tmp:rw,size=64m,mode=1777','--user','65534:65534','--cap-drop','ALL','--security-opt','no-new-privileges','-e','GOLD_WORKERS=4','-v',f'{folder}:/gold:ro','-v',f'{oracle}:/oracle.py:ro','blast-radius-dependency-review','python','/oracle.py',f'/gold/commits/{item["name"]}.patch']
  try:
   run=subprocess.run(command,capture_output=True,text=True,timeout=260)
   if run.returncode:raise RuntimeError(run.stdout[-4000:]+'\n'+run.stderr[-4000:])
   data=json.loads(run.stdout);data['patch_sha256']=item['sha256'];data['oracle_workers']=4
   dest.write_text(json.dumps(data,indent=2));return job,data
  except Exception as exc:
   subprocess.run(['docker','rm','-f',name],capture_output=True)
   (folder/'labels'/(item['name']+'.incomplete.txt')).write_text(str(exc))
   return job,None
 # Keep holdout identities, labels and counts out of the development transcript.
 counts={split:0 for split in manifest['partitions']};answers={split:{} for split in manifest['partitions']}
 with ThreadPoolExecutor(max_workers=args.jobs) as pool:
  futures=[pool.submit(label,job) for job in jobs]
  for f in as_completed(futures):
   (split,item,folder),data=f.result();counts[split]+=1
   if data is not None:
    answers[split][item['name']]=data
    (folder/'answers.json').write_text(json.dumps({'commits':answers[split]},indent=2))
   if split=='development':print(f'{split} {counts[split]}/{len(manifest["partitions"][split])}: {item["name"]} '+(f'failures={len(data["failing"])} seconds={data["wall"]:.1f}' if data else 'INCOMPLETE'),flush=True)
   else:print(f'holdout {counts[split]}/{len(manifest["partitions"][split])}: '+('oracle complete' if data else 'INCOMPLETE'),flush=True)
 if any(len(answers[s])!=counts[s] for s in counts):raise SystemExit('Incomplete cases remain; do not treat as gold.')
 print('Labeling complete',flush=True)

if __name__=='__main__':main()
