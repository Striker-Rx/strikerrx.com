#!/usr/bin/env python3
"""Install form capture on the existing EC2 server, with its key in Secrets Manager."""
import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import tarfile
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = '010133602250'
STACK = 'strikerrx-web'
SECRET = 'strikerrx/website/snowflake'


def add_secret_policy(template):
    template.setdefault('Parameters', {})['FormSecretArn'] = {'Type': 'String', 'Default': ''}
    template.setdefault('Conditions', {})['HasFormSecret'] = {
        'Fn::Not': [{'Fn::Equals': [{'Ref': 'FormSecretArn'}, '']}]}
    template['Resources']['FormSecretReadPolicy'] = {
        'Type': 'AWS::IAM::Policy', 'Condition': 'HasFormSecret',
        'Properties': {'PolicyName': 'ReadWebsiteSnowflakeSecret', 'Roles': [{'Ref': 'Role'}],
                       'PolicyDocument': {'Version': '2012-10-17', 'Statement': [{
                           'Effect': 'Allow', 'Action': 'secretsmanager:GetSecretValue',
                           'Resource': {'Ref': 'FormSecretArn'}}]}}}
    return template


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', default='strikerrx')
    parser.add_argument('--key', type=Path,
                        default=Path.home() / '.snowflake/keys/strikerrx_website_svc_key.p8')
    parser.add_argument('--prepare-only', action='store_true',
                        help='Prepare the secret, policy, tables, and localhost-only API while DNS/HTTPS is pending')
    args = parser.parse_args()

    def aws(*command):
        result = subprocess.run(['aws', *command, '--profile', args.profile, '--region', 'us-east-1',
                                 '--no-cli-pager', '--output', 'json'],
                                check=True, capture_output=True, text=True)
        return json.loads(result.stdout) if result.stdout.strip() else {}

    def remote(instance, command, timeout=1200):
        sent = aws('ssm', 'send-command', '--instance-ids', instance,
                   '--document-name', 'AWS-RunShellScript', '--parameters',
                   json.dumps({'commands': [command], 'executionTimeout': [str(timeout)]}))
        command_id = sent['Command']['CommandId']
        print('SSM command: ' + command_id, flush=True)
        for attempt in range(timeout // 5 + 12):
            time.sleep(5)
            results = aws('ssm', 'list-command-invocations', '--command-id', command_id,
                          '--details')['CommandInvocations']
            if not results or results[0]['Status'] in ('Pending', 'InProgress', 'Delayed'):
                if attempt and attempt % 12 == 0:
                    print('Server setup is still running…', flush=True)
                continue
            invocation = results[0]
            for plugin in invocation.get('CommandPlugins', []):
                print(plugin.get('Output', ''))
            if invocation['Status'] != 'Success':
                raise SystemExit('Remote setup failed: ' + invocation['Status'] +
                                 '. Inspect this SSM command for the full output: ' + command_id)
            return
        raise SystemExit('Remote status timed out; inspect SSM command ' + command_id)

    identity = aws('sts', 'get-caller-identity')
    if identity['Account'] != ACCOUNT:
        raise SystemExit('Wrong AWS account. Expected ' + ACCOUNT + '; no changes made.')
    stack = aws('cloudformation', 'describe-stacks', '--stack-name', STACK)['Stacks'][0]
    if stack['StackStatus'] not in ('CREATE_COMPLETE', 'UPDATE_COMPLETE'):
        raise SystemExit('Stack is not ready for an update: ' + stack['StackStatus'])
    outputs = {o['OutputKey']: o['OutputValue'] for o in stack['Outputs']}
    # Stop before creating the secret or installing the API if HTTPS is not ready.
    if not args.prepare_only:
        remote(outputs['InstanceId'], 'set -eu; '
               'test -s /etc/letsencrypt/live/strikerrx.com/fullchain.pem || '
               '{ echo "Complete DNS and deploy.py certificate before installing forms."; exit 1; }; '
               'curl -fsS --resolve strikerrx.com:443:127.0.0.1 https://strikerrx.com/ -o /dev/null', 60)
    if not args.key.is_file():
        raise SystemExit('Private key not found: ' + str(args.key))
    subprocess.run(['openssl', 'rsa', '-in', str(args.key), '-check', '-noout'],
                   check=True, capture_output=True)
    subprocess.run(['snow', 'sql', '--connection', 'striker_admin', '--filename',
                    str(ROOT / 'server/schema.sql')], check=True)
    secret_data = {'account': 'HDSBMSO-NNC01524', 'user': 'STRIKERRX_WEBSITE_SVC',
                   'warehouse': 'DEVELOPER_WH', 'private_key': args.key.read_text()}
    try:
        existing_secret = aws('secretsmanager', 'describe-secret', '--secret-id', SECRET)
    except subprocess.CalledProcessError as error:
        if 'ResourceNotFoundException' not in error.stderr:
            raise
        existing_secret = None
    # Only a mode-0600 temporary file contains the secret; never put it in args, output, or the archive.
    with tempfile.TemporaryDirectory(prefix='strikerrx-secret-') as temp:
        payload = Path(temp) / 'secret.json'
        with open(payload, 'x', opener=lambda path, flags: os.open(path, flags, 0o600)) as handle:
            json.dump(secret_data, handle)
        if existing_secret:
            arn = existing_secret['ARN']
            aws('secretsmanager', 'put-secret-value', '--secret-id', arn, '--secret-string', f'file://{payload}')
        else:
            created = aws('secretsmanager', 'create-secret', '--name', SECRET,
                          '--description', 'StrikerRx website Snowflake key-pair connection',
                          '--secret-string', f'file://{payload}', '--tags', 'Key=Project,Value=StrikerRx')
            arn = created['ARN']
    del secret_data
    print('Snowflake credentials stored in Secrets Manager (value not displayed).', flush=True)
    body = aws('cloudformation', 'get-template', '--stack-name', STACK)['TemplateBody']
    template = json.loads(body) if isinstance(body, str) else body
    if template.get('Resources', {}).get('Role', {}).get('Type') != 'AWS::IAM::Role':
        raise SystemExit('Expected EC2 Role resource is missing. Secret saved; no role changes made.')
    previous = json.dumps(template, sort_keys=True)
    add_secret_policy(template)
    current_parameters = {p['ParameterKey']: p.get('ParameterValue') for p in stack.get('Parameters', [])}
    if previous != json.dumps(template, sort_keys=True) or current_parameters.get('FormSecretArn') != arn:
        with tempfile.TemporaryDirectory(prefix='strikerrx-stack-') as temp:
            file = Path(temp) / 'stack.json'
            file.write_text(json.dumps(template))
            parameters = [{'ParameterKey': name, 'UsePreviousValue': True}
                          for name in current_parameters if name != 'FormSecretArn']
            parameters.append({'ParameterKey': 'FormSecretArn', 'ParameterValue': arn})
            aws('cloudformation', 'validate-template', '--template-body', f'file://{file}')
            change_set = 'forms-secret-' + str(time.time_ns())
            aws('cloudformation', 'create-change-set', '--stack-name', STACK,
                '--change-set-name', change_set, '--change-set-type', 'UPDATE',
                '--template-body', f'file://{file}', '--capabilities', 'CAPABILITY_IAM',
                '--parameters', json.dumps(parameters))
            aws('cloudformation', 'wait', 'change-set-create-complete', '--stack-name', STACK,
                '--change-set-name', change_set)
            changes = aws('cloudformation', 'describe-change-set', '--stack-name', STACK,
                          '--change-set-name', change_set)['Changes']
            for change in changes:
                resource = change.get('ResourceChange', {})
                if (resource.get('LogicalResourceId') != 'FormSecretReadPolicy' or
                        resource.get('Action') not in ('Add', 'Modify') or
                        resource.get('Replacement') not in (None, 'False')):
                    raise SystemExit('Unexpected infrastructure change; change set was NOT executed. '
                                     'Inspect ' + change_set + ' before proceeding.')
            print('Granting EC2 read access to this secret through CloudFormation…', flush=True)
            aws('cloudformation', 'execute-change-set', '--stack-name', STACK,
                '--change-set-name', change_set)
            aws('cloudformation', 'wait', 'stack-update-complete', '--stack-name', STACK)
    files = ['index.html', 'server/app.py', 'server/schema.sql', 'server/requirements.txt',
             'server/install.sh', 'server/strikerrx-forms.service', 'server/apache-forms.conf']
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode='w:gz') as tar:
        for name in files:
            tar.add(ROOT / name, arcname=name)
    blob = archive.getvalue()
    encoded = base64.b64encode(blob).decode()
    digest = hashlib.sha256(blob).hexdigest()
    release = '/opt/strikerrx-forms/releases/' + digest[:16]
    command = "set -eu\npython3 - <<'INSTALL_FILES'\n" + f'''
import base64, hashlib, io, pathlib, tarfile
data = base64.b64decode({encoded!r})
assert hashlib.sha256(data).hexdigest() == {digest!r}
destination = pathlib.Path({release!r})
destination.mkdir(parents=True, exist_ok=True)
with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
    archive.extractall(destination, filter='data')
INSTALL_FILES
export SNOWFLAKE_SECRET_ARN={shlex.quote(arn)}
export STRIKERRX_PREPARE_ONLY={'1' if args.prepare_only else '0'}
bash {shlex.quote(release + '/server/install.sh')}
'''
    if len(command.encode()) > 60000:
        raise SystemExit('Release exceeds the inline SSM limit; secret and policy saved, code not installed.')
    remote(outputs['InstanceId'], command)
    if args.prepare_only:
        print('Localhost-only API prepared. Rerun without --prepare-only after DNS/HTTPS is ready.')
        return
    print('Deployment complete: https://strikerrx.com/api/health')
    print('Tables: WEBSITE_PROVIDER_REQUESTS, WEBSITE_PATIENT_QUESTIONS, WEBSITE_CAREER_APPLICATIONS')
    print('All tables are in RAW_PRD.STRIKER_WEBSITE and capture SUBMITTED_AT in UTC.')


if __name__ == '__main__':
    try:
        main()
    except subprocess.CalledProcessError as error:
        # AWS calls that handle the secret reference a private file, never a literal value.
        message = error.stderr.decode() if isinstance(error.stderr, bytes) else error.stderr
        raise SystemExit(message.strip() if message else 'Deployment command failed.') from None
