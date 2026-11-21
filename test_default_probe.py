"""Entry-value coverage and conservative default-signature selection contracts."""
import ast
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
sys.path.insert(0,'/app')
from default_probe import DefaultReads,build
import selector as s

class ActualDefaults(unittest.TestCase):
    def reader(self,values):
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        path=Path(directory.name)/'index.json';path.write_text(json.dumps({'sqlglot/example.py:1':values}))
        found=[];return DefaultReads(path,found.append),found
    def test_later_default_use_is_not_lost_after_override(self):
        reader,found=self.reader({'flag':False,'limit':7});code=object();key='sqlglot/example.py:1'
        self.assertFalse(reader.observe(key,code,SimpleNamespace(f_locals={'flag':True,'limit':8}),'test_one'))
        self.assertFalse(reader.observe(key,code,SimpleNamespace(f_locals={'flag':False,'limit':8}),'test_one'))
        self.assertTrue(reader.observe(key,code,SimpleNamespace(f_locals={'flag':True,'limit':7}),'test_one'))
        self.assertEqual(set(found),{'default/'+key+'/flag','default/'+key+'/limit'})
    def test_boolean_and_integer_are_distinct_bindings(self):
        reader,found=self.reader({'flag':False});key='sqlglot/example.py:1';code=object()
        self.assertFalse(reader.observe(key,code,SimpleNamespace(f_locals={'flag':0}),'test_one'))
        self.assertEqual(found,[])
        self.assertTrue(reader.observe(key,code,SimpleNamespace(f_locals={'flag':False}),'test_one'))
    def test_new_owner_does_not_inherit_seen_parameters(self):
        reader,found=self.reader({'flag':False});key='sqlglot/example.py:1';code=object()
        self.assertTrue(reader.observe(key,code,SimpleNamespace(f_locals={'flag':False}),'test_one'))
        self.assertFalse(reader.observe(key,code,SimpleNamespace(f_locals={'flag':True}),'test_two'))
    def test_default_index_matches_decorated_code_first_line(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'sqlglot').mkdir();p=root/'sqlglot/example.py'
            p.write_text('class Example:\n    @staticmethod\n    def run(flag=False, *, limit=7):\n        return flag\n')
            scope={};exec(compile(p.read_text(),str(p),'exec'),scope)
            code=scope['Example'].run.__code__
            self.assertEqual(build(root)[f'sqlglot/example.py:{code.co_firstlineno}'],{'flag':False,'limit':7})
    def test_real_monitoring_entry_locals_after_explicit_override(self):
        reader,found=self.reader({'flag':False});key='sqlglot/example.py:1'
        def function(flag=False):return flag
        monitor=sys.monitoring;tool=monitor.PROFILER_ID
        def entered(code,offset):
            if code is function.__code__:
                complete=reader.observe(key,code,sys._getframe(1),'test_one')
                return monitor.DISABLE if complete else None
            return monitor.DISABLE
        monitor.use_tool_id(tool,'default-contract')
        try:
            monitor.register_callback(tool,monitor.events.PY_START,entered);monitor.set_events(tool,monitor.events.PY_START)
            self.assertTrue(function(True));self.assertFalse(function())
        finally:
            monitor.set_events(tool,0);monitor.register_callback(tool,monitor.events.PY_START,None);monitor.free_tool_id(tool)
        self.assertIn('default/'+key+'/flag',found)

class SignatureSafety(unittest.TestCase):
    source=b'class Example:\n    def run(self, flag=False):\n        return flag\n'
    patched=source.replace(b'flag=False',b'flag=True')
    def mapping(self,owners=(1,),references=None):
        return {'argument_defaults':True,'owners':['@collection','test_one','test_two'],'coverage':{'default/sqlglot/example.py:2/flag':owners},'test_references':references or {}}
    def test_method_defaults_follow_actual_receivers(self):
        self.assertEqual(s.default_argument_candidates('sqlglot/example.py',self.source,self.patched,self.mapping()),{'test_one'})
    def test_collection_stays_broad(self):
        self.assertIsNone(s.default_argument_candidates('sqlglot/example.py',self.source,self.patched,self.mapping((0,1))))
    def test_metadata_inspection_is_retained(self):
        self.assertEqual(s.default_argument_candidates('sqlglot/example.py',self.source,self.patched,self.mapping((),{'signature':['test_two']})),{'test_two'})
    def test_unrelated_body_change_is_rejected(self):
        self.assertIsNone(s.default_argument_candidates('sqlglot/example.py',self.source,self.patched.replace(b'return flag',b'return not flag'),self.mapping()))
    def test_custom_decorator_is_rejected(self):
        source=self.source.replace(b'    def run',b'    @custom\n    def run')
        self.assertIsNone(s.default_argument_candidates('sqlglot/example.py',source,source.replace(b'flag=False',b'flag=True'),self.mapping()))
    def test_nonfinite_original_defaults_are_rejected(self):
        source=self.source.replace(b'flag=False',b'flag=1e999')
        self.assertIsNone(s.default_argument_candidates('sqlglot/example.py',source,source.replace(b'flag=1e999',b'flag=1.0'),self.mapping()))

if __name__=='__main__':unittest.main()
