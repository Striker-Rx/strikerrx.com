#!/bin/bash
set -euo pipefail
# Invoked from an extracted release by deploy/deploy-forms.py. Only the ARN is passed.
: "${SNOWFLAKE_SECRET_ARN:?Missing secret ARN}"
release_dir="$(cd "$(dirname "$0")/.." && pwd)"
if [ "${STRIKERRX_PREPARE_ONLY:-0}" != 1 ]; then
  test -s /etc/letsencrypt/live/strikerrx.com/fullchain.pem || {
    echo 'HTTPS is not configured. Complete DNS and run deploy.py certificate first.' >&2
    exit 1
  }
fi
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y python3-venv
id strikerrx-forms >/dev/null 2>&1 || useradd --system --home-dir /var/lib/strikerrx-forms --shell /usr/sbin/nologin strikerrx-forms
install -d -o strikerrx-forms -g strikerrx-forms -m 700 /var/lib/strikerrx-forms
python3 -m venv /opt/strikerrx-forms/venv
/opt/strikerrx-forms/venv/bin/pip install --disable-pip-version-check -r "$release_dir/server/requirements.txt"
printf 'SNOWFLAKE_SECRET_ARN=%s\n' "$SNOWFLAKE_SECRET_ARN" > /etc/strikerrx-forms.env
chmod 640 /etc/strikerrx-forms.env
chown root:strikerrx-forms /etc/strikerrx-forms.env
# Schema is initialized by the admin CLI; prove all three inserts under the runtime role.
/opt/strikerrx-forms/venv/bin/python "$release_dir/server/app.py" --smoke
previous_release="$(readlink /opt/strikerrx-forms/current || true)"
ln -sfn "$release_dir" /opt/strikerrx-forms/current
install -m 644 "$release_dir/server/strikerrx-forms.service" /etc/systemd/system/strikerrx-forms.service
systemctl daemon-reload
systemctl enable strikerrx-forms.service
systemctl restart strikerrx-forms.service
api_ready=false
for attempt in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8001/api/health >/dev/null; then api_ready=true; break; fi
  sleep 1
done
if [ "$api_ready" != true ]; then
  if [ -n "$previous_release" ]; then
    ln -sfn "$previous_release" /opt/strikerrx-forms/current
    systemctl restart strikerrx-forms.service
  fi
  echo 'API failed to start. Frontend not published. Inspect journalctl -u strikerrx-forms.' >&2
  exit 1
fi
if [ "${STRIKERRX_PREPARE_ONLY:-0}" = 1 ]; then
  echo 'Secret, tables, and localhost-only API are ready. Public proxy and frontend are unchanged until HTTPS is ready.'
  exit 0
fi
install -m 644 "$release_dir/server/apache-forms.conf" /etc/apache2/conf-available/strikerrx-forms.conf
a2enmod proxy proxy_http headers
a2enconf strikerrx-forms
apache2ctl configtest
systemctl reload apache2
# Verify the public HTTPS virtual host and proxy locally, including certificate validation.
curl --fail --silent --show-error --resolve strikerrx.com:443:127.0.0.1 https://strikerrx.com/api/health
install -d -m 700 /var/backups/strikerrx
cp -p /var/www/strikerrx/index.html "/var/backups/strikerrx/index-before-forms-$(date +%s).html"
install -m 644 "$release_dir/index.html" /var/www/strikerrx/index.html.new
mv /var/www/strikerrx/index.html.new /var/www/strikerrx/index.html
echo 'Form API, tables, and updated website are installed. Test inserts were rolled back.'
