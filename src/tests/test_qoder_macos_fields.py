"""The macOS field builder is local and does not copy a sampled host identity."""
import copy
import secrets

import pytest

from src.oauth.qoder import device_macos, device_profile, device_blocks
from src.oauth.qoder.device_block_1a import transform


def test_1a_matches_independent_synthetic_vector_and_roundtrips():
    assert transform(bytes(range(20))).hex() == "9ae369b149b4b19caf90520cd30d2c436b459565"
    for _ in range(32):
        raw = secrets.token_bytes(40)
        assert transform(transform(raw), inverse=True) == raw
    assert device_blocks.encode_block(0x1A, [b""], key=b"1234")[11:] == bytes(
        value ^ b"1234"[(index + 2) % 4] for index, value in enumerate(device_blocks.CONSTANTS[0x1A]))


def test_macos_complete_profile_is_generated_and_kept_by_upgrade():
    first = device_profile.generate(platform="macos-arm64")
    second = device_profile.generate(platform="macos-arm64")
    decoded = device_blocks.decode_blocks(device_profile.validate(first))
    assert [len(fields) for fields in decoded] == [2, 44, 5, 3]
    assert decoded[0][0] == b"" and len(decoded[0][1]) == 32 and decoded[0][1].isalnum()
    assert decoded[3] == [b"", b"{}", b""]
    assert device_profile.upgrade(first) == first
    assert device_profile.summary({"qoder_device_profile": first})["status"] == "bound"
    assert device_profile.fingerprint(first) != device_profile.fingerprint(second)
    assert decoded[0][1] != device_blocks.decode_blocks(device_profile.validate(second))[0][1]
    first["platform"] = "linux-x64"
    with pytest.raises(ValueError):
        device_profile.validate(first)


def test_macos_fields_match_observed_shape_and_generate_independent_identities():
    first, second = device_macos.generate_fields(), device_macos.generate_fields()
    assert len(first) == 44
    assert first[5:9] == [b"1.1.2", b"MacOS", b"Version 26.2 (Build 25C56)", b"runtime-info-darwin-arm64"]
    assert first[12:19] == [b""] * 7
    assert first[21:23] == [b""] * 2
    assert first[37] == b"arm64"
    assert first[1] == first[34] == first[41]
    assert all(first[index] != second[index] for index in (11, 19, 20, 23))
    assert all(b"MemTotal" not in field for field in first)
    device_macos.validate_fields(first)


@pytest.mark.parametrize("index,value", [(5, b"1.5.0"), (6, b"Linux"), (12, b"duplicate-mac"),
    (20, b"serial"), (23, b"not-a-uuid"), (28, b"meminfo"), (37, b"x86_64"), (43, b"extra")])
def test_macos_fields_reject_mixed_linux_or_invalid_identity(index, value):
    fields = copy.deepcopy(device_macos.generate_fields())
    fields[index] = value
    with pytest.raises(ValueError):
        device_macos.validate_fields(fields)
