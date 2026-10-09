"""Small headless command line entry point."""
import argparse
import getpass
import os
from pathlib import Path

from .models import SceneRuntimeConfig


def main():
    parser = argparse.ArgumentParser(prog="python -m heterosbox")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="validate a scene JSON file")
    validate.add_argument("scene")
    serve = commands.add_parser("serve", help="serve local API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--prompt-key", action="store_true", help="read DeepSeek key without echo or saving it")
    args = parser.parse_args()
    if args.command == "validate":
        scene = SceneRuntimeConfig.model_validate_json(Path(args.scene).read_text(encoding="utf-8"))
        from .arbiter import WriteArbiter
        WriteArbiter(scene)
        print(f"Valid scene: {scene.scene_id} ({len(scene.agents)} agents)")
    elif args.command == "serve":
        if args.prompt_key:
            os.environ["DEEPSEEK_API_KEY"] = getpass.getpass("DeepSeek API Key: ")
        local_env = Path(".env.local")
        if local_env.is_file():
            accepted = {"DEEPSEEK_API_KEY", "MIMO_API_KEY", "MIMO_BASE_URL", "GEMINI_API_KEY"}
            for line in local_env.read_text(encoding="utf-8-sig").splitlines():
                name, separator, value = line.partition("=")
                if separator and name in accepted and not os.getenv(name):
                    os.environ[name] = value.strip()
        import uvicorn
        from .api import create_app
        uvicorn.run(create_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
