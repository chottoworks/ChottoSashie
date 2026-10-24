"""アンインストールで消す一覧（APP_ENTRIES）が、保存先へ書くものを漏れなく含むか。

保存先へ新しい種類のフォルダを足したのに一覧へ足し忘れると、「アプリデータも削除」を
選んでもそれだけが残る。書き込む側の場所の決め方から、直下の名前を集めて突き合わせる。
"""

from pathlib import Path

from chotto_sashie_shared.data_removal import APP_ENTRIES
from chotto_sashie_shared.local_images import CANDIDATE_IMAGES, local_image_path
from chotto_sashie_api import main
from chotto_sashie_api.orphans import managed_folders


def _top_level(path: Path, root: Path) -> str:
    return path.relative_to(root).parts[0]


def test_every_folder_the_app_writes_is_removed_on_uninstall() -> None:
    root = main.settings.data_dir
    written = [
        *managed_folders(root),
        local_image_path(root, CANDIDATE_IMAGES, "x"),
        main.settings.database_path,
        main.MANAGED_ROOT,
    ]

    assert {_top_level(path, root) for path in written} <= set(APP_ENTRIES)
