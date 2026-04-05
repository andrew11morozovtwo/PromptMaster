import os
import sys
import argparse
from typing import List, Optional
import json
import uuid
from datetime import datetime, timezone
import os

try:
	from dotenv import load_dotenv  # type: ignore
except Exception:
	load_dotenv = None

try:
	from openai import OpenAI  # type: ignore
except Exception as exc:
	print("Missing dependency 'openai'. Install with: pip install -r requirements.txt", file=sys.stderr)
	raise


def read_env() -> None:
	"""
	Load environment variables from a .env file if python-dotenv is available.
	"""
	if load_dotenv is not None:
		# Resolve .env relative to this script's directory to avoid CWD issues
		base_dir = os.path.dirname(os.path.abspath(__file__))
		env_path = os.path.join(base_dir, ".env")
		env_local_path = os.path.join(base_dir, ".env.local")

		# Do not override existing environment variables with .env
		if os.path.exists(env_path):
			# Use utf-8-sig to safely strip BOM if present (PowerShell often writes BOM)
			load_dotenv(dotenv_path=env_path, override=False, encoding="utf-8-sig")
		# Allow .env.local to override for local tweaks
		if os.path.exists(env_local_path):
			load_dotenv(dotenv_path=env_local_path, override=True, encoding="utf-8-sig")


def getenv_with_default(name: str, default: Optional[str] = None) -> str:
	value = os.getenv(name)
	if value is None or value == "":
		if default is None:
			script_dir = os.path.dirname(os.path.abspath(__file__))
			print(
				f"Environment variable {name} is required and not set.\n"
				f"Ensure it is defined in your environment or in {os.path.join(script_dir, '.env')}",
				file=sys.stderr,
			)
			sys.exit(1)
		return default
	return value


def build_client() -> OpenAI:
	"""
	Construct OpenAI client honoring proxy/base URL and API key from env.
	"""
	api_key = getenv_with_default("OPENAI_API_KEY")
	base_url = getenv_with_default("OPENAI_BASE_URL", "https://api.proxyapi.ru/openai/v1")
	return OpenAI(api_key=api_key, base_url=base_url)


def make_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(
		description="Terminal AI agent (OpenAI Chat Completions via proxy)."
	)
	# Built-in language mirroring hint is always applied; user can add extra system text
	parser.add_argument(
		"--model",
		default=os.getenv("OPENAI_MODEL", "gpt-4o"),
		help="Model to use (default: env OPENAI_MODEL or 'gpt-4o').",
	)
	parser.add_argument(
		"--system",
		default=os.getenv("AGENT_SYSTEM_PROMPT", "").strip(),
		help="Optional system prompt to guide the assistant.",
	)
	parser.add_argument(
		"--stream",
		action="store_true",
		help="Enable streaming responses.",
	)
	parser.add_argument(
		"--show-usage",
		action="store_true",
		help="Print token usage per exchange (prompt/completion/total).",
	)
	parser.add_argument(
		"--no-color",
		action="store_true",
		help="Disable ANSI colors.",
	)
	parser.add_argument(
		"-m",
		"--message",
		default=None,
		help="Send a single message (non-interactive). If omitted, starts REPL.",
	)
	return parser


def color(text: str, code: str, enabled: bool) -> str:
	if enabled:
		return f"\033[{code}m{text}\033[0m"
	return text


def resolve_model_from_menu(model_value: str) -> str:
	"""
	If model_value is a digit and MODEL_MENU is defined (comma-separated),
	map the digit to the corresponding model id. '0' returns the original value.
	"""
	val = (model_value or "").strip()
	if not val.isdigit():
		return model_value
	if val == "0":
		return model_value
	menu = os.getenv("MODEL_MENU", "")
	if not menu.strip():
		return model_value
	models = [m.strip() for m in menu.split(",") if m.strip()]
	idx = int(val) - 1
	if 0 <= idx < len(models):
		return models[idx]
	return model_value


# --- Simple persistent storage for user profiles and conversation history ---
def get_store_path() -> str:
	base_dir = os.path.dirname(os.path.abspath(__file__))
	data_dir = os.path.join(base_dir, "data")
	os.makedirs(data_dir, exist_ok=True)
	return os.path.join(data_dir, "agent_store.json")


