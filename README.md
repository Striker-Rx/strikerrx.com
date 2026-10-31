# strikerrx.com

Website design prototype for StrikerRx, published as a static site on GitHub Pages.

## EC2 form capture

The EC2 deployment targets account `010133602250`, region `us-east-1`, and stack
`strikerrx-web`. Homepage placeholder testimonials are temporarily disabled in
`index.html`; restore the enclosing conditional only after replacing them with approved quotes.

Provider, patient, and careers forms now POST to a same-origin `/api/forms/` endpoint.
Success appears only after a database commit. These forms require the EC2 backend;
GitHub Pages alone does not provide submission storage. The backend uses these tables
in Snowflake account `HDSBMSO-NNC01524`, database `RAW_PRD`, schema `STRIKER_WEBSITE`:

| Form | Table |
| --- | --- |
| Provider request | `WEBSITE_PROVIDER_REQUESTS` |
| Patient question | `WEBSITE_PATIENT_QUESTIONS` |
| Career application | `WEBSITE_CAREER_APPLICATIONS` |

Each row has a generated `SUBMISSION_ID` and a server-recorded UTC `SUBMITTED_AT`
(`TIMESTAMP_TZ`), plus the individual form fields. Careers include the selected role,
note, and résumé filename, content type, and binary contents (PDF/DOC/DOCX, maximum 2 MB).
Provider categories are stored as an array. The separate `strikerrx-snowflake-email` project sends new-row notifications through SMTP2GO using Snowflake streams, a queue, and a triggered task.

To install on the existing server, first complete DNS and the Let's Encrypt certificate,
then run from a terminal with AWS access:

```sh
python3 deploy/deploy-forms.py --profile strikerrx
```

The installer verifies the account and existing HTTPS site, initializes the schema through the separate `striker_admin` Snowflake CLI connection, and stores the dedicated service key from `~/.snowflake/keys/strikerrx_website_svc_key.p8` in Secrets Manager as `strikerrx/website/snowflake`. It adds `secretsmanager:GetSecretValue` for that exact secret ARN to the EC2 role through CloudFormation. It inspects a change set and refuses any unrelated infrastructure change. Secret contents are never included in the deployment archive, repository, frontend, or SSM command. Only a temporary owner-readable local file is used for secret upload.

The backend runs as `strikerrx-forms` behind Apache on `127.0.0.1:8001`. It retrieves the key through the EC2 instance role and caches it in memory for five minutes. There is no EC2 private-key file. The runtime authenticates as `STRIKERRX_WEBSITE_SVC` with only the `STRIKERRX_WEBSITE_WRITER` role. That role has `USAGE` on `RAW_PRD`, `STRIKER_WEBSITE`, and `DEVELOPER_WH`, plus `INSERT` on the three intake tables. It has no `SELECT` privilege and no access to other schemas or databases. Secondary roles are disabled.

Installation preserves existing tables and rows, tests all three inserts under the
runtime role in a rolled-back transaction, verifies the HTTPS proxy, and only then
publishes the new frontend. The old page is backed up under `/var/backups/strikerrx`.
The script is safe to rerun after a partial installation. If schema creation fails,
check that the Snowflake user can assume `ACCOUNTADMIN` for the one-time setup.

To finish HTTPS if it has not already been configured:

```sh
python3 deploy/deploy.py certificate --profile strikerrx
```

For subsequent page-only changes after the backend is installed:

```sh
python3 deploy/update-page.py --profile strikerrx
```

API health: `https://strikerrx.com/api/health` verifies that the process and proxy are
running; it does not query Snowflake. Database smoke check on EC2:

```sh
sudo sh -c 'set -a; . /etc/strikerrx-forms.env; set +a; /opt/strikerrx-forms/venv/bin/python /opt/strikerrx-forms/current/server/app.py --smoke'
sudo journalctl -u strikerrx-forms
```

The public API validates required fields, email/NPI formats, payload size, résumé type,
and origin. It binds SQL values, logs no submission payloads, and limits each IP to ten
attempts per ten minutes. This in-memory limit assumes the configured single Gunicorn
worker; it resets on process restart. Submission writes are synchronous; failed writes
show an error and preserve the entered form. A retry after an ambiguous network failure
can create a duplicate row, so use IDs/timestamps when reviewing submissions.

