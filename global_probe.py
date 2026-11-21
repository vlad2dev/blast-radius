"""Pinned scalar binding provenance; contains no patched outcomes or test labels."""
import ast
import builtins
from functools import cache
import json
import math
from pathlib import Path
import types
import sys

def module_name(path):
    name=path.removesuffix('.py').replace('/','.')
    return name.removesuffix('.__init__')

def build(root):
    trees={p.relative_to(root).as_posix():ast.parse(p.read_bytes()) for folder in ['sqlglot','tests'] for p in sorted((root/folder).rglob('*.py'))}
    modules={module_name(p):p for p in trees};bindings={p:{} for p in trees};origins={};imports={p:[] for p in trees};exposures={p:set() for p in trees}
    for path,tree in trees.items():
        module=module_name(path)
        if path.startswith('sqlglot/'):
            for node in tree.body:
                target=node.targets[0] if isinstance(node,ast.Assign) and len(node.targets)==1 else node.target if isinstance(node,ast.AnnAssign) else None
                if not isinstance(target,ast.Name) or not target.id.isupper() or node.value is None:continue
                try:value=ast.literal_eval(node.value)
                except (ValueError,TypeError,SyntaxError):continue
                if type(value) not in (type(None),bool,int,float,str) or isinstance(value,float) and not math.isfinite(value):continue
                key=f'global/{module}:{target.id}';bindings[path].setdefault(target.id,set()).add(key);origins[key]=dict(module=module,name=target.id,path=path)
        for node in ast.walk(tree):
            if isinstance(node,ast.Import):
                exposures[path].update(alias.name for alias in node.names if alias.name.startswith('sqlglot'))
            if isinstance(node,ast.ImportFrom):
                name=node.module or ''
                if node.level:
                    package=module if path.endswith('/__init__.py') else module.rsplit('.',1)[0]
                    parts=package.split('.')
                    if node.level>len(parts):continue
                    name='.'.join(parts[:len(parts)-node.level+1]+([name] if name else []))
                imports[path].append((name,[(a.name,a.asname or a.name) for a in node.names]))
                if name.startswith('sqlglot'):exposures[path].add(name)
    for _ in range(len(trees)+1):
        changed=False
        for path,items in imports.items():
            for module,names in items:
                other=modules.get(module)
                if other is None:continue
                for exported,local in names:
                    entries=bindings[other].items() if exported=='*' else [(local,bindings[other].get(exported,set()))]
                    for alias,values in entries:
                        current=bindings[path].setdefault(alias,set())
                        if not set(values)<=current:current.update(values);changed=True
        if not changed:break
    else:raise ValueError('scalar import dependency closure did not converge')
    metadata=set()
    by_name={}
    for key,origin in origins.items():by_name.setdefault(origin['name'],set()).add(key)
    for path,tree in trees.items():
        annotations=[]
        for node in ast.walk(tree):
            if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)):
                arguments=[*node.args.posonlyargs,*node.args.args,*node.args.kwonlyargs]
                if node.args.vararg is not None:arguments.append(node.args.vararg)
                if node.args.kwarg is not None:arguments.append(node.args.kwarg)
                annotations.extend(a.annotation for a in arguments if a.annotation is not None)
                if node.returns is not None:annotations.append(node.returns)
            elif isinstance(node,ast.AnnAssign):annotations.append(node.annotation)
        for annotation in annotations:
            for node in ast.walk(annotation):
                if isinstance(node,ast.Name):metadata.update(bindings[path].get(node.id,()))
                elif isinstance(node,ast.Attribute):metadata.update(by_name.get(node.attr,()))
    return dict(metadata=sorted(metadata),bindings={p:{name:sorted(v) for name,v in names.items() if v} for p,names in bindings.items()},origins=origins,exposures={p:sorted(v) for p,v in exposures.items()})

@cache
def sites(index,module,name):
    data=json.loads(index.read_text());key=f'global/{module}:{name}'
    if key not in data['origins'] or key in data['metadata']:return None
    scopes={module_name(p) for p,names in data['bindings'].items() if any(key in values for values in names.values()) or any(module==exposed or module.startswith(exposed+'.') for exposed in data['exposures'].get(p,()))}
    return (key,*sorted(f'global-escape/{scope}' for scope in scopes))

