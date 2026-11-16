import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from datetime import timedelta
from unittest.mock import patch

from test_dev_work import DevelopmentWorkTests
from app.routes.dev_executor import DevExecution, now
from app.dev_work_model import DevWorkEvent
from dev_worker import worker


class ExecutorTests(DevelopmentWorkTests):
    def setUp(self):
        super().setUp()
        self.extra = patch.dict(os.environ, {'DEV_EXECUTOR_ENABLED': 'true', 'DEV_EXECUTOR_KEY': 'x' * 32})
        self.extra.start()
        self.worker_headers = {'X-Dev-Worker-Key': 'x' * 32}
        self.identity = {'workerId': 'test-pc', 'claimId': 'a' * 32, 'token': 'b' * 64}
        self.prefix = '/internal/imperial/dev-executor'

    def tearDown(self):
        self.extra.stop()
        super().tearDown()

    def authorized(self):
        job = self.submit().json()['id']
        r = self.client.post(f'{self.prefix}/jobs/{job}/authorize', headers=self.headers,
                             json={'workerId': 'test-pc', 'baseSha': 'c' * 40})
        self.assertEqual(r.status_code, 200, r.text)
        return job

    def claim(self):
        return self.client.post(self.prefix + '/claim', headers=self.worker_headers, json=self.identity)

    def test_authorization_and_credentials_are_separate(self):
        self.submit()
        self.assertFalse(self.claim().json()['claimed'])
        self.assertEqual(self.client.post(self.prefix + '/claim', headers=self.headers, json=self.identity).status_code, 401)
        self.assertEqual(self.client.post(self.prefix + '/jobs/1/authorize', headers=self.worker_headers,
                                         json={'workerId': 'test-pc', 'baseSha': 'c' * 40}).status_code, 401)

    def test_recovery_and_one_final_outcome(self):
        job = self.authorized()
        run = self.claim().json()
        self.assertEqual(run, self.claim().json())
        payload = {**self.identity, 'outcome': 'review_needed', 'candidateSha': 'd' * 40,
                   'artifactHash': 'e' * 64, 'checks': 'synthetic_contract_passed'}
        path = self.prefix + '/runs/' + str(run['id'])
        with self.Session() as db:
            db.get(DevExecution, run['id']).lease_until = now() - timedelta(seconds=1)
            db.commit()
        self.assertEqual(self.client.post(path + '/result', headers=self.worker_headers, json=payload).status_code, 409)
        self.assertEqual(self.client.post(path + '/heartbeat', headers=self.worker_headers, json=self.identity).status_code, 200)
        for duplicate in (False, True):
            r = self.client.post(path + '/result', headers=self.worker_headers, json=payload)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json()['duplicate'], duplicate)
        payload['candidateSha'] = 'f' * 40
        self.assertEqual(self.client.post(path + '/result', headers=self.worker_headers, json=payload).status_code, 409)
        with self.Session() as db:
            self.assertEqual(db.query(DevWorkEvent).filter_by(job_id=job, action='final_outcome').count(), 1)
        status = self.client.get(f'{self.prefix}/jobs/{job}', headers=self.headers).json()
        self.assertEqual(status['state'], 'review_needed')
        self.assertNotIn('token', json.dumps(status))

    def test_stolen_claim_and_disabled_gate(self):
        self.authorized(); run = self.claim().json()
        bad = {**self.identity, 'token': 'f' * 64}
        self.assertEqual(self.client.post(self.prefix + '/claim', headers=self.worker_headers, json=bad).status_code, 409)
        with patch.dict(os.environ, {'DEV_EXECUTOR_ENABLED': 'false'}):
            self.assertEqual(self.claim().status_code, 503)
        app_path = self.prefix + '/runs/' + str(run['id']) + '/heartbeat'
        self.assertEqual(self.client.post(app_path, headers=self.worker_headers, json=bad).status_code, 409)

    def test_model_receipt_cannot_stand_in_for_synthetic(self):
        self.authorized(); run = self.claim().json()
        r = self.client.post(self.prefix + '/runs/' + str(run['id']) + '/result', headers=self.worker_headers,
                             json={**self.identity, 'outcome': 'review_needed', 'candidateSha': 'd'*40,
                                   'artifactHash': 'e'*64, 'checks': 'bounded_patch_validated'})
        self.assertEqual(r.status_code, 422)


