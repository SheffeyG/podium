import json

import httpx

from podium.sponsorblock import (
    SkipSegment,
    SponsorBlockClient,
    normalize_segments,
    segment_hash,
)


async def test_fetches_and_filters_skip_segments() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/skipSegments"
        assert request.url.params["videoID"] == "BV1ab411c7mD"
        assert request.url.params["cid"] == "1001"
        assert json.loads(request.url.params["categories"]) == ["sponsor", "intro"]
        assert request.headers["origin"].startswith("chrome-extension://")
        return httpx.Response(
            200,
            json=[
                {
                    "cid": "1001",
                    "category": "sponsor",
                    "actionType": "skip",
                    "segment": [10.0, 20.0],
                    "UUID": "one",
                },
                {
                    "cid": "other",
                    "category": "intro",
                    "actionType": "skip",
                    "segment": [0.0, 5.0],
                    "UUID": "wrong-cid",
                },
                {
                    "cid": "1001",
                    "category": "intro",
                    "actionType": "mute",
                    "segment": [0.0, 5.0],
                    "UUID": "wrong-action",
                },
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        segments = await SponsorBlockClient(
            http,
            "https://bsbsb.top",
            ("sponsor", "intro"),
        ).get_segments("BV1ab411c7mD", 1001)

    assert [(segment.start, segment.end) for segment in segments] == [(10.0, 20.0)]


def test_normalizes_segments_and_builds_stable_hash() -> None:
    segments = (
        SkipSegment(10.0, 20.0, "sponsor", "one"),
        SkipSegment(18.0, 30.0, "intro", "two"),
        SkipSegment(95.0, 120.0, "outro", "three"),
    )

    normalized = normalize_segments(segments, duration=100.0)

    assert normalized == ((10.0, 30.0), (95.0, 100.0))
    assert segment_hash("BV1ab411c7mD", 1001, normalized) == segment_hash(
        "BV1ab411c7mD", 1001, normalized
    )
