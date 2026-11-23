import pytest

from rental_agent.config import DefaultModels, load_default_models


def test_default_models_come_from_the_file(tmp_path, monkeypatch):
    path = tmp_path / "models.toml"
    path.write_text('jobs = "claude-haiku-5-5"\nchat = "claude-opus-5-5"\n')
    monkeypatch.setenv("RENTAL_MODELS_FILE", str(path))
    assert load_default_models() == DefaultModels(jobs="claude-haiku-5-5", chat="claude-opus-5-5")


def test_repo_default_models_load():
    assert load_default_models() == DefaultModels(jobs="claude-opus-5-5", chat="claude-sonnet-5-5")


@pytest.mark.parametrize("text", ['jobs = "claude-opus-5-5"\n',                    # no chat
                                  'jobs = "gpt-x"\nchat = "claude-opus-5-5"\n'])
def test_unknown_or_missing_model_is_rejected(tmp_path, monkeypatch, text):
    path = tmp_path / "models.toml"
    path.write_text(text)
    monkeypatch.setenv("RENTAL_MODELS_FILE", str(path))
    with pytest.raises(ValueError, match="must be one of"):
        load_default_models()


def test_missing_file_is_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("RENTAL_MODELS_FILE", str(tmp_path / "none.toml"))
    with pytest.raises(RuntimeError, match="Default models file not found"):
        load_default_models()