def load_store() -> dict:
	path = get_store_path()
	if not os.path.exists(path):
		return {"users": {}}
	try:
		with open(path, "r", encoding="utf-8") as f:
			return json.load(f)
	except Exception:
		# In case of corruption, start fresh but do not overwrite the file yet
		return {"users": {}}


def save_store(store: dict) -> None:
	path = get_store_path()
	tmp_path = path + ".tmp"
	with open(tmp_path, "w", encoding="utf-8") as f:
		json.dump(store, f, ensure_ascii=False, indent=2)
	os.replace(tmp_path, path)


def limit_history_to_pairs(history: List[dict], max_pairs: int = 10) -> List[dict]:
	# Keep only the last 2 * max_pairs messages (user+assistant pairs)
	max_messages = max_pairs * 2
	if len(history) <= max_messages:
		return history
	return history[-max_messages:]


def get_or_create_user_profile(store: dict, name: str) -> dict:
	key = name.strip().lower()
	now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
	users = store.setdefault("users", {})
	if key in users:
		profile = users[key]
		profile["updated_at"] = now
		# Ensure fields exist
		profile.setdefault("name", name.strip())
		profile.setdefault("user_id", str(uuid.uuid4()))
		profile.setdefault("history", [])
		profile["history"] = limit_history_to_pairs(profile["history"], 10)
		return profile

	profile = {
		"name": name.strip(),
		"user_id": str(uuid.uuid4()),
		"history": [],  # list of {"role": "user"|"assistant", "content": "..."}
		"created_at": now,
		"updated_at": now,
	}
	users[key] = profile
	return profile


