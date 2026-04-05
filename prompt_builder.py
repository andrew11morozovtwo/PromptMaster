import os
import sys
import json
import re
from typing import Any, Dict, Optional

try:
	from openai import OpenAI  # type: ignore
except Exception:
	print("Missing dependency 'openai'. Install with: pip install -r requirements.txt", file=sys.stderr)
	raise

try:
	# Reuse env loading and client creation from agent.py if available
	from agent import read_env, build_client  # type: ignore
except Exception:
	read_env = None  # type: ignore
	build_client = None  # type: ignore


# ---- Prompt templates (can be overridden via ENV) ----
_GOAL_CLASSIFIER_PROMPT_DEFAULT = """
Ты классификатор целей для генерации промптов. Анализируй запрос: "{user_input}"

ВОЗМОЖНЫЕ ЦЕЛИ: article, code, analysis, creative, Q&A, translation, summary

Твоя задача:
1) Определи цель (goal) и предполагаемую роль автора (role).
2) Выдели необходимые параметры (fields) для качественного результата по этой цели.
3) Проанализируй исходный запрос пользователя и ЗАПОЛНИ те параметры, которые уже явно указаны, в объекте "provided" (ключ → значение как в запросе).
4) Сформируй "missing_keys" ТОЛЬКО из тех ключей, которые отсутствуют в "provided".

Верни ТОЛЬКО JSON:
{{
  "goal": "article",
  "role": "технический писатель",
  "fields": [
    {{"key": "length", "question": "Какой объём текста нужен?", "example": "1000-1500 слов", "required": true}},
    {{"key": "style", "question": "Какой стиль?", "example": "научпоп / разговорный / формальный", "required": true}},
    {{"key": "audience", "question": "Кто аудитория?", "example": "инженеры-студенты", "required": true}},
    {{"key": "tone", "question": "Тональность?", "example": "воодушевляющий / нейтральный", "required": false}}
  ],
  "provided": {{"length": "1000-1500 слов", "style": "научпоп"}},
  "missing_keys": ["audience"]
}}

Примеры:
"напиши код" → {{
  "goal": "code", "role": "программист",
  "fields": [
    {{"key": "language", "question": "Какой язык программирования?", "example": "Python", "required": true}},
    {{"key": "io", "question": "Какой формат входа/выхода?", "example": "stdin/stdout", "required": true}}
  ],
  "provided": {{}},
  "missing_keys": ["language","io"]
}}
"проанализируй" → {{
  "goal": "analysis", "role": "аналитик",
  "fields": [
    {{"key": "subject", "question": "Что именно анализировать?", "example": "отчёт продаж Q1", "required": true}},
    {{"key": "metrics", "question": "Какие метрики важны?", "example": "выручка, маржа", "required": false}}
  ],
  "provided": {{"metrics": "выручка, маржа"}},
  "missing_keys": ["subject"]
}}
Всегда отвечай на языке исходного запроса пользователя.
""".strip()


_MODEL_ADAPTER_PROMPT_DEFAULT = """
Ты адаптер промптов под модели ИИ. Для модели "{model}" определи:

- temperature (0.1-0.9)
- structure (CoT, XML, JSON, few-shot)
- language (ru/en)
- special features

Верни ТОЛЬКО JSON:
{{"temp": 0.3, "structure": "CoT+XML", "lang": "ru", "features": "use <thinking> tags"}}

Модели:
- gpt-4o-mini: temp 0.7, few-shot
- claude-3-5-sonnet: temp 0.3, CoT+XML  
- yandexgpt-lite: temp 0.5, русский
- gigachat: temp 0.4, формальный русский
 - qwen2.5-72b-instruct: temp 0.4, JSON, китайский/английский
 - glm-4: temp 0.4, CoT+JSON, формальный китайский
 - baichuan2-53b: temp 0.5, few-shot, китайский
 - yi-large: temp 0.4, CoT, китайский
 - deepseek-chat: temp 0.5, CoT+few-shot, краткость
 - moonshot-v1-8k: temp 0.3, CoT+XML, структурированность
Всегда отвечай на языке исходного запроса пользователя.
""".strip()


