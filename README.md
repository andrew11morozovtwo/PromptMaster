## Terminal AI Agent (OpenAI via Proxy)

### 1) Setup (Windows PowerShell)
```powershell
cd D:\zero-code\PromptMaster
python -m venv .venv
. .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2) Configure environment
- Copy `env.example` to `.env` and set values:
  - `OPENAI_API_KEY` (required)
  - `OPENAI_BASE_URL` (default: `https://api.proxyapi.ru/openai/v1`)
  - `OPENAI_MODEL` (default: `gpt-4o`)
  - `AGENT_SYSTEM_PROMPT` (optional)

### 3) Usage
- REPL (interactive):
```powershell
python .\agent.py --stream
```
  - Commands: `/exit` to quit, `/clear` to clear history
  - Optional: `--system "Вы — helpful ассистент"` to set a system prompt

- Single message (non-interactive):
```powershell
python .\agent.py -m "Привет!" --stream
```

### 4) Notes
- Colors are enabled in TTY; use `--no-color` to disable.
- Streaming can be toggled with `--stream`.

