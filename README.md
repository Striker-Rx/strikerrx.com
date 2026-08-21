# strikerrx.com

Website design prototype for StrikerRx, published as a static site on GitHub Pages.

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
