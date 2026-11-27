"""导出 FastAPI OpenAPI 3.1 Schema，供前端 openapi-typescript 同步类型与契约比对。

用法：
    cd app && uv run python -m scripts.export_openapi [output.json]
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from menu_capture.api.server import create_app


def export_schema(output_path: Path | None = None) -> dict:
    with tempfile.TemporaryDirectory(prefix="mc-openapi-") as tmp:
        app = create_app(Path(tmp) / "hub", role="hub", admin_password="openapi-export-only")
        schema = app.openapi()
    text = json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return schema


def main() -> None:
    target = Path(sys.argv[1]).expanduser().resolve() if len(sys.argv) > 1 else None
    export_schema(target)


if __name__ == "__main__":
    main()
