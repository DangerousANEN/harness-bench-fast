# Руководство по чистой установке, запуску и тестированию харнессов

Это руководство содержит простые инструкции по локальному развертыванию веб-панели (без GigaChat и лишних зависимостей) и примеры тестирования различных ИИ-агентов (harnesses) через этот бенчмарк.

---

## 1. Чистая установка и запуск

Бенчмарк написан на Python 3.12+ и Next.js. Для локального запуска веб-панели используйте один из двух способов.

### Способ A: Через Docker Compose (Самый простой, не требует установки Python/Node.js)

1. Клонируйте репозиторий (если еще не клонирован):
   ```bash
   git clone https://github.com/DangerousANEN/harness-bench-fast.git
   cd harness-bench-fast
   ```
2. Поднимите стек одной командой:
   ```bash
   docker-compose up --build
   ```
3. Откройте панель в браузере: `http://localhost:8765`.

### Способ B: Локально (в виртуальном окружении)

Для сборки и запуска без Docker вам понадобятся:
- **Python 3.12+** (рекомендуется использовать быстрый пакетный менеджер `uv` или обычный `pip`)
- **Node.js 20+** (для сборки фронтенда)

1. **Подготовка окружения и установка Python зависимостей**:
   ```bash
   # Создаем venv и активируем его
   python -m venv .venv
   source .venv/bin/activate  # На Windows: .venv\Scripts\activate

   # Устанавливаем проект и пакеты веб-панели
   pip install -e ".[web,openrouter]"
   ```

2. **Сборка фронтенда**:
   ```bash
   cd frontend
   npm install --legacy-peer-deps
   npm run build
   cd ..
   ```

3. **Запуск веб-панели (FastAPI)**:
   ```bash
   # Запуск Uvicorn-сервера (он автоматически подхватит скомпилированный Next.js)
   uvicorn web.main:app --host 127.0.0.1 --port 8765 --reload
   ```
   Откройте `http://localhost:8765`.

---

## 2. Руководство по тестированию ИИ-агентов (Harnesses)

Бенчмарк запускает тест-кейсы, создавая изолированную временную директорию (workspace) для каждого теста, подготавливает входные файлы и передает задачу агенту. 

Для тестирования сторонних CLI-агентов используется режим `run-cli`. Бенчмарк выполняет указанную shell-команду, добавляя в конец промпт задачи в кавычках.

### 1. Claude Code (`claudecode`)
Официальный консольный агент от Anthropic.
- **Команда для CLI**:
  ```bash
  uv run python -m harness_bench run-cli \
      --cli-command "claude -y --dangerously-skip-permissions" \
      --concurrency 3
  ```
- **Настройка**: Убедитесь, что токен `ANTHROPIC_API_KEY` экспортирован в окружение. Флаг `-y` автоматически соглашается на выполнение команд.

### 2. Hermes CLI (`hermes`)
- **Команда для CLI**:
  ```bash
  uv run python -m harness_bench run-cli \
      --cli-command "hermes run" \
      --concurrency 5
  ```
- **Настройка**: hermes должен быть установлен глобально в системе и настроен на работу в текущей директории.

### 3. DeepAgents (`deepagents`)
Вы можете запустить встроенный runner DeepAgents через сторонние модели без использования GigaChat-профайлов (используя OpenRouter).
- **Команда для CLI (через OpenRouter)**:
  ```bash
  uv run python -m harness_bench run-openrouter \
      --model deepseek/deepseek-v4-flash \
      --concurrency 5
  ```
- **Настройка**: Требуется экспортировать `OPENROUTER_API_KEY` в окружение (или прописать в `.env`).

### 4. OpenCode (`opencode`)
Для тестирования локальных или API моделей через OpenCode:
- **Команда для CLI**:
  ```bash
  uv run python -m harness_bench run-cli \
      --cli-command "opencode run -m vllm/qwen3.6-27b" \
      --timeout 900
  ```
- **Настройка**: Для точного тестирования отключите автоформатирование кода (LSP/Formatters) в настройках OpenCode, чтобы тесты на посимвольное совпадение не падали из-за изменения стилей кавычек и отступов.

### 5. Codex CLI (`codex`)
- **Команда для CLI**:
  ```bash
  uv run python -m harness_bench run-cli \
      --cli-command "codex exec -m gpt-5.5 --dangerously-bypass-approvals-and-sandbox" \
      --json-output results.json
  ```
- **Настройка**: Флаг `--json-output` позволяет сохранять промежуточные результаты после каждого выполненного теста.

### 6. OpenClaw (`openclaw`)
- **Команда для CLI**:
  ```bash
  uv run python -m harness_bench run-cli \
      --cli-command "openclaw" \
      --concurrency 5
  ```

### 7. PiClaw (`piclaw`)
- **Команда для CLI**:
  ```bash
  uv run python -m harness_bench run-cli \
      --cli-command "piclaw" \
      --concurrency 4
  ```

---

## Как это выглядит в веб-панели

При запуске нового прогона (Run) через панель:
1. Выберите в выпадающем списке **Harness Type**: `CLI Runner (Shell command)`.
2. В поле **CLI Command template** введите шаблон запуска вашего агента (например, `claude -y --dangerously-skip-permissions` или `opencode run -m vllm/qwen3.6-27b`).
3. При необходимости укажите переменные окружения в блоке **Env Variables Override (JSON)**, например:
   ```json
   {
     "OPENAI_API_KEY": "your-key-here",
     "ANTHROPIC_API_KEY": "your-key-here"
   }
   ```
4. Нажмите **LAUNCH RUN**. Вкладка переключится в режим реального времени, где по вебсокету будет отображаться лог выполнения каждой задачи, затраченное время, шаги и токены.
