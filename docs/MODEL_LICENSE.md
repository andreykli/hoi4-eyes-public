# Public model profile

Default: **qwen3:4b-instruct**, through local Ollama, with thinking disabled.
License: **Apache-2.0**, verified in the installed tag and the upstream
Qwen3-4B-Instruct-2507 license. Exact digest and license hash are recorded in
release/model_profile.json. No model weights or inference service are bundled.
Ollama enforces the JSON Schema; Python validates identity, directions and votes.
Python derives the aggregate status from validated votes; it never changes them.
Canonical contradictory statuses are still rejected. Two bounded attempts remain.

The former qwen2.5-coder:3b has Qwen Research License, limited to research/evaluation.
It is not the public default. Its local weights and previous configuration are
retained for explicit rollback within those terms; no automatic fallback to it.
An optional environment override is the user's own local configuration, not a
claim that every third-party model has this release's license clearance.

RU: публичный профиль использует qwen3:4b-instruct, Apache-2.0. Старая 3B-модель
ограничена исследованием/оценкой; она сохранена локально, но не выбирается
автоматически. Откат: перед запуском EYES явно задайте OLLAMA_MODEL нужному
локальному тегу, соблюдая его лицензию. Очистка переменной возвращает новый
публичный профиль. Платные API и внешняя модель не подключались.

FR : modèle public qwen3:4b-instruct, Apache-2.0, via Ollama local, sans réflexion.
L’ancien modèle 3B reste disponible localement pour une évaluation explicite,
pas comme remplacement automatique. Les poids ne sont pas distribués.

Primary sources:
- https://ollama.com/library/qwen3:4b-instruct
- https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507/blob/main/LICENSE
- https://huggingface.co/Qwen/Qwen2.5-Coder-3B-Instruct/blob/main/LICENSE