_META_GENERATOR_PROMPT_DEFAULT = """
Ты лучший в мире prompt engineer. Создай ИДЕАЛЬНЫЙ промпт для модели "{model}".

📋 Контекст пользователя:
Goal: {goal}
Детали: {details}
Роль: {role}
Адаптация: {adapter_instructions}

✅ Требования к промпту:
1. Роль + контекст + CoT (Think step-by-step)
2. Output format (JSON/Markdown/table)
3. Без примеров и без few-shot. НЕ добавляй секции с названиями "Пример", "Примеры", "Example(s)".
4. Длина 200–500 слов
5. {structure} структура из адаптера
6. Строго следуй теме и деталям из спецификации; не меняй тематику и формулировки пользователя.
7. Если тема присутствует в спецификации — ИСПОЛЬЗУЙ ЕЁ БЕЗ ИЗМЕНЕНИЙ. Никаких альтернативных тем.
8. Если адаптер предложил few-shot — проигнорируй это требование. Никаких примеров.

Верни ТОЛЬКО JSON:
{{"prompt": "You are {role}. {goal}...", "why_good": "3 причины почему этот промпт отличный", "estimated_quality": "8.5/10"}}
Всегда отвечай на языке исходного запроса пользователя и, если уместно, формируй промпт на том же языке.
""".strip()


_SELF_REFINE_PROMPT_DEFAULT = """
Ты критик промптов. Оцени промпт по шкале 1–10:

📊 Критерии (веса):
- Ясность инструкций (30%)
- Структура CoT/logic (25%) 
- Output format (20%)
- Few-shot примеры (15%)
- Модель-адаптация (10%)

Исходный промпт:
{original_prompt}

Верни ТОЛЬКО JSON:
{{"score": 8.2, "critique": "Что плохо + почему", "refined_prompt": "Улучшенная версия", "improvements": ["1", "2", "3"]}}

Улучшай ИТЕРАТИВНО, не ломай логику.
Всегда отвечай на языке исходного запроса пользователя. Поле refined_prompt ДОЛЖНО быть на этом же языке.
""".strip()

# Load overrides from environment (use utf-8-sig tolerant reading already set by agent.read_env)
GOAL_CLASSIFIER_PROMPT = os.getenv("GOAL_CLASSIFIER_PROMPT", _GOAL_CLASSIFIER_PROMPT_DEFAULT)
MODEL_ADAPTER_PROMPT = os.getenv("MODEL_ADAPTER_PROMPT", _MODEL_ADAPTER_PROMPT_DEFAULT)
META_GENERATOR_PROMPT = os.getenv("META_GENERATOR_PROMPT", _META_GENERATOR_PROMPT_DEFAULT)
SELF_REFINE_PROMPT = os.getenv("SELF_REFINE_PROMPT", _SELF_REFINE_PROMPT_DEFAULT)

# Role selection rules (can be overridden in .env as ROLE_RULES_HINT)
_ROLE_RULES_HINT_DEFAULT = """
Правила выбора роли (role):
- Если запрос о постах для каналов/соцсетей/телеграма/блога — роль: "администратор канала" (или "редактор канала").
- Если запрос о НАУЧНЫХ СТАТЬЯХ, исследовании, публикации — роль: "доцент" или "профессор" (выберите наиболее уместное).
- В противном случае подбирай профессиональную роль по контексту (не всегда "технический писатель").
""".strip()
ROLE_RULES_HINT = os.getenv("ROLE_RULES_HINT", _ROLE_RULES_HINT_DEFAULT)

