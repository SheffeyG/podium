import struct

import pytest

from mp4 import (
    Mp4Error,
    build_manifest,
    parse_sidx,
    parse_top_level_boxes,
    patch_fragment,
)


def box(box_type: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), box_type) + payload


def full_box(box_type: bytes, version: int, payload: bytes, flags: int = 0) -> bytes:
    return box(box_type, bytes((version,)) + flags.to_bytes(3, "big") + payload)


def make_sidx(
    version: int,
    references: tuple[tuple[int, int, int, bool, int, int], ...],
    *,
    timescale: int = 1000,
    earliest: int = 0,
    first_offset: int = 0,
) -> bytes:
    payload = bytearray(struct.pack(">II", 7, timescale))
    if version == 0:
        payload.extend(struct.pack(">II", earliest, first_offset))
    else:
        payload.extend(struct.pack(">QQ", earliest, first_offset))
    payload.extend(struct.pack(">HH", 0, len(references)))
    for (
        reference_type,
        size,
        duration,
        starts_with_sap,
        sap_type,
        sap_delta,
    ) in references:
        size_word = (reference_type << 31) | size
        sap_word = (int(starts_with_sap) << 31) | (sap_type << 28) | sap_delta
        payload.extend(struct.pack(">III", size_word, duration, sap_word))
    return full_box(b"sidx", version, bytes(payload), flags=0x010203)


def make_prefix(sidx: bytes, gap: bytes = b"") -> bytes:
    return box(b"ftyp", b"isom") + box(b"moov", b"") + sidx + gap


def make_fragment(tfdt_version: int, decode_time: int, sequence: int = 9) -> bytes:
    mfhd = full_box(b"mfhd", 0, struct.pack(">I", sequence))
    tfdt_format = ">I" if tfdt_version == 0 else ">Q"
    tfdt = full_box(b"tfdt", tfdt_version, struct.pack(tfdt_format, decode_time))
    traf = box(b"traf", tfdt)
    moof = box(b"moof", mfhd + traf)
    return moof + box(b"mdat", b"aac-payload")


def find_child(data: bytes, parent_type: str, child_type: str):
    parent = next(
        item for item in parse_top_level_boxes(data) if item.type == parent_type
    )
    offset = parent.data_start
    while offset < parent.end:
        size, raw_type = struct.unpack_from(">I4s", data, offset)
        if raw_type.decode("ascii") == child_type:
            return offset, size
        offset += size
    raise AssertionError(f"missing {child_type}")


def test_parses_top_level_boxes_and_sidx_v0() -> None:
    references = (
        (0, 100, 2000, True, 1, 17),
        (0, 120, 3000, False, 0, 0),
    )
    data = make_prefix(make_sidx(0, references, earliest=23))

    boxes = parse_top_level_boxes(data)
    sidx = parse_sidx(data)

    assert [item.type for item in boxes] == ["ftyp", "moov", "sidx"]
    assert sidx.version == 0
    assert sidx.flags == 0x010203
    assert sidx.reference_id == 7
    assert sidx.timescale == 1000
    assert sidx.earliest_presentation_time == 23
    assert sidx.first_offset == 0
    assert sidx.references[0].referenced_size == 100
    assert sidx.references[0].subsegment_duration == 2000
    assert sidx.references[0].starts_with_sap is True
    assert sidx.references[0].sap_type == 1
    assert sidx.references[0].sap_delta_time == 17


def test_parses_sidx_v1_64_bit_fields_and_rebuilds_v1() -> None:
    large_offset_data = make_prefix(
        make_sidx(
            1,
            ((0, 80, 48000, True, 2, 3),),
            timescale=48000,
            earliest=(1 << 32) + 5,
            first_offset=(1 << 32) + 7,
        )
    )
    source_prefix = make_prefix(
        make_sidx(
            1,
            ((0, 80, 48000, True, 2, 3),),
            timescale=48000,
            earliest=(1 << 32) + 5,
        )
    )

    parsed_large_offset = parse_sidx(large_offset_data)
    sidx = parse_sidx(source_prefix)
    manifest = build_manifest(source_prefix, ())
    rebuilt_sidx = parse_sidx(manifest.prefix)

    assert parsed_large_offset.first_offset == (1 << 32) + 7
    assert sidx.version == 1
    assert sidx.earliest_presentation_time == (1 << 32) + 5
    assert sidx.references[0].sap_type == 2
    assert rebuilt_sidx.version == 1
    assert rebuilt_sidx.earliest_presentation_time == 0
    assert rebuilt_sidx.references == sidx.references


