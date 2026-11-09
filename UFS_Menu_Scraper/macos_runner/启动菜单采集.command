#!/bin/zsh
set -eu
cd "${0:A:h}"
if [[ ! -x .venv/bin/python ]]; then
  /opt/homebrew/bin/uv sync --frozen --python 3.12
fi
# 仅清理本服务进程的代理环境，不修改系统或其他应用的代理设置。
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy
export NO_PROXY='*' no_proxy='*'
.venv/bin/python -m menu_capture.service
open 'http://127.0.0.1:8765'
