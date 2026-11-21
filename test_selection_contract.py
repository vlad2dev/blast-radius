"""Counterexamples for narrowing; execute in the submission's Linux image."""
import ast
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,'/app')
import selector as s

class TestSourceScope(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.root=Path(self.directory.name)
        (self.root/'tests').mkdir();(self.root/'sqlglot').mkdir()
        self.base=patch.object(s,'BASE',self.root);self.base.start()
        self.path='tests/test_scope_contract.py';self.ids={'tests/test_scope_contract.py::TestScope::test_value'}
        self.source='import unittest\nclass TestScope(unittest.TestCase):\n    def test_value(self):\n        self.assertEqual(parse("SELECT 1"), "SELECT 1")\n'
        (self.root/self.path).write_text(self.source)
        s.isolated_test_module.cache_clear();s.module_syntax.cache_clear()
    def tearDown(self):
        self.base.stop();self.directory.cleanup();s.isolated_test_module.cache_clear();s.module_syntax.cache_clear()
    def select(self,new):
        return s.test_module_candidates(self.path,(self.source,new,set(),set()),{self.path:self.ids})
    def test_expected_literal_can_be_scoped(self):
        self.assertEqual(self.select(self.source.replace(', "SELECT 1")',', "SELECT 2")')),self.ids)
    def test_query_call_argument_stays_broad(self):
        self.assertIsNone(self.select(self.source.replace('parse("SELECT 1")','parse("SELECT 2")')))
    def test_extra_state_mutation_stays_broad(self):
        self.assertIsNone(self.select(self.source+'        external.value = True\n'))
    def test_available_import_stays_broad(self):
        self.assertIsNone(self.select('import os\n'+self.source))
    def test_absent_import_scopes_collection_failure(self):
        self.assertEqual(self.select('import gold_contract_absent_463874\n'+self.source),self.ids)
    def test_inbound_reference_stays_broad(self):
        (self.root/'tests/test_other.py').write_text('from tests.test_scope_contract import TestScope\n')
        self.assertIsNone(self.select('import gold_contract_absent_463874\n'+self.source))
    def test_top_level_state_stays_broad(self):
        self.source+='GLOBAL_STATE = initialize()\n';(self.root/self.path).write_text(self.source)
        self.assertIsNone(self.select('import gold_contract_absent_463874\n'+self.source))
    def test_pure_fixture_paths_and_standard_decorators(self):
        self.source='import unittest\nimport os\nfrom unittest import mock\nclass TestScope(unittest.TestCase):\n    file_dir = os.path.dirname(__file__)\n    fixtures_dir = os.path.join(file_dir, "fixtures")\n    @mock.patch("target.name")\n    def test_value(self, mocked):\n        self.assertEqual(1, 1)\n'
        (self.root/self.path).write_text(self.source)
        self.assertEqual(self.select('import gold_contract_absent_463874\n'+self.source),self.ids)
    def test_nested_decorator_calls_are_not_declarations(self):
        self.source=self.source.replace('    def test_value', '    @unittest.skipIf(initialize(), "reason")\n    def test_value')
        (self.root/self.path).write_text(self.source)
        self.assertIsNone(self.select('import gold_contract_absent_463874\n'+self.source))
    def test_annotation_calls_are_not_declarations(self):
        self.source=self.source.replace('def test_value(self):','def test_value(self, value: initialize()):')
        (self.root/self.path).write_text(self.source)
        self.assertIsNone(self.select('import gold_contract_absent_463874\n'+self.source))
    def test_class_definition_cannot_write_external_state(self):
        self.source=self.source.replace('class TestScope(unittest.TestCase):','class TestScope(unittest.TestCase):\n    external.value = 1')
        (self.root/self.path).write_text(self.source)
        self.assertIsNone(self.select('import gold_contract_absent_463874\n'+self.source))
    def test_custom_metaclass_stays_broad(self):
        self.source=self.source.replace('unittest.TestCase','CustomBase');(self.root/self.path).write_text(self.source)
        self.assertIsNone(self.select('import gold_contract_absent_463874\n'+self.source))

class ClassConfiguration(unittest.TestCase):
    def test_literal_shape_changes_are_supported(self):
        for before,after in [('QUOTES = ["a"]','QUOTES = ["a", "b"]'),('MAPPING = {"a": 1}','MAPPING = {"b": 2}'),('FLAGS = {"a"}','FLAGS = {"b", "c"}')]:
            with self.subTest(before=before):
                changes=s.class_changes('sqlglot/dialects/example.py',('class Example:\n    '+before+'\n').encode(),('class Example:\n    '+after+'\n').encode())
                self.assertEqual(len(changes),1)
    def test_root_metaclass_defaults_are_not_scoped_to_instance_readers(self):
        for header in ['class Example(metaclass=Meta):', 'class Example(type):']:
            before=(header+'\n    FLAG = False\n').encode()
            after=before.replace(b'False',b'True')
            with self.subTest(header=header):
                self.assertIsNone(s.class_candidates('sqlglot/example.py',before,after,{},{}))
    def test_calls_are_not_passive_literals(self):
        before=b'class Example:\n    QUOTES = ["a"]\n'
        self.assertFalse(s.class_changes('sqlglot/dialects/example.py',before,b'class Example:\n    QUOTES = initialize()\n'))
    def test_simple_keyword_addition_requires_no_trie_change(self):
        left=ast.parse('{"FOO": TokenType.VAR}',mode='eval').body
        with patch.object(s,'ordinary_keyword_characters',return_value=frozenset('+- "')):
            simple=ast.parse('{"FOO": TokenType.VAR,"BAR": TokenType.PLUS}',mode='eval').body
            special=ast.parse('{"FOO": TokenType.VAR,"B AR": TokenType.PLUS}',mode='eval').body
            self.assertEqual(s.value_consumers('sqlglot/dialects/example.py','KEYWORDS',left,simple),('keyword/BAR',))
            self.assertEqual(s.value_consumers('sqlglot/dialects/example.py','KEYWORDS',left,special),())
    def test_unknown_punctuation_stays_broad(self):
        with patch.object(s,'ordinary_keyword_characters',return_value=None):
            self.assertEqual(s.value_consumers('sqlglot/dialects/example.py','KEYWORDS',ast.parse('{}',mode='eval').body,ast.parse('{"FOO": 1}',mode='eval').body),())

class MembershipObservation(unittest.TestCase):
    def test_negative_membership_is_recorded_without_changing_results(self):
        import comparison_probe as probe
        original=probe.importlib.machinery.SourceFileLoader.get_code
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'sqlglot').mkdir();filename=root/'sqlglot/tokenizer_core.py'
            filename.write_text('class Core:\n    def check(self, key):\n        if 1 < 2:\n            return key in self.keywords, key not in self.keywords\n')
            observations=[]
            try:
                probe.install(root,lambda key:None,observations.append)
                spec=importlib.util.spec_from_file_location('gold_probe_core',filename)
                module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
                obj=module.Core();obj.keywords={'PRESENT':1}
                self.assertEqual(obj.check('ABSENT'),(False,True));self.assertEqual(obj.check('PRESENT'),(True,False))
                self.assertIn('keyword-membership/ABSENT',observations);self.assertIn('keyword/PRESENT',observations)
            finally:probe.importlib.machinery.SourceFileLoader.get_code=original

if __name__=='__main__':unittest.main()
