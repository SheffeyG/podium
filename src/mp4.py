from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from typing import Iterable


class Mp4Error(ValueError):
    """Raised when an MP4 structure cannot be parsed or safely rewritten."""


@dataclass(frozen=True, slots=True)
class Mp4Box:
    type: str
    start: int
    size: int
    header_size: int

    @property
    def data_start(self) -> int:
        return self.start + self.header_size

    @property
    def end(self) -> int:
        return self.start + self.size


@dataclass(frozen=True, slots=True)
class SidxReference:
    reference_type: int
    referenced_size: int
    subsegment_duration: int
    starts_with_sap: bool
    sap_type: int
    sap_delta_time: int


@dataclass(frozen=True, slots=True)
class Sidx:
    box: Mp4Box
    version: int
    flags: int
    reference_id: int
    timescale: int
    earliest_presentation_time: int
    first_offset: int
    references: tuple[SidxReference, ...]


@dataclass(frozen=True, slots=True)
class ManifestFragment:
    source_index: int
    source_start: int
    size: int
    output_start: int
    duration: int
    new_decode_time: int


@dataclass(frozen=True, slots=True)
class VirtualMediaManifest:
    prefix: bytes
    fragments: tuple[ManifestFragment, ...]
    timescale: int
    output_length: int
    output_duration: float

    @property
    def output_duration_ticks(self) -> int:
        return sum(fragment.duration for fragment in self.fragments)


_UINT32_MAX = (1 << 32) - 1
_UINT64_MAX = (1 << 64) - 1
_SIDX_REFERENCE_SIZE_MAX = (1 << 31) - 1
_SAP_DELTA_MAX = (1 << 28) - 1


def _read_u32(data: bytes, offset: int, context: str) -> int:
    if offset < 0 or offset + 4 > len(data):
        raise Mp4Error(f"truncated {context}")
    return struct.unpack_from(">I", data, offset)[0]


def _read_u64(data: bytes, offset: int, context: str) -> int:
    if offset < 0 or offset + 8 > len(data):
        raise Mp4Error(f"truncated {context}")
    return struct.unpack_from(">Q", data, offset)[0]


def _parse_box(data: bytes, offset: int, limit: int) -> Mp4Box:
    if offset < 0 or limit > len(data) or offset >= limit:
        raise Mp4Error("invalid box bounds")
    if limit - offset < 8:
        raise Mp4Error(f"truncated box header at offset {offset}")

    size32 = _read_u32(data, offset, "box size")
    type_bytes = data[offset + 4 : offset + 8]
    try:
        box_type = type_bytes.decode("ascii")
    except UnicodeDecodeError as exc:
        raise Mp4Error(f"non-ASCII box type at offset {offset}") from exc

    header_size = 8
    if size32 == 1:
        if limit - offset < 16:
            raise Mp4Error(f"truncated large box header for {box_type}")
        size = _read_u64(data, offset + 8, f"{box_type} large size")
        header_size = 16
    elif size32 == 0:
        size = limit - offset
    else:
        size = size32

    if box_type == "uuid":
        header_size += 16
    if size < header_size:
        raise Mp4Error(f"invalid size {size} for {box_type} box")
    if size > limit - offset:
        raise Mp4Error(f"{box_type} box extends beyond its parent")

    return Mp4Box(box_type, offset, size, header_size)


def _parse_boxes(data: bytes, start: int, end: int) -> tuple[Mp4Box, ...]:
    if start < 0 or end < start or end > len(data):
        raise Mp4Error("invalid box range")

    boxes: list[Mp4Box] = []
    offset = start
    while offset < end:
        box = _parse_box(data, offset, end)
        boxes.append(box)
        offset = box.end
    if offset != end:
        raise Mp4Error("box range does not end on a box boundary")
    return tuple(boxes)


def parse_top_level_boxes(data: bytes) -> tuple[Mp4Box, ...]:
    """Parse every complete top-level box in *data*."""

    if not data:
        raise Mp4Error("MP4 data is empty")
    return _parse_boxes(data, 0, len(data))


