from __future__ import annotations

import stat
import os
import zipfile

import pytest

from aviation_ft import core, generation, providers
from test_core import sample_job
from test_generation import configured_job


def test_external_provider_requires_https_and_privacy_consent(sample_job):
    with pytest.raises(ValueError, match="确认允许"):
        providers.save_external(
            sample_job, "https://api.example.com/v1", "remote-model", "secret-key", False,
        )
    with pytest.raises(ValueError, match="HTTPS"):
        providers.save_external(
            sample_job, "http://api.example.com/v1", "remote-model", "secret-key", True,
        )


def test_external_provider_is_job_scoped_masked_and_not_exported(sample_job, monkeypatch):
    job_id, _ = configured_job(sample_job, monkeypatch)
    public = providers.save_external(
        job_id, "https://api.example.com/v1/", "remote-model", "secret-key-1234", True,
    )
    assert public == {
        "role": "generator",
        "mode": "external",
        "configured": True,
        "base_url": "https://api.example.com/v1",
        "model": "remote-model",
        "preset_id": "custom",
        "provider_name": "自定义兼容接口",
        "api_key_masked": "••••••••1234",
        "updated_at": public["updated_at"],
        "privacy_acknowledged": True,
    }
    assert "secret-key-1234" not in str(public)
    secret_path = providers.provider_dir() / f"{job_id}.json"
    if os.name != "nt":
        assert stat.S_IMODE(secret_path.stat().st_mode) == 0o600
    else:
        assert secret_path.parent == providers.provider_dir()

    cfg = generation.config(job_id)
    assert cfg["provider_mode"] == "external"
    assert cfg["served_model_name"] == "remote-model"
    assert cfg["api_key"] == "secret-key-1234"

    calls = []

    def fake_request(url, payload=None, timeout=5, api_key=None):
        calls.append((url, api_key, payload))
        return {"choices": [{"message": {"content": "OK"}}]}

    monkeypatch.setattr(generation, "_request", fake_request)
    assert generation.probe(job_id)["ready"] is True
    assert calls[0][0:2] == (
        "https://api.example.com/v1/chat/completions", "secret-key-1234",
    )
    assert calls[0][2]["model"] == "remote-model"

    archive, _, _ = core.export_job(job_id, final=False, include_audit=True)
    with zipfile.ZipFile(archive) as bundle:
        assert all("secret" not in name for name in bundle.namelist())
        contents = b"".join(bundle.read(name) for name in bundle.namelist()).decode(
            "utf-8", errors="ignore"
        )
        assert "secret-key-1234" not in contents
        card = bundle.read("dataset_card.json").decode("utf-8")
        assert '"mode": "external"' in card
        assert '"model": "remote-model"' in card

    local = providers.use_local(job_id)
    assert local["mode"] == "local"
    assert generation.config(job_id)["provider_mode"] == "local"



def test_domestic_preset_only_requires_api_key(sample_job, monkeypatch):
    job_id, _ = configured_job(sample_job, monkeypatch)
    public = providers.save_external(
        job_id, "", "", "deepseek-key", True, "deepseek",
    )
    assert public["provider_name"] == "DeepSeek"
    assert public["base_url"] == "https://api.deepseek.com"
    assert public["model"] == "deepseek-flash"
    cfg = generation.config(job_id)
    assert cfg["api_key"] == "deepseek-key"
    payload = {"chat_template_kwargs": {"enable_thinking": False}, "model": "deepseek-flash"}
    monkeypatch.setattr(generation, "_request", lambda url, **kwargs: kwargs["payload"])
    sent = generation._configured_request(cfg, "/chat/completions", payload)
    assert "chat_template_kwargs" not in sent
