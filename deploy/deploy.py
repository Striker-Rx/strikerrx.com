#!/usr/bin/env python3
"""Initial EC2 deployment, followed by certificate issuance after DNS cutover."""
import argparse
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import tarfile
import tempfile
import time

ACCOUNT = '010133602250'
REGION = 'us-east-1'
STACK = 'strikerrx-web'
ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['provision', 'certificate'])
    parser.add_argument('--profile', required=True)
    parser.add_argument('--email', default='strikerrx-aws@strikerrx.com',
                        help='Certificate registration email')
    args = parser.parse_args()
    if args.action == 'certificate' and not args.email:
        parser.error('--email is required for certificate issuance')

    def aws(*arguments):
        result = subprocess.run(
            ['aws', '--profile', args.profile, '--region', REGION,
             '--no-cli-pager', '--output', 'json', *arguments],
            check=True, text=True, stdout=subprocess.PIPE)
        return json.loads(result.stdout) if result.stdout.strip() else {}

    identity = aws('sts', 'get-caller-identity')
    if identity['Account'] != ACCOUNT:
        raise SystemExit(f"Refusing deployment: expected {ACCOUNT}, got {identity['Account']}")

    def outputs():
        stack = aws('cloudformation', 'describe-stacks', '--stack-name', STACK)['Stacks'][0]
        return {item['OutputKey']: item['OutputValue'] for item in stack['Outputs']}

    def remote(instance, command):
        response = aws('ssm', 'send-command', '--instance-ids', instance,
                       '--document-name', 'AWS-RunShellScript', '--parameters',
                       json.dumps({'commands': [command], 'executionTimeout': ['900']}))
        command_id = response['Command']['CommandId']
        for _ in range(180):
            time.sleep(5)
            invocations = aws('ssm', 'list-command-invocations', '--command-id',
                              command_id, '--details')['CommandInvocations']
            if not invocations or invocations[0]['Status'] in ('Pending', 'InProgress', 'Delayed'):
                continue
            invocation = invocations[0]
            for plugin in invocation.get('CommandPlugins', []):
                print(plugin.get('Output', ''))
            if invocation['Status'] != 'Success':
                raise SystemExit(f"Remote command failed: {invocation['Status']} ({command_id})")
            return
        raise SystemExit(f'SSM command timed out; inspect {command_id}')

    if args.action == 'provision':
        stacks = aws('cloudformation', 'list-stacks')['StackSummaries']
        if any(s['StackName'] == STACK and s['StackStatus'] != 'DELETE_COMPLETE' for s in stacks):
            raise SystemExit('Stack already exists. Inspect it before changing or redeploying the site.')
        bucket = f'strikerrx-artifacts-{ACCOUNT}-{REGION}'
        buckets = aws('s3api', 'list-buckets')['Buckets']
        if not any(b['Name'] == bucket for b in buckets):
            aws('s3api', 'create-bucket', '--bucket', bucket)
        aws('s3api', 'put-public-access-block', '--bucket', bucket,
            '--public-access-block-configuration', json.dumps({
                'BlockPublicAcls': True, 'IgnorePublicAcls': True,
                'BlockPublicPolicy': True, 'RestrictPublicBuckets': True}))
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / 'site.tar.gz'
            with tarfile.open(archive, 'w:gz') as tar:
                for name in ('index.html', 'support.js', 'image-slot.js', 'assets', 'vendor'):
                    tar.add(ROOT / name, arcname=name)
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            key = f'releases/{digest}.tar.gz'
            aws('s3api', 'put-object', '--bucket', bucket, '--key', key,
                '--body', str(archive), '--server-side-encryption', 'AES256')
        aws('cloudformation', 'validate-template', '--template-body',
            f'file://{ROOT / "deploy/stack.json"}')
        aws('cloudformation', 'create-stack', '--stack-name', STACK,
            '--template-body', f'file://{ROOT / "deploy/stack.json"}',
            '--capabilities', 'CAPABILITY_IAM', '--parameters', json.dumps([
                {'ParameterKey': 'ArtifactBucket', 'ParameterValue': bucket},
                {'ParameterKey': 'ArtifactKey', 'ParameterValue': key}]),
            '--tags', 'Key=Project,Value=StrikerRx')
        aws('cloudformation', 'wait', 'stack-create-complete', '--stack-name', STACK)
        result = outputs()
        for _ in range(120):
            info = aws('ssm', 'describe-instance-information', '--filters',
                       json.dumps([{'Key': 'InstanceIds', 'Values': [result['InstanceId']]}]))
            if any(i['PingStatus'] == 'Online' for i in info['InstanceInformationList']):
                break
            time.sleep(5)
        else:
            raise SystemExit('Server created, but SSM is not online; inspect EC2 bootstrap logs.')
        remote(result['InstanceId'], 'set -eu; cloud-init status --wait; '
               'test -f /var/lib/strikerrx-ready; apache2ctl configtest; '
               'curl -fsS -o /dev/null -H "Host: strikerrx.com" http://127.0.0.1/')
        print(json.dumps(result, indent=2))
        print('Set strikerrx.com A record to PublicIp; remove stale AAAA records. '
              'Then run the certificate action. Existing DNS records have not been changed.')
    else:
        result = outputs()
        # Run only after DNS resolves directly to this Elastic IP, with port 80 reachable.
        remote(result['InstanceId'], 'set -eu; test -f /var/lib/strikerrx-ready; '
               'certbot --apache --non-interactive --agree-tos --redirect '
               f'--email {shlex.quote(args.email)} -d strikerrx.com; '
               'apache2ctl configtest; certbot renew --dry-run')
        print('Certificate installed and renewal dry-run passed. Verify https://strikerrx.com externally.')


if __name__ == '__main__':
    main()
