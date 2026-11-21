#!/usr/bin/env python3
"""Повторяемое наполнение через публичный gateway. Секреты только в окружении."""
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import sys
import urllib.request
import uuid

MARKER = 'overmindv-starter-v1'


def validate(package):
    topics = {t['key'] for t in package['topics']}
    keys = set()
    titles = set()
    for task in package['tasks']:
        answers, correct = task['answers'], task['correct']
        if (task['key'] in keys or task['title'] in titles or task['topic'] not in topics
                or len(answers) < 2 or len(set(a.strip().casefold() for a in answers)) != len(answers)
                or not correct or len(set(correct)) != len(correct)
                or any(type(i) is not int or i < 0 or i >= len(answers) for i in correct)
                or not task['statement'].strip()):
            raise ValueError('Invalid starter exercise')
        keys.add(task['key'])
        titles.add(task['title'])


class Gateway:
    def __init__(self, url):
        self.url = url
        self.token = ''

    def __call__(self, query, variables):
        headers = {'Content-Type': 'application/json'}
        if self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        request = urllib.request.Request(self.url, data=json.dumps({'query': query, 'variables': variables}).encode(), headers=headers)
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
        if result.get('errors'):
            # Не выводим серверные ошибки и переменные с паролем.
            raise RuntimeError('Gateway operation failed')
        return result['data']


def pages(api, field, selection, extra=''):
    result = []
    for offset in range(0, 100000, 100):
        query = 'query($offset:Int!){' + field + '(pagination:{limit:100,offset:$offset}' + extra + '){' + selection + '}}'
        page = api(query, {'offset': offset})[field]
        if isinstance(page, dict):
            page = page['items']
        result.extend(page)
        if len(page) < 100:
            return result
    raise RuntimeError('Catalog pagination limit exceeded')


def seed(api, package, state, save):
    validate(package)
    topics = pages(api, 'topics', 'id slug title status')
    tasks = pages(api, 'adminITTasks', 'items{id title status}')
    by_tag = {}
    if any(t['key'] not in state for t in package['tasks']):
        for candidate in tasks:
            detail = api('query($id:ID!){adminITTask(id:$id){id status tags}}', {'id': candidate['id']})['adminITTask']
            for tag in detail['tags']:
                if tag.startswith(MARKER + ':'):
                    by_tag[tag] = detail
    topic_ids = {}
    for topic in package['topics']:
        slug = MARKER + '-' + topic['key']
        found = next((t for t in topics if t['slug'] == slug), None)
        if found is None:
            found = api('mutation($input:CreateTopicInput!){createTopic(input:$input){id status}}', {'input': {
                'title': topic['title'], 'slug': slug, 'description': topic['description'],
                'status': 'active', 'orderIndex': 0,
            }})['createTopic']
        if found['status'] == 'active':
            topic_ids[topic['key']] = found['id']
    created = 0
    for task in package['tasks']:
        key = task['key']
        if key in state or task['topic'] not in topic_ids:
            continue
        tag = MARKER + ':' + key
        found = by_tag.get(tag)
        if found is None:
            found = api('mutation($input:ITTaskInput!){createITTask(input:$input){id status}}', {'input': {
                'title': task['title'], 'statement': task['statement'],
                'topicId': topic_ids[task['topic']], 'taskType': 'single_choice' if len(task['correct']) == 1 else 'multiple_choice',
                'difficulty': task.get('difficulty', 'easy'),
                'tags': [MARKER, tag, task['topic'], 'interview'],
                'options': [{'text': a, 'isCorrect': i in task['correct']} for i, a in enumerate(task['answers'])],
            }})['createITTask']
            created += 1
        # После сбоя между create и publish восстанавливаем только собственный draft.
        if found['status'] == 'draft':
            api('mutation($id:ID!){changeITTaskStatus(id:$id,status:published){id}}', {'id': found['id']})
        state[key] = found['id']
        save(state)
    print(json.dumps({'event': 'starter_ready', 'created': created, 'registered': len(state)}, ensure_ascii=False))


def queue_sources(api, sources):
    failed = 0
    previous = {j['idempotencyKey']: j for j in pages(api, 'taskCollectionJobs', 'items{id idempotencyKey status finishedAt}')}
    for url in sources:
        base = MARKER + ':' + url
        attempts = [str(uuid.uuid5(uuid.NAMESPACE_URL, base + ('' if i == 0 else ':retry:' + str(i)))) for i in range(3)]
        known = [(i, previous[key]) for i, key in enumerate(attempts) if key in previous]
        attempt = 0
        if known:
            attempt, job = known[-1]
            if job['status'] in ('succeeded', 'queued', 'running'):
                print(json.dumps({'event': 'source_existing', 'url': url, 'job': job['id'], 'status': job['status']}))
                continue
            finished = job.get('finishedAt')
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(finished.replace('Z', '+00:00'))).total_seconds() if finished else 0
            if age < 3600 or attempt == len(attempts) - 1:
                failed += 1
                print(json.dumps({'event': 'source_retry_deferred', 'url': url, 'job': job['id'], 'status': job['status']}))
                continue
            attempt += 1
        try:
            job = api('mutation($input:StartTaskCollectionInput!){startTaskCollection(input:$input){id status}}', {
                'input': {'idempotencyKey': attempts[attempt], 'websiteUrls': [url], 'maxItemsPerSource': 100}})['startTaskCollection']
            print(json.dumps({'event': 'source_job', 'url': url, 'job': job['id'], 'status': job['status']}))
        except Exception:
            failed += 1
            print(json.dumps({'event': 'source_queue_failed', 'url': url}), file=sys.stderr)
    return failed


def main():
    if os.environ.get('CATALOG_BOOTSTRAP_ENABLED', 'true').lower() != 'true':
        print('Catalog bootstrap disabled')
        return 0
    package = json.loads(Path(os.environ.get('CATALOG_PACKAGE', '/app/data/starter-catalog.json')).read_text())
    validate(package)
    state_file = Path(os.environ.get('CATALOG_STATE', '/state/catalog.json'))
    state_file.parent.mkdir(parents=True, exist_ok=True)
    with state_file.with_suffix('.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = json.loads(state_file.read_text()) if state_file.exists() else {}
        def save(value):
            temporary = state_file.with_suffix('.tmp')
            temporary.write_text(json.dumps(value))
            temporary.replace(state_file)
        api = Gateway(os.environ.get('CATALOG_GATEWAY_URL', 'http://api-gateway:8081/graphql'))
        api.token = api('mutation($input:LoginInput!){login(input:$input){token}}', {'input': {
            'email': os.environ['BOOTSTRAP_SUPERUSER_EMAIL'], 'password': os.environ['BOOTSTRAP_SUPERUSER_PASSWORD']}})['login']['token']
        seed(api, package, state, save)
        if os.environ.get('CATALOG_IMPORT_EXTERNAL', 'true').lower() == 'true':
            failed = queue_sources(api, package['sources'])
            if failed:
                return 2
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print(json.dumps({'event': 'bootstrap_failed', 'kind': type(exc).__name__}), file=sys.stderr)
        sys.exit(1)
