"""共有フォルダ（ComfyUI・モデルの置き場）の扱いを確かめる。"""

from __future__ import annotations

import tomllib

from pathlib import Path

import pytest

from chotto_sashie_shared.comfyui_provision import (
    SOURCE_EXISTING,
    SOURCE_SHARED,
    classify_comfyui_source,
    managed_comfyui_paths,
    resolve_comfyui_install,
)
from chotto_sashie_shared.model_layout import models_relative_path
from chotto_sashie_shared.shared_env import (
    RESOURCES_DIR_ENV,
    ResourcesNotFoundError,
    app_data_root,
    app_resources_root,
    shared_comfyui_root,
    shared_models_root,
)


def _fake_install(comfyui_root: Path, *, version: str = "") -> tuple[Path, Path]:
    comfy_dir, python = managed_comfyui_paths(comfyui_root)
    comfy_dir.mkdir(parents=True, exist_ok=True)
    (comfy_dir / "main.py").write_text("", encoding="utf-8")
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("", encoding="utf-8")
    if version:
        (comfy_dir / "comfyui_version.py").write_text(
            f'__version__ = "{version}"\n', encoding="utf-8"
        )
    return comfy_dir, python


@pytest.fixture
def isolated_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """データ領域と共有フォルダを、この試験の中だけに閉じ込める。"""
    monkeypatch.setenv("CHOTTO_SASHIE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CHOTTO_SASHIE_SHARED_DIR", str(tmp_path / "shared"))
    return tmp_path


def test_shared_folder_is_outside_the_data_area(isolated_app: Path) -> None:
    """共有フォルダはデータ領域の外に置く（保存先の引っ越しに巻き込まない）。"""
    shared = shared_comfyui_root()
    assert not shared.is_relative_to(app_data_root())
    assert shared_models_root().parent == shared.parent


def test_shared_install_is_used(isolated_app: Path) -> None:
    """共有フォルダに用意した本体を使う。"""
    _fake_install(shared_comfyui_root())

    install = resolve_comfyui_install()
    assert install is not None
    assert classify_comfyui_source(install[0]) == SOURCE_SHARED


def test_nothing_is_found_before_provisioning(isolated_app: Path) -> None:
    assert resolve_comfyui_install() is None


def test_configured_path_inside_the_shared_folder_is_not_borrowing(isolated_app: Path) -> None:
    """共有物を指した「流用」設定は借り物として扱わない（アプリから更新できるまま）。"""
    comfy_dir, python = _fake_install(shared_comfyui_root())

    install = resolve_comfyui_install(str(comfy_dir), str(python))
    assert install == (comfy_dir, python)
    assert classify_comfyui_source(install[0]) == SOURCE_SHARED


def test_user_specified_install_is_still_borrowed(isolated_app: Path, tmp_path: Path) -> None:
    """利用者が指定した既存インストールは、借り物として扱う。"""
    external = tmp_path / "ExistingComfyUI"
    external.mkdir()
    (external / "main.py").write_text("", encoding="utf-8")
    python = tmp_path / "python.exe"
    python.write_text("", encoding="utf-8")

    install = resolve_comfyui_install(str(external), str(python))
    assert install == (external, python)
    assert classify_comfyui_source(install[0]) == SOURCE_EXISTING


def test_data_dir_follows_the_environment_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHOTTO_SASHIE_DATA_DIR", str(tmp_path / "moved"))
    assert app_data_root() == (tmp_path / "moved").resolve()


def test_models_relative_path_follows_existing_folder_names(tmp_path: Path) -> None:
    """既存フォルダで別名（unet など）が使われていれば、そちらへ揃える。"""
    root = tmp_path / "Models"
    (root / "unet").mkdir(parents=True)

    assert models_relative_path(root, "models/diffusion_models/a.safetensors") == "unet/a.safetensors"
    # 無い種別は標準名で作る。
    assert models_relative_path(root, "models/loras/b.safetensors") == "loras/b.safetensors"


# 同梱物のうち、いちばん深いものの相対パス長の上限。
#
# Windowsの既定はパス全体で260文字。インストールすると
# `chotto_sashie_shared/_resources/`（31文字）が前に付き、その手前に
# インストール先（`C:\Users\<利用者名>\...\site-packages\` で90文字前後）が来る。
# 90 + 31 + 130 = 251 で、長い利用者名でも収まる余地を残す値にしている。
# 同梱物を増やして超えたときは、深い階層を持ち込まないか、配布時に
# `CHOTTO_SASHIE_RESOURCES_DIR` で浅い場所を指す。
_DEEPEST_RESOURCE_PATH_BUDGET = 130


def test_bundled_resources_stay_within_the_windows_path_budget() -> None:
    """同梱物の階層が深くなりすぎていないか。

    深いまま増やすと、インストール先が長い利用者の環境でだけ展開に失敗する。
    実際に、長い作業フォルダへ入れたときにこれで失敗した。

    測る対象は pyproject.toml の force-include（＝実際に配られるもの）から取る。
    ここで一覧を書き写すと、同梱物を足したときに片方だけ増えて素通りする。
    """
    repository = Path(__file__).resolve().parents[2]
    with (repository / "pyproject.toml").open("rb") as stream:
        config = tomllib.load(stream)
    shipped = config["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert shipped, "同梱物の指定が pyproject.toml から消えている"

    deepest = ""
    for source, destination in shipped.items():
        prefix = destination.split("chotto_sashie_shared/_resources/", 1)[-1]
        for path in (repository / source).rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            relative = f"{prefix}/{path.relative_to(repository / source).as_posix()}"
            if len(relative) > len(deepest):
                deepest = relative

    assert len(deepest) <= _DEEPEST_RESOURCE_PATH_BUDGET, deepest


def test_resources_root_follows_the_environment_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """配布版が自分の場所を教えられる（決め打ちの探索に頼らない）。"""
    monkeypatch.setenv(RESOURCES_DIR_ENV, str(tmp_path / "resources"))

    assert app_resources_root() == (tmp_path / "resources").resolve()


def test_resources_root_stops_when_nothing_is_bundled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """見つからないときは、当てずっぽうの場所を返さずに止める。

    ここで近そうな場所を返すと、起動はするのに生成だけが失敗する、という
    原因の見えない壊れ方になる。
    """
    monkeypatch.setenv(RESOURCES_DIR_ENV, "")
    monkeypatch.setattr(
        "chotto_sashie_shared.shared_env.__file__", str(tmp_path / "nowhere" / "shared_env.py")
    )

    with pytest.raises(ResourcesNotFoundError, match="model-manifests"):
        app_resources_root()
