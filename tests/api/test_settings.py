from chotto_sashie_api.settings import Settings, default_data_dir


def test_empty_data_dir_env_falls_back_to_default(monkeypatch) -> None:
    # 空文字が渡っても作業フォルダへ落ちず、既定の保存先を使う。
    monkeypatch.setenv("CHOTTO_SASHIE_DATA_DIR", "")
    assert Settings().data_dir == default_data_dir()


def test_dot_data_dir_env_falls_back_to_default(monkeypatch) -> None:
    monkeypatch.setenv("CHOTTO_SASHIE_DATA_DIR", ".")
    assert Settings().data_dir == default_data_dir()


def test_explicit_data_dir_env_is_respected(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CHOTTO_SASHIE_DATA_DIR", str(tmp_path))
    assert Settings().data_dir == tmp_path


def test_api_token_env_is_read(monkeypatch) -> None:
    monkeypatch.setenv("CHOTTO_SASHIE_API_TOKEN", "abc123")
    assert Settings().api_token == "abc123"
