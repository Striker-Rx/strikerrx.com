#!/usr/bin/env python3
"""Publish index.html to the existing StrikerRx server without replacing resources."""
import argparse
import base64
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', default='strikerrx')
    args = parser.parse_args()

    def aws(*command):
        result = subprocess.run(
            ['aws', *command, '--profile', args.profile, '--region', 'us-east-1',
             '--no-cli-pager', '--output', 'json'],
            check=True, capture_output=True, text=True)
        return json.loads(result.stdout) if result.stdout.strip() else {}

    if aws('sts', 'get-caller-identity')['Account'] != '010133602250':
        raise SystemExit('Wrong AWS account; expected 010133602250. No changes made.')
    stack = aws('cloudformation', 'describe-stacks', '--stack-name', 'strikerrx-web')['Stacks'][0]
    if stack['StackStatus'] not in ('CREATE_COMPLETE', 'UPDATE_COMPLETE'):
        raise SystemExit('Stack is not ready for a page update. No changes made.')
    outputs = {o['OutputKey']: o['OutputValue'] for o in stack['Outputs']}
    source = (Path(__file__).resolve().parents[1] / 'index.html').read_bytes()
    digest = hashlib.sha256(source).hexdigest()
    encoded = base64.b64encode(gzip.compress(source)).decode('ascii')
    command = 'set -eu\n'
    if b'/api/forms/' in source:
        command += ('curl --fail --silent --show-error --resolve strikerrx.com:443:127.0.0.1 '
                    'https://strikerrx.com/api/health > /dev/null\n')
    command += "python3 - <<'UPDATE_PAGE'\n" + f'''
import base64, gzip, hashlib, os, pathlib, shutil, time
page = pathlib.Path('/var/www/strikerrx/index.html')
if not page.is_file():
    raise SystemExit('Expected deployed page is missing; refusing update')
content = gzip.decompress(base64.b64decode({encoded!r}))
assert hashlib.sha256(content).hexdigest() == {digest!r}
backup = pathlib.Path('/var/backups/strikerrx')
backup.mkdir(mode=0o700, parents=True, exist_ok=True)
shutil.copy2(page, backup / ('index-' + str(time.time_ns()) + '.html'))
temporary = page.with_name('index.html.new')
temporary.write_bytes(content)
temporary.chmod(0o644)
os.replace(temporary, page)
print('Published index.html SHA256: ' + {digest!r})
UPDATE_PAGE
apache2ctl configtest
curl -fsS -o /dev/null -H 'Host: strikerrx.com' http://127.0.0.1/
'''
    if len(command.encode()) > 60000:
        raise SystemExit('Page exceeds the inline update size limit. No changes made.')
    sent = aws('ssm', 'send-command', '--instance-ids', outputs['InstanceId'],
               '--document-name', 'AWS-RunShellScript', '--parameters',
               json.dumps({'commands': [command], 'executionTimeout': ['120']}))
    command_id = sent['Command']['CommandId']
    print(f'Updating {outputs["InstanceId"]}; SSM command {command_id}', flush=True)
    for _ in range(36):
        time.sleep(5)
        invocations = aws('ssm', 'list-command-invocations', '--command-id', command_id,
                          '--details')['CommandInvocations']
        if not invocations or invocations[0]['Status'] in ('Pending', 'InProgress', 'Delayed'):
            continue
        invocation = invocations[0]
        for plugin in invocation.get('CommandPlugins', []):
            print(plugin.get('Output', ''))
        if invocation['Status'] != 'Success':
            raise SystemExit('Update did not complete successfully: ' + invocation['Status'])
        print('Page updated. Elastic IP: ' + outputs['PublicIp'])
        return
    raise SystemExit('Update status timed out; inspect SSM command ' + command_id)


if __name__ == '__main__':
    try:
        main()
    except subprocess.CalledProcessError as error:
        raise SystemExit(error.stderr.strip()) from None
