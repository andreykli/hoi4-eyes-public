# HOI4-EYES 0.9.0-beta.1 — First Public Beta

By **NightPulse** · https://github.com/andreykli/hoi4-eyes-public

## English


Negotiate with countries and consult your government using local Ollama.
Includes diplomatic chats/conferences, exact treaty revisions and confirmations,
guarded game packages, Bridge result checking, diplomatic memory, Atlas,
Recognition Atlas, Country Maker, meetings and fleet planning templates.

Requirements: Windows, Python 3.10+, WebView2 Runtime, local Ollama with
qwen3:4b-instruct, a legal HOI4 installation. Target game version: **1.19.5.0**.
No paid AI API required. Models and Python dependencies install separately.

Extract Windows-source to a writable folder, run setup_windows.bat once, then
start.bat. Enable one EYES mod entry and start with a disposable campaign.
Workshop contains game scripts only; it cannot supply the AI companion by itself.

This is a beta: signed/prepared/sent does not mean executed. Bridge must confirm
the game effect. Some script installations require closing/restarting HOI4.
Not every negotiated idea is executable. Unknown geometry blocks territory actions;
fleet templates and many political discussions remain advisory.

Historical development evidence: 1408 automated tests and 8 distribution diagnostics
passed; an earlier isolated HOI4 QA achieved 21/21 with Bridge 21/21. These do not
certify every public-package route, map mod, DLC combination or visual screen.
See docs/BETA_ACCEPTANCE.md before playing an important campaign.

## Русский


Первая публичная beta от **NightPulse**: локальная дипломатия с Ollama,
конференции, точные редакции договоров, согласования, проверяемые игровые пакеты,
память, Atlas, Country Maker, совещания и морские шаблоны.

Нужны Windows, Python 3.10+, WebView2, Ollama с qwen3:4b-instruct и HOI4.
Целевая версия игры — **1.19.5.0**. Платные API не нужны. Распакуйте Windows-source,
запустите setup_windows.bat, затем start.bat. Включайте только одну копию мода.
Начинайте с отдельной тестовой кампании. Подписка Workshop не заменяет приложение.

Согласование не означает исполнение: нужен подтверждённый Bridge игровой эффект.
Некоторым пакетам требуется перезапуск. Не все предложения исполнимы; часть
политических и морских функций работает как планирование внутри EYES.
Результаты 1408 тестов и прежнего игрового QA 21/21 не заменяют приёмку всех
текущих экранов и модифицированных карт. См. docs/BETA_ACCEPTANCE.md.

## Français


Première beta publique par **NightPulse** : diplomatie locale avec Ollama,
conférences, révisions précises, confirmations, actions contrôlées, mémoire,
Atlas, Country Maker, réunions et modèles de planification navale.

Windows, Python 3.10+, WebView2, Ollama qwen3:4b-instruct et HOI4 sont requis.
Cible : **1.19.5.0**. Extraire Windows-source, lancer setup_windows.bat puis
start.bat. Une seule copie EYES active ; commencer par une campagne jetable.
L’abonnement Workshop ne remplace pas l’application.

La signature n’est pas une preuve d’exécution. Le Bridge doit vérifier l’effet.
Certains scripts nécessitent un redémarrage ; toutes les propositions ne sont
pas exécutables. Le QA historique ne certifie pas tous les écrans ou mods de carte.

## License and assets

Apache-2.0 for original EYES code; retain applicable NOTICE attribution.
Third-party notices/licenses remain separate. Preserve CREDITS and attribution.
Windows-source is a Python source package, not a signed standalone EXE.
Archive SHA-256 values are in SHA256SUMS.txt; verify before extraction.
No personal profile, save, conversation database or model weights are included.

Original Steam Workshop: https://steamcommunity.com/sharedfiles/filedetails/?id=3842411221
