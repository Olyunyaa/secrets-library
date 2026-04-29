# Monthly Report Redesign — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Переработать `generate_monthly_report.py`: расширить матчинг (3 новых lookup-источника) и заменить 6 групп на 4 action-oriented группы.

**Architecture:** Два независимых изменения — (1) добавить `load_lookup_tabs()` и подключить к матчингу, (2) переписать `classify()` и обновить все зависимые функции. Каждое изменение атомарно и проверяется через `--dry-run`.

**Tech Stack:** Python 3, gspread, Telethon, argparse. Без тестового фреймворка — верификация через `--dry-run` и print-вывод.

---

## Task 1: Добавить `load_lookup_tabs()` — новые источники для матчинга

**Files:**
- Modify: `generate_monthly_report.py:368` (после `load_known_matches`)

**Step 1: Написать функцию `load_lookup_tabs()`**

Вставить после строки 374 (после `load_known_matches`):

```python
def load_lookup_tabs(gc, spreadsheet_id, prev_tab_name=None):
    """Build email↔username index from Антихрупкость, Резиденты, and prev Свод."""
    email_to_uname = {}  # email_lower → username_lower (no @)
    uname_to_email = {}  # username_lower → email_lower

    sh = gc.open_by_key(spreadsheet_id)

    def _add(email, uname):
        e = str(email).strip().lower()
        u = str(uname).strip().lower().lstrip("@")
        if e and u and "@" in e and "." in e.split("@")[-1] and len(u) >= 3:
            email_to_uname.setdefault(e, u)
            uname_to_email.setdefault(u, e)

    # Антихрупкость 2026 — no header row: detect email/username by pattern
    try:
        ws = sh.worksheet("Антихрупкость 2026")
        for row in ws.get_all_values():
            email = next((c for c in row if "@" in c and "." in c.split("@")[-1] and " " not in c), "")
            uname = next((c for c in row if c.startswith("@") and len(c) >= 4), "")
            if email and uname:
                _add(email, uname)
        print(f"  Антихрупкость: {len(email_to_uname)} пар email↔ник")
    except Exception as e:
        print(f"  ⚠️  Антихрупкость 2026: {e}")

    # Резиденты — has header. Only use: Имя, Почта, Телеграм ник columns.
    try:
        ws = sh.worksheet("Резиденты")
        records = ws.get_all_records()
        if records:
            sample = records[0]
            email_col = next((k for k in sample if "почт" in k.lower()), None)
            uname_col = next((k for k in sample if "телеграм" in k.lower() or "ник" in k.lower()), None)
            if email_col and uname_col:
                before = len(email_to_uname)
                for rec in records:
                    _add(rec.get(email_col, ""), rec.get(uname_col, ""))
                print(f"  Резиденты: +{len(email_to_uname) - before} новых пар (итого {len(email_to_uname)})")
            else:
                print(f"  ⚠️  Резиденты: не найдены колонки Почта/Ник. Есть: {list(sample.keys())}")
    except Exception as e:
        print(f"  ⚠️  Резиденты: {e}")

    # Предыдущий Свод — scan data rows for @username (col 1) + email (col 2)
    if prev_tab_name:
        try:
            ws = sh.worksheet(prev_tab_name)
            before = len(email_to_uname)
            for row in ws.get_all_values():
                if len(row) < 3:
                    continue
                uname_cell = row[1].strip()
                email_cell = row[2].strip()
                if uname_cell.startswith("@") and "@" in email_cell and "⚠️" not in email_cell:
                    _add(email_cell, uname_cell)
            print(f"  Предыдущий свод: +{len(email_to_uname) - before} новых пар (итого {len(email_to_uname)})")
        except Exception as e:
            print(f"  ⚠️  Предыдущий свод lookup: {e}")

    return email_to_uname, uname_to_email
```

**Step 2: Проверить синтаксис**

```bash
cd "/Users/olgaperova/Desktop/Ontrí Advisory/Ontri Проект для Секреты/"
python3 -c "import generate_monthly_report; print('OK')"
```
Ожидаем: `OK`

**Step 3: Commit**

```bash
git add generate_monthly_report.py
git commit -m "feat: add load_lookup_tabs() for Антихрупкость, Резиденты, prev Свод"
```

---

## Task 2: Интегрировать новые lookup-и в `match_stripe_to_members()`

**Files:**
- Modify: `generate_monthly_report.py:446` (функция `match_stripe_to_members`)

