# Только синтетические примеры

Эти пять JSON предназначены для Argon и тестов backend: POST accepted, GET processing,
failed, done/no_violation, done/violation с двумя событиями и 1/3 JPEG-кадрами.
Вымышленная длительность — 5 секунд. JPEG нарисован программой, содержит надпись
SYNTHETIC FIXTURE и не взят из банковского видео. example.invalid — заведомо
тестовый источник, не правовое основание. В каталог правил он не включается.

Не импортировать fixtures или генератор из runtime. Сервис не реализован, demo endpoint
не создан. Перегенерация: `python scripts/make_contract_examples.py`.