def _single_box(boxes: Iterable[Mp4Box], box_type: str) -> Mp4Box:
    matches = [box for box in boxes if box.type == box_type]
    if not matches:
        raise Mp4Error(f"missing {box_type} box")
    if len(matches) > 1:
        raise Mp4Error(f"multiple {box_type} boxes are not supported")
    return matches[0]


def parse_sidx(data: bytes, box: Mp4Box | None = None) -> Sidx:
    """Parse an SIDX v0 or v1 box from top-level MP4 data."""

    if box is None:
        box = _single_box(parse_top_level_boxes(data), "sidx")
    if box.type != "sidx":
        raise Mp4Error(f"expected sidx box, got {box.type}")
    if box.end > len(data):
        raise Mp4Error("sidx box extends beyond input")

    offset = box.data_start
    if box.end - offset < 12:
        raise Mp4Error("truncated sidx full box header")
    version = data[offset]
    flags = int.from_bytes(data[offset + 1 : offset + 4], "big")
    if version not in (0, 1):
        raise Mp4Error(f"unsupported sidx version {version}")

    reference_id = _read_u32(data, offset + 4, "sidx reference ID")
    timescale = _read_u32(data, offset + 8, "sidx timescale")
    if timescale == 0:
        raise Mp4Error("sidx timescale must be greater than zero")
    offset += 12

    if version == 0:
        earliest_presentation_time = _read_u32(
            data, offset, "sidx earliest presentation time"
        )
        first_offset = _read_u32(data, offset + 4, "sidx first offset")
        offset += 8
    else:
        earliest_presentation_time = _read_u64(
            data, offset, "sidx earliest presentation time"
        )
        first_offset = _read_u64(data, offset + 8, "sidx first offset")
        offset += 16

    if offset + 4 > box.end:
        raise Mp4Error("truncated sidx reference count")
    reference_count = struct.unpack_from(">H", data, offset + 2)[0]
    offset += 4
    expected_end = offset + reference_count * 12
    if expected_end != box.end:
        if expected_end > box.end:
            raise Mp4Error("truncated sidx references")
        raise Mp4Error("unexpected trailing bytes in sidx box")

    references: list[SidxReference] = []
    for _ in range(reference_count):
        size_word, duration, sap_word = struct.unpack_from(">III", data, offset)
        references.append(
            SidxReference(
                reference_type=size_word >> 31,
                referenced_size=size_word & _SIDX_REFERENCE_SIZE_MAX,
                subsegment_duration=duration,
                starts_with_sap=bool(sap_word >> 31),
                sap_type=(sap_word >> 28) & 0x7,
                sap_delta_time=sap_word & _SAP_DELTA_MAX,
            )
        )
        offset += 12

    return Sidx(
        box=box,
        version=version,
        flags=flags,
        reference_id=reference_id,
        timescale=timescale,
        earliest_presentation_time=earliest_presentation_time,
        first_offset=first_offset,
        references=tuple(references),
    )


