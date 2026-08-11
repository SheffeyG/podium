# Podium

Podium exposes video audio tracks as podcast RSS feeds. Video audio is streamed
from its source on demand; it is not downloaded, transcoded, or stored locally.

## Configure

Create the private runtime configuration from the tracked example, then add a
feed and account cookie as needed:

```bash
cp example.yaml config.yaml
```

`config.yaml` is ignored by Git; `example.yaml` is the credential-free
template that should be committed.

```yaml
base_url: https://podium.example.com

bilibili:
  cookie: null
  sessdata: null

feeds:
  - slug: talks
    title: Talks
    description: Selected video audio
    author: Podium
    language: zh-cn
    users:
      - uid: 193147738
        limit: 20
```

`users` adds the latest video submissions from a configured user homepage. A
user can be written as a numeric UID, `uid193147738`, a supported user-space
URL, or an object with a `limit` from 1 to 100. The default limit is 20. Every
feed must contain at least one user. `limit` is the target number of compatible
videos in the feed: Podium scans up to the latest 100 submissions and skips
videos that do not expose a standalone AAC audio track.

Feed updates are request-driven; Podium does not run a timer. Each RSS request
checks submissions from newest to oldest and stops as soon as it reaches a
known BV identifier. Known videos and stable episode metadata are persisted in
`data/podium.db`, so service restarts do not trigger another full scan. Set
`PODIUM_STATE_DB` to override the database path.

Video user-space APIs may be subject to platform risk control and generally
need an authenticated browser cookie. Export the complete cookie through the
environment instead of storing it in YAML:

```bash
export BILIBILI_COOKIE='SESSDATA=...; bili_jct=...; DedeUserID=...; buvid3=...; buvid4=...'
```

`BILIBILI_SESSDATA` remains supported for video requests that only need a
session. A full `BILIBILI_COOKIE` takes precedence. Do not provide an account
password, commit cookies, put them in feed URLs, or expose them in logs.

## Run

```bash
uv sync
uv run uvicorn --app-dir src main:app --host 0.0.0.0 --port 8000
```

Endpoints:

- `GET /feeds/{slug}.xml` returns a podcast RSS feed.
- `GET|HEAD /media/{bvid}/{cid}.m4a` proxies the selected AAC stream.
- `GET /health` reports process and configuration status.

For a custom config path, set `PODIUM_CONFIG=/path/to/config.yaml`.

## Test

```bash
uv run pytest
```

The video-platform web APIs used by this project may change without notice.
Only expose video audio you have permission to access and redistribute.