**Step 1: Добавить параметр `extra_email_to_uname` в сигнатуру**

Строка 446, изменить:
```python
def match_stripe_to_members(stripe_payments, members_by_uid, known_matches):
```
на:
```python
def match_stripe_to_members(stripe_payments, members_by_uid, known_matches, extra_email_to_uname=None):
```

**Step 2: Вставить стратегию 2b после блока `# 2. known_matches` (строка ~510)**

Вставить после блока known_matches (после `continue` на ~строке 516), перед `# 3. Username exactly`:

```python
        # 2b. Extra lookup tabs (Антихрупкость, Резиденты, prev Свод)
        if extra_email_to_uname and stripe_email in extra_email_to_uname:
            uname = extra_email_to_uname[stripe_email]
            if uname in username_to_uid:
                uid = username_to_uid[uname]
                matched[uid] = payment
                continue
```

**Step 3: Проверить синтаксис**

```bash
python3 -c "import generate_monthly_report; print('OK')"
```
Ожидаем: `OK`

**Step 4: Commit**

```bash
git add generate_monthly_report.py
git commit -m "feat: add extra lookup strategy 2b in match_stripe_to_members"
```

---

## Task 3: Переписать `classify()` — новая 4-группная логика

**Files:**
- Modify: `generate_monthly_report.py:614` (функция `classify`)

**Step 1: Заменить тело функции `classify()` целиком**

Строки 614–662, заменить на:

```python
def classify(in_list, in_ch, in_chat, in_prak, stripe_payment, season_start):
    has_access = in_ch or in_chat or in_prak

    stripe_paid = False
    stripe_failed = False
    if stripe_payment:
        s = stripe_payment["status"].lower()
        stripe_paid = (s == "paid")
        stripe_failed = (s == "failed")

    # Group 1 🔴: in any TG group without valid payment → remove
    if has_access and not stripe_paid:
        if stripe_failed:
            return 1, "🔴 Убрать", "Удалить — платёж не прошёл"
        return 1, "🔴 Убрать", "Удалить — нет оплаты"

    # Group 1 🔴: failed + no access at all
    if stripe_failed and not has_access:
        return 1, "🔴 Убрать", "Платёж не прошёл, доступа нет — помочь с оплатой?"

    # Group 2 🟡: paid but not in channel OR not in Vova's list
    if stripe_paid and (not in_ch or not in_list):
        actions = []
        if not in_ch:
            actions.append("добавить в канал")
        if not in_list:
            actions.append("добавить в список Salebot")
        return 2, "🟡 Вове", f"Вова: {', '.join(actions)}"

    # Group 3 🟠: paid + channel + list + missing chat or practice
    if stripe_paid and in_ch and in_list:
        if not (in_chat and in_prak):
            missing = []
            if not in_chat:
                missing.append("Чат")
            if not in_prak:
                missing.append("Практика")
            return 3, "🟠 Неполный доступ", f"Нет в: {', '.join(missing)}"
        return 4, "✅ Всё ОК", ""

    # Fallback: in_list but no payment and no TG access — unusual
    return 1, "🔴 Разобраться", "Проверить вручную"
```

**Step 2: Убедиться, что синтаксис верный**

```bash
python3 -c "import generate_monthly_report; print('OK')"
```
Ожидаем: `OK`

**Step 3: Быстрая проверка логики — вставить в конец файла (временно) и запустить**

```python
# temp check — удалить после проверки
if __name__ == "__test__":
    from generate_monthly_report import classify
    assert classify(True, False, False, False, None, "2026-04-23") == (1, "🔴 Убрать", "Удалить — нет оплаты")
    assert classify(True, True, True, True, {"status": "Paid"}, "2026-04-23") == (4, "✅ Всё ОК", "")
    assert classify(True, True, False, False, {"status": "Paid"}, "2026-04-23") == (3, "🟠 Неполный доступ", "Нет в: Чат, Практика")
    assert classify(True, False, True, True, {"status": "Paid"}, "2026-04-23") == (2, "🟡 Вове", "Вова: добавить в канал")
    print("classify() assertions passed")
```

