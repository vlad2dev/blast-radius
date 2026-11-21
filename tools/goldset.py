"""Generate a fixed, partitioned mutation corpus from the public pinned source.

No labels are used here. Gold labels come from independent full-suite execution.
This file is tooling and is excluded from the submission image.
"""
import argparse
import ast
import difflib
import hashlib
import json
import random
from pathlib import Path

SEED = 20261120

def replace_node(source, node, value):
    lines = source.splitlines(keepends=True)
    first = lines[node.lineno - 1]
    last = lines[node.end_lineno - 1]
    return ''.join(lines[:node.lineno-1]) + first[:node.col_offset] + value + last[node.end_col_offset:] + ''.join(lines[node.end_lineno:])

def generate(base, catalog, output):
    cases = []
    def add(name, category, filename, changed):
        source = (base / filename).read_text(encoding='utf-8')
        assert source != changed, name
        patch = f'diff --git a/{filename} b/{filename}\n' + ''.join(difflib.unified_diff(source.splitlines(keepends=True), changed.splitlines(keepends=True), fromfile=f'a/{filename}', tofile=f'b/{filename}'))
        cases.append(dict(name=name, category=category, patch=patch))
    for filename, field in [
        ('sqlglot/dialects/mysql.py','QUOTES'),
        ('sqlglot/dialects/bigquery.py','QUOTES'),
        ('sqlglot/dialects/mysql.py','COMMENTS'),
        ('sqlglot/dialects/bigquery.py','COMMENTS'),
        ('sqlglot/dialects/mysql.py','IDENTIFIERS'),
        ('sqlglot/dialects/bigquery.py','IDENTIFIERS'),
        ('sqlglot/dialects/postgres.py','BYTE_STRINGS'),
        ('sqlglot/tokens.py','COMMENTS'),
    ]:
        source=(base/filename).read_text(encoding='utf-8'); tree=ast.parse(source)
        node=next(n.value for n in ast.walk(tree) if isinstance(n,ast.Assign) and isinstance(n.targets[0],ast.Name) and n.targets[0].id==field)
        value=ast.literal_eval(node)
        replacement=repr(value[:-1]) if len(value)>1 else repr(value+['~'])
        add(Path(filename).stem+'-'+field.lower(),'class-container',filename,replace_node(source,node,replacement))
    for filename in ['sqlglot/dialects/mysql.py','sqlglot/dialects/postgres.py']:
        source=(base/filename).read_text(encoding='utf-8'); tree=ast.parse(source)
        node=next(n.value for n in ast.walk(tree) if isinstance(n,ast.Assign) and isinstance(n.targets[0],ast.Name) and n.targets[0].id=='TIME_MAPPING')
        assert isinstance(node,ast.Dict)
        value=ast.literal_eval(node); value.pop(next(iter(value)))
        add(Path(filename).stem+'-time-map','class-dictionary',filename,replace_node(source,node,repr(value)))
    for field in ['INDEX','ARG_KEY','CLASS','VALUE']:
        filename='sqlglot/serde.py'; source=(base/filename).read_text(encoding='utf-8'); tree=ast.parse(source)
        node=next(n.value for n in tree.body if isinstance(n,ast.Assign) and n.targets[0].id==field)
        add('serde-'+field.lower(),'module-binding',filename,replace_node(source,node,repr('gold_'+field.lower())))
    for filename in ['tests/test_build.py','tests/test_jsonpath.py','tests/test_serde.py','tests/test_diff.py']:
        source=(base/filename).read_text(encoding='utf-8')
        add(Path(filename).stem+'-import','test-collection',filename,'import goldset_intentionally_absent_20261120\n'+source)
    for filename in ['tests/test_build.py','tests/test_jsonpath.py']:
        source=(base/filename).read_text(encoding='utf-8'); tree=ast.parse(source)
        node=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name.startswith('test'))
        # An extra failing assertion in an existing test; catalog stays fixed.
        lines=source.splitlines(keepends=True); index=node.body[0].lineno-1
        lines.insert(index,' '*(node.col_offset+4)+'assert False, "goldset fixed mutation"\n')
        add(Path(filename).stem+'-assert','test-body',filename,''.join(lines))
    for filename,field,value in [('sqlglot/errors.py','ERROR_MESSAGE_CONTEXT_DEFAULT','7'),('sqlglot/typing/snowflake.py','MAX_PRECISION','23')]:
        source=(base/filename).read_text(encoding='utf-8'); tree=ast.parse(source)
        node=next(n.value for n in tree.body if isinstance(n,ast.Assign) and isinstance(n.targets[0],ast.Name) and n.targets[0].id==field)
        add(Path(filename).stem+'-scalar','module-binding',filename,replace_node(source,node,value))
    for filename in ['sqlglot/generators/sqlite.py','sqlglot/parsers/dremio.py']:
        source=(base/filename).read_text(encoding='utf-8'); tree=ast.parse(source)
        node=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and len(n.body)>1)
        lines=source.splitlines(keepends=True);lines.insert(node.body[0].lineno-1,' '*(node.col_offset+4)+'raise ValueError("goldset fixed mutation")\n')
        add(Path(filename).stem+'-raise','function-body',filename,''.join(lines))
    # Broad structural mutations; selection is fixed before any oracle result.
    files = sorted((base/'sqlglot').rglob('*.py'))
    body_files = [f for f in files if any(part in f.parts for part in ('optimizer','generators','parsers','expressions'))]
    rng = random.Random(SEED+1); rng.shuffle(body_files)
    for filename_path in body_files[:22]:
        filename=filename_path.relative_to(base).as_posix();source=filename_path.read_text(encoding='utf-8');tree=ast.parse(source)
        choices=[]
        for function in ast.walk(tree):
            if isinstance(function,ast.FunctionDef):
                choices.extend(n for n in ast.walk(function) if isinstance(n,ast.If) and not isinstance(n.test,ast.Constant))
        if choices:
            node=choices[0].test
            add(filename.replace('/','-')+'-condition','control-flow',filename,replace_node(source,node,'not ('+ast.get_source_segment(source,node)+')'))
        returns=[n for n in ast.walk(tree) if isinstance(n,ast.Return) and isinstance(n.value,ast.Constant) and n.value.value is not None]
        if returns:
            node=returns[0].value;value=node.value
            replacement=not value if isinstance(value,bool) else value+1 if isinstance(value,(int,float)) else value+' gold' if isinstance(value,str) else None
            if replacement is not None:add(filename.replace('/','-')+'-return','return-value',filename,replace_node(source,node,repr(replacement)))
    dialect_files=sorted((base/'sqlglot/dialects').glob('*.py'));rng.shuffle(dialect_files)
    for filepath in dialect_files[:24]:
        filename=filepath.relative_to(base).as_posix();source=filepath.read_text(encoding='utf-8');tree=ast.parse(source)
        flags=[n.value for n in ast.walk(tree) if isinstance(n,ast.Assign) and isinstance(n.targets[0],ast.Name) and n.targets[0].id.isupper() and isinstance(n.value,ast.Constant) and type(n.value.value) is bool]
        if flags:add(filepath.stem+'-flag','class-flag',filename,replace_node(source,flags[0],repr(not flags[0].value)))
        dictionaries=[n.value for n in ast.walk(tree) if isinstance(n,ast.Assign) and isinstance(n.targets[0],ast.Name) and n.targets[0].id=='KEYWORDS' and isinstance(n.value,ast.Dict)]
        if dictionaries:
            node=dictionaries[0]; pairs=[(k,v) for k,v in zip(node.keys,node.values) if isinstance(k,ast.Constant) and isinstance(k.value,str) and isinstance(v,ast.Attribute)]
            if pairs:
                key,value=pairs[0]
                # Change an existing mapping and separately add a new key. Both
                # values refer to the pinned TokenType namespace, without calls.
                replacement=ast.unparse(value.value)+'.PLUS'
                if ast.unparse(value)!=replacement:add(filepath.stem+'-keyword-value','registry-value',filename,replace_node(source,value,replacement))
                fresh=ast.Dict(keys=node.keys+[ast.Constant('GOLDSET_NEW_TOKEN')],values=node.values+[ast.Attribute(value=value.value,attr='PLUS',ctx=ast.Load())])
                add(filepath.stem+'-keyword-add','registry-addition',filename,replace_node(source,node,ast.unparse(fresh)))
    for filepath in body_files[:16]:
        filename=filepath.relative_to(base).as_posix();source=filepath.read_text(encoding='utf-8');tree=ast.parse(source)
        choices=[d for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) for d in n.args.defaults if isinstance(d,ast.Constant) and type(d.value) is bool]
        if choices:add(filename.replace('/','-')+'-default','signature-default',filename,replace_node(source,choices[0],repr(not choices[0].value)))
    test_files=sorted((base/'tests').glob('test_*.py'));rng.shuffle(test_files)
    used={c['name'] for c in cases}
    for filepath in test_files[:12]:
        filename=filepath.relative_to(base).as_posix();source=filepath.read_text(encoding='utf-8');tree=ast.parse(source)
        name=filepath.stem+'-extra-import'
        # Place after future imports to keep this an import failure, not syntax.
        futures=[n.end_lineno for n in tree.body if isinstance(n,ast.ImportFrom) and n.module=='__future__']
        lines=source.splitlines(keepends=True);lines.insert(max(futures,default=0),'import goldset_intentionally_absent_20261120\n')
        add(name,'test-collection',filename,''.join(lines))
    for filepath in body_files[22:28]:
        filename=filepath.relative_to(base).as_posix();source=filepath.read_text(encoding='utf-8')
        add(filename.replace('/','-')+'-import-error','source-import',filename,source+'\nraise ImportError("fixed gold mutation")\n')
    for filepath in body_files[28:34]:
        filename=filepath.relative_to(base).as_posix();source=filepath.read_text(encoding='utf-8')
        add(filename.replace('/','-')+'-syntax','source-syntax',filename,source+'\ndef gold_invalid(:\n')
    for filepath in body_files[34:40]:
        filename=filepath.relative_to(base).as_posix();source=filepath.read_text(encoding='utf-8')
        add(filename.replace('/','-')+'-comment','nonsemantic',filename,'# fixed public-source mutation\n'+source)
    fixture_files=sorted((base/'tests/fixtures/optimizer').glob('*.sql'));rng.shuffle(fixture_files)
    for filepath in fixture_files[:12]:
        filename=filepath.relative_to(base).as_posix();source=filepath.read_text(encoding='utf-8')
        if 'SELECT ' in source:add(filepath.stem+'-fixture','fixture',filename,source.replace('SELECT ','SELECT gold_missing_column, ',1))
    # Combined changes exercise union and fallback interactions.
    originals=list(cases)
    for i in range(8):
        left,right=originals[i],originals[-1-i]
        if left['patch'].splitlines()[0]==right['patch'].splitlines()[0]:continue
        cases.append(dict(name='combined-'+str(i),category='multi-file',patch=left['patch']+right['patch']))
    # Preserve v1 assignments: cases already used for development can never
    # reappear in the sealed holdout after expanding the corpus.
    previous=output.parent/'gold-v1'/'manifest.json'
    known={item['name']:split for split,items in json.loads(previous.read_text())['partitions'].items() for item in items} if previous.exists() else {}
    rng=random.Random(SEED);rng.shuffle(cases)
    groups={}
    for case in cases: groups.setdefault(case['category'],[]).append(case)
    partitions={'development':[], 'holdout':[]}
    for group in groups.values():
        for i,case in enumerate(group): partitions[known.get(case['name'], 'development' if i%2==0 else 'holdout')].append(case)
    manifest={'seed':SEED,'source_commit':'eb11aec4be8cb2122afe713a4816662c3f0fb2fa','partitions':{}}
    for split,items in partitions.items():
        folder=output/split; (folder/'commits').mkdir(parents=True,exist_ok=True)
        (folder/'tests.json').write_bytes(catalog.read_bytes())
        manifest['partitions'][split]=[]
        for case in sorted(items,key=lambda c:c['name']):
            patch=case.pop('patch').encode();(folder/'commits'/(case['name']+'.patch')).write_bytes(patch)
            manifest['partitions'][split].append(dict(case,sha256=hashlib.sha256(patch).hexdigest()))
    (output/'manifest.json').write_text(json.dumps(manifest,indent=2))
    print(json.dumps({k:len(v) for k,v in partitions.items()}))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('baseline',type=Path);p.add_argument('catalog',type=Path);p.add_argument('output',type=Path);a=p.parse_args();generate(a.baseline,a.catalog,a.output)
