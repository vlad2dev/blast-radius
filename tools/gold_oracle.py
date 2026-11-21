"""Label exactly one public mutation with the whole pytest suite."""
import json,os,shutil,subprocess,sys,tempfile,time
from pathlib import Path
sys.path.insert(0,'/app')
from selector import apply_patch
patch=Path(sys.argv[1]); tests=json.loads(Path('/gold/tests.json').read_text())['tests'];base=Path('/opt/sqlglot')
edits=apply_patch(patch,base)
with tempfile.TemporaryDirectory(prefix='oracle-') as work:
 root=Path(work)/'source';shutil.copytree(base,root,copy_function=os.symlink)
 for path,(_,content,_,_) in edits.items():
  target=root/path;target.unlink();target.write_bytes(content)
 report=Path(work)/'report.json'
 env=dict(os.environ,PYTHONPATH=f'/app:{root}',BLAST_RESULT=str(report),BLAST_COLLECT='0')
 workers=int(os.environ.get('GOLD_WORKERS','0'))
 if workers:
  (root/'gold_reports.py').write_text('import test_observer as observer\nfrom pathlib import Path\ndef pytest_sessionstart(session):\n    worker = getattr(session.config, "workerinput", {}).get("workerid", "controller")\n    observer.OUTPUT = observer.OUTPUT.with_name("report-" + worker + ".json")\n')
 command=[sys.executable,'-m','pytest','-q','--tb=no','-p','no:cacheprovider','-p','test_observer','--continue-on-collection-errors','tests']
 if workers: command[3:3]=['-n',str(workers),'-p','gold_reports']
 start=time.monotonic()
 run=subprocess.run(command,cwd=root,env=env,capture_output=True,text=True,timeout=240)
 reports=list(Path(work).glob('report-*.json')) if workers else [report]
 if run.returncode not in (0,1,2) or not reports or not all(p.exists() for p in reports):raise RuntimeError(run.stdout[-4000:]+run.stderr[-2000:])
 data={key:sorted({item for p in reports for item in json.loads(p.read_text())[key]}) for key in ('finished','failed','collection_errors')}
 errors=set(data['collection_errors'])
 by_file={t['file'] for t in tests};assert errors<=by_file,errors
 required={t['id'] for t in tests if t['file'] not in errors}
 assert required<=set(data['finished']),str(sorted(required-set(data['finished']))[:20])
 failed=set(data['failed']);failed.update(t['id'] for t in tests if t['file'] in errors)
 assert failed<={t['id'] for t in tests},str(failed-{t['id'] for t in tests})
 print(json.dumps(dict(failing=sorted(t['number'] for t in tests if t['id'] in failed),kind='independent-mutation',wall=time.monotonic()-start,completed=len(data['finished']),collection_errors=sorted(errors))))