def _validate_uint(value: int, maximum: int, name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise Mp4Error(f"{name} must be an integer")
    if value < 0 or value > maximum:
        raise Mp4Error(f"{name} is out of range")


def rebuild_sidx(
    sidx: Sidx,
    references: Iterable[SidxReference],
    *,
    earliest_presentation_time: int = 0,
    first_offset: int = 0,
) -> bytes:
    """Build a valid SIDX box while preserving its identity and SAP metadata."""

    refs = tuple(references)
    if len(refs) > 0xFFFF:
        raise Mp4Error("sidx has too many references")
    value_max = _UINT32_MAX if sidx.version == 0 else _UINT64_MAX
    _validate_uint(earliest_presentation_time, value_max, "earliest presentation time")
    _validate_uint(first_offset, value_max, "first offset")
    _validate_uint(sidx.reference_id, _UINT32_MAX, "reference ID")
    _validate_uint(sidx.timescale, _UINT32_MAX, "timescale")
    if sidx.timescale == 0:
        raise Mp4Error("sidx timescale must be greater than zero")

    payload = bytearray()
    payload.extend(bytes((sidx.version,)))
    payload.extend(sidx.flags.to_bytes(3, "big"))
    payload.extend(struct.pack(">II", sidx.reference_id, sidx.timescale))
    value_format = ">II" if sidx.version == 0 else ">QQ"
    payload.extend(struct.pack(value_format, earliest_presentation_time, first_offset))
    payload.extend(struct.pack(">HH", 0, len(refs)))

    for reference in refs:
        _validate_uint(reference.reference_type, 1, "reference type")
        _validate_uint(
            reference.referenced_size, _SIDX_REFERENCE_SIZE_MAX, "reference size"
        )
        _validate_uint(reference.subsegment_duration, _UINT32_MAX, "reference duration")
        _validate_uint(reference.sap_type, 7, "SAP type")
        _validate_uint(reference.sap_delta_time, _SAP_DELTA_MAX, "SAP delta time")
        size_word = (reference.reference_type << 31) | reference.referenced_size
        sap_word = (
            (int(reference.starts_with_sap) << 31)
            | (reference.sap_type << 28)
            | reference.sap_delta_time
        )
        payload.extend(
            struct.pack(">III", size_word, reference.subsegment_duration, sap_word)
        )

    if sidx.box.header_size == 16:
        size = 16 + len(payload)
        return struct.pack(">I4sQ", 1, b"sidx", size) + payload
    size = 8 + len(payload)
    if size > _UINT32_MAX:
        raise Mp4Error("rebuilt sidx is too large")
    return struct.pack(">I4s", size, b"sidx") + payload


def _normalize_skip_ranges(
    skip_ranges: Iterable[tuple[float, float]],
) -> tuple[tuple[float, float], ...]:
    ranges: list[tuple[float, float]] = []
    for start, end in skip_ranges:
        if not math.isfinite(start) or not math.isfinite(end):
            raise Mp4Error("skip range values must be finite")
        if start < 0 or end <= start:
            raise Mp4Error(f"invalid skip range ({start}, {end})")
        ranges.append((start, end))

    ranges.sort()
    merged: list[tuple[float, float]] = []
    for start, end in ranges:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return tuple(merged)


def build_manifest(
    source_prefix: bytes,
    skip_ranges: Iterable[tuple[float, float]],
) -> VirtualMediaManifest:
    """Build a fragment-level virtual file map from an fMP4 init prefix.

    ``source_prefix`` must end exactly where the first SIDX reference starts.
    A reference is removed when its timeline midpoint is inside a skip range.
    """

    boxes = parse_top_level_boxes(source_prefix)
    ftyp = _single_box(boxes, "ftyp")
    moov = _single_box(boxes, "moov")
    sidx_box = _single_box(boxes, "sidx")
    if not (ftyp.start < moov.start < sidx_box.start):
        raise Mp4Error("expected ftyp, moov, and sidx boxes in that order")

    sidx = parse_sidx(source_prefix, sidx_box)
    source_media_start = sidx_box.end + sidx.first_offset
    if source_media_start != len(source_prefix):
        raise Mp4Error("source prefix must end at the first referenced fragment")
    if not sidx.references:
        raise Mp4Error("sidx contains no fragment references")
    if any(reference.reference_type != 0 for reference in sidx.references):
        raise Mp4Error("indirect sidx references are not supported")
    if any(reference.referenced_size == 0 for reference in sidx.references):
        raise Mp4Error("sidx fragment size must be greater than zero")
    if any(reference.subsegment_duration == 0 for reference in sidx.references):
        raise Mp4Error("sidx fragment duration must be greater than zero")

    normalized_ranges = _normalize_skip_ranges(skip_ranges)
    kept: list[tuple[int, int, SidxReference]] = []
    source_offset = source_media_start
    timeline_ticks = 0
    for index, reference in enumerate(sidx.references):
        midpoint_seconds = (
            timeline_ticks + reference.subsegment_duration / 2
        ) / sidx.timescale
        should_skip = any(
            start <= midpoint_seconds < end for start, end in normalized_ranges
        )
        if not should_skip:
            kept.append((index, source_offset, reference))
        source_offset += reference.referenced_size
        timeline_ticks += reference.subsegment_duration

    if not kept:
        raise Mp4Error("skip ranges remove every fragment")

    gap = source_prefix[sidx_box.end :]
    rebuilt = rebuild_sidx(
        sidx,
        (reference for _, _, reference in kept),
        earliest_presentation_time=0,
        first_offset=len(gap),
    )
    prefix = source_prefix[: sidx_box.start] + rebuilt + gap

    fragments: list[ManifestFragment] = []
    output_offset = len(prefix)
    decode_time = 0
    for source_index, source_start, reference in kept:
        fragments.append(
            ManifestFragment(
                source_index=source_index,
                source_start=source_start,
                size=reference.referenced_size,
                output_start=output_offset,
                duration=reference.subsegment_duration,
                new_decode_time=decode_time,
            )
        )
        output_offset += reference.referenced_size
        decode_time += reference.subsegment_duration

    return VirtualMediaManifest(
        prefix=prefix,
        fragments=tuple(fragments),
        timescale=sidx.timescale,
        output_length=output_offset,
        output_duration=decode_time / sidx.timescale,
    )


def _rescale(value: int, source_timescale: int, target_timescale: int) -> int:
    _validate_uint(value, _UINT64_MAX, "decode time")
    if source_timescale <= 0 or target_timescale <= 0:
        raise Mp4Error("timescales must be greater than zero")
    return (value * target_timescale + source_timescale // 2) // source_timescale


def patch_fragment(
    data: bytes,
    new_decode_time: int,
    new_sequence: int,
    *,
    source_timescale: int,
    target_timescale: int | None = None,
) -> bytes:
    """Patch ``mfhd`` and all ``tfdt`` boxes without changing box sizes.

    ``new_decode_time`` is expressed in ``source_timescale`` units. It is
    rescaled to ``target_timescale`` before being written to each ``tfdt``.
    """

    _validate_uint(new_sequence, _UINT32_MAX, "fragment sequence")
    target_timescale = target_timescale or source_timescale
    scaled_decode_time = _rescale(new_decode_time, source_timescale, target_timescale)

    top_level = parse_top_level_boxes(data)
    moof = _single_box(top_level, "moof")
    if not any(box.type == "mdat" for box in top_level):
        raise Mp4Error("fragment is missing mdat box")

    moof_children = _parse_boxes(data, moof.data_start, moof.end)
    mfhd = _single_box(moof_children, "mfhd")
    if mfhd.size != mfhd.header_size + 8:
        raise Mp4Error("invalid mfhd box size")
    if data[mfhd.data_start] != 0:
        raise Mp4Error("unsupported mfhd version")

    trafs = [box for box in moof_children if box.type == "traf"]
    if not trafs:
        raise Mp4Error("moof is missing traf box")

    tfdts: list[Mp4Box] = []
    for traf in trafs:
        tfdt = _single_box(_parse_boxes(data, traf.data_start, traf.end), "tfdt")
        version = data[tfdt.data_start]
        expected_size = tfdt.header_size + (8 if version == 0 else 12)
        if version not in (0, 1):
            raise Mp4Error(f"unsupported tfdt version {version}")
        if tfdt.size != expected_size:
            raise Mp4Error("invalid tfdt box size")
        if version == 0 and scaled_decode_time > _UINT32_MAX:
            raise Mp4Error("decode time does not fit tfdt version 0")
        if scaled_decode_time > _UINT64_MAX:
            raise Mp4Error("decode time does not fit tfdt version 1")
        tfdts.append(tfdt)

    patched = bytearray(data)
    struct.pack_into(">I", patched, mfhd.data_start + 4, new_sequence)
    for tfdt in tfdts:
        version = data[tfdt.data_start]
        if version == 0:
            struct.pack_into(">I", patched, tfdt.data_start + 4, scaled_decode_time)
        else:
            struct.pack_into(">Q", patched, tfdt.data_start + 4, scaled_decode_time)
    return bytes(patched)
