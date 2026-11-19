# Спецификация шаблонов HOI4-EYES

Каждый шаблон — одна запись JSON-массива в файле `tools/templates/<файл>.json`.
Файл — массив объектов, UTF-8 без BOM.

## Поля

| Поле | Тип | Правила |
|---|---|---|
| `id` | string | `^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*\.\d{3}$`, например `accept.friendly.001`; уникален во всём каталоге |
| `category` | string | одна из: `acceptance`, `rejection`, `counteroffer`, `world_news`, `third_country`, `memory` |
| `subcategory` | string | короткий смысловой ярлык темы: `security`, `trade`, `territory`, `alliance`, `protocol`, `sovereignty`, `economy`, `crisis`, `general` |
| `tone` | string | одна из: `friendly`, `neutral`, `cautious`, `distrustful`, `cold`, `solemn`, `compromising`, `firm`, `disappointed`, `conciliatory` |
| `statuses` | array[string] | допустимые статусы, только из: `proposal`, `accept`, `refuse`, `counter_offer`, `partial_accept`, `defer`, `clarify`, `execution_confirmed`, `execution_verified`, `evidence_pending`, `not_factual` |
| `placeholders` | array[string] | ровно те плейсхолдеры, что встречаются в тексте; только из: `{COUNTRY_A}`, `{COUNTRY_B}`, `{COUNTRY_C}`, `{TREATY_TYPE}`, `{DATE}`, `{CONDITIONS}`, `{TERMS}`, `{TOPIC}`, `{ACTION}` |
| `ru` | string | русский текст, 12–260 символов, без переносов строк, без двойных пробелов |
| `en` | string | английский текст, 12–260 символов, без переносов строк, без двойных пробелов |

## Смысловые правила

1. RU и EN — один и тот же смысл, одна и та же степень уверенности. Нельзя, чтобы EN обещал больше.
2. Тон должен быть реально слышен: `firm` не может звучать как `friendly`.
3. `acceptance` — только согласие; `rejection` — только отказ; `counteroffer` — только
   встречное предложение или компромисс. Никакого смешения.
4. Никаких выдуманных фактов: не писать «война закончилась», «войска выведены», «санкции сняты»,
   если это не следует из места шаблона. Только дипломатическая позиция и намерение.
5. Никаких имён лидеров, дат, чисел, если они не в плейсхолдере.
6. `world_news` — заголовок-стиль: короткое утверждение о событии, без обращения к игроку.
   Обязательно используйте `{COUNTRY_A}` и `{COUNTRY_B}` там, где речь о двух сторонах.
7. `third_country` — говорит третья страна: `{COUNTRY_A}` — она, `{COUNTRY_B}` и `{COUNTRY_C}` —
   стороны договора. Оценка должна опираться на интерес, а не на «мы всегда против».
8. `memory` — отсылка к прошлым переговорам: «в прошлый раз», «повторно», «после вашего отказа».
9. Запрещены шаблоны, обещающие исполнение: «договор вступил в силу», «эффект применён».
   Для этого есть отдельные статусы.

## Разнообразие

- Не повторяйте зачин: если 20 шаблонов начинаются с «Наше правительство», это плохо.
- Меняйте синтаксис: прямые заявления, осторожные конструкции, риторические вопросы,
  безличные формулы, короткие и длинные фразы.
- Внутри одной категории допускается не более 2 шаблонов с одинаковым первым словом на 50 записей.
- Никаких двух записей с одинаковым нормализованным текстом (без учёта плейсхолдеров).

## Пример

```json
{
  "id": "accept.friendly.001",
  "category": "acceptance",
  "subcategory": "security",
  "tone": "friendly",
  "statuses": ["accept"],
  "placeholders": ["{COUNTRY_A}", "{COUNTRY_B}", "{TREATY_TYPE}"],
  "ru": "Наше правительство принимает условия {TREATY_TYPE}. Мы рассчитываем, что это укрепит стабильность между {COUNTRY_A} и {COUNTRY_B}.",
  "en": "Our government accepts the terms of the {TREATY_TYPE}. We expect this to strengthen stability between {COUNTRY_A} and {COUNTRY_B}."
}
```
