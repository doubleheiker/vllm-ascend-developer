#!/usr/bin/env python3
"""
从项目私有 config/test.yaml 或 Plugin 模板生成 curl 测试脚本。

用法：
    # 本工作流尚未执行 bootstrap 时，先检查并补齐固定 .dev/run
    python3 "${CLAUDE_PLUGIN_ROOT}/scripts/path_policy.py" bootstrap
    python3 "${CLAUDE_PLUGIN_ROOT}/scripts/generate_curl.py"
    python3 "${CLAUDE_PLUGIN_ROOT}/scripts/generate_curl.py" --dry-run

生成的 curl_test.sh 可直接在容器内执行，用于发送推理请求。
"""

import argparse
import json
import sys

import yaml

from path_policy import (
    get_config_dir,
    get_project_root,
    get_run_dir,
    validate_local_write,
)


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="生成 curl 测试脚本")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--test-index", type=int, default=0)
    parser.add_argument("--prompt-index", type=int, default=0)
    parser.add_argument("--project-root", help="用户项目根目录")
    parser.add_argument("--config-dir", help="配置目录，默认优先项目私有配置")
    args = parser.parse_args()

    project_root = get_project_root(args.project_root)
    config_dir = get_config_dir(project_root, args.config_dir)
    test_yaml = config_dir / "test.yaml"
    if not test_yaml.is_file():
        sys.exit(f"找不到测试配置: {test_yaml}")

    with open(test_yaml, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    tests = cfg.get("tests", [])
    if args.test_index >= len(tests):
        sys.exit(f"测试索引 {args.test_index} 超出范围（共 {len(tests)} 条）")

    test = tests[args.test_index]
    endpoint = test["endpoint"]
    params = test.get("params", {})
    prompts = test.get("prompts", [])
    if args.prompt_index >= len(prompts):
        sys.exit(f"prompt 索引 {args.prompt_index} 超出范围（共 {len(prompts)} 条）")

    # model 名来源：test.yaml params.model > model.yaml
    model_name = params.get("model")
    if not model_name:
        model_yaml = config_dir / "model.yaml"
        if model_yaml.exists():
            with open(model_yaml, encoding="utf-8") as f:
                model_cfg = yaml.safe_load(f)
            model_name = model_cfg.get("served_model_name", "deepseek")
        else:
            model_name = "deepseek"

    payload = {
        "model": model_name,
        "prompt": prompts[args.prompt_index],
        "max_tokens": params.get("max_tokens", 10),
        "temperature": params.get("temperature", 0),
    }
    payload_json = json.dumps(payload, ensure_ascii=False)

    script = f"""#!/bin/bash
# 自动生成，来源: {test_yaml}
# 重新生成: python3 "${{CLAUDE_PLUGIN_ROOT}}/scripts/generate_curl.py"

unset http_proxy
unset https_proxy

curl {endpoint} \\
    -H "Content-Type: application/json" \\
    -d '{payload_json}'
"""

    if args.dry_run:
        print(script)
    else:
        run_dir = get_run_dir(project_root)
        output = validate_local_write(
            run_dir / "generated" / "curl_test.sh",
            project_root,
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(script, encoding="utf-8")
        output.chmod(0o755)
        print(f"已生成: {output}")


if __name__ == "__main__":
    main()