def test_manifest_removes_middle_fragment_and_rebuilds_sidx() -> None:
    references = (
        (0, 100, 1000, True, 1, 0),
        (0, 120, 1000, True, 1, 0),
        (0, 80, 2000, True, 1, 0),
    )
    source_prefix = make_prefix(make_sidx(0, references))
    source_media_start = len(source_prefix)

    manifest = build_manifest(source_prefix, ((1.1, 1.9),))
    rebuilt_sidx = parse_sidx(manifest.prefix)

    assert [item.source_index for item in manifest.fragments] == [0, 2]
    assert manifest.fragments[0].source_start == source_media_start
    assert manifest.fragments[1].source_start == source_media_start + 220
    assert manifest.fragments[0].output_start == len(manifest.prefix)
    assert manifest.fragments[1].output_start == len(manifest.prefix) + 100
    assert manifest.fragments[0].new_decode_time == 0
    assert manifest.fragments[1].new_decode_time == 1000
    assert manifest.output_length == len(manifest.prefix) + 180
    assert manifest.output_duration_ticks == 3000
    assert manifest.output_duration == 3.0
    assert rebuilt_sidx.earliest_presentation_time == 0
    assert rebuilt_sidx.first_offset == 0
    assert [item.referenced_size for item in rebuilt_sidx.references] == [100, 80]
    assert [item.subsegment_duration for item in rebuilt_sidx.references] == [
        1000,
        2000,
    ]


@pytest.mark.parametrize("tfdt_version", [0, 1])
def test_patch_fragment_updates_mfhd_tfdt_and_preserves_mdat(
    tfdt_version: int,
) -> None:
    fragment = make_fragment(tfdt_version, decode_time=123, sequence=9)
    original_mdat = next(
        item for item in parse_top_level_boxes(fragment) if item.type == "mdat"
    )
    original_mdat_bytes = fragment[original_mdat.start : original_mdat.end]

    patched = patch_fragment(
        fragment,
        new_decode_time=1500,
        new_sequence=42,
        source_timescale=1000,
        target_timescale=48000,
    )

    moof = next(item for item in parse_top_level_boxes(patched) if item.type == "moof")
    mfhd_offset, _ = find_child(patched, "moof", "mfhd")
    _, traf_size = find_child(patched, "moof", "traf")
    traf_offset, _ = find_child(patched, "moof", "traf")
    tfdt_offset = traf_offset + 8
    tfdt_size = struct.unpack_from(">I", patched, tfdt_offset)[0]
    tfdt_format = ">I" if tfdt_version == 0 else ">Q"
    tfdt_value = struct.unpack_from(tfdt_format, patched, tfdt_offset + 12)[0]
    patched_mdat = next(
        item for item in parse_top_level_boxes(patched) if item.type == "mdat"
    )

    assert moof.size + patched_mdat.size == len(patched)
    assert traf_size == 8 + tfdt_size
    assert struct.unpack_from(">I", patched, mfhd_offset + 12)[0] == 42
    assert tfdt_value == 72000
    assert len(patched) == len(fragment)
    assert patched[patched_mdat.start : patched_mdat.end] == original_mdat_bytes


def test_rejects_malformed_boxes_and_unsafe_manifest_inputs() -> None:
    with pytest.raises(Mp4Error, match="extends beyond"):
        parse_top_level_boxes(struct.pack(">I4s", 20, b"ftyp") + b"short")

    unsupported_sidx = make_prefix(make_sidx(2, ()))
    with pytest.raises(Mp4Error, match="unsupported sidx version"):
        parse_sidx(unsupported_sidx)

    indirect = make_prefix(make_sidx(0, ((1, 100, 1000, True, 1, 0),)))
    with pytest.raises(Mp4Error, match="indirect sidx references"):
        build_manifest(indirect, ())

    source_prefix = make_prefix(make_sidx(0, ((0, 100, 1000, True, 1, 0),)))
    with pytest.raises(Mp4Error, match="remove every fragment"):
        build_manifest(source_prefix, ((0.0, 1.0),))


def test_patch_fragment_rejects_missing_required_boxes_and_v0_overflow() -> None:
    with pytest.raises(Mp4Error, match="missing mdat"):
        patch_fragment(
            box(b"moof", full_box(b"mfhd", 0, struct.pack(">I", 1))),
            0,
            1,
            source_timescale=1000,
        )

    fragment = make_fragment(0, decode_time=0)
    with pytest.raises(Mp4Error, match="does not fit tfdt version 0"):
        patch_fragment(
            fragment,
            new_decode_time=1 << 32,
            new_sequence=1,
            source_timescale=1,
        )