class PromptBuilder:
	def __init__(self, client: Optional[OpenAI] = None):
		if client is None:
			if read_env:
				read_env()
			if build_client:
				client = build_client()
			else:
				api_key = os.getenv("OPENAI_API_KEY")
				base_url = os.getenv("OPENAI_BASE_URL", "https://api.proxyapi.ru/openai/v1")
				if not api_key:
					print("OPENAI_API_KEY is required for PromptBuilder.", file=sys.stderr)
					sys.exit(1)
				client = OpenAI(api_key=api_key, base_url=base_url)
		self.client = client
		self.data: Dict[str, Any] = {}
		# Fixed API model to run all calls against (default: gpt-4o)
		self.api_model = os.getenv("PROMPT_BUILDER_API_MODEL", os.getenv("OPENAI_MODEL", "gpt-4o"))

	def _detect_lang(self, text: str) -> str:
		# Simple heuristic: Cyrillic -> ru, else en
		if re.search(r"[А-Яа-яЁё]", text):
			return "ru"
		return "en"

	def _infer_role(self, user_input: str, current_role: Optional[str]) -> Optional[str]:
		text = (user_input or "").lower()
		if any(k in text for k in ["канал", "пост", "телеграм", "tg", "соцсет", "соц-сет"]):
			return "администратор канала"
		if any(k in text for k in ["науч", "исследован", "публикаци", "статья", "журнал", "peer review"]):
			# Prefer more academic role for scientific contexts
			return "доцент"
		return current_role

	def _extract_first_json_object(self, text: str) -> Optional[str]:
		# Try code fences first
		if text.strip().startswith("```"):
			parts = text.split("```")
			for part in parts:
				p = part.strip()
				if p.startswith("{") and p.endswith("}"):
					return p
		# Fallback: find first balanced {...}
		start_idxs = [m.start() for m in re.finditer(r"\{", text)]
		for start in start_idxs:
			depth = 0
			for i in range(start, len(text)):
				if text[i] == "{":
					depth += 1
				elif text[i] == "}":
					depth -= 1
					if depth == 0:
						return text[start : i + 1]
		return None

	def _parse_json(self, raw: str) -> Dict[str, Any]:
		raw = raw.strip()
		if not raw:
			return {}
		try:
			return json.loads(raw)
		except Exception:
			extracted = self._extract_first_json_object(raw)
			if extracted:
				return json.loads(extracted)
			# Last resort
			raise

	def classify_goal(self, user_input: str, model: str = "gpt-4o-mini") -> Dict[str, Any]:
		sys_prompt = GOAL_CLASSIFIER_PROMPT.format(user_input=user_input) + "\n\n" + ROLE_RULES_HINT
		lang = self._detect_lang(user_input)
		resp = self.client.chat.completions.create(
			model=self.api_model,
			messages=[
				{"role": "system", "content": sys_prompt},
				{"role": "user", "content": user_input},
			],
			temperature=0.1,
			max_tokens=200,
			response_format={"type": "json_object"},
		)
		data = self._parse_json(resp.choices[0].message.content or "{}")
		# Post-adjust role with a lightweight heuristic if model left default role
		role_before = data.get("role")
		inferred = self._infer_role(user_input, role_before)
		if inferred and inferred != role_before:
			data["role"] = inferred
		self.data.update(data)
		self.data.setdefault("original_input", user_input)
		self.data["lang"] = lang
		return data

	def adapt_model(self, model: str, details: Optional[str] = None, goal: Optional[str] = None, role: Optional[str] = None) -> Dict[str, Any]:
		sys_prompt = MODEL_ADAPTER_PROMPT.format(model=model)
		lang = self.data.get("lang", "en")
		# Build a brief spec to help the adapter synthesize stage-3 inputs
		spec_lines = []
		if goal:
			spec_lines.append(f"goal: {goal}")
		if role:
			spec_lines.append(f"role: {role}")
		if details:
			spec_lines.append(f"details: {details}")
		spec_text = "\n".join(spec_lines) if spec_lines else ""
		user_msg = (
			("Адаптируй под модель " if lang == "ru" else "Adapt for model ")
			+ model
			+ (". " + ("Используй спецификацию:\n" if lang == "ru" else "Use this spec:\n") + spec_text if spec_text else "")
		)
		resp = self.client.chat.completions.create(
			model=self.api_model,
			messages=[
				{"role": "system", "content": sys_prompt},
				{"role": "user", "content": user_msg},
			],
			temperature=0.2,
			max_tokens=450,
			response_format={"type": "json_object"},
		)
		data = self._parse_json(resp.choices[0].message.content or "{}")
		# Ensure container and place structured hints for stage 3
		adapter: Dict[str, Any] = dict(data)
		# Normalize nested structure for stage 3 consumption
		structured = adapter.get("structured") or adapter.get("stage3") or {}
		if not structured:
			# try to compose from flat keys if present
			keys = {
				"role_expert": adapter.get("role_expert") or role or self.data.get("role"),
				"task": adapter.get("task"),
				"context": adapter.get("context"),
				"format": adapter.get("format"),
				"style": adapter.get("style"),
				"example": adapter.get("example"),
			}
			structured = {k: v for k, v in keys.items() if v}
		adapter["structured"] = structured
		self.data["adapter"] = adapter
		self.data["model"] = model
		return adapter

	def generate_final(self, details: str) -> Dict[str, Any]:
		model = self.data.get("model", "gpt-4o-mini")  # target model for which we design the prompt
		goal = self.data.get("goal", "Q&A")
		role = self.data.get("role", "ассистент")
		adapter = self.data.get("adapter", {})
		structure = adapter.get("structure", "CoT")
		adapter_instructions = f"temp={adapter.get('temp')}, features={adapter.get('features')}, lang={adapter.get('lang')}"
		# Stage-2 synthesized scaffold for stage 3
		stage2_struct = adapter.get("structured") or {}
		# Build additional scaffold text block to pass to stage 3 (as context, not as examples)
		def _fmt(label: str, key: str) -> str:
			val = stage2_struct.get(key)
			return f"[{label}] {val}" if val else ""
		stage2_lines = [
			_fmt("Роль", "role_expert"),
			_fmt("Задача", "task"),
			_fmt("Контекст", "context"),
			_fmt("Формат", "format"),
			_fmt("Стиль", "style"),
			_fmt("Пример", "example"),  # this is a reference only; stage 3 still instructed to avoid few-shot
		]
		stage2_block = "\n".join([ln for ln in stage2_lines if ln])
		lang = self.data.get("lang", "en")
		original_input = self.data.get("original_input", "")

		meta_prompt = META_GENERATOR_PROMPT.format(
			model=model,
			goal=goal,
			details=(details + ("\n" + stage2_block if stage2_block else "")),
			role=role,
			adapter_instructions=adapter_instructions,
			structure=structure,
		)
		# Передаём спецификацию как отдельное сообщение для точного якоря темы
		spec = {
			"original_input": original_input,
			"goal": goal,
			"role": role,
			"adapter": adapter,
			"details": details,
		}
		spec_text = json.dumps(spec, ensure_ascii=False)
		resp = self.client.chat.completions.create(
			model=self.api_model,
			messages=[
				{"role": "system", "content": meta_prompt},
				{"role": "user", "content": ("Используй СТРОГО эту спецификацию (JSON): " if lang == "ru" else "Use STRICTLY this specification (JSON): ") + spec_text},
				{"role": "user", "content": ("Создай промпт по инструкциям выше без изменения темы." if lang == "ru" else "Create the prompt per the instructions above without changing the topic.")},
			],
			temperature=0.2,
			max_tokens=800,
			response_format={"type": "json_object"},
		)
		data = self._parse_json(resp.choices[0].message.content or "{}")
		self.data["meta"] = data
		return data

	def self_refine(self, original_prompt: str, model: Optional[str] = None) -> Dict[str, Any]:
		model = model or self.data.get("model", "gpt-4o-mini")  # target model only for context
		sys_prompt = SELF_REFINE_PROMPT.format(original_prompt=original_prompt)
		lang = self.data.get("lang", "en")
		resp = self.client.chat.completions.create(
			model=self.api_model,
			messages=[
				{"role": "system", "content": sys_prompt},
				{"role": "system", "content": ("Всегда отвечай строго на русском языке. Поле refined_prompt также на русском." if lang == "ru" else "Always answer strictly in English. The refined_prompt field must be in English.")},
				{"role": "user", "content": ("Оцени и улучшай итеративно. Ответ верни в JSON." if lang == "ru" else "Evaluate and iteratively improve. Return JSON.")},
			],
			temperature=0.2,
			max_tokens=600,
			response_format={"type": "json_object"},
		)
		data = self._parse_json(resp.choices[0].message.content or "{}")
		self.data["self_refine"] = data
		return data

