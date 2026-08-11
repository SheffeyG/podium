from __future__ import annotations

from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from bilibili import (
    BilibiliClient,
    BilibiliError,
    NoCompatibleAudioError,
    VideoNotFoundError,
)
from config import load_config
from media import MediaProxy
from rss import PodcastService


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config()
    timeout = httpx.Timeout(connect=10, read=None, write=30, pool=10)
    client = httpx.AsyncClient(timeout=timeout, follow_redirects=True)
    bilibili = BilibiliClient(
        client,
        sessdata=config.sessdata,
        cookie=config.bilibili_cookie,
    )

    app.state.config = config
    app.state.client = client
    app.state.bilibili = bilibili
    app.state.podcast = PodcastService(bilibili, config.base_url)
    app.state.media = MediaProxy(bilibili, client)
    try:
        yield
    finally:
        await client.aclose()


app = FastAPI(title="Podium", version="0.1.0", lifespan=lifespan)


@app.exception_handler(VideoNotFoundError)
async def video_not_found_handler(
    request: Request, exc: VideoNotFoundError
) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(NoCompatibleAudioError)
async def no_audio_handler(
    request: Request, exc: NoCompatibleAudioError
) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": str(exc)})


@app.exception_handler(BilibiliError)
async def bilibili_error_handler(
    request: Request, exc: BilibiliError
) -> JSONResponse:
    return JSONResponse(status_code=502, content={"detail": str(exc)})


@app.get("/health")
async def health(request: Request) -> dict[str, str | int]:
    return {
        "status": "ok",
        "feeds": len(request.app.state.config.feeds),
    }


@app.get("/feeds/{slug}.xml")
async def podcast_feed(slug: str, request: Request) -> Response:
    feed = request.app.state.config.feed_by_slug(slug)
    if feed is None:
        raise HTTPException(status_code=404, detail="feed not found")
    xml = await request.app.state.podcast.build_feed(feed)
    return Response(
        content=xml,
        media_type=None,
        headers={
            "Cache-Control": "public, max-age=300",
            "Content-Type": "application/rss+xml; charset=utf-8",
        },
    )


@app.api_route("/media/{bvid}/{cid}.m4a", methods=["GET", "HEAD"])
async def podcast_media(
    bvid: str,
    cid: int,
    request: Request,
) -> Response:
    try:
        return await request.app.state.media.handle(bvid, cid, request)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="invalid media identifier") from exc
