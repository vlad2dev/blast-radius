"""Scalar provenance counterexamples, including aliases and opaque namespaces."""
import ast,json,sys,tempfile,types,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,'/app')
import global_probe as probe
import selector as s

class BindingProvenance(unittest.TestCase):
    def setUp(self):
        self.work=tempfile.TemporaryDirectory();self.addCleanup(self.work.cleanup);self.root=Path(self.work.name)
        (self.root/'sqlglot').mkdir();(self.root/'tests').mkdir()
        (self.root/'sqlglot/source.py').write_text('VALUE = 7\n')
        (self.root/'sqlglot/bridge.py').write_text('from sqlglot.source import VALUE as OTHER\n')
        (self.root/'tests/consumer.py').write_text('from sqlglot.bridge import OTHER as local\ndef read():\n    return local\n')
        self.path=self.root/'index.json';self.data=probe.build(self.root);self.path.write_text(json.dumps(self.data));self.found=[];self.reader=probe.GlobalReads(self.path,self.found.append)
    def execute(self,source,scope=None,path='tests/consumer.py'):
        tree,changed=self.reader.transform(path,ast.parse(source));ast.fix_missing_locations(tree);scope=scope or {};exec(compile(tree,path,'exec'),scope);return scope
    def test_transitive_import_aliases_keep_origin(self):
        self.assertEqual(self.data['bindings']['tests/consumer.py']['local'],['global/sqlglot.source:VALUE'])
        namespace=self.execute('def read():\n    return local\n',{'local':7})
        self.assertEqual(namespace['read'](),7);self.assertIn('global/sqlglot.source:VALUE',self.found)
    def test_same_name_in_unrelated_module_is_not_the_binding(self):
        self.execute('VALUE=99\ndef read():\n    return VALUE\n',path='tests/unrelated.py')['read']()
        self.assertNotIn('global/sqlglot.source:VALUE',self.found)
    def test_dynamic_module_attribute_read_records_origin(self):
        module=types.ModuleType('sqlglot.source');module.VALUE=7
        namespace=self.execute('def read():\n    return getattr(holder, "VALUE")\n',{'holder':module})
        self.assertEqual(namespace['read'](),7);self.assertIn('global/sqlglot.source:VALUE',self.found)
    def test_custom_getattr_callable_is_preserved(self):
        module=types.ModuleType('sqlglot.source');module.VALUE=7
        namespace=self.execute('def read():\n    return getattr(holder, "VALUE")\n',{'holder':module,'getattr':lambda obj,name:99})
        self.assertEqual(namespace['read'](),99)
    def test_module_dictionary_escape_records_actual_module(self):
        module=types.ModuleType('sqlglot.source');module.VALUE=7
        namespace=self.execute('def read():\n    return vars(holder)\n',{'holder':module});self.assertIs(namespace['read'](),module.__dict__)
        self.assertIn('global-escape/sqlglot.source',self.found)
    def test_postponed_annotations_are_unchanged(self):
        source='from __future__ import annotations\ndef read(x: local) -> local:\n    return x\n'
        namespace=self.execute(source,{'local':7});self.assertEqual(namespace['read'].__annotations__,{'x':'local','return':'local'})
    def test_evaluated_annotations_force_conservative_lookup(self):
        (self.root/'tests/annotation.py').write_text('from sqlglot.source import VALUE as local\ndef read(x: local):\n    return x\n')
        path=self.root/'annotations.json';path.write_text(json.dumps(probe.build(self.root)))
        self.assertIsNone(probe.sites(path,'sqlglot.source','VALUE'))
    def test_captured_default_still_records_definition_time_read(self):
        namespace=self.execute('def read(x=local):\n    return x\n',{'local':7})
        self.assertEqual(namespace['read'](),7);self.assertIn('global/sqlglot.source:VALUE',self.found)

class BindingSelection(unittest.TestCase):
    def select(self,reads=(1,),escaped=(),refs=None):
        mapping={'scalar_globals':True,'owners':['@collection','test_one','test_two'],'coverage':{'global/sqlglot.example:VALUE':reads,'global-escape/sqlglot.example':escaped},'test_references':refs or {}}
        with patch.object(s,'global_sites',return_value=('global/sqlglot.example:VALUE','global-escape/sqlglot.example')):
            return s.global_binding_candidates('sqlglot/example.py',b'VALUE = "v"\n',b'VALUE = "q"\n',mapping)
    def test_only_actual_binding_readers_selected(self):self.assertEqual(self.select(),{'test_one'})
    def test_collection_reads_stay_broad(self):self.assertIsNone(self.select((0,1)))
    def test_collection_namespace_escape_stays_broad(self):self.assertIsNone(self.select(escaped=(0,)))
    def test_metadata_inspection_is_retained(self):self.assertEqual(self.select(refs={'getmembers':['test_two']}),{'test_one','test_two'})

if __name__=='__main__':unittest.main()