```bash
python3 -c "
from generate_monthly_report import classify
assert classify(True, False, False, False, None, '2026-04-23') == (1, '🔴 Убрать', 'Удалить — нет оплаты')
assert classify(True, True, True, True, {'status': 'Paid'}, '2026-04-23') == (4, '✅ Всё ОК', '')
assert classify(True, True, False, False, {'status': 'Paid'}, '2026-04-23') == (3, '🟠 Неполный доступ', 'Нет в: Чат, Практика')
assert classify(True, False, True, True, {'status': 'Paid'}, '2026-04-23') == (2, '🟡 Вове', 'Вова: добавить в канал')
print('classify() assertions passed')
"
```
Ожидаем: `classify() assertions passed`

**Step 4: Commit**

```bash
git add generate_monthly_report.py
git commit -m "feat: rewrite classify() with 4-group action-oriented logic"
```

---

## Task 4: Обновить `SECTION_HEADERS`, `SECTION_COLORS`

**Files:**
- Modify: `generate_monthly_report.py:892` (константы `SECTION_HEADERS`, `SECTION_COLORS`)

**Step 1: Заменить оба словаря**

Строки 892–908, заменить на:

```python
SECTION_HEADERS = {
    1: "🔴 УБРАТЬ ИЗ КЛУБА — НЕТ ОПЛАТЫ / ПЛАТЁЖ НЕ ПРОШЁЛ",
    2: "🟡 ВОВЕ — НУЖНО РАЗОБРАТЬСЯ (оплата есть, нет канала или списка)",
    3: "🟠 НЕПОЛНЫЙ ДОСТУП — нет в Чате/Практике (может быть выбором)",
    4: "✅ ВСЁ ОК",
}

SECTION_COLORS = {
    1: {"red": 0.96, "green": 0.78, "blue": 0.78},   # red
    2: {"red": 1.00, "green": 0.97, "blue": 0.68},   # yellow
    3: {"red": 1.00, "green": 0.89, "blue": 0.68},   # orange
    4: {"red": 0.76, "green": 0.93, "blue": 0.76},   # green
}
```

**Step 2: Проверить синтаксис**

```bash
python3 -c "import generate_monthly_report; print('OK')"
```

**Step 3: Commit**

```bash
git add generate_monthly_report.py
git commit -m "feat: update SECTION_HEADERS and SECTION_COLORS for 4-group layout"
```

---

## Task 5: Обновить `build_analytics()` — Action Items под новую группировку

**Files:**
- Modify: `generate_monthly_report.py:789` (функция `build_analytics`)

**Step 1: Заменить блок `# Action items` (строки ~811–863)**

Найти строку `# Action items` и заменить весь блок action items до `return {`:

```python
    # Action items
    actions = []

    # 1. Удалить из клуба — группа 1 (для подтверждения Таней)
    to_remove = [r for r in rows if r["group"] == 1]
    no_payment = [r for r in to_remove if "нет оплаты" in r["action"].lower()]
    failed = [r for r in to_remove if "платёж не прошёл" in r["action"].lower()]
    if no_payment:
        names = ", ".join(r["name"] for r in no_payment)
        actions.append(f"🔴 Удалить из клуба (нет оплаты): {names}")
    if failed:
        names = ", ".join(r["name"] for r in failed)
        actions.append(f"🔴 Удалить из клуба (FAILED платёж): {names}")

    # 2. Вове — добавить в канал
    add_to_ch = [r for r in rows if r["group"] == 2 and not r["in_channel"]]
    if add_to_ch:
        names = ", ".join(r["name"] for r in add_to_ch)
        actions.append(f"🟡 Вова: добавить в канал: {names}")

    # 3. Вове — добавить в список Salebot
    add_to_list = [r for r in rows if r["group"] == 2 and not r["in_list"]]
    if add_to_list:
        names = ", ".join(r["name"] for r in add_to_list)
        actions.append(f"🟡 Вова: добавить в список Salebot: {names}")

    # 4. Вове — идентифицировать непривязанные Stripe Paid
    if unmatched:
        for p in unmatched:
            if p["status"].lower() == "paid":
                actions.append(f"❓ Вова: идентифицировать Stripe Paid: {p['email']} ({p['amount']:.0f} GBP, {p['date']})")

    # 5. Обновить known_matches.json — нечёткие матчи ⚠️
    if uncertain_count:
        uncertain_rows = [r for r in rows if r["email_uncertain"]]
        for r in uncertain_rows:
            actions.append(f"⚠️ Подтвердить матч: {r['username']} ↔ {r['email'].replace('⚠️ ', '')}")

    # 6. Обновить tanya_agreements.json — истекающие/истёкшие договорённости
    expiring = [r for r in rows if r.get("agreement_state") == "expiring"]
    if expiring:
        names = ", ".join(f"{r['name']} ({r['username']})" for r in expiring)
        actions.append(f"⏰ Договорённость истекает — уточнить у Тани: {names}")

    expired = [r for r in rows if r.get("agreement_state") == "expired"]
    if expired:
        names = ", ".join(f"{r['name']} ({r['username']})" for r in expired)
        actions.append(f"❌ Договорённость истекла — обновить tanya_agreements.json: {names}")

    stripe_also = [r for r in rows if r.get("agreement_state") == "stripe_also"]
    if stripe_also:
        names = ", ".join(f"{r['name']} ({r['username']}): {r['stripe']}" for r in stripe_also)
        actions.append(f"💰 Договорённость + Stripe — проверить у Тани: {names}")
```

