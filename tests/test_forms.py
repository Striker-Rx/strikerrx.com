import base64
from datetime import datetime, timezone
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('forms', ROOT / 'server/app.py')
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


def sample(kind):
    if kind == 'provider':
        return dict(practice='Test practice', ptype='Other', pphone='8320000000', pname='Test provider',
                    npi='1234567890', email='test@example.com', message='Please contact us', categories=['Other'])
    if kind == 'career':
        return dict(role='Pharmacist', name='Test person', email='test@example.com',
                    resume={'name': 'resume.pdf', 'data': base64.b64encode(b'%PDF-1.4\nsample').decode()})
    return dict(name='Test person', email='test@example.com', message='Please contact me')


class FormTests(unittest.TestCase):
    def setUp(self):
        app._requests.clear()

    def request(self, kind='patient', body=None, **overrides):
        payload = json.dumps(sample(kind) if body is None else body).encode()
        environ = dict(PATH_INFO='/api/forms/' + kind, REQUEST_METHOD='POST',
                       HTTP_ORIGIN='https://strikerrx.com', CONTENT_TYPE='application/json',
                       CONTENT_LENGTH=str(len(payload)), REMOTE_ADDR='198.51.100.7')
        environ['wsgi.input'] = io.BytesIO(payload)
        environ.update(overrides)
        result = {}
        def start(status, headers):
            result.update(status=status, headers=dict(headers))
        result['body'] = json.loads(b''.join(app.application(environ, start)))
        return result

    def test_each_form_saves_with_server_utc_timestamp(self):
        for kind in app.TABLES:
            with self.subTest(kind=kind), patch.object(app, 'save', return_value='saved-id') as save:
                response = self.request(kind)
                self.assertEqual(response['status'], '201 Created')
                args = save.call_args.args
                self.assertEqual(args[0], kind)
                timestamp = datetime.fromisoformat(args[2])
                self.assertEqual(timestamp.utcoffset().total_seconds(), 0)
                self.assertLess(abs((datetime.now(timezone.utc) - timestamp).total_seconds()), 5)
                self.assertEqual(response['body']['submission_id'], 'saved-id')
                self.assertEqual(response['headers']['Cache-Control'], 'no-store')

    def test_failure_never_reports_success_or_leaks_details(self):
        with patch.object(app, 'save', side_effect=RuntimeError('private_key SECRET patient record')):
            response = self.request()
        self.assertEqual(response['status'], '503 Service Unavailable')
        self.assertNotIn('submission_id', response['body'])
        self.assertNotIn('SECRET', json.dumps(response))

    def test_invalid_input_never_reaches_snowflake(self):
        cases = [([], {}), ({}, {}), (dict(sample('patient'), email='not-an-email'), {}),
                 (dict(sample('patient'), message='x' * 10001), {}),
                 (dict(sample('patient'), submitted_at='yesterday'), {}),
                 (sample('patient'), {'HTTP_ORIGIN': 'https://unrelated.example'}),
                 (sample('patient'), {'CONTENT_TYPE': 'text/plain'}),
                 (sample('patient'), {'CONTENT_LENGTH': str(app.MAX_BODY + 1)})]
        with patch.object(app, 'save') as save:
            for body, overrides in cases:
                response = self.request(body=body, **overrides)
                self.assertNotEqual(response['status'], '201 Created')
            save.assert_not_called()

    def test_required_provider_fields_and_npi(self):
        data = sample('provider')
        for name in ('practice', 'ptype', 'pphone', 'pname', 'npi', 'email', 'message'):
            with self.subTest(field=name), self.assertRaises(app.InvalidSubmission):
                app.validate('provider', {k: v for k, v in data.items() if k != name})
        with self.assertRaises(app.InvalidSubmission):
            app.validate('provider', dict(data, npi='123'))

    def test_resume_size_type_and_base64_are_validated(self):
        for resume in ({'name': 'x.pdf', 'data': '!'},
                       {'name': 'x.html', 'data': base64.b64encode(b'<html>').decode()},
                       {'name': '../x.pdf', 'data': base64.b64encode(b'%PDF-1.4').decode()},
                       {'name': 'x.pdf', 'data': base64.b64encode(b'%PDF-' + b'x' * app.MAX_RESUME).decode()}):
            with self.subTest(name=resume['name']), self.assertRaises(app.InvalidSubmission):
                app.validate('career', dict(sample('career'), resume=resume))
        self.assertEqual(app.validate('career', sample('career'))['resume']['content_type'], 'application/pdf')

    def test_values_are_bound_and_tables_are_distinct(self):
        sqls = []
        for kind in app.TABLES:
            cursor = Mock()
            data = sample(kind)
            field = 'message' if kind != 'career' else 'note'
            attack = "Robert'); DROP TABLE users; --"
            data[field] = attack
            app.insert(cursor, kind, app.validate(kind, data), 'uuid', '2026-10-14T00:00:00+00:00')
            sql, values = cursor.execute.call_args.args
            self.assertIn(app.TABLES[kind], sql)
            self.assertIn('SUBMITTED_AT', sql)
            self.assertNotIn(attack, sql)
            self.assertIn(attack, values)
            self.assertEqual(sql.count('%s'), len(values))
            sqls.append(sql)
        self.assertEqual(len(set(sqls)), 3)

    def test_transaction_commits_only_after_insert(self):
        connection = Mock()
        with patch.object(app, 'connect', return_value=connection), patch.object(app, 'insert') as insert:
            app.save('patient', {}, '2026-10-14T00:00:00+00:00')
        connection.autocommit.assert_called_once_with(False)
        insert.assert_called_once()
        connection.commit.assert_called_once()
        connection.close.assert_called_once()
        connection.reset_mock()
        with patch.object(app, 'connect', return_value=connection), patch.object(app, 'insert', side_effect=RuntimeError):
            with self.assertRaises(RuntimeError):
                app.save('patient', {}, 'timestamp')
        connection.commit.assert_not_called()
        connection.close.assert_called_once()

    def test_rate_limit(self):
        with patch.object(app, 'save', return_value='saved'):
            for _ in range(10):
                self.assertEqual(self.request()['status'], '201 Created')
            self.assertEqual(self.request()['status'], '429 Too Many Requests')

    def test_form_tables_use_raw_production_website_schema(self):
        prefix = 'RAW_PRD.STRIKER_WEBSITE.'
        self.assertEqual(set(app.TABLES.values()), {
            prefix + 'WEBSITE_PROVIDER_REQUESTS',
            prefix + 'WEBSITE_PATIENT_QUESTIONS',
            prefix + 'WEBSITE_CAREER_APPLICATIONS',
        })
        schema = (ROOT / 'server/schema.sql').read_text()
        self.assertIn('CREATE SCHEMA IF NOT EXISTS RAW_PRD.STRIKER_WEBSITE', schema)
        self.assertNotIn('ANALYTICS_DEV', schema)

    def test_runtime_credential_is_not_used_for_schema_administration(self):
        install = (ROOT / 'server/install.sh').read_text()
        deploy = (ROOT / 'deploy/deploy-forms.py').read_text()
        self.assertNotIn('app.py" --init', install)
        self.assertIn("'user': 'STRIKERRX_WEBSITE_SVC'", deploy)
        self.assertIn("['snow', 'sql', '--connection', 'striker_admin'", deploy)

    def test_schema_is_additive_with_timestamp_per_table(self):
        schema = (ROOT / 'server/schema.sql').read_text()
        self.assertEqual(schema.count('CREATE TABLE IF NOT EXISTS'), 3)
        self.assertEqual(schema.count('SUBMITTED_AT TIMESTAMP_TZ NOT NULL DEFAULT CURRENT_TIMESTAMP()'), 3)
        self.assertNotIn('CREATE OR REPLACE', schema)
        self.assertNotIn('DROP ', schema)

    def test_policy_limits_secret_access(self):
        template = json.loads((ROOT / 'deploy/stack.json').read_text())
        policy = template['Resources']['FormSecretReadPolicy']['Properties']
        self.assertEqual(policy['Roles'], [{'Ref': 'Role'}])
        self.assertEqual(policy['PolicyDocument']['Statement'], [{
            'Effect': 'Allow', 'Action': 'secretsmanager:GetSecretValue',
            'Resource': {'Ref': 'FormSecretArn'}}])


if __name__ == '__main__':
    unittest.main()
