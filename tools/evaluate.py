"""Run a selector under scorer limits and preserve reproducible local evidence."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time

def main():
 p=argparse.ArgumentParser();p.add_argument('dataset',type=Path);p.add_argument('output',type=Path);p.add_argument('--image',required=True);p.add_argument('--selector',type=Path);a=p.parse_args()
 data=a.dataset.resolve();out=a.output.resolve();out.parent.mkdir(parents=True,exist_ok=True)
 measure='import json,subprocess,sys,time\nfrom pathlib import Path\ndef cpu():\n return int(dict(line.split() for line in Path("/sys/fs/cgroup/cpu.stat").read_text().splitlines())["usage_usec"])\nstart=time.monotonic();before=cpu();r=subprocess.run([sys.executable,"/app/selector.py"]);print(json.dumps(dict(seconds_wall=time.monotonic()-start,seconds_cpu=(cpu()-before)/1e6)),file=sys.stderr);sys.exit(r.returncode)'
 command=['docker','run','--rm','--platform','linux/amd64','--network','none','--cpus','2','--memory','4096m','--memory-swap','4096m','--pids-limit','256','--read-only','--tmpfs','/tmp:rw,size=64m,mode=1777','--cap-drop','ALL','--security-opt','no-new-privileges','--user','65534:65534','--env','HOME=/tmp','--volume',f'{data}/commits:/data/commits:ro','--volume',f'{data}/tests.json:/data/tests.json:ro']
 if a.selector:command+=['--volume',f'{a.selector.resolve()}:/app/selector.py:ro']
 command += [a.image,'python','-c',measure]
 start=time.monotonic();run=subprocess.run(command,capture_output=True,text=True,timeout=190)
 out.write_text(run.stdout)
 repo=Path(__file__).resolve().parents[1]
 git=subprocess.run(['git','rev-parse','HEAD'],cwd=repo,capture_output=True,text=True)
 dirty=subprocess.run(['git','status','--porcelain','--untracked-files=no'],cwd=repo,capture_output=True,text=True)
 evidence=dict(image=a.image,command=command,returncode=run.returncode,host_wall=time.monotonic()-start,stderr=run.stderr,commit=git.stdout.strip(),dirty=bool(dirty.stdout),output_sha256=hashlib.sha256(out.read_bytes()).hexdigest(),selector_sha256=hashlib.sha256(a.selector.read_bytes()).hexdigest() if a.selector else None)
 out.with_suffix('.evidence.json').write_text(json.dumps(evidence,indent=2));print(json.dumps(evidence))
 if run.returncode:raise SystemExit(run.returncode)
 if len(run.stdout.encode())>2*1024*1024:raise SystemExit('Output exceeds scorer limit')

if __name__=='__main__':main()
