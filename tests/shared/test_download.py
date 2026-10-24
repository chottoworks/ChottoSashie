import hashlib
from pathlib import Path

import httpx
import pytest

from chotto_sashie_shared.download import (
    build_setup_plan,
    DownloadError,
    DownloadSpec,
    download_with_resume,
    find_matching_external_file,
    inspect_managed_file,
    safe_destination,
)


def make_spec(data: bytes) -> DownloadSpec:
    return DownloadSpec(
        id="test-model",
        display_name_ja="テストモデル",
        download_url="https://models.invalid/model.bin",
        destination="models/diffusion_models/model.bin",
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        license="test",
        source="https://models.invalid",
    )


def test_destination_cannot_escape_managed_root(tmp_path: Path) -> None:
    with pytest.raises(DownloadError):
        safe_destination(tmp_path, "../external/model.bin")


def test_download_resumes_part_and_validates_hash(tmp_path: Path) -> None:
    data = b"0123456789" * 100
    spec = make_spec(data)
    destination = safe_destination(tmp_path, spec.destination)
    destination.parent.mkdir(parents=True)
    partial = destination.with_name(f"{destination.name}.part")
    partial.write_bytes(data[:321])

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["range"] == "bytes=321-"
        return httpx.Response(206, content=data[321:])

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = download_with_resume(tmp_path, spec, client=client)

    assert result.read_bytes() == data
    assert not partial.exists()
    assert inspect_managed_file(tmp_path, spec, verify_hash=True).state == "ready"


def test_external_match_is_read_only(tmp_path: Path) -> None:
    data = b"external model"
    candidate = tmp_path / "external.safetensors"
    candidate.write_bytes(data)
    before = candidate.stat().st_mtime_ns

    assert find_matching_external_file([candidate], make_spec(data)) == candidate.resolve()
    assert candidate.stat().st_mtime_ns == before


def test_plan_excludes_matching_external_file_from_download(tmp_path: Path) -> None:
    data = b"external model"
    candidate = tmp_path / "external.safetensors"
    candidate.write_bytes(data)
    spec = make_spec(data)
    spec = DownloadSpec(**{**spec.__dict__, "external_read_only_reuse": True})

    plan = build_setup_plan(
        tmp_path / "managed",
        [spec],
        external_files=[candidate],
    )

    assert plan["required_download_bytes"] == 0
    assert plan["components"][0]["state"] == "external_ready"
