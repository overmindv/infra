import importlib.util
import json
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location('bootstrap', Path(__file__).parents[1] / 'bootstrap_catalog.py')
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)

class BootstrapTests(unittest.TestCase):
    def test_package_is_valid(self):
        package = json.loads((Path(__file__).parents[2] / 'data/starter-catalog.json').read_text())
        bootstrap.validate(package)
        self.assertGreaterEqual(len(package['tasks']), 60)
        self.assertGreaterEqual(len(package['sources']), 10)

    def test_reject_duplicate_options(self):
        with self.assertRaises(ValueError):
            bootstrap.validate({'topics': [{'key': 'sql'}], 'tasks': [{'key': 'a', 'topic': 'sql', 'title': 'a', 'statement': 'b', 'answers': ['x', 'x'], 'correct': [0]}], 'sources': []})

    def test_external_failures_are_isolated(self):
        calls = []
        def api(query, variables):
            if query.startswith('query'):
                return {'taskCollectionJobs': {'items': []}}
            calls.append(variables)
            raise RuntimeError('do not expose credentials')
        result = bootstrap.queue_sources(api, ['https://leetcode.com/problems/two-sum/', 'https://leetcode.com/problems/valid-parentheses/'])
        self.assertEqual(result, 2)
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0]['input']['idempotencyKey'], calls[1]['input']['idempotencyKey'])


class FakeAPI:
    def __init__(self):
        self.topics = []
        self.tasks = []
        self.fail_publish = False

    def __call__(self, query, variables):
        if query.startswith('query'):
            if 'adminITTasks(' in query:
                return {'adminITTasks': {'items': self.tasks[variables['offset']:variables['offset']+100]}}
            if 'adminITTask(' in query:
                return {'adminITTask': next(t for t in self.tasks if t['id'] == variables['id'])}
            return {'topics': self.topics[variables['offset']:variables['offset']+100]}
        if 'createTopic(' in query:
            item = dict(variables['input'], id='topic-'+str(len(self.topics)))
            self.topics.append(item)
            return {'createTopic': item}
        if 'createITTask(' in query:
            item = dict(variables['input'], id='task-'+str(len(self.tasks)), status='draft')
            self.tasks.append(item)
            return {'createITTask': item}
        if self.fail_publish:
            raise RuntimeError('publish unavailable')
        item = next(t for t in self.tasks if t['id'] == variables['id'])
        item['status'] = 'published'
        return {'changeITTaskStatus': item}


class IdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.package = {'topics': [{'key': 'sql', 'title': 'SQL', 'description': ''}], 'tasks': [{'key': 'sql-1', 'topic': 'sql', 'title': 'SQL exercise', 'statement': 'Choose', 'answers': ['a', 'b'], 'correct': [0]}], 'sources': []}
        self.api = FakeAPI()

    def test_repeat_and_lost_state_do_not_duplicate(self):
        for _ in range(3):
            bootstrap.seed(self.api, self.package, {}, lambda state: None)
        self.assertEqual(len(self.api.tasks), 1)
        self.assertEqual(len(self.api.topics), 1)
        self.assertEqual(self.api.tasks[0]['status'], 'published')

    def test_resume_after_create_before_publish(self):
        self.api.fail_publish = True
        with self.assertRaises(RuntimeError):
            bootstrap.seed(self.api, self.package, {}, lambda state: None)
        self.api.fail_publish = False
        bootstrap.seed(self.api, self.package, {}, lambda state: None)
        self.assertEqual(len(self.api.tasks), 1)
        self.assertEqual(self.api.tasks[0]['status'], 'published')

    def test_archived_task_is_not_republished(self):
        bootstrap.seed(self.api, self.package, {}, lambda state: None)
        self.api.tasks[0]['status'] = 'archived'
        bootstrap.seed(self.api, self.package, {}, lambda state: None)
        self.assertEqual(self.api.tasks[0]['status'], 'archived')


class RecoveryTests(IdempotencyTests):
    def test_renamed_task_is_recovered_by_tag(self):
        bootstrap.seed(self.api, self.package, {}, lambda state: None)
        self.api.tasks[0]['title'] = 'Авторское название'
        bootstrap.seed(self.api, self.package, {}, lambda state: None)
        self.assertEqual(len(self.api.tasks), 1)

    def test_failed_source_retries_after_cooldown(self):
        import uuid
        url = 'https://leetcode.com/problems/two-sum/'
        old_key = str(uuid.uuid5(uuid.NAMESPACE_URL, bootstrap.MARKER + ':' + url))
        calls = []
        def api(query, variables):
            if query.startswith('query'):
                return {'taskCollectionJobs': {'items': [{'id': 'old', 'idempotencyKey': old_key, 'status': 'failed', 'finishedAt': '2020-01-01T00:00:00Z'}]}}
            calls.append(variables)
            return {'startTaskCollection': {'id': 'new', 'status': 'queued'}}
        self.assertEqual(bootstrap.queue_sources(api, [url]), 0)
        self.assertEqual(len(calls), 1)
        self.assertNotEqual(calls[0]['input']['idempotencyKey'], old_key)

class SourceRetryPolicyTests(unittest.TestCase):
    def test_success_running_cooldown_and_exhausted_do_not_enqueue(self):
        from datetime import datetime, timezone
        import uuid
        url = 'https://leetcode.com/problems/two-sum/'
        base = bootstrap.MARKER + ':' + url
        for status, age, suffix, expected in [('succeeded', '2020-01-01T00:00:00Z', '', 0), ('running', None, '', 0), ('queued', None, '', 0), ('failed', datetime.now(timezone.utc).isoformat(), '', 1), ('partial', '2020-01-01T00:00:00Z', ':retry:2', 1)]:
            with self.subTest(status=status, suffix=suffix):
                def api(query, variables):
                    self.assertTrue(query.startswith('query'))
                    return {'taskCollectionJobs': {'items': [{'id': 'j', 'idempotencyKey': str(uuid.uuid5(uuid.NAMESPACE_URL, base+suffix)), 'status': status, 'finishedAt': age}]}}
                self.assertEqual(bootstrap.queue_sources(api, [url]), expected)


if __name__ == '__main__':
    unittest.main()
