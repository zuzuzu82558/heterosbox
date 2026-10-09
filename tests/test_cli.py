import sys
from heterosbox.__main__ import main


def test_validate_cli(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["heterosbox", "validate", "examples/cooperative.json"])
    main()
    assert "cooperative-repair" in capsys.readouterr().out


def test_serve_cli_uses_in_memory_key(monkeypatch):
    import uvicorn
    captured = {}
    monkeypatch.setattr(sys, "argv", ["heterosbox", "serve", "--prompt-key", "--port", "8123"])
    monkeypatch.setattr("getpass.getpass", lambda prompt: "test-only")
    monkeypatch.setattr(uvicorn, "run", lambda app, host, port: captured.update({"host": host, "port": port}))
    main()
    assert captured == {"host": "127.0.0.1", "port": 8123}


def test_serve_cli_loads_local_env_file(monkeypatch):
    import os
    import uvicorn
    from pathlib import Path

    monkeypatch.setattr(Path, "is_file", lambda path: path.name == ".env.local")
    monkeypatch.setattr(Path, "read_text", lambda path, encoding: "DEEPSEEK_API_KEY=test-only\n")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["heterosbox", "serve", "--port", "8123"])
    captured = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, host, port: captured.update({"host": host, "port": port}))
    main()
    assert os.environ["DEEPSEEK_API_KEY"] == "test-only"
    assert captured == {"host": "127.0.0.1", "port": 8123}
