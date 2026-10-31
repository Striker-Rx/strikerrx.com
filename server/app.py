"""Same-origin WSGI form endpoint. Credentials are fetched using the EC2 role."""
import base64
import binascii
from collections import deque
from contextlib import closing
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import threading
import time
import uuid

TABLES = {
    'provider': 'RAW_PRD.STRIKER_WEBSITE.WEBSITE_PROVIDER_REQUESTS',
    'patient': 'RAW_PRD.STRIKER_WEBSITE.WEBSITE_PATIENT_QUESTIONS',
    'career': 'RAW_PRD.STRIKER_WEBSITE.WEBSITE_CAREER_APPLICATIONS',
}
FIELDS = {
    'provider': [('practice', 'PRACTICE_NAME', 300, True), ('ptype', 'PRACTICE_TYPE', 100, True),
                 ('pphone', 'PRACTICE_PHONE', 60, True), ('paddress', 'PRACTICE_ADDRESS', 1000, False),
                 ('pname', 'PRESCRIBER_NAME', 300, True), ('creds', 'CREDENTIALS', 100, False),
                 ('npi', 'NPI', 10, True), ('license', 'LICENSE_NUMBER', 100, False),
                 ('email', 'EMAIL', 254, True), ('phone', 'PHONE', 60, False),
                 ('message', 'MESSAGE', 10000, True), ('source', 'REFERRAL_SOURCE', 1000, False)],
    'patient': [('name', 'NAME', 300, True), ('email', 'EMAIL', 254, True),
                ('phone', 'PHONE', 60, False), ('provider', 'PROVIDER_NAME', 300, False),
                ('message', 'MESSAGE', 10000, True)],
    'career': [('role', 'ROLE_TITLE', 300, True), ('name', 'NAME', 300, True),
               ('email', 'EMAIL', 254, True), ('phone', 'PHONE', 60, False),
               ('note', 'NOTE', 10000, False)],
}
MAX_RESUME = 2 * 1024 * 1024
MAX_BODY = 3 * 1024 * 1024
_secret = None
_secret_expires = 0
_secret_lock = threading.Lock()
_requests = {}
_rate_lock = threading.Lock()


class InvalidSubmission(ValueError):
    pass


