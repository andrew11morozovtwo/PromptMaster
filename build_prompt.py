import sys
import os
from typing import Optional

try:
	from agent import read_env, build_client  # type: ignore
except Exception:
	read_env = None  # type: ignore
	build_client = None  # type: ignore

from prompt_builder import PromptBuilder


def ask(prompt: str, default: Optional[str] = None) -> str:
	while True:
		try:
			val = input(prompt).strip()
		except (EOFError, KeyboardInterrupt):
			print()
			sys.exit(0)
		if val:
			return val
		if default is not None:
			return default
		print("Поле не может быть пустым.")


def _infer_goal(user_input: str) -> str:
	ui = (user_input or "").lower()
	if any(k in ui for k in ["код", "програм", "скрипт", "function", "algorithm"]):
		return "code"
	if any(k in ui for k in ["перевод", "translate", "翻译"]):
		return "translation"
	if any(k in ui for k in ["анал", "analysis", "проанализируй"]):
		return "analysis"
	if any(k in ui for k in ["резюме", "summary", "summar"]):
		return "summary"
	if any(k in ui for k in ["вопрос", "ответ", "q&a"]):
		return "Q&A"
	if any(k in ui for k in ["творч", "creative", "story", "poem"]):
		return "creative"
	# default
	return "article"


def _default_fields_for_goal(goal: str):
	if goal == "code":
		return [
			{"key": "language", "question": "Какой язык программирования?", "example": "Python", "required": True},
			{"key": "io", "question": "Какой формат входа/выхода?", "example": "stdin/stdout", "required": True},
			{"key": "constraints", "question": "Ограничения/сложность?", "example": "O(n log n), без внешних библиотек", "required": False},
		]
	# article and default
	return [
		{"key": "length", "question": "Какой объём текста нужен?", "example": "1000-1500 слов", "required": True},
		{"key": "style", "question": "Какой стиль?", "example": "научпоп / разговорный / формальный", "required": True},
		{"key": "audience", "question": "Кто аудитория?", "example": "школьники / студенты / широкая аудитория", "required": True},
		{"key": "tone", "question": "Тональность?", "example": "воодушевляющий / нейтральный", "required": False},
	]


def _auto_extract_provided(user_input: str) -> dict:
	ui = (user_input or "").lower()
	res = {}
	# length
	import re
	m = re.search(r"(\d{2,5})\s*(слов|слова|словах|знак|знаков|characters|chars)", ui)
	if m:
		res["length"] = f"{m.group(1)} {m.group(2)}"
	elif "страниц" in ui or "страницы" in ui or "страницa" in ui:
		m2 = re.search(r"(\d+)\s*(страниц[аи]?)", ui)
		if m2:
			res["length"] = f"{m2.group(1)} {m2.group(2)}"
	# style
	if any(k in ui for k in ["научпоп", "науч-поп", "science-pop"]):
		res["style"] = "научпоп"
	elif any(k in ui for k in ["разговор", "conversational"]):
		res["style"] = "разговорный"
	elif "формаль" in ui:
		res["style"] = "формальный"
	# audience
	if "школьник" in ui:
		res["audience"] = "школьники"
	elif "студент" in ui:
		res["audience"] = "студенты"
	elif "преподават" in ui:
		res["audience"] = "преподаватели"
	elif "приемн" in ui:
		res["audience"] = "приемная комиссия"
	# tone
	if "нейтрал" in ui:
		res["tone"] = "нейтральный"
	elif "воодушев" in ui or "вдохнов" in ui:
		res["tone"] = "воодушевляющий"
	return res


def _is_supported_by_current_api(model_id: str) -> bool:
	# All PromptBuilder API calls now go to PROMPT_BUILDER_API_MODEL (e.g., gpt-4o),
	# while 'model_id' here is only the TARGET model for which we adapt the prompt.
	# Therefore, allow any selection.
	return True


def select_model(default_model: str = "gpt-4o-mini") -> str:
	# Allow overriding menu via .env: comma-separated model ids
	env_menu = os.getenv("MODEL_MENU", "")
	if env_menu.strip():
		models = [m.strip() for m in env_menu.split(",") if m.strip()]
	else:
		# Defaults include hints from .env (gpt-4o-mini, claude-3-5-sonnet, yandexgpt-lite, gigachat) + a few extras
		models = [
			"gpt-4o-mini",
			"gpt-4o",
			"claude-3-5-sonnet",
			"claude-3-5-haiku",
			"yandexgpt-lite",
			"yandexgpt-pro",
			"gigachat",
		]
	print("\nВыберите модель:")
	for idx, m in enumerate(models, start=1):
		tag = " (по умолчанию)" if m == default_model else ""
		print(f"  {idx}. {m}{tag}")
	print("  0. Другое (ввести вручную)")
	while True:
		choice = ask("Номер модели: ", None)
		if choice.isdigit():
			n = int(choice)
			if n == 0:
				manual = ask("Введите идентификатор модели вручную: ")
				if _is_supported_by_current_api(manual):
					return manual
				print("Эта модель, вероятно, не поддерживается текущим API. Выберите другую.")
				continue
			if 1 <= n <= len(models):
				selected = models[n - 1]
				if _is_supported_by_current_api(selected):
					return selected
				print("Эта модель, вероятно, не поддерживается текущим API. Выберите другую.")
				continue
		# Allow pressing Enter to accept default
		if choice == "" and default_model:
			if _is_supported_by_current_api(default_model):
				return default_model
			print("Модель по умолчанию не поддерживается текущим API. Выберите другую.")
			continue
		print("Введите номер из списка или 0 для ручного ввода.")