def append_exchange_and_persist(store: dict, profile: dict, user_text: str, assistant_text: str) -> None:
	profile["history"].append({"role": "user", "content": user_text})
	profile["history"].append({"role": "assistant", "content": assistant_text})
	profile["history"] = limit_history_to_pairs(profile["history"], 10)
	profile["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
	save_store(store)


def run_single_message(client: OpenAI, model: str, system_prompt: str, content: str, stream: bool, use_color: bool, show_usage: bool) -> int:
	messages: List[dict] = []
	# Ensure assistant mirrors user's language
	lang_hint = os.getenv("AGENT_LANG_HINT", "Always respond in the same language as the user's last message.")
	messages.append({"role": "system", "content": lang_hint})
	if system_prompt:
		messages.append({"role": "system", "content": system_prompt})
	messages.append({"role": "user", "content": content})

	if stream:
		print(color("Assistant:", "1;36", use_color), end=" ", flush=True)
		try:
			accumulated_parts: List[str] = []
			last_usage = None
			for chunk in client.chat.completions.create(model=model, messages=messages, stream=True):
				if not chunk.choices:
					continue
				# Try to capture usage if the SDK provides it on final chunks
				if getattr(chunk, "usage", None) is not None:
					last_usage = chunk.usage
				delta = getattr(chunk.choices[0].delta, "content", None)
				if delta:
					accumulated_parts.append(delta)
					print(delta, end="", flush=True)
			print()
			if show_usage and last_usage is not None:
				pt = getattr(last_usage, "prompt_tokens", None)
				ct = getattr(last_usage, "completion_tokens", None)
				tt = getattr(last_usage, "total_tokens", None)
				print(color(f"[usage] prompt={pt} completion={ct} total={tt}", "2;37", use_color))
			return 0
		except Exception as e:
			print()
			print(color(f"Error: {e}", "1;31", use_color), file=sys.stderr)
			return 2
	else:
		resp = client.chat.completions.create(model=model, messages=messages)
		text = resp.choices[0].message.content or ""
		print(color("Assistant:", "1;36", use_color), text, sep=" ")
		if show_usage and getattr(resp, "usage", None) is not None:
			pt = getattr(resp.usage, "prompt_tokens", None)
			ct = getattr(resp.usage, "completion_tokens", None)
			tt = getattr(resp.usage, "total_tokens", None)
			print(color(f"[usage] prompt={pt} completion={ct} total={tt}", "2;37", use_color))
		return 0


def run_repl(client: OpenAI, model: str, system_prompt: str, stream: bool, use_color: bool, show_usage: bool) -> int:
	print(color("AI Agent (type /exit to quit)", "1;33", use_color))
	if system_prompt:
		print(color("System prompt active.", "2;37", use_color))

	# Ask for user's name and load or create profile with persisted history
	store = load_store()
	while True:
		try:
			name = input(color("Введите ваше имя:", "1;35", use_color) + " ").strip()
		except (EOFError, KeyboardInterrupt):
			print()
			return 0
		if name:
			break
		print(color("Имя не должно быть пустым.", "1;31", use_color))

	profile = get_or_create_user_profile(store, name)
	save_store(store)

	chat_history: List[dict] = []
	# Always enforce language mirroring
	chat_history.append({"role": "system", "content": os.getenv("AGENT_LANG_HINT", "Always respond in the same language as the user's last message.")})
	if system_prompt:
		chat_history.append({"role": "system", "content": system_prompt})

	# Restore last up to 10 exchanges into current session
	if profile["history"]:
		chat_history.extend(profile["history"])
		print(color(f"Загружено {len(profile['history'])//2} прошлых диалогов для {profile['name']} (id: {profile['user_id']}).", "2;37", use_color))

	while True:
		try:
			user_input = input(color("You:", "1;34", use_color) + " ").strip()
		except (EOFError, KeyboardInterrupt):
			print()
			return 0

		if not user_input:
			continue
		if user_input.lower() in {"/exit", "/quit", ":q"}:
			return 0
		if user_input.lower() in {"/clear"}:
			chat_history = [{"role": "system", "content": system_prompt}] if system_prompt else []
			print(color("History cleared.", "2;37", use_color))
			continue

		chat_history.append({"role": "user", "content": user_input})

		if stream:
			print(color("Assistant:", "1;36", use_color), end=" ", flush=True)
			try:
				accumulated_parts: List[str] = []
				last_usage = None
				for chunk in client.chat.completions.create(model=model, messages=chat_history, stream=True):
					if not chunk.choices:
						continue
					if getattr(chunk, "usage", None) is not None:
						last_usage = chunk.usage
					delta = getattr(chunk.choices[0].delta, "content", None)
					if delta:
						accumulated_parts.append(delta)
						print(delta, end="", flush=True)
				print()
				full_text = "".join(accumulated_parts)
				chat_history.append({"role": "assistant", "content": full_text})
				# Persist this exchange
				append_exchange_and_persist(store, profile, user_input, full_text)
				if show_usage and last_usage is not None:
					pt = getattr(last_usage, "prompt_tokens", None)
					ct = getattr(last_usage, "completion_tokens", None)
					tt = getattr(last_usage, "total_tokens", None)
					print(color(f"[usage] prompt={pt} completion={ct} total={tt}", "2;37", use_color))
			except Exception as e:
				print()
				print(color(f"Error: {e}", "1;31", use_color), file=sys.stderr)
		else:
			resp = client.chat.completions.create(model=model, messages=chat_history)
			text = resp.choices[0].message.content or ""
			print(color("Assistant:", "1;36", use_color), text)
			chat_history.append({"role": "assistant", "content": text})
			append_exchange_and_persist(store, profile, user_input, text)
			if show_usage and getattr(resp, "usage", None) is not None:
				pt = getattr(resp.usage, "prompt_tokens", None)
				ct = getattr(resp.usage, "completion_tokens", None)
				tt = getattr(resp.usage, "total_tokens", None)
				print(color(f"[usage] prompt={pt} completion={ct} total={tt}", "2;37", use_color))

	return 0


def main() -> int:
	read_env()
	parser = make_parser()
	args = parser.parse_args()

	use_color = not args.no_color and sys.stdout.isatty()

	client = build_client()
	# Allow numeric selection via MODEL_MENU for --model or OPENAI_MODEL
	model = resolve_model_from_menu(args.model)
	system_prompt = args.system

	if args.message is not None:
		return run_single_message(client, model, system_prompt, args.message, args.stream, use_color, args.show_usage)
	return run_repl(client, model, system_prompt, args.stream, use_color, args.show_usage)


if __name__ == "__main__":
	sys.exit(main())

