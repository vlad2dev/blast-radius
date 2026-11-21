"""Record actual entry arguments matching literal defaults on the pinned source."""
import ast
import json
import math
from pathlib import Path
import sys

def build(root):
    result={}
    for path in sorted((root/'sqlglot').rglob('*.py')):
        relative=path.relative_to(root).as_posix()
        for node in ast.walk(ast.parse(path.read_bytes())):
            if not isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)):continue
            params=[*node.args.posonlyargs,*node.args.args]
            defaults=list(zip(params[len(params)-len(node.args.defaults):],node.args.defaults))
            defaults.extend((p,d) for p,d in zip(node.args.kwonlyargs,node.args.kw_defaults) if d is not None)
            values={}
            for param,expression in defaults:
                try:value=ast.literal_eval(expression)
                except (ValueError,TypeError,SyntaxError):continue
                if type(value) not in (type(None),str,bool,int,float) or isinstance(value,float) and not math.isfinite(value):continue
                values[param.arg]=value
            if values:
                first=min([node.lineno,*[d.lineno for d in node.decorator_list]])
                result[f'{relative}:{first}']=values
    return result

class DefaultReads:
    def __init__(self,index,emit):
        self.sites=json.loads(index.read_text());self.emit=emit;self.seen={}
    def observe(self,key,code,frame,owner):
        values=self.sites.get(key)
        if values is None:return True
        previous=self.seen.get(code)
        observed=previous[1] if previous and previous[0]==owner else set()
        local=frame.f_locals
        for name,default in values.items():
            if name not in observed and name in local:
                value=local[name]
                if type(value) is type(default) and value==default:
                    self.emit(f'default/{key}/{name}');observed.add(name)
        self.seen[code]=(owner,observed)
        return len(observed)==len(values)

if __name__=='__main__':Path(sys.argv[2]).write_text(json.dumps(build(Path(sys.argv[1])),separators=(',',':')))
