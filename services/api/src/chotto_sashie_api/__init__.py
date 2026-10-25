"""ちょっと挿絵 ローカルAPI。"""

from importlib.metadata import version

# 版は pyproject.toml の1か所に書き、入れたパッケージの情報から読む（配布版もアプリ本体を
# wheel で入れるので同じく読める）。ここへ書き写すと、版を上げたときに取り残される。
__version__ = version("chotto-sashie")