Run local tests without cloud credentials:

```sh
python3 -m unittest discover -s tests -p 'test_*.py' -v
node --test tests/forms.test.cjs
```

While DNS is pending, `python3 deploy/deploy-forms.py --prepare-only` prepares the
secret, EC2 policy, tables, and API bound only to localhost. It does not publish the
new frontend or enable the Apache API proxy. Run again without that flag once HTTPS
is ready to complete activation.

## Source

This site is a Claude Design canvas exported to a static page.

- **Design project:** `a8c61411-072c-4a1f-ba92-5fac0a71f6a8`
- **Source file:** `StrikerRx Site.dc.html` (published here as `index.html`)

## What is in here

| Path | Purpose |
| --- | --- |
| `index.html` | The page. A `.dc.html` template — markup inside `<x-dc>`, page logic in the trailing `<script type="text/x-dc">` block. |
| `support.js` | Claude Design (`dc-runtime`) template engine. Renders `<x-dc>` into `#dc-root` via React. Unmodified. |
| `image-slot.js` | The `<image-slot>` custom element used for the photo placeholders. Unmodified. |
| `vendor/` | React + ReactDOM 18.3.1 UMD builds. |
| `assets/` | Logos and the mark watermark. |
| `assets/photos/` | Site photography (~500 KB), served from this repo. |
| `.nojekyll` | Tells Pages to serve files as-is instead of running Jekyll. |

## How it renders

`support.js` reads the `<x-dc>` template plus the `text/x-dc` logic script, evaluates the
`{{ ... }}` bindings and `<sc-for>` / `<sc-if>` directives, and mounts the result with React.
The whole site is one file: navigation between Home / About / What We Compound / Partner With
Us / Insights / Careers is component state, not separate pages or URLs.

Because of that, there are **no per-page URLs and no deep links** — every visitor lands on Home
and clicks through. There is also no server-side HTML, so crawlers that do not execute
JavaScript see an empty page. Both are properties of the design prototype, not bugs.

## Divergence from the design source

`index.html` is a byte-for-byte copy of `StrikerRx Site.dc.html` except for the `<head>`, which
gains three lines:

- a `<title>`, so the browser tab is not just the URL
- local `<script>` tags for React and ReactDOM

`support.js` normally pulls React from `unpkg.com` at runtime. It skips that fetch when
`window.React` and `window.ReactDOM` already exist, so loading the vendored copies first keeps
the page working on a network that cannot reach unpkg. The vendored files were checked against
the SRI hashes `support.js` expects.

To re-sync after a design change, re-export the `.dc.html` and re-apply those `<head>` lines.

## Known console noise

Two 404s appear in devtools and are harmless:

- `.image-slots.state.json` — sidecar the design editor uses to persist dropped images. It does
  not exist outside the editor, and the slots fall back to their `src`.
- `{{ p.img }}` — `image-slot.js` upgrades the `<image-slot>` elements in the raw, unrendered
  template before React replaces it, so one literal binding gets requested as a URL. The
  rendered Insights cards use the real image URLs.

## Photography

All five photos are stored in `assets/photos/` and served from this repo, so nothing depends on
`strikerrx.com` staying up or keeping its current upload paths. They were pulled once from
`https://strikerrx.com/wp-content/uploads/...` and keep their original filenames:

| File | Used on |
| --- | --- |
| `Home-Header-02.jpg` | Home hero (Direction A and B) and the mobile hero |
| `GLP-01-Mock-Up-03.jpg` | About, and the third Insights card |
| `The-Gold-Standard-in-Patient-Safety-Striker-blog-1024x536.jpg` | First Insights card |
| `Striker-Pharma-Compound-Pharmacy-vs-1-1024x536.jpg` | Second Insights card |
| `Striker-Attending-Conference-Web-Art-fb-1024x536.jpg` | Careers |

They are the full-size originals, not resized for the slots they fill — worth optimising if page
weight ever matters. If a photo is updated on the live WordPress site, it will **not** update
here; re-download it.

The only remaining external request is the Archivo webfont from Google Fonts.

## Local preview

Needs a real HTTP server — `support.js` fetches the page source, which `file://` blocks.

```sh
python3 -m http.server 8000
# then open http://localhost:8000/
```