**Step 2: Обновить `group_counts` в `return` — убрать старые группы 5, 6**

В блоке `return {` строка `"group_counts": group_counts` остаётся как есть — она считает автоматически, просто теперь будут только группы 1–4.

**Step 3: Проверить синтаксис**

```bash
python3 -c "import generate_monthly_report; print('OK')"
```

**Step 4: Commit**

```bash
git add generate_monthly_report.py
git commit -m "feat: update build_analytics action items for 4-group layout"
```

---

## Task 6: Подключить всё в `main()`

**Files:**
- Modify: `generate_monthly_report.py:1122` (функция `main`)

**Step 1: Вызвать `load_lookup_tabs()` после загрузки Google Sheets (после Step 1 в main)**

После строки `print("   ✓ Подключено")` добавить:

```python
    # Step 1b: Load extra lookup tabs for matching
    print("\n1b. Загружаю lookup-таблицы (Антихрупкость, Резиденты, предыдущий Свод)...")
    extra_email_to_uname, extra_uname_to_email = load_lookup_tabs(gc, SPREADSHEET_ID, prev_tab)
    print(f"   ✓ {len(extra_email_to_uname)} пар email↔ник из дополнительных источников")
```

**Step 2: Передать `extra_email_to_uname` в `match_stripe_to_members`**

Найти строку (≈1239):
```python
    stripe_matched, stripe_uncertain, stripe_unmatched = match_stripe_to_members(
        stripe_payments, all_members_for_diff, known_matches
    )
```

Заменить на:
```python
    stripe_matched, stripe_uncertain, stripe_unmatched = match_stripe_to_members(
        stripe_payments, all_members_for_diff, known_matches,
        extra_email_to_uname=extra_email_to_uname
    )
```

**Step 3: Проверить синтаксис**

```bash
python3 -c "import generate_monthly_report; print('OK')"
```

**Step 4: Сделать dry-run (Telegram сессия должна быть активна)**

```bash
cd "/Users/olgaperova/Desktop/Ontrí Advisory/Ontri Проект для Секреты/"
python3 generate_monthly_report.py \
  --season-start 2026-04-23 \
  --month "Май 2026" \
  --stripe-tab "Stripe Apr 23" \
  --prev-tab "Свод участников и оплат 23.03.26-23.04.26" \
  --dry-run
```

Проверить в выводе:
- `Антихрупкость:` — показывает количество пар
- `Резиденты:` — показывает количество пар
- `Предыдущий свод:` — показывает количество пар
- Группы в dry-run: только 🔴/🟡/🟠/✅ (не должно быть старых номеров 5, 6)
- Action items содержат `🔴 Удалить`, `🟡 Вова:`, `❓ Вова: идентифицировать`

**Step 5: Commit**

```bash
git add generate_monthly_report.py
git commit -m "feat: wire load_lookup_tabs and extra_email_to_uname into main()"
```

---

## Финальная проверка

После всех задач:

```bash
python3 -c "
import generate_monthly_report as m
# Проверить classify()
assert m.classify(True, False, False, False, None, '2026-04-23')[0] == 1
assert m.classify(True, True, True, True, {'status': 'Paid'}, '2026-04-23')[0] == 4
assert m.classify(True, True, False, False, {'status': 'Paid'}, '2026-04-23')[0] == 3
assert m.classify(True, False, True, True, {'status': 'Paid'}, '2026-04-23')[0] == 2
# Проверить SECTION_HEADERS
assert 4 in m.SECTION_HEADERS
assert 5 not in m.SECTION_HEADERS
assert 6 not in m.SECTION_HEADERS
print('Все проверки пройдены ✅')
"
```

Ожидаем: `Все проверки пройдены ✅`