class WorkerTests(unittest.TestCase):
    def fixture(self, root):
        repo = root / 'repo'; repo.mkdir()
        subprocess.run(['git', 'init', str(repo)], check=True, capture_output=True)
        (repo/'README.md').write_text('Synthetic repository\n')
        worker.git(repo, 'add', '.')
        worker.git(repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-m', 'base')
        state = root / 'state'; state.mkdir()
        return {'root': str(state), 'repository': str(repo)}, {'job': {
            'id': 1, 'baseSha': worker.git(repo, 'rev-parse', 'HEAD'), 'mode': 'synthetic'}}

    def test_commit_is_not_duplicated_on_recovery(self):
        with tempfile.TemporaryDirectory() as td:
            config, journal = self.fixture(Path(td))
            path = Path(config['root']) / 'journal.json'
            first = worker.execute(config, journal, path)
            second = worker.execute(config, journal, path)
            self.assertEqual(first, second)
            tree = Path(config['root']) / 'job-1'
            self.assertEqual(worker.git(tree, 'rev-list', '--count', journal['job']['baseSha'] + '..HEAD'), '1')

    def test_os_lock_excludes_second_process(self):
        with tempfile.TemporaryDirectory() as td:
            with worker.lock(Path(td)):
                script = 'from pathlib import Path; from dev_worker.worker import lock; import sys\nwith lock(Path(sys.argv[1])): print("unexpected")'
                r = subprocess.run([os.sys.executable, '-c', script, td], capture_output=True)
                self.assertNotEqual(r.returncode, 0)

    def test_ambiguous_inference_is_not_repeated(self):
        with tempfile.TemporaryDirectory() as td:
            config, journal = self.fixture(Path(td))
            journal['job']['mode'] = 'local_patch'; journal['modelStarted'] = True
            with patch.object(worker, 'propose') as model:
                with self.assertRaises(worker.Refuse):
                    worker.execute(config, journal, Path(config['root'])/'journal.json')
                model.assert_not_called()

    def test_path_escape_refused(self):
        with self.assertRaises(worker.Refuse):
            worker.validate_edit(Path('.'), {'path': '../secret', 'find': 'a', 'replace': 'b'})

    def test_killed_process_recovers_committed_candidate(self):
        import time
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config, journal = self.fixture(root)
            settings = root / 'settings.json'
            settings.write_text(json.dumps({'config': config, 'journal': journal}))
            marker = root / 'committed'
            script = '''import json, sys, time
from pathlib import Path
from dev_worker import worker
root = Path(sys.argv[1]); data = json.loads((root/'settings.json').read_text())
original = worker.git
def interrupted_git(repo, *args):
    out = original(repo, *args)
    if 'commit' in args:
        (root/'committed').write_text('ready')
        time.sleep(60)
    return out
worker.git = interrupted_git
with worker.lock(Path(data['config']['root'])):
    worker.execute(data['config'], data['journal'], Path(data['config']['root'])/'journal.json')
'''
            process = subprocess.Popen([os.sys.executable, '-c', script, str(root)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 15
                while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertTrue(marker.exists(), 'Child did not reach committed interruption point')
                process.kill(); process.wait(timeout=5)
                with worker.lock(Path(config['root'])):
                    result = worker.execute(config, journal, Path(config['root'])/'journal.json')
                self.assertEqual(result['checks'], 'synthetic_contract_passed')
                self.assertEqual(worker.git(Path(config['root'])/'job-1', 'rev-list', '--count', journal['job']['baseSha']+'..HEAD'), '1')
            finally:
                if process.poll() is None:
                    process.kill(); process.wait()
                process.stderr.close()
