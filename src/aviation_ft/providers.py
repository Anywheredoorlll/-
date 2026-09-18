from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from aviation_ft import core

def provider_dir() -> Path:
    return core.ROOT / "runtime/provider_secrets"


def _path(job_id: str) -> Path:
    core.job_dir(job_id)
    return provider_dir() / f"{job_id}.json"


def _validate_url(base_url: str) -> str:
    value = base_url.strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("API 地址不能包含账号、密码、查询参数或片段")
    if parsed.scheme == "https" and parsed.hostname:
        return value
    if parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"} and parsed.port:
        return value
    raise ValueError("外部 API 必须使用 HTTPS；本机接口可使用带端口的 HTTP")


def public_config(job_id: str) -> dict:
    stored = core.read_json(_path(job_id), {})
    key = stored.get("api_key", "")
    preset = _preset(stored.get("preset_id", ""))
    return {
        "mode": "external" if stored.get("enabled") else "local",
        "configured": bool(stored.get("enabled") and key),
        "base_url": stored.get("base_url", ""),
        "model": stored.get("model", ""),
        "preset_id": stored.get("preset_id", "custom"),
        "provider_name": preset["name"] if preset else "自定义兼容接口",
        "api_key_masked": f"••••••••{key[-4:]}" if len(key) >= 4 else "",
        "updated_at": stored.get("updated_at"),
        "privacy_acknowledged": bool(stored.get("privacy_acknowledged")),
    }


def save_external(
    job_id: str,
    base_url: str,
    model: str,
    api_key: str,
    privacy_acknowledged: bool,
    preset_id: str = "",
) -> dict:
    path = _path(job_id)
    previous = core.read_json(path, {})
    secret = api_key.strip() or previous.get("api_key", "")
    if not privacy_acknowledged:
        raise ValueError("必须确认允许将任务证据发送到外部 API")
    if not secret:
        raise ValueError("请填写 API Key")
    preset = _preset(preset_id)
    if preset_id and preset_id != "custom" and not preset:
        raise ValueError("不支持的模型服务商")
    if preset:
        selected_model = model.strip() or preset["models"][0]
        if selected_model not in preset["models"]:
            raise ValueError("所选模型不属于该服务商预设")
        selected_url = preset["base_url"]
    else:
        selected_model = model.strip()
        if not selected_model:
            raise ValueError("请填写模型名称")
        selected_url = _validate_url(base_url)
    directory = provider_dir()
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    core.write_json(path, {
        "enabled": True,
        "preset_id": preset["id"] if preset else "custom",
        "base_url": selected_url,
        "model": selected_model[:200],
        "api_key": secret,
        "privacy_acknowledged": True,
        "updated_at": core.now(),
    })
    path.chmod(0o600)
    return public_config(job_id)


def use_local(job_id: str) -> dict:
    path = _path(job_id)
    if path.exists():
        stored = core.read_json(path, {})
        stored["enabled"] = False
        stored["updated_at"] = core.now()
        core.write_json(path, stored)
        path.chmod(0o600)
    return public_config(job_id)


def effective_config(job_id: str | None, local: dict) -> dict:
    if not job_id:
        return local
    stored = core.read_json(_path(job_id), {})
    if not stored.get("enabled"):
        return local
    if not stored.get("privacy_acknowledged") or not stored.get("api_key"):
        raise ValueError("外部 API 配置不完整")
    result = dict(local)
    result.update({
        "provider_mode": "external",
        "base_url": _validate_url(stored.get("base_url", "")),
        "served_model_name": stored.get("model", ""),
        "model_id": f"external:{stored.get('model', '')}",
        "model_revision": None,
        "api_key": stored["api_key"],
    })
    return result

PRESETS = (
    {
        "id": "deepseek", "name": "DeepSeek", "mark": "DS",
        "base_url": "https://api.deepseek.com",
        "models": ("deepseek-flash", "deepseek-v4-pro"),
        "note": "DeepSeek 官方 API",
    },
    {
        "id": "dashscope", "name": "通义千问 · 阿里云百炼", "mark": "QW",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ("qwen-plus", "qwen3.6-plus"),
        "note": "阿里云百炼 OpenAI 兼容接口",
    },
    {
        "id": "zhipu", "name": "智谱 GLM", "mark": "GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "models": ("glm-5.2", "glm-4-plus"),
        "note": "智谱开放平台",
    },
    {
        "id": "doubao", "name": "豆包 · 火山方舟", "mark": "DB",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "models": ("doubao-seed-2-0-lite-260215",),
        "note": "火山方舟模型服务",
    },
    {
        "id": "hunyuan", "name": "腾讯混元", "mark": "HY",
        "base_url": "https://api.hunyuan.cloud.tencent.com/v1",
        "models": ("hunyuan-turbos-latest",),
        "note": "腾讯混元 OpenAI 兼容接口",
    },
    {
        "id": "qianfan", "name": "百度千帆 · 文心", "mark": "ERNIE",
        "base_url": "https://qianfan.baidubce.com/v2",
        "models": ("ernie-5.0", "ernie-4.5-turbo-128k"),
        "note": "百度千帆 OpenAI 兼容接口",
    },
)


def provider_presets() -> list[dict]:
    return [{**item, "models": list(item["models"])} for item in PRESETS]


def _preset(preset_id: str) -> dict | None:
    return next((item for item in PRESETS if item["id"] == preset_id), None)