class GlobalReads:
    def __init__(self,index,emit):
        self.index=json.loads(index.read_text());self.emit=emit;self.raw_getattr=builtins.getattr;self.raw_vars=builtins.vars
        self.names={item['name'] for item in self.index['origins'].values()}
        self.module_names={item['module'] for item in self.index['origins'].values()}
        builtins._blast_scalar_binding=self.read
        builtins._blast_scalar_attribute=self.attribute
        builtins._blast_scalar_getattr=self.getattr
        builtins._blast_scalar_namespace=self.namespace
        builtins._blast_scalar_escape=self.escape
    def read(self,keys,value):
        for key in keys:self.emit(key)
        return value
    def escape(self,module,value):
        self.record_namespace(value)
        self.emit('global-escape/'+module)
        return value
    def attribute(self,obj,name):
        self.record_attribute(obj,name)
        return self.raw_getattr(obj,name)
    def record_attribute(self,obj,name):
        if isinstance(obj,types.ModuleType) and isinstance(name,str):
            module=self.raw_vars(obj).get('__name__')
            if isinstance(module,str) and f'global/{module}:{name}' in self.index['origins']:
                self.emit(f'global/{module}:{name}')
    def getattr(self,callee,obj,name,*default):
        self.record_attribute(obj,name)
        result=callee(obj,name,*default)
        if name in {'__dict__','__globals__'}:self.record_namespace(result)
        return result
    def record_namespace(self,value):
        if type(value) is dict:
            module=value.get('__name__')
            if isinstance(module,str) and module.startswith('sqlglot'):
                self.emit('global-escape/'+module)
    def namespace(self,obj,attribute):
        result=self.raw_getattr(obj,attribute);self.record_namespace(result);return result
    def transform(self,path,tree):
        bindings=self.index['bindings'].get(path,{})
        probe=self;module=module_name(path)
        class Observe(ast.NodeTransformer):
            changed=False
            def visit_FunctionDef(self,node):
                arguments=[*node.args.posonlyargs,*node.args.args,*node.args.kwonlyargs]
                if node.args.vararg is not None:arguments.append(node.args.vararg)
                if node.args.kwarg is not None:arguments.append(node.args.kwarg)
                saved=[a.annotation for a in arguments];returned=node.returns
                for argument in arguments:argument.annotation=None
                node.returns=None
                node=self.generic_visit(node)
                for argument,annotation in zip(arguments,saved):argument.annotation=annotation
                node.returns=returned
                return node
            visit_AsyncFunctionDef=visit_FunctionDef
            def visit_AnnAssign(self,node):
                node.target=self.visit(node.target)
                if node.value is not None:node.value=self.visit(node.value)
                return node
            def visit_Name(self,node):
                keys=bindings.get(node.id)
                if keys and isinstance(node.ctx,ast.Load):
                    self.changed=True
                    return ast.copy_location(ast.Call(func=ast.Name(id='_blast_scalar_binding',ctx=ast.Load()),args=[ast.Tuple(elts=[ast.Constant(k) for k in keys],ctx=ast.Load()),node],keywords=[]),node)
                return node
            def visit_Attribute(self,node):
                node=self.generic_visit(node)
                if isinstance(node.ctx,ast.Load):
                    if node.attr in probe.names:
                        self.changed=True
                        return ast.copy_location(ast.Call(func=ast.Name(id='_blast_scalar_attribute',ctx=ast.Load()),args=[node.value,ast.Constant(node.attr)],keywords=[]),node)
                    if node.attr in {'__dict__','__globals__'}:
                        self.changed=True
                        return ast.copy_location(ast.Call(func=ast.Name(id='_blast_scalar_namespace',ctx=ast.Load()),args=[node.value,ast.Constant(node.attr)],keywords=[]),node)
                return node
            def visit_Call(self,node):
                node=self.generic_visit(node)
                if isinstance(node.func,ast.Name) and not node.keywords:
                    if node.func.id=='getattr' and len(node.args) in (2,3):
                        self.changed=True
                        return ast.copy_location(ast.Call(func=ast.Name(id='_blast_scalar_getattr',ctx=ast.Load()),args=[node.func,*node.args],keywords=[]),node)
                    if node.func.id in {'globals','locals','vars','eval','exec'}:
                        self.changed=True
                        return ast.copy_location(ast.Call(func=ast.Name(id='_blast_scalar_escape',ctx=ast.Load()),args=[ast.Constant(module),node],keywords=[]),node)
                return node
        transform=Observe();tree=transform.visit(tree)
        return tree,transform.changed

if __name__=='__main__':Path(sys.argv[2]).write_text(json.dumps(build(Path(sys.argv[1])),separators=(',',':')))