def validate(kind, data):
    if not isinstance(data, dict):
        raise InvalidSubmission('Please submit a valid form.')
    allowed = {field[0] for field in FIELDS[kind]} | ({'categories'} if kind == 'provider' else set())
    if kind == 'career':
        allowed.add('resume')
    if set(data) - allowed:
        raise InvalidSubmission('The form contains unsupported fields. Please refresh and try again.')
    cleaned = {}
    for name, _, limit, required in FIELDS[kind]:
        value = data.get(name, '')
        if not isinstance(value, str) or len(value) > limit or '\x00' in value:
            raise InvalidSubmission(f'Please check the {name} field (maximum {limit} characters).')
        value = value.strip()
        if required and not value:
            raise InvalidSubmission('Please complete all required fields.')
        cleaned[name] = value
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', cleaned['email']):
        raise InvalidSubmission('Please enter a valid email address.')
    if kind == 'provider':
        if not re.fullmatch(r'\d{10}', cleaned['npi']):
            raise InvalidSubmission('Please enter a 10-digit NPI number.')
        categories = data.get('categories', [])
        if (not isinstance(categories, list) or len(categories) > 20 or
                any(not isinstance(v, str) or len(v) > 100 for v in categories)):
            raise InvalidSubmission('Please check the selected categories.')
        cleaned['categories'] = categories
    if kind == 'career':
        resume = data.get('resume')
        if not isinstance(resume, dict) or set(resume) != {'name', 'data'}:
            raise InvalidSubmission('Please attach your résumé (PDF, DOC, or DOCX; up to 2 MB).')
        name, encoded = resume['name'], resume['data']
        if (not isinstance(name, str) or not name or len(name) > 255 or
                '/' in name or '\\' in name or '\x00' in name or not isinstance(encoded, str) or
                len(encoded) > 4 * ((MAX_RESUME + 2) // 3)):
            raise InvalidSubmission('Please attach a résumé up to 2 MB with a valid filename.')
        try:
            contents = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise InvalidSubmission('The résumé could not be read. Please attach it again.') from None
        types = {
            '.pdf': (b'%PDF-', 'application/pdf'),
            '.doc': (bytes.fromhex('D0CF11E0A1B11AE1'), 'application/msword'),
            '.docx': (b'PK\x03\x04', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
        }
        signature, content_type = types.get(Path(name).suffix.lower(), (b'', ''))
        if not signature or not contents.startswith(signature) or len(contents) > MAX_RESUME:
            raise InvalidSubmission('Please attach a PDF, DOC, or DOCX résumé up to 2 MB.')
        cleaned['resume'] = {'name': name, 'data': encoded, 'content_type': content_type}
    return cleaned


def credentials():
    global _secret, _secret_expires
    import boto3
    from botocore.config import Config
    with _secret_lock:
        if _secret is None or time.monotonic() >= _secret_expires:
            client = boto3.client('secretsmanager', region_name='us-east-1',
                                  config=Config(connect_timeout=5, read_timeout=5,
                                                retries={'max_attempts': 2}))
            result = client.get_secret_value(SecretId=os.environ['SNOWFLAKE_SECRET_ARN'])
            _secret = json.loads(result['SecretString'])
            _secret_expires = time.monotonic() + 300
        return dict(_secret)


def connect(admin=False):
    import snowflake.connector
    from cryptography.hazmat.primitives import serialization
    secret = credentials()
    key = serialization.load_pem_private_key(secret['private_key'].encode(), password=None)
    der = key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    params = dict(account=secret['account'], user=secret['user'], warehouse=secret['warehouse'],
                  database='RAW_PRD', schema='STRIKER_WEBSITE', authenticator='SNOWFLAKE_JWT',
                  private_key=der, login_timeout=15, network_timeout=20, socket_timeout=15,
                  session_parameters={'TIMEZONE': 'UTC', 'STATEMENT_TIMEOUT_IN_SECONDS': 30,
                                      'QUERY_TAG': 'strikerrx-website-forms'})
    params['role'] = 'ACCOUNTADMIN' if admin else 'STRIKERRX_WEBSITE_WRITER'
    connection = snowflake.connector.connect(**params)
    if not admin:
        try:
            with closing(connection.cursor()) as cursor:
                cursor.execute('USE SECONDARY ROLES NONE')
        except Exception:
            connection.close()
            raise
    return connection


def insert(cursor, kind, data, submission_id, submitted_at):
    # Table/column identifiers come only from the allowlists above; all values are bound.
    columns = ['SUBMISSION_ID', 'SUBMITTED_AT'] + [field[1] for field in FIELDS[kind]]
    expressions = ['%s', 'TO_TIMESTAMP_TZ(%s)'] + ['%s'] * len(FIELDS[kind])
    values = [submission_id, submitted_at] + [data[field[0]] for field in FIELDS[kind]]
    if kind == 'provider':
        columns.append('CATEGORIES')
        expressions.append('PARSE_JSON(%s)')
        values.append(json.dumps(data['categories']))
    if kind == 'career':
        columns.extend(['RESUME_FILENAME', 'RESUME_CONTENT_TYPE', 'RESUME_CONTENT'])
        expressions.extend(['%s', '%s', "TO_BINARY(%s, 'BASE64')"])
        values.extend([data['resume']['name'], data['resume']['content_type'], data['resume']['data']])
    cursor.execute(f'INSERT INTO {TABLES[kind]} ({", ".join(columns)}) '
                   f'SELECT {", ".join(expressions)}', tuple(values))


def save(kind, data, submitted_at):
    submission_id = str(uuid.uuid4())
    with closing(connect()) as connection:
        connection.autocommit(False)
        with closing(connection.cursor()) as cursor:
            insert(cursor, kind, data, submission_id, submitted_at)
        connection.commit()
    return submission_id


def rate_allowed(address):
    now = time.monotonic()
    with _rate_lock:
        # One Gunicorn worker with threads keeps this per-IP limiter shared and bounded.
        for key in list(_requests):
            if not _requests[key] or _requests[key][-1] < now - 600:
                del _requests[key]
        if address not in _requests and len(_requests) >= 10000:
            return False
        hits = _requests.setdefault(address, deque())
        while hits and hits[0] < now - 600:
            hits.popleft()
        if len(hits) >= 10:
            return False
        hits.append(now)
        return True


def application(environ, start_response):
    def respond(status, payload):
        body = json.dumps(payload).encode()
        start_response(status, [('Content-Type', 'application/json'), ('Cache-Control', 'no-store'),
                                ('X-Content-Type-Options', 'nosniff'), ('Content-Length', str(len(body)))])
        return [body]

    path = environ.get('PATH_INFO', '')
    if path == '/api/health' and environ.get('REQUEST_METHOD') == 'GET':
        return respond('200 OK', {'ok': True})
    kind = path.removeprefix('/api/forms/') if path.startswith('/api/forms/') else ''
    if kind not in TABLES:
        return respond('404 Not Found', {'error': 'Not found.'})
    if environ.get('REQUEST_METHOD') != 'POST':
        return respond('405 Method Not Allowed', {'error': 'Use POST.'})
    if environ.get('HTTP_ORIGIN') != 'https://strikerrx.com':
        return respond('403 Forbidden', {'error': 'Please submit the form from https://strikerrx.com.'})
    if environ.get('CONTENT_TYPE', '').split(';')[0].strip() != 'application/json':
        return respond('415 Unsupported Media Type', {'error': 'Expected a JSON form submission.'})
    try:
        length = int(environ.get('CONTENT_LENGTH', '0'))
    except ValueError:
        length = 0
    if length <= 0 or length > MAX_BODY:
        return respond('413 Content Too Large', {'error': 'The submission is too large or empty.'})
    # Apache strips client-supplied X-Forwarded-For, then adds the real remote address.
    address = environ.get('REMOTE_ADDR', '')
    if address in ('127.0.0.1', '::1'):
        address = environ.get('HTTP_X_FORWARDED_FOR', address).split(',')[-1].strip()
    if not rate_allowed(address):
        return respond('429 Too Many Requests', {'error': 'Please wait ten minutes before trying again.'})
    submitted_at = datetime.now(timezone.utc).isoformat()
    try:
        data = validate(kind, json.loads(environ['wsgi.input'].read(length)))
    except (InvalidSubmission, ValueError, UnicodeDecodeError) as error:
        message = str(error) if isinstance(error, InvalidSubmission) else 'Please submit a valid form.'
        return respond('400 Bad Request', {'error': message})
    try:
        submission_id = save(kind, data, submitted_at)
    except Exception:
        # Do not log form values, key material, or connector exceptions containing SQL.
        logging.error('Snowflake form insert failed (%s)', kind)
        return respond('503 Service Unavailable', {'error': 'We could not save your submission. '
                       'Please try again or call 832.437.8624.'})
    return respond('201 Created', {'ok': True, 'submission_id': submission_id, 'submitted_at': submitted_at})


def initialize():
    with closing(connect(admin=True)) as connection, closing(connection.cursor()) as cursor:
        for statement in Path(__file__).with_name('schema.sql').read_text().split(';'):
            if statement.strip():
                cursor.execute(statement)
        for table in TABLES.values():
            cursor.execute(f'SELECT SUBMISSION_ID, SUBMITTED_AT FROM {table} LIMIT 0')
    print('Three form tables and the insert-only runtime role are ready.')


def smoke():
    samples = {
        'provider': dict(practice='Deployment check', ptype='Other', pphone='0000000000',
                         pname='Deployment check', npi='0000000000', email='test@example.invalid',
                         message='Synthetic deployment check'),
        'patient': dict(name='Deployment check', email='test@example.invalid', message='Synthetic deployment check'),
        'career': dict(role='Deployment check', name='Deployment check', email='test@example.invalid',
                       resume={'name': 'check.pdf', 'data': base64.b64encode(b'%PDF-1.4\ncheck').decode()}),
    }
    with closing(connect()) as connection:
        connection.autocommit(False)
        try:
            with closing(connection.cursor()) as cursor:
                for kind, sample in samples.items():
                    insert(cursor, kind, validate(kind, sample), str(uuid.uuid4()),
                           datetime.now(timezone.utc).isoformat())
        finally:
            connection.rollback()
    print('Runtime role inserted all three form types successfully; synthetic rows rolled back.')


if __name__ == '__main__':
    import sys
    try:
        if sys.argv[1:] == ['--init']:
            initialize()
        elif sys.argv[1:] == ['--smoke']:
            smoke()
        else:
            raise SystemExit('Use --init or --smoke; serve with gunicorn app:application.')
    except Exception as error:
        raise SystemExit('Snowflake setup failed: ' + type(error).__name__ +
                         ' (code ' + str(getattr(error, 'errno', 'unknown')) +
                         '). Check key registration, network access, and Snowflake role permissions.') from None
