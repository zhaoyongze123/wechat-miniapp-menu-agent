import os
from unittest.mock import Mock, patch

from menu_capture.config import ModelConfig
from menu_capture.model import VisionModel
from menu_capture.server import create_app


def test_model_client_ignores_proxy_environment():
    # 即使调用方没有走服务入口，模型客户端也不能继承失效的环境代理。
    with patch.dict(os.environ, {"HTTPS_PROXY": "http://127.0.0.1:1"}):
        with patch("menu_capture.model.httpx.Client") as client:
            client.return_value = Mock()
            model = VisionModel(ModelConfig(base_url="https://example.com/v1", model="test"), "test-key")
            assert client.call_args.kwargs["trust_env"] is False
            assert client.call_args.kwargs.get("verify", True) is True
            model.close()


def test_service_factory_disables_proxy_for_all_start_methods(tmp_path):
    keys = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
    with patch.dict(os.environ, {key: "http://127.0.0.1:1" for key in keys}):
        create_app(tmp_path)
        assert all(key not in os.environ for key in keys)
        assert os.environ["NO_PROXY"] == "*"
        assert os.environ["no_proxy"] == "*"