def confirm(prompt: str, default_yes: bool = True) -> bool:
	default_hint = "Y/n" if default_yes else "y/N"
	while True:
		try:
			val = input(f"{prompt} ({default_hint}): ").strip().lower()
		except (EOFError, KeyboardInterrupt):
			print()
			return False
		if not val:
			return default_yes
		if val in {"y", "yes", "д", "да"}:
			return True
		if val in {"n", "no", "н", "нет"}:
			return False
		print("Ответьте 'y' или 'n'.")


def main() -> int:
	if read_env:
		read_env()
	client = build_client() if build_client else None
	builder = PromptBuilder(client)

	user_input = ask("Опишите вашу задачу (1-2 предложения): ")
	model = select_model(default_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"))

	print("\n[1/4] Классификация цели...")
	class_data = builder.classify_goal(user_input, model=model)
	print(f"Результат: goal={class_data.get('goal')} | role={class_data.get('role')}")
	# Keep granular Q&A even if classifier misses schema
	goal = class_data.get("goal") or _infer_goal(user_input)
	role_detected = class_data.get("role")
	fields = class_data.get("fields") or _default_fields_for_goal(goal)
	provided = (class_data.get("provided") or {}) | _auto_extract_provided(user_input)
	collected = dict(provided)  # prefill with detected values from user input
	if fields:
		to_ask = [f for f in fields if f.get("key") not in collected]
		if to_ask:
			print("Требуемые параметры (уточним недостающие):")
			for f in to_ask:
				key = f.get("key")
				q = f.get("question") or f"Уточните {key}"
				ex = f.get("example")
				prompt_line = f"{q}" + (f" (напр.: {ex})" if ex else "")
				val = ask(prompt_line + "\n> ")
				collected[key] = val
	else:
		# Fallback: единый запрос деталей, если классификатор не дал схемы
		details_q = "Уточните недостающие детали задачи"
		val = ask(f"{details_q}\n> ")
		collected["details_freeform"] = val
	# Скомпилируем details-строку для следующих этапов
	if collected:
		details = "\n".join(f"{k}: {v}" for k, v in collected.items())
	else:
		details = ""
	print("[OK] Этап 1/4 завершён.")
	if not confirm("Продолжать?"):
		print("Остановлено пользователем.")
		return 0

	print("\n[2/4] Адаптация под модель...")
	adapter = builder.adapt_model(model=model, details=details, goal=goal, role=role_detected)
	print("Результат адаптера:", adapter)
	print("[OK] Этап 2/4 завершён.")
	if not confirm("Продолжать?"):
		print("Остановлено пользователем.")
		return 0

	print("\n[3/4] Генерация мета-промпта...")
	meta = builder.generate_final(details=details)
	final_prompt = (meta or {}).get("prompt", "") if isinstance(meta, dict) else ""
	print("\n=== Сформированный промпт ===\n")
	if final_prompt:
		print(final_prompt)
	else:
		print("[WARN] Модель не вернула поле 'prompt'. Ниже — сырой ответ JSON от этапа 3:")
		print(meta)
	print("\n=== Обоснование ===")
	print((meta or {}).get("why_good", ""))
	print("Качество:", (meta or {}).get("estimated_quality", ""))
	print("[OK] Этап 3/4 завершён.")
	if not confirm("Перейти к self-refine?"):
		print("Готово.")
		return 0

	print("\n[4/4] Self-refine...")
	try:
		ref = builder.self_refine(final_prompt, model=model)
		print("\n=== Улучшенная версия (self-refine) ===\n")
		print(ref.get("refined_prompt", ""))
		print("\nКритика:", ref.get("critique", ""))
		print("Улучшения:", ref.get("improvements", []))
		print("[OK] Этап 4/4 завершён.")
	except Exception as e:
		print("\n[WARN] Не удалось распарсить JSON от self-refine. Пропускаю этот шаг.")
		print("Причина:", e)
		print("[OK] Завершаем без self-refine.")

	print("\nГотово.")
	return 0


if __name__ == "__main__":
	sys.exit(main())

