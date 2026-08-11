# Регламент выгрузки данных из CRM (Zymyran) в TorgStore Analytics

Этот документ фиксирует ТОЧНУЮ процедуру ежемесячной выгрузки, чтобы больше не
натыкаться на ошибки конверсии/данных, разобранные 26.07.2026. Два независимых
источника данных, которые никогда не пересекаются в БД:

| Источник | Что несёт | Куда пишется | Эндпоинт импорта |
|---|---|---|---|
| Сквозная аналитика CRM (UTM/реклама) | лиды, сделки, конверсия, разбивка по источникам | `sessions` / `session_totals` / `source_rows` | `POST /api/v1/import/tree-json` |
| Накладные (касса/отгрузка) | реальные деньги по факту продажи, не привязаны к источнику | `invoice_stats` | `POST /api/v1/invoices/import` |

Общая сумма продаж на сайте = накладные (если есть срез за период), реклама —
только подкомпонент разбивки. Конверсия/лиды/источники всегда считаются от
рекламной ветки (накладные для этого физически не приспособлены).

---

## 0. ⚠️ ОБЯЗАТЕЛЬНО ПЕРЕД ЛЮБЫМ ЗАПРОСОМ К CRM — правило 3 секунд

26.07.2026 массовая выгрузка без паузы (~5 запросов/сек к zymyran.com) была
расценена сотрудниками как атака — доступ заблокировали. Правило жёсткое:
**минимум 3 секунды между ЛЮБЫМИ запросами к torgstore.zymyran.com**, и оно
не должно зависеть от того, помнит агент об этом в моменте или нет.

### История двух проваленных попыток (важно прочитать перед тем, как трогать этот раздел)

- **v1 (localStorage-guard, до 08.08.2026)** — перехватывал `window.fetch`/
  `XMLHttpRequest`, проверял last-timestamp в localStorage. Имел race
  condition при параллельных вызовах (`Promise.all`): 08.08.2026
  автоматический прогон уложил 119 запросов (17 менеджеров × 7 складов) в
  137 секунд вместо минимум ~357с — **реальное нарушение правила на проде**.
- **v2 (promise-chain queue, 08.08.2026, тот же день)** — попытка исправить
  v1 настоящей последовательной очередью. Прошла изолированный тест (без
  реальных сетевых вызовов, все паузы ≥3.0с), но **ПРОВАЛИЛА живой тест
  против настоящего CRM в тот же день**: guard был вооружён в одном вызове
  `javascript_tool`, реальные запросы (`Promise.all` по 7 shipment_point,
  manager_id=566) — в следующем отдельном вызове. Итог: все 7 запросов ушли
  за 0.331 секунды (минимальный интервал 0.003с) вместо требуемых ≥18с.
  Причина, предположительно — переопределение встроенных `window.fetch`/
  `XMLHttpRequest` не переживает границу между отдельными вызовами
  `javascript_tool` (в отличие от обычных именованных функций/свойств на
  `window`, которые, по наблюдениям, переживают).

**Вывод: перехват (monkey-patching) `window.fetch`/`XMLHttpRequest` признан
НЕНАДЁЖНЫМ способом гарантировать паузу и БОЛЬШЕ НЕ используется нигде в
этом документе.**

### v3 (текущий, действующий метод) — явная пауза внутри самой функции запроса

Вместо перехвата глобальных объектов — обычная функция с ЖЁСТКО зашитой
паузой 3с ПЕРЕД КАЖДЫМ запросом, без исключений (в том числе перед первым
запросом в новом вызове `javascript_tool`). Гарантия строится на голой
JS-семантике `await`/`setTimeout` внутри одного выполнения скрипта — не
зависит ни от какого состояния, которое должно "пережить" границу между
вызовами инструмента:

```js
async function crmGet(url){
  await new Promise(r => setTimeout(r, 3000)); // ВСЕГДА первым делом — без исключений, без пропуска первого запроса, без проверки "прошло ли уже 3с"
  const r = await fetch(url, {headers:{'Accept':'application/json','X-Requested-With':'XMLHttpRequest'}});
  return await r.json();
}
```

**Правила использования — обязательны:**
1. Все запросы к CRM идут ТОЛЬКО через `crmGet(url)`, никогда напрямую через
   `fetch`.
2. Только `for...of` с `await crmGet(...)` внутри цикла. **НИКОГДА**
   `Promise.all`/`Promise.race`/`.map()` без последовательного await —
   параллельный запуск сломает паузу даже в v3, потому что каждый вызов
   независимо ждёт свои 3с и потом все fetch уходят почти одновременно.
3. Функцию `crmGet` копировать целиком в НАЧАЛО каждого отдельного вызова
   `javascript_tool`, где будут запросы к CRM — не полагаться на то, что она
   уже определена из предыдущего вызова.
4. В конце скрипта, который делает несколько запросов, — самопроверка:
   ```js
   // timestamps — массив Date.now(), записанный сразу после каждого crmGet
   const gaps = timestamps.slice(1).map((t,i) => (t - timestamps[i]) / 1000);
   if (!gaps.every(g => g >= 2.9)) throw new Error('RATE LIMIT VIOLATION: ' + JSON.stringify(gaps));
   ```
   Если самопроверка кидает ошибку — остановиться и сообщить пользователю,
   а не продолжать выгрузку молча.

**Живой тест 08.08.2026** (2 реальных запроса к `total-sum-ajax`, ОДИН вызов
`javascript_tool`, без Promise.all): `totalSeconds=7.365`, `gap=3.743с` —
подтверждено ≥3с против настоящего CRM. В отличие от v1/v2 гарантия здесь не
зависит от того, пережило ли что-то границу между вызовами инструмента —
функция самодостаточна на каждый отдельный вызов.

Если это делает агент через браузерную автоматизацию (Claude in Chrome) —
первым действием в любом скрипте, который обращается к torgstore.zymyran.com,
идёт определение `crmGet` (см. выше), и только потом сами запросы.

## 1. Сквозная аналитика (реклама/UTM) — основная выгрузка

### 1.1 Почему именно этот эндпоинт

**Используем только `report/tree`, никогда не используем async-export /
xlsx-выгрузку** — она подтверждённо завышала выручку на 10-70% (баг найден и
задокументирован в комментарии к `POST /import/tree-json` в `imports.py`).
`report/tree` — тот же live-запрос, что рисует экран CRM, поэтому цифры сайта
и цифры в самой CRM гарантированно совпадают.

### 1.2 Эндпоинты CRM (сессия в CRM должна быть авторизована в браузере)

Список менеджеров CRM (для сопоставления имени → CRM id):
```
GET https://torgstore.zymyran.com/api/crm/leads/pineline/managers?pineline_id=1
Headers: Accept: application/json, X-Requested-With: XMLHttpRequest
```

Отчёт по одному менеджеру за период:
```
GET https://torgstore.zymyran.com/api/analytics/report/tree
    ?view=utm&pineline_id=1
    &start_date=YYYY-MM-01&end_date=YYYY-MM-{последний день}
    &manager_id=<CRM_ID>
    &date_filter=created_at
    &sort_dir=asc
Headers: Accept: application/json, X-Requested-With: XMLHttpRequest
```

`date_filter=created_at` — обязателен: он привязывает и лиды, и сделки к одной
и той же когорте «лиды, поступившие в этот период». Именно поэтому поле
`conversion` в ответе уже корректно посчитано и не требует пересчёта.

Ответ: `{ total: {...}, data: [ {label, metrics:{...}}, ... ] }`. Поля в
`total` и в каждом `metrics`:
`leads, new_leads, repeat_leads, sales, new_sales, repeat_sales, revenue,
repeat_revenue, conversion, average_check, procent_of_repeat_leads,
procent_of_repeat_sales, not_implemented` (у `total` дополнительно ещё
`new_revenue`, `procent_of_new_sales`, `sale_after` — они не используются).

### 1.3 ⚠️ ГЛАВНОЕ ПРАВИЛО — конверсия

**`conversion` из ответа CRM переносится на сайт КАК ЕСТЬ, без пересчёта.**
Это `sales/leads` за период, посчитанное самой CRM по той же когорте — то,
что видно в самой CRM. Не путать с `procent_of_new_sales` — это другое поле
(«доля новых продаж среди всех продаж»), оно НЕ является конверсией и на
сайте нигде не показывается.

❌ Не делать: `new_sales / new_leads` — так уже пробовали 26.07.2026, это
дало другое число (напр. 39% вместо реальных 28% у Клары Кабилановой) и было
откачено.
✅ Делать: `conv = total.conversion` (и `conv = metrics.conversion` для
каждой строки-источника), без модификаций. Именно так сейчас и реализовано
в `backend/routers/imports.py` (`POST /import/tree-json`) — трогать эту
логику снова не нужно.

### 1.4 Формат запроса на сайт

```
POST http://localhost:8000/api/v1/import/tree-json
Content-Type: application/json

{
  "manager_id": "<UUID менеджера на сайте, или '_dept' для Весь отдел>",
  "period": "Июль 2026",
  "tree": { "total": {...как в CRM...}, "data": [ {"label": "...", "metrics": {...}}, ... ] }
}
```
Апсертит (delete+insert) `session_totals` и `source_rows` по паре
`(manager_id, period)`, затем сам пересчитывает `analytics_cache`
(`compute_and_cache`) — отдельно вызывать `/recalculate` не нужно.

### 1.5 Технические ограничения браузера — и как их обходить

Два независимых ограничения подтверждены практикой (см. сессию 26.07.2026):

1. **Chrome Private Network Access (PNA)** блокирует/подвешивает fetch с
   https-вкладки CRM (torgstore.zymyran.com) напрямую на
   `http://localhost:8000` — запрос зависает без внятной ошибки. **Решение:**
   POST на сайт делается ИЗ ВКЛАДКИ С САЙТОМ (localhost, same-origin), а не из
   вкладки CRM.
2. **Обрезание вывода JS-инструмента** на ~1000-1050 символов — полный JSON с
   17 менеджерами не поместится в один вызов. **Решение:** компактная
   сериализация без повторяющихся ключей (см. ниже) — передаём массивы, а не
   объекты с именами полей.

**Схема с двумя вкладками:**
- Вкладка A (CRM, torgstore.zymyran.com, авторизована) — только читает и
  компактно упаковывает данные, ничего никуда не отправляет.
- Вкладка B (сайт, localhost:8000) — принимает компактную строку как литерал
  следующего вызова и делает `fetch` на свой же origin.

### 1.6 Готовые скрипты (копипаст)

**В консоли вкладки B (сайт, localhost:8000) — один раз при старте сессии:**
```js
// Порядок полей фиксирован — используется во всех compact-строках ниже.
const FIELDS = ['leads','new_leads','repeat_leads','sales','new_sales','repeat_sales',
  'revenue','repeat_revenue','conversion','average_check',
  'procent_of_repeat_leads','procent_of_repeat_sales','not_implemented'];

window.__mgrIdMap = {}; // заполнить: {'Имя Фамилия на сайте': 'UUID на сайте'}
const mgrs = await fetch('/api/v1/managers').then(r=>r.json());
mgrs.forEach(m => window.__mgrIdMap[m.name] = m.id);

window.__importCompact = async function(name, period, compactStr){
  const [totalArr, rows] = JSON.parse(compactStr);
  const total = {}; FIELDS.forEach((f,i)=>total[f]=totalArr[i]);
  const data = rows.map(r=>{
    const metrics = {}; FIELDS.forEach((f,i)=>metrics[f]=r[i+1]);
    return {label: r[0], metrics};
  });
  const payload = { manager_id: window.__mgrIdMap[name], period, tree: { total, data } };
  const resp = await fetch('/api/v1/import/tree-json', {
    method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)
  });
  return {name, status: resp.status, ok: resp.ok, body: await resp.json()};
};
```

**В консоли вкладки A (CRM) — на каждого менеджера:**
```js
const FIELDS = ['leads','new_leads','repeat_leads','sales','new_sales','repeat_sales',
  'revenue','repeat_revenue','conversion','average_check',
  'procent_of_repeat_leads','procent_of_repeat_sales','not_implemented'];

async function fetchCompact(crmManagerId, startDate, endDate){
  const url = `/api/analytics/report/tree?view=utm&pineline_id=1&start_date=${startDate}&end_date=${endDate}&manager_id=${crmManagerId}&date_filter=created_at&sort_dir=asc`;
  const r = await fetch(url, {headers:{'Accept':'application/json','X-Requested-With':'XMLHttpRequest'}}).then(x=>x.json());
  const totalArr = FIELDS.map(f => r.total[f] ?? 0);
  const rows = r.data.map(d => [d.label, ...FIELDS.map(f => d.metrics[f] ?? 0)]);
  return JSON.stringify([totalArr, rows]);
}
// пример: await fetchCompact(548, '2026-07-01', '2026-07-31')
// строку-результат вставить литералом в __importCompact(...) во вкладке B.
// Если строка длиннее ~1000 символов (менеджер с большим числом источников) —
// разбить rows на 2 части (.slice(0,n)/.slice(n)) и склеить в вызове B.
```

### 1.7 Чек-лист после выгрузки (без пересборки бэкенда — это просто данные)

1. `rows_imported` в ответе `/import/tree-json` совпадает с числом строк
   источников в CRM.
2. `totals.leads/sales/revenue` в ответе совпадают с итоговой строкой в CRM.
3. Открыть страницу «Источники» на сайте для этого менеджера/периода —
   должно быть несколько реальных каналов, а не один «Другое».
4. Сверить конверсию на «Менеджер»/«Сравнение» с числом, которое показывает
   сама CRM (то самое `total.conversion`) — они обязаны совпадать один в один.
5. Если после импорта старой сессии на сайте видны старые числа — обновить
   страницу с cache-bust (`?v=<что угодно>`), кэш аналитики инвалидируется
   сам при перезаписи `session_totals`/`source_rows`.

---

## 2. Накладные (касса/отгрузка) — второй, независимый источник

```
POST http://localhost:8000/api/v1/invoices/import
Content-Type: application/json

{
  "manager_id": "<UUID менеджера на сайте>",
  "period": "Июль 2026",
  "channel": "Розничные продажи",
  "city": "Алматы",
  "gross_revenue": 21823276,
  "doc_count": 13,
  "returns_amount": 782460,
  "returns_count": 2
}
```
`net_revenue` считается на бэкенде (`gross_revenue - returns_amount`),
передавать не нужно. Апсерт идёт по `(manager_id, period, channel, city)`.

### 2.1 Источник в CRM — найдено и проверено 10.08.2026 (быстрый JSON, БЕЗ DOM-скрейпинга)

Раньше единственный известный источник был виджет «Аналитика → Топ продаж →
Менеджеры» — HTML-фрагмент, требующий DOM-парсинга (медленно). 10.08.2026
найден быстрый способ через тот же `total-sum-ajax`, что уже используется для
Скидок (§3.5), плюс НОВЫЙ эндпоинт `list-ajax` с той же формой параметров —
оба возвращают чистый JSON, оба дёргаются простым `fetch()`, DOM трогать не
нужно вообще.

**Страница CRM, где были найдены оба эндпоинта:** Склад → Накладные
(`/service/warehouse/products/requests`). Форма фильтров на этой странице —
`.requestsFilterForm`, submit-обработчик (`$('body').on('submit',
'.requestsFilterForm', ...)`) при реальном сабмите одним махом стреляет СРАЗУ
несколькими запросами без пауз между ними (`list-ajax` ×3 + `total-sum-ajax`
×1 за ~1-2 секунды) — **это найдено эмпирически 10.08.2026 и является
нарушением правила 3с, если сабмитить форму по-настоящему**
(`$(...).trigger('submit')` или реальный клик). ⚠️ **НИКОГДА не сабмитить эту
форму через UI/trigger('submit') — только вызывать оба эндпоинта напрямую
через `fetch()` с ручной паузой 3с перед каждым отдельным вызовом**, как ниже.

#### Эндпоинт 1 — `total-sum-ajax` (сумма, уже использовался для Скидок)

`GET /service/warehouse/products/requests/total-sum-ajax` — см. §3.5 за полным
описанием параметров. Возвращает `{success, message, totalSum,
totalSumWithoutDiscount}`. Для накладных `gross_revenue` = `totalSum` (не
`totalSumWithoutDiscount` — та поправка нужна только для скидок).

#### Эндпоинт 2 — `list-ajax` (количество документов, НОВОЕ, найдено 10.08.2026)

`POST /service/warehouse/products/requests/list-ajax` — та же форма
параметров, что и `total-sum-ajax` (сериализация `.requestsFilterForm`), но
методом POST и с `Content-Type: application/x-www-form-urlencoded`. Возвращает
чистый JSON: `{success, message, data, totalCount, totalSum}`.

**`totalCount` — это и есть `doc_count`.** `totalSum` в этом эндпоинте ВСЕГДА
`0` (не считается) — за суммой всё равно нужно ходить в `total-sum-ajax`
отдельно.

Подтверждено 10.08.2026 живым тестом: `manager_id` реально фильтрует
(Клара Кабиланова `manager_id=548` → `totalCount=84`; Ерганат Аубакир
`manager_id=566` → `totalCount=59`, при одинаковых остальных параметрах —
разные числа, значит фильтр по менеджеру реально работает).

### 2.1.1 Расхождение с БД — РАЗОБРАНО и объяснено (10.08.2026)

Изначально свежий тест для Ерганата за июль 2026 дал 59 документов /
29,920,319 ₸ против уже лежащих в БД 63 / 32,017,144 ₸ (~6-7% меньше). После
разбора — обе причины найдены и подтверждены:

1. **`date_delivered_checked` — обязательный чекбокс, без него `date_delivered`
   молча игнорируется.** Поля `date_assembled_checked`, `date_completed_checked`,
   `date_delivered_checked`, `date_returned_checked`, `date_canceled_checked`,
   `date_debt_checked` — это чекбоксы (`type=checkbox`, `value="on"`), а НЕ
   декоративные подписи. Без `<поле>_checked=on` в запросе бэкенд использует
   только верхний `date`/`date_other` (дата СОЗДАНИЯ документа), а не дату
   доставки — это ДРУГОЙ фильтр. Подтверждено живым A/B-тестом 10.08.2026:
   created-date фильтр (`date=other&date_other=...`) дал 29,920,319 ₸;
   delivery-date фильтр (`date=any&date_delivered_checked=on&date_delivered=...`)
   дал 33,772,734 ₸ для одних и тех же manager/период/status — числа РАЗНЫЕ,
   значит переключатель реально работает и разница именно в выборе поля даты.
   **Для «продано в периоде X» нужен `date_delivered_checked=on`, не
   `date=other`.**
2. **Старое значение в БД (63/32.0M) — это НЕполный месяц.** У существующей
   строки `snapshot_at = 2026-07-25` — импорт был сделан 25 июля, т.е. ДО
   конца месяца (не хватает 26-31 июля). Разница в ~8 документов при темпе
   Ерганата ~2/день за недостающие 6 дней — совершенно ожидаемая величина.
   **Это не баг, это устаревший частичный снэпшот** — свежая выгрузка за
   полный месяц ЗАКОНОМЕРНО больше.

### 2.1.2 Отдельная, подтверждённая проблема — `sale_channel` НЕ фильтрует (баг CRM, не мой)

Прямым A/B-тестом (одинаковые все параметры, меняется только `sale_channel`)
подтверждено на ОБОИХ эндпоинтах: `sale_channel=0` («любой») и `sale_channel=2`
(«Розничные продажи Алматы») дают **побайтово идентичный** результат —
`total-sum-ajax` вернул `33,772,734 ₸` в обоих случаях, `list-ajax` вернул
`totalCount=71` в обоих случаях (тест на Ерганате, delivery-date фильтр,
status=8). Поле правильное (`select[name="sale_channel"]`, не мультивыбор,
без `[]`), значит это реальное ограничение бэкенда CRM для этой пары
эндпоинтов, а не ошибка в параметрах с моей стороны.

**Следствие:** через `total-sum-ajax`/`list-ajax` можно получить только
выручку/количество документов «по ВСЕМ каналам сразу» — по отдельному каналу
(«Розничные продажи» отдельно от «Каспи»/«Дилеры» и т.д.) эти два эндпоинта
корректно не фильтруют. Разбивка по каналу (задача «Разбивка накладных по
каналу», пока не реализована на сайте) через этот быстрый путь недостижима;
если она когда-нибудь понадобится — только через медленный DOM-путь (виджет
«Топ продаж → Менеджеры», реальный UI-клик по фильтру канала).

**Решение по умолчанию — ИСПРАВЛЕНО 10.08.2026 после аудита-перед-прогоном:**
изначально планировалось писать под меткой `"Все каналы"`, но проверка кода
сайта (`backend/analytics/team_center.py:_invoice_net`, фронтенд
`invoiceNetForScope`/`invoiceBreakdownForScope`/`renderMgrInvoices`) показала:
**везде накладные суммируются по ВСЕМ строкам `(manager_id, period)` без
фильтра по каналу.** Апсерт же ключуется на `(manager_id, period, channel,
city)` — то есть новая строка с ДРУГОЙ меткой канала не перезаписала бы
старую, а легла бы РЯДОМ, и выручка/кол-во документов задвоились бы на всём
сайте (Обзор, Менеджер, Сравнение, Прогноз, Командный центр) для любого
периода, где уже есть строка `"Розничные продажи"`.
**Поэтому канал в payload остаётся `"Розничные продажи"` (тот же, что и
раньше)** — это тот же ключ апсерта, значит новые (более точные, по всем
каналам физически) данные корректно ПЕРЕЗАПИШУТ старые, а не задублируются.
Метка теперь технически не совсем точна (число по факту «все каналы», не
только розница) — это сознательный компромисс ради целостности данных, до
тех пор пока задача «Разбивка накладных по каналу» (#95) не даст сайту
реальную поддержку нескольких строк на канал.

Для **возвратов** (`returns_amount`/`returns_count`) — те же два эндпоинта,
только `status_id[]=7` («Возврат осуществлен») вместо `8`.

### 2.2 Готовый скрипт v3 (консоль вкладки CRM) — crmGet копировать целиком в начало КАЖДОГО отдельного вызова javascript_tool, см. §0

```js
// v3 — см. §0. Пауза 3с зашита безусловно перед КАЖДЫМ запросом.
async function crmGet(url, opts){
  await new Promise(r => setTimeout(r, 3000));
  const r = await fetch(url, Object.assign({headers:{'Accept':'application/json','X-Requested-With':'XMLHttpRequest'}}, opts||{}));
  return await r.json();
}

// ⚠️ date_delivered_checked=on ОБЯЗАТЕЛЕН — без него date_delivered молча
// игнорируется и фильтруется по дате СОЗДАНИЯ документа, не по дате
// доставки (см. §2.1.1, разобрано и подтверждено 10.08.2026).
// ⚠️ sale_channel НЕ РАБОТАЕТ на этой паре эндпоинтов (подтверждено §2.1.2,
// A/B-тест дал побайтово одинаковый результат при channel=0 и channel=2) —
// всегда шлём 0 («все каналы»), параметр оставлен в сигнатуре только чтобы
// не забыть при след. проверке, будет ли это когда-нибудь починено в CRM.
function buildQS(dateFrom, dateTo, managerId, saleChannel, statusId){
  const dr = encodeURIComponent(`${dateFrom} - ${dateTo}`); // ДД/ММ/ГГГГ
  return `search=&sortby=default&smart=&sku=&client_name=&type=any&date=any`
    + `&date_other=&date_assembled_checked=&date_assembled=`
    + `&date_completed_checked=&date_completed=`
    + `&date_delivered_checked=on&date_delivered=${dr}`
    + `&date_returned_checked=&date_returned=&date_canceled_checked=&date_canceled=`
    + `&date_debt_checked=&date_debt=`
    + `&invoice_sum_from=&invoice_sum_to=&client_id=&manager_id=${managerId}`
    + `&assembler_id=all&packager_id=all&courier_id=all&payments_method=0&discount_id=any`
    + `&sale_channel=${saleChannel}&utm_source=0&utm_medium=&utm_campaign=&utm_term=&utm_content=`
    + `&bill_id=0&service_point=0&shipment_point=0&return_warehouse=0&with_docs=all&promotion=all`
    + `&via_source=0&page=1&path=%2Fservice%2Fwarehouse%2Fproducts%2Frequests&status_id%5B%5D=${statusId}`;
}

let timestampsLog = []; // сбрасывать перед каждым новым менеджером

// gross_revenue + doc_count (status 8 = Доставлено) ИЛИ
// returns_amount + returns_count (status 7 = Возврат осуществлен)
// saleChannel всегда 0 (см. предупреждение выше — фильтр всё равно не работает)
async function invoiceStatsRaw(crmManagerId, dateFrom, dateTo, saleChannel, statusId){
  const qs = buildQS(dateFrom, dateTo, crmManagerId, saleChannel, statusId);

  const sumJ = await crmGet(`/service/warehouse/products/requests/total-sum-ajax?${qs}`);
  timestampsLog.push(Date.now());
  const num = s => Number(String(s).replace(/[^\d.-]/g,'')) || 0;

  const cntJ = await crmGet(`/service/warehouse/products/requests/list-ajax`, {
    method: 'POST',
    headers: {'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'},
    body: qs,
  });
  timestampsLog.push(Date.now());

  return { amount: num(sumJ.totalSum), count: cntJ.totalCount };
}

// Пример: Ерганат Аубакир (CRM id 566), Июль 2026. saleChannel всегда 0.
async function invoiceStatsForManager(crmManagerId, dateFrom, dateTo){
  timestampsLog = [];
  const delivered = await invoiceStatsRaw(crmManagerId, dateFrom, dateTo, 0, 8);
  const returned  = await invoiceStatsRaw(crmManagerId, dateFrom, dateTo, 0, 7);

  const gaps = timestampsLog.slice(1).map((t,i) => (t - timestampsLog[i]) / 1000);
  if (!gaps.every(g => g >= 2.9)) {
    throw new Error('RATE LIMIT VIOLATION for manager ' + crmManagerId + ': ' + JSON.stringify(gaps));
  }
  return {
    gross_revenue: delivered.amount,
    doc_count: delivered.count,
    returns_amount: returned.amount,
    returns_count: returned.count,
    _debug: { gaps }
  };
}
// await invoiceStatsForManager(566, '01/07/2026', '31/07/2026')
```

4 запроса на менеджера/период (2 статуса × 2 эндпоинта) — с правилом 3с это
~12с на менеджера, для 17 менеджеров ~3.5 минуты. Значительно быстрее
DOM-скрейпинга (Товарная аналитика, §2.5.1, занимает 20-190с на менеджера).

Результат передавать в `POST /api/v1/invoices/import` как в начале раздела 2,
**с `"channel": "Все каналы"`** (не «Розничные продажи» — см. §2.1.2, эти
эндпоинты не умеют фильтровать по каналу, число всегда по всем каналам сразу).

### 2.3 Полный прогон по всем менеджерам (fire-and-forget) — обязателен для 17+ менеджеров

⚠️ **Аудит 10.08.2026 (по запросу пользователя, ДО первого реального массового
прогона):** живой A/B-тест на 2 менеджерах подряд подтвердил, что пауза
держится честно — 8 запросов, все 7 разрывов между ними ~4.0с (минимум
3.998с), **включая стык между менеджерами** (там раньше проваливались v1/v2,
см. §0). Но арифметика на масштаб не сходится с одним `javascript_tool`
вызовом: 17 менеджеров × 4 запроса × ~4с ≈ 270с (~4.5 минуты) — это **намного
больше** 45-секундного потолка `Runtime.evaluate` (см. §2.5.1, та же ловушка
уже ловилась на Товарной аналитике). Функция `invoiceStatsForManager` из §2.2
сама по себе корректна и безопасна, но **`await`-ить её в цикле по всем 17
менеджерам одним вызовом `javascript_tool` НЕЛЬЗЯ** — упадёт по таймауту
инструмента (сам JS в странице при этом продолжит работать независимо, как и
в случае с товарной аналитикой, но результат перестанет быть виден). Нужен
тот же fire-and-forget + poll паттерн, что и в §2.5.1.

**Шаг 1 — на табе B (сайт), получить список менеджеров:**
```js
const r = await fetch('/api/v1/managers');
const j = await r.json();
window.__siteMgrsInv = j.filter(m => m.id !== '00000000-0000-0000-0000-000000000001')
                        .map(m => ({id: m.id, name: m.name}));
window.__siteMgrsInv.length;
```

**Шаг 2 — на табе A (CRM, `/service/warehouse/products/requests`), запустить
fire-and-forget цикл.** `crmGet`/`buildQS`/`invoiceStatsRaw`/`invoiceStatsForManager`
— как в §2.2 (id менеджеров в СВОЁМ числовом пространстве этой страницы,
сопоставлять по имени свежо на каждый прогон, как в §2.5.1 — id НЕ совпадают
с другими отчётами):
```js
// ...вставить сюда crmGet/buildQS/invoiceStatsRaw/invoiceStatsForManager из §2.2 целиком...

function norm(s){ return s.replace(/\s+/g,' ').trim().toLowerCase(); }
const sel = document.querySelector('select[name="manager_id"]');
const opts = Array.from(sel.options).map(o => ({value:o.value, text:o.textContent.trim()}));
const byNorm = {}; opts.forEach(o => byNorm[norm(o.text)] = o);

// подставить сюда реальный список из window.__siteMgrsInv (Шаг 1): [{id, name}, ...]
const siteMgrs = [/* ... */];

const matched = [], unmatchedNames = [];
for (const m of siteMgrs) {
  const hit = byNorm[norm(m.name)];
  if (hit) matched.push({siteId: m.id, name: m.name, crmId: hit.value}); else unmatchedNames.push(m.name);
}

window.__invProgress = 0;
window.__invTotal = matched.length;
window.__invUnmatched = unmatchedNames;
window.__invDone = false;
window.__invError = null;
window.__invGaps = null;
window.__invPayload = null;
window.__invAuditLog = []; // таймстамп ПЕРЕД каждым реальным fetch — глобальный, на весь прогон

(async () => {
  try {
    const rowsOut = [];
    for (const m of matched) {
      const stats = await invoiceStatsForManager(m.crmId, DATE_FROM, DATE_TO); // напр. '01/08/2026', '10/08/2026'
      rowsOut.push({
        manager_id: m.siteId,
        period: PERIOD_LABEL,        // напр. 'Август 2026' — см. §2.5 про частичный месяц, если период неполный
        channel: 'Розничные продажи', // см. §2.1.2 — ТОТ ЖЕ ключ апсерта, что и раньше (не "Все каналы"!),
                                       // иначе задвоится с уже существующими строками на всём сайте
        city: 'Алматы',
        gross_revenue: stats.gross_revenue,
        doc_count: stats.doc_count,
        returns_amount: stats.returns_amount,
        returns_count: stats.returns_count,
      });
      window.__invProgress++;
    }

    // САМОПРОВЕРКА §0 на ВЕСЬ прогон целиком (не только внутри одного менеджера)
    const gaps = window.__invAuditLog.slice(1).map((t,i) => (t - window.__invAuditLog[i]) / 1000);
    if (!gaps.every(g => g >= 2.9)) throw new Error('RATE LIMIT VIOLATION: ' + JSON.stringify(gaps));

    window.__invGaps = gaps;
    window.__invPayload = rowsOut;
    window.__invDone = true;
  } catch (e) {
    window.__invError = String(e);
    window.__invDone = true;
  }
})();

JSON.stringify({ started: true, totalManagers: matched.length, unmatchedNames });
```
⚠️ Чтобы самопроверка видела ВЕСЬ прогон (а не только пары внутри одного
менеджера, как в базовом `invoiceStatsForManager` из §2.2), `crmGet` в этом
цикле должен писать timestamp в `window.__invAuditLog` (глобальный, без
сброса между менеджерами) вместо/в дополнение к локальному `timestampsLog`
— именно так был устроен живой аудит-тест 10.08.2026 (см. выше), просто
скопировать оттуда `crmGet`.

**Шаг 2b — опрос прогресса** (отдельные короткие вызовы, каждые ~15-20с;
полный прогон 17 менеджеров ≈ 68 запросов × ~4с ≈ 4.5-5 минут — это
нормально, скорость не приоритет):
```js
JSON.stringify({
  progress: window.__invProgress, total: window.__invTotal,
  done: window.__invDone, error: window.__invError,
  gaps: window.__invGaps, payloadRows: window.__invPayload ? window.__invPayload.length : null,
});
```
Если `error` не `null` — прогон упал (в т.ч. если самопроверка поймала
нарушение §0) — **остановиться, не импортировать частичные данные молча**,
разобраться в причине (см. §0 «если правило неясно применимо — трактовать в
пользу осторожности»), при необходимости `navigate()` для чистого состояния
и перезапустить Шаг 2 с нуля.

**Шаг 3 — забрать payload без обрезки** (после `done:true` и `error:null`,
тот же приём, что в §1.6/§3.5/§2.5.1):
```js
document.body.innerHTML = '<pre id="__dump"></pre>';
document.getElementById('__dump').textContent = JSON.stringify(window.__invPayload);
'dumped, length=' + document.getElementById('__dump').textContent.length;
```
затем `get_page_text` на этом же табе.

**Шаг 4 — на табе B (сайт), отправить построчно** (эндпоинт `/invoices/import`
принимает один срез manager/period/channel/city за раз, не батч — цикл на
табе B к СВОЕМУ backend не подпадает под правило §0, паузы не нужны):
```js
const payload = /* JSON.parse(...) текста из Шага 3 */;
const results = [];
for (const row of payload) {
  const r = await fetch('/api/v1/invoices/import', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(row),
  });
  results.push({manager_id: row.manager_id, status: r.status, ok: r.ok});
}
JSON.stringify(results);
```


---

## 3. Когда НУЖНА пересборка бэкенда, а когда нет

- **Обычная ежемесячная выгрузка данных** (разделы 1 и 2 выше) — пересборка
  контейнера НЕ требуется, это просто API-вызовы к уже запущенному сервису.
- **Пересборка (`docker-compose up --build -d`) нужна только если менялся
  код** (`backend/**/*.py`) — backend не примонтирован как volume в
  `docker-compose.yml`, в отличие от `frontend/`, который подхватывается
  сразу. После пересборки код-логики (не данных) может понадобиться разовая
  инвалидация кэша аналитики: `PATCH /api/v1/settings/cache_version` со
  значением на 1 больше текущего.

## 3.5 Скидки БЕЗ учёта УЦЕНКИ (найдено 07.08.2026)

Уценённые товары получают скидку от компании (списание/уценка склада), а не от
менеджера — эту скидку нельзя приписывать менеджеру при расчёте его личной
скидочной активности. Раньше `discounts.py` (раздел 2, `total-sum-ajax`) брал
`totalSum`/`totalSumWithoutDiscount` по ВСЕМ складам сразу — это завышало
«скидку менеджера» на сумму уценки.

### Как технически отделить

У `total-sum-ajax` есть параметр `shipment_point` («Точка отгрузки» /
«склад отправки» в фильтрах CRM: Склад → Накладные → Фильтры). Это `<select>`
с ОДНИМ значением (не мультивыбор), `0` = все склады. Найденные склады, чьё
название содержит «УЦЕНКА» (проверено 07.08.2026, могут появляться новые —
проверять периодически через `document.querySelector('select[name="shipment_point"]').options`):

| ID | Название |
|---|---|
| 27 | Уценка |
| 29 | Уценка (Хаб) |
| 228 | Уценка Шымкент |
| 236 | Уценка Астана |
| 243 | Шымкент Витрина Уценка |
| 577 | Уценка Магазин Шымкент |

(Ещё есть 32 «Уценка (Шымкент)» и 33 «Уценка (Астана)» — встречаются в
`service_point`/«Точка обслуживания», но не в списке `shipment_point`; на
всякий случай можно тоже проверить, если суммы не сойдутся.)

### Формула

Так как `shipment_point` не мультивыбор, «исключить уценку» одним запросом
нельзя — считаем через вычитание:

```
sale_amount_чистый     = totalSum(shipment_point=0)     - Σ totalSum(shipment_point=<id уценки>)
discount_amount_чистый = totalSumWithoutDiscount(sp=0)  - Σ totalSumWithoutDiscount(sp=<id уценки>)
```

Это значит **7 запросов на менеджера/период** вместо 1 (общий + 6 по складам
уценки) — с правилом 3с это ~21с на менеджера, для 17 менеджеров ~6 минут.
Это нормально, скорость не важна (см. §0).

⚠️ Важно про параметр `date`: если оставить `date=any`, `date_other` (диапазон
дат) ИГНОРИРУЕТСЯ и CRM отдаёт сумму за всю историю — это подтверждённый
баг/особенность CRM, найден 07.08.2026 при тестировании. **Обязательно ставить
`date=other`**, чтобы `date_other=<ДД/ММ/ГГГГ - ДД/ММ/ГГГГ>` реально применился.

### Готовый скрипт v3 (консоль вкладки CRM) — crmGet копировать целиком в начало КАЖДОГО отдельного вызова javascript_tool, см. §0

```js
const UCENKA_IDS = [27, 29, 228, 236, 243, 577];

// v3 — см. §0. НЕ полагается на перехват fetch/XHR (v1/v2 признаны ненадёжными).
// Пауза 3с зашита прямо здесь, безусловно, перед КАЖДЫМ запросом.
async function crmGet(url){
  await new Promise(r => setTimeout(r, 3000));
  const r = await fetch(url, {headers:{'Accept':'application/json','X-Requested-With':'XMLHttpRequest'}});
  return await r.json();
}

// ⚠️ ОБЯЗАТЕЛЕН status_id[]=8 (Доставлено) — без него totalSum завышен почти
// в 1.5 раза (сумма по ВСЕМ статусам, а не только по факту доставленных).
// Найдено и подтверждено 07.08.2026 сверкой с реальным browser-запросом
// (Claude in Chrome → read_network_requests после ручной установки фильтров
// в UI CRM) — см. «Проверено 07.08.2026» ниже.
async function totalSumRaw(crmManagerId, dateFrom, dateTo, shipmentPoint){
  const dr = encodeURIComponent(`${dateFrom} - ${dateTo}`); // формат ДД/ММ/ГГГГ
  const url = `/service/warehouse/products/requests/total-sum-ajax?search=&sortby=default&smart=&sku=&client_name=&type=any&date=other&date_other=${dr}&date_assembled=${dr}&date_completed=${dr}&date_delivered=${dr}&date_returned=${dr}&date_canceled=${dr}&date_debt=${dr}&invoice_sum_from=&invoice_sum_to=&client_id=&manager_id=${crmManagerId}&assembler_id=all&packager_id=all&courier_id=all&payments_method=0&discount_id=any&sale_channel=0&utm_source=0&utm_medium=&utm_campaign=&utm_term=&utm_content=&bill_id=0&service_point=0&shipment_point=${shipmentPoint}&return_warehouse=0&with_docs=all&promotion=all&via_source=0&page=0&path=/service/warehouse/products/requests&status_id[]=8`;
  const j = await crmGet(url);
  timestampsLog.push(Date.now()); // для самопроверки паузы после серии запросов
  const num = s => Number(String(s).replace(/[^\d.-]/g,'')) || 0;
  return { sale: num(j.totalSum), discount: num(j.totalSumWithoutDiscount) };
}

let timestampsLog = []; // сбрасывать перед каждым новым менеджером

// Пример: Ерганат Аубакир (CRM id 566), Июль 2026
async function discountClean(crmManagerId, dateFrom, dateTo){
  timestampsLog = [];
  const all = await totalSumRaw(crmManagerId, dateFrom, dateTo, 0);
  let ucenkaSale = 0, ucenkaDiscount = 0;
  for (const id of UCENKA_IDS){
    const u = await totalSumRaw(crmManagerId, dateFrom, dateTo, id); // for...of + await — НИКОГДА Promise.all
    ucenkaSale += u.sale; ucenkaDiscount += u.discount;
  }
  // Самопроверка ОБЯЗАТЕЛЬНА: если хоть один разрыв < 2.9с — бросаем ошибку и НЕ импортируем эти данные.
  const gaps = timestampsLog.slice(1).map((t,i) => (t - timestampsLog[i]) / 1000);
  if (!gaps.every(g => g >= 2.9)) {
    throw new Error('RATE LIMIT VIOLATION for manager ' + crmManagerId + ': ' + JSON.stringify(gaps));
  }
  return {
    sale_amount: all.sale - ucenkaSale,
    discount_amount: all.discount - ucenkaDiscount,
    _debug: { all, ucenkaSale, ucenkaDiscount, gaps }
  };
}
// await discountClean(566, '01/07/2026', '31/07/2026')
```

Результат (`sale_amount`/`discount_amount`) передавать в `POST /discounts/import`
как раньше (раздел 2) — формат payload не меняется, меняется только то, ЧТО
подставляется в эти два поля (очищенное от уценки, а не сырое).

### Проверено 07.08.2026 (Ерганат Аубакир, июль 2026) — исправлено после аудита

Первая версия этого раздела (без `status_id[]=8`) давала sale=44,584,633₸ —
это сумма по ВСЕМ статусам, а не только «Доставлено», поэтому число было
завышено. Ниже — исправленные цифры, сверенные с реальным запросом браузера
(через ручную установку фильтров в UI CRM + `read_network_requests`):

- Все склады, статус=Доставлено (`status_id[]=8`): sale=29,920,319₸, discount=3,065,401₸
- Только «Уценка (Хаб)» (id 29): sale=134,550₸, discount=14,950₸
- Остальные 5 складов уценки (27, 228, 236, 243, 577): sale=0₸, discount=0₸ (у этого менеджера пусто в июле)
- Итого уценка: sale=134,550₸ (0.45% от общей суммы), discount=14,950₸ (0.49% от общей скидки)
- «Чистая» скидка (без уценки): sale=29,785,769₸, discount=3,050,451₸

У этого конкретного менеджера уценка — маленькая доля (<0.5%), но по другим
менеджерам/периодам доля может быть куда больше — этим и объясняется, зачем
это разделять, а не просто списать как погрешность. Полная проверка по всем
менеджерам не проводилась (дорого по времени из-за правила 3с × 7 запросов
на менеджера) — делать по явному запросу пользователя.

⚠️ **Отдельная находка аудита 07.08.2026, не связанная с уценкой:** «чистая»
сумма выше (29.9M₸) всё равно НЕ совпадает с тем, что сейчас лежит в БД сайта
для Ерганата за июль (21.3M₸ sale / 2.0M₸ discount) — это старый снепшот,
загруженный раньше. Расхождение объясняется, скорее всего, тем, что статус
«Доставлено» у части накладных наступает ПОЗЖЕ создания — с момента импорта
прошло время, и больше накладных за июль успели дойти до статуса «Доставлено»,
чем было на момент снепшота. Это ожидаемое поведение снепшота, а не баг
методологии — но означает, что цифры скидок в БД дрейфуют и стареют так же,
как и другие «снепшотные» метрики на сайте.

**Пока НЕ переделаны задним числом** уже загруженные скидки за июнь/июль/август
(они всё ещё считают уценку как часть скидки менеджера И основаны на старом
снепшоте статусов) — это отдельная задача, делать только по явному запросу
пользователя.

## 2.5 Товарная аналитика — ТРЕТИЙ независимый источник, тоже входит в «актуализировать»

⚠️ Найдено 08.08.2026: при запросе «актуализировать данные» этот источник
раньше молча пропускался (заливались только Сквозная аналитика и Накладные),
хотя в карточке менеджера есть отдельный блок «📦 Товарная аналитика», который
от этого стареет. Пользователь заметил это как «карточка показывает не тот
период» — на самом деле карточка честно показывает fallback-предупреждение
(см. `renderProductBlock` во frontend), но сам факт, что источник не обновился
вместе с остальными, был реальным пробелом процесса.

**Правило: «актуализировать данные за период X» = обновить ВСЕ источники
из чек-листа в §5, включая товарную аналитику, а не только то, что первым
приходит в голову.**

Источник в CRM: раздел «Аналитика → Товары» (или похожий отчёт по продажам
товаров/категорий). Отчёт ПОДДЕРЖИВАЕТ произвольный диапазон дат по дате
доставки: в панели фильтров есть чекбокс «ДАТА ДОСТАВКИ» — если его включить,
появляется поле ввода диапазона в формате «MM/DD/YYYY - MM/DD/YYYY» (например
«08/01/2026 - 08/07/2026»), которое фильтрует отчёт на любой произвольный
диапазон дней, а не только на целый месяц (подтверждено 08.08.2026: чекбокс
успешно использован в CRM UI, диапазон 08/01/2026-08/07/2026 дал корректные
данные по менеджерам). То есть данные за неполный месяц можно получить из
CRM напрямую, без импровизаций и обходных путей.

⚠️ Но даже с произвольным диапазоном на стороне CRM, схема импорта на сайте
(`POST /import/products`) хранит данные ТОЛЬКО с гранулярностью «месяц»
(`period` = «Месяц Год», резолвится через `MONTH_MAP`) — способа пометить
запись как «частичный месяц» на уровне схемы БД нет. Поэтому если залить
частичный CRM-срез (например, только 1-7 число месяца) через этот эндпоинт,
итоговая запись на сайте будет неотличима от записи за полный месяц. Для
неполного месяца выгружать можно, но нужно явно указывать в комментарии/
этикетке пользователю, что это ЧАСТИЧНЫЙ месяц, а не полный — иначе через
3 недели цифры будут выглядеть как «обвал продаж» при сравнении с
предыдущими полными месяцами.

```
POST http://localhost:8000/api/v1/import/products
Content-Type: multipart/form-data

file=<Excel-файл с 3 листами: «По месяцам» / «По категориям» / «По товарам»>
year=2026
```
Формат листов и сопоставление менеджеров по имени — см. `backend/routers/products.py`.
Апсерт идёт по (manager_id, period) для сводки, категории/товары — по имени
(чтобы не терять точечные правки, `product_overrides`, между перезаливами).

### 2.5.1 Готовый скрипт v3 (автоматизация, DOM-driven) — найдено и проверено 10.08.2026

Этот отчёт в CRM — **НЕ** простой GET/JSON-эндпоинт как `report/tree` (§1) или
`total-sum-ajax` (§3.5). Это Laravel-страница с настоящей `<form>` (39
фильтров), submit которой перехватывается клиентским JS и превращается в
AJAX `POST https://torgstore.zymyran.com/service/analytics/products/sales/form-post`,
обновляющий DOM на месте (без перехода страницы). Правильный способ
автоматизации — управлять реальными полями формы и реальной кнопкой, а не
пытаться руками собрать payload (вероятно Livewire/CSRF-подписанный, не
воспроизводится напрямую).

**Подтверждено эмпирически 10.08.2026:**
- Смена `<select name="manager_id">` (`.value` + `dispatchEvent('change')`)
  сама по себе **не порождает сетевой запрос** к zymyran.com (проверено через
  `read_network_requests` с `clear:true` — после смены значения лог пуст).
  Значит внутри цикла по менеджерам единственное действие, которое считается
  «запросом к CRM» по правилу §0 — это клик по кнопке «Поиск». Именно перед
  ним (и только перед ним) обязательна пауза ≥3с.
- Единственный `<table>` на странице содержит на каждую строку 16 колонок:
  индексы 0-9 — чистые «сырые» значения (SKU, Название, Категория, Продано,
  Сумма, Документы, Остатки, Мин.кол-во, Код поставщика, дублирующее полное
  описание), индексы 10-15 — те же данные, но в форматированном виде для
  отображения («194 600 ₸», «1 шт» и т.п.) — их парсить не нужно, есть чистые
  числа в 3-4.
- Чекбокс `input[name="date_delivered[enabled]"]` + текстовое поле
  `input[name="date_delivered[date]"]` (формат `MM/DD/YYYY - MM/DD/YYYY`)
  задают производный диапазон дат — независимо от того, что выбрано в самой
  форме по умолчанию.
- Сопоставление менеджеров: `<select name="manager_id">` в ЭТОМ отчёте имеет
  СВОЙ независимый набор числовых id (205 опций, вообще все сотрудники в
  Zymyran, не только 18 активных менеджеров сайта) — id НЕ совпадают с id из
  других отчётов (`shipment_point`/`total-sum-ajax` и т.п.). Сопоставлять
  нужно **по точному имени**, свежо на каждый прогон (список сотрудников в
  CRM может меняться), а не хардкодить id. 10.08.2026: 17 из 18 менеджеров
  сайта нашлись точным совпадением имени; **Виолетта Воробьева в этом
  отчёте отсутствует полностью** (не найдена даже частичным совпадением по
  фамилии) — это не баг скрипта, а факт CRM-справочника на эту дату,
  логировать как `managers_unmatched` и не считать ошибкой выполнения.

**Архитектура выгрузки — два таба, как и для остальных источников:**
Таб A = CRM (`.../products/sales`) — сбор данных построчно по менеджерам.
Таб B = сайт (`torgstore-api.onrender.com`) — единственный, кто реально
отправляет `POST /import/products-json` (прямой cross-origin fetch с таба
CRM на сайт проверен 10.08.2026 и **не работает** — `TypeError: Failed to
fetch` на защищённых Basic-Auth эндпоинтах, хотя `/health` без авторизации
проходит; значит CORS/Basic-Auth-связка блокирует прямой межтабный запрос,
и обходной путь — не нужен, раз уже есть рабочий паттерн с двумя табами).

**Шаг 1 — на табе B (сайт), получить актуальный список менеджеров** (не CRM-запрос, не под правилом §0):
```js
const r = await fetch('/api/v1/managers');
const j = await r.json();
window.__siteMgrs = j.filter(m => m.id !== '00000000-0000-0000-0000-000000000001')
                      .map(m => m.name);
window.__siteMgrs.length;
```

**⚠️ Найдено живым прогоном 10.08.2026 — ДВЕ ловушки, обе исправлены ниже:**
1. **CDP `Runtime.evaluate` обрывается на ~45с.** Полный цикл по 17
   менеджерам занимает 1.5-4+ минуты (см. п.2) — то есть НЕЛЬЗЯ `await`-ить
   весь цикл внутри одного вызова `javascript_tool`: он падает с ошибкой
   `timed out after 45000ms`, хотя сам JS в странице при этом **продолжает
   выполняться независимо** (это подтверждено — `window`-переменные после
   такой ошибки продолжали обновляться). Проблема не в самом выполнении, а
   в том, что вызывающий инструмент перестаёт ждать результат.
   **Решение: fire-and-forget.** Вызов `javascript_tool` запускает цикл как
   `(async () => {...})()` **без `await`** на верхнем уровне и сразу
   возвращает `{started:true}` — весь долгий цикл живёт в странице сам по
   себе. Прогресс/результат читаются отдельными короткими вызовами
   `javascript_tool` (poll), см. Шаг 2b.
2. **Фоновая (не активная) вкладка Chrome троттлит таймеры непредсказуемо.**
   Живой тест 10.08.2026: вместо ожидаемых ~6с/менеджер реальный темп был
   ~20-22с/менеджер, и на 7-м менеджере `document.querySelectorAll('table')[0]`
   вернул `undefined` (таблица не успела перерендериться к моменту фиксированной
   паузы). **Фиксированные `setTimeout`-паузы после клика ненадёжны** —
   вместо них `extractOne` ниже опрашивает текст кнопки «Поиск» (у неё
   есть строго различимое состояние загрузки — `textContent` содержит
   «Подождите…» пока идёт AJAX) до готовности, с таймаутом и одним защитным
   повтором клика, если таблица всё равно не появилась.

```js
const MONTH_NAMES = ['','Январь','Февраль','Март','Апрель','Май','Июнь','Июль','Август','Сентябрь','Октябрь','Ноябрь','Декабрь'];
const today = new Date();
const first = new Date(today.getFullYear(), today.getMonth(), 1);
const fmt = d => String(d.getMonth()+1).padStart(2,'0') + '/' + String(d.getDate()).padStart(2,'0') + '/' + d.getFullYear();
const dateRangeStr = fmt(first) + ' - ' + fmt(today);
const monthName = MONTH_NAMES[today.getMonth()+1];
const year = today.getFullYear();

// подставить сюда реальный список из window.__siteMgrs (Шаг 1)
const siteNames = [/* ...17-18 имён... */];

function norm(s){ return s.replace(/\s+/g,' ').trim().toLowerCase(); }
const sel = document.querySelector('select[name="manager_id"]');
const opts = Array.from(sel.options).map(o => ({value:o.value, text:o.textContent.trim()}));
const byNorm = {}; opts.forEach(o => byNorm[norm(o.text)] = o);
const matched = [], unmatchedNames = [];
for (const name of siteNames) {
  const hit = byNorm[norm(name)];
  if (hit) matched.push({name, crmId: hit.value}); else unmatchedNames.push(name);
}

const dateEnabled = document.querySelector('input[name="date_delivered[enabled]"]');
const dateField = document.querySelector('input[name="date_delivered[date]"]');
if (!dateEnabled.checked) { dateEnabled.checked = true; dateEnabled.dispatchEvent(new Event('change', {bubbles:true})); }
dateField.value = dateRangeStr;
dateField.dispatchEvent(new Event('change', {bubbles:true}));

window.__productsProgress = 0;
window.__productsTotal = matched.length;
window.__productsUnmatched = unmatchedNames;
window.__productsDone = false;
window.__productsError = null;
window.__productsClickTimestamps = [];
window.__productsPayload = null;

async function waitButtonIdle(btn, timeoutMs) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    if (!btn.textContent.includes('Подождите')) return true;
    await new Promise(r => setTimeout(r, 300));
  }
  return false;
}

// clickLog — общий массив, куда КАЖДЫЙ клик (включая повторные попытки) пишет свой timestamp,
// чтобы самопроверка правила §0 покрывала реальные сетевые события, а не только успешные
async function extractOne(crmId, clickLog, attempt) {
  attempt = attempt || 1;
  const s = document.querySelector('select[name="manager_id"]');
  s.value = crmId;
  s.dispatchEvent(new Event('change', {bubbles:true}));

  // ПРАВИЛО §0: ≥3с ВСЕГДА перед единственным сетевым действием (клик) — без исключений, в том числе перед повтором
  await new Promise(r => setTimeout(r, 3500));
  const btn = Array.from(document.querySelectorAll('button')).find(b => b.textContent.trim().startsWith('Поиск'));
  clickLog.push(Date.now());
  btn.click();
  await new Promise(r => setTimeout(r, 400));           // дать «Подождите» успеть появиться в DOM
  await waitButtonIdle(btn, 20000);                      // опрос вместо фиксированной паузы, до 20с
  await new Promise(r => setTimeout(r, 300));             // небольшой запас — таблица иногда обновляется на кадр позже кнопки

  const table = document.querySelectorAll('table')[0];
  if (!table) {
    if (attempt >= 2) throw new Error('Таблица не найдена после ' + attempt + ' попыток, crmId=' + crmId);
    return extractOne(crmId, clickLog, attempt + 1);      // защитный повтор — свой собственный ≥3.5с перед клик ом уже гарантирован рекурсией
  }
  const rows = Array.from(table.querySelectorAll('tbody tr, tr')).slice(1)
    .map(tr => Array.from(tr.querySelectorAll('td,th')).map(td => td.textContent.replace(/\s+/g,' ').trim()))
    .filter(r => r.length >= 5 && r[0]);
  return rows.map(r => ({ product_name: r[1], category: r[2], qty: Number(r[3])||0, revenue: Number(r[4])||0 }));
}

(async () => {
  try {
    const rowsOut = [];
    for (const m of matched) {
      const items = await extractOne(m.crmId, window.__productsClickTimestamps);

      const itemMap = new Map();
      for (const it of items) {
        const key = it.product_name;
        const cur = itemMap.get(key) || { product_name: it.product_name, category: it.category, qty: 0, revenue: 0 };
        cur.qty += it.qty; cur.revenue += it.revenue;
        itemMap.set(key, cur);
      }
      const itemsOut = Array.from(itemMap.values());

      const catMap = new Map();
      for (const it of itemsOut) {
        const cur = catMap.get(it.category) || { category: it.category, qty: 0, revenue: 0 };
        cur.qty += it.qty; cur.revenue += it.revenue;
        catMap.set(it.category, cur);
      }

      rowsOut.push({
        manager: m.name, month: monthName,
        qty: itemsOut.reduce((s,i)=>s+i.qty,0),
        revenue: itemsOut.reduce((s,i)=>s+i.revenue,0),
        categories: Array.from(catMap.values()),
        items: itemsOut,
      });
      window.__productsProgress++;
    }

    // САМОПРОВЕРКА правила §0 — обязательна, останавливаемся при нарушении
    const gaps = window.__productsClickTimestamps.slice(1).map((t,i) => (t - window.__productsClickTimestamps[i]) / 1000);
    if (!gaps.every(g => g >= 2.9)) throw new Error('RATE LIMIT VIOLATION: ' + JSON.stringify(gaps));

    window.__productsPayload = { year, rows: rowsOut, source: 'crm-auto-' + today.toISOString().slice(0,10) };
    window.__productsGaps = gaps;
    window.__productsDone = true;
  } catch (e) {
    window.__productsError = String(e);
    window.__productsDone = true;
  }
})();

JSON.stringify({ started: true, totalManagers: matched.length, unmatchedNames });
```

**Шаг 2b — опрос прогресса** (отдельные короткие вызовы `javascript_tool`,
каждые ~30-40с, пока `done` не станет `true`; полный прогон 17 менеджеров
занимал в живом тесте от ~2 до ~6+ минут из-за троттлинга фоновой вкладки —
это нормально, скорость не приоритет, важно не нарушить §0):
```js
JSON.stringify({
  progress: window.__productsProgress, total: window.__productsTotal,
  done: window.__productsDone, error: window.__productsError,
  gaps: window.__productsGaps, payloadRows: window.__productsPayload ? window.__productsPayload.rows.length : null,
});
```
Если `error` не `null` — прогон упал (например, после исчерпания повторов
на отсутствующей таблице); почитать `error`, при необходимости `navigate()`
на тот же URL для чистого состояния и запустить Шаг 2 заново с нуля (это
не нарушает §0 — сама перезагрузка страницы такой же «один запрос», как
описано в §0 «технические ограничения браузера»).

**Шаг 3 — только после того, как Шаг 2b показал `done:true` и `error:null` —
забрать полный payload без обрезки вывода** (стандартный обход лимита
вывода `javascript_tool` для больших данных — см. §0 «технические
ограничения браузера», такой же приём, что и для §1.6/§3.5):
```js
document.body.innerHTML = '<pre id="__dump"></pre>';
document.getElementById('__dump').textContent = JSON.stringify(window.__productsPayload);
'dumped, length=' + document.getElementById('__dump').textContent.length;
```
затем прочитать через `get_page_text` на этом же табе. ⚠️ Это уничтожает
живой DOM страницы — после этого таб CRM для дальнейшей работы непригоден
без `navigate()`-перезагрузки (это нормально, весь сбор данных на сегодня
уже закончен на этом шаге).

**Шаг 4 — на табе B (сайт), отправить итог:**
```js
const payload = /* JSON.parse(...) текста, полученного в Шаге 3 */;
const r = await fetch('/api/v1/import/products-json', {
  method: 'POST',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify(payload),
});
const j = await r.json();
JSON.stringify({status: r.status, ...j});
```
Проверить в ответе: `managers_unmatched` пуст (или содержит только заведомо
известных отсутствующих, например Воробьеву), `summary_count`/`item_count`
разумны по масштабу, `import_id` присутствует.

**Итого сетевых запросов к zymyran.com за один прогон** = ровно N (по числу
сопоставленных менеджеров, ~17), каждый отделён от предыдущего ≥3с явным
`await` внутри Шага 2 — это единственная точка, где правило §0 применимо
(смена select/date полей запросов не порождает, см. выше).

---

## 6. Причины отказа — найдено и проверено 10.08.2026 (быстрый JSON, БЕЗ DOM-скрейпинга)

```
POST http://localhost:8000/api/v1/decline-reasons/import
Content-Type: application/json

{
  "manager_id": "<UUID менеджера на сайте>",
  "period": "Август 2026",
  "pipeline": "Отдел первичных продаж",
  "sample_size": 12,
  "reasons": [
    {"reason": "Дорого", "count": 5},
    {"reason": "Нет в наличии", "count": 3},
    {"reason": "Передумал", "count": 4}
  ]
}
```
⚠️ **Это ЗАМЕНА, не накопление**: бэкенд удаляет старый срез по ключу
`(manager_id, period, pipeline)` и вставляет новый (см.
`backend/routers/decline_reasons.py:60-69`). Значит на вход всегда нужен
**весь диапазон периода целиком** (1 число месяца → сегодня), а не только
«новые» лиды со вчерашнего дня — ровно как для остальных источников в §5.
Разные `pipeline` для одного `manager_id`+`period` НЕ конфликтуют и не
перезаписывают друг друга (уникальность в БД — по четвёрке `manager_id,
period, pipeline, reason`), фронтенд суммирует их корректно с раскрытием
`pipelinesLabel` (проверено 10.08.2026 отдельным аудитом кода, добавлено в
сессии 07.08.2026) — так что бояться задвоения при смене названия воронки
не нужно.

### 6.1 Источник в CRM

Раньше (сессии до 10.08.2026) причины отказа читались вручную, по одному
лиду за раз через UI (см. комментарий в начале `decline_reasons.py` и задачи
#286-289/#308/#313 в трекере) — без задокументированной, воспроизводимой
процедуры. 10.08.2026 реверс-инжинирингом найдены 3 быстрых JSON-эндпоинта
CRM, которые вместе полностью автоматизируют выгрузку без единого клика по
UI и без парсинга HTML:

**Воронка:** `pineline_id=91` = «Отдел первичных продаж» (не «Розница
Алматы», `pineline_id=1` — это другая, более старая воронка; список всех
воронок компании — `GET /api/crm/leads/pineline/get_pinelines`). Это
активная воронка, в которую сейчас реально падают новые лиды (проверено
живым просмотром 10.08.2026) — совпадает с тем, что нашла предыдущая сессия
для 1-6 августа (задача #312/#313).

**Эндпоинт 1 — список «отказных» лидов периода:**
```
GET /api/crm/leads/pineline/stage/leads_by_all_stages
    ?pineline_id=91&type_ids[]=3&page=N&limit=100
```
`type_ids[]=3` = фильтр по типу этапа «Не реализовано» (не по конкретному
`stage_id` — на случай если в воронке несколько «отказных» этапов; в текущей
воронке 91 такой этап один, `stage_id=510`). Ответ — `{stages:[{stage,
total_sum, leads:[...], meta:{page, total_pages, total_records}}]}`. Каждый
`lead` уже содержит `id`, `manager.id`, `manager.full_name`, `create.created_at`
— **без доп. запроса за менеджером**. Пагинация — `page`/`limit` (макс.
проверенный `limit=100`). У эндпоинта **нет** параметра диапазона дат —
дата-фильтрация делается на своей стороне (`created_at` уже есть в ответе).

**Эндпоинт 2 — детали одного лида (поле «Причина отказа»):**
```
GET /api/crm/leads/details?lead_id=<id>
```
Ответ: `{data:{..., custom_fields:[{id:9, name:"Причина отказа", value:"Нет в
наличии", ...}, ...]}}`. Поле `id=9` — фиксированный ID кастомного поля
«Причина отказа» в этой воронке (получен через `GET
/api/crm/leads/fields/stages?pineline_id=91`, там же — полный список из 8
возможных значений: Дорого / Нет в наличии / Купил у других / Передумал /
Недозвон-Нет ответа / Запрос БТ / Запрос Запчасти / Другой город-Цена
логистики). Если лид ещё не переведён в «Не реализовано» — `value: null`
(на практике не должно случаться, т.к. поле `required_from_stage` = «Не
реализовано» на стороне CRM).

**Почему нужен запрос №2 на КАЖДЫЙ лид:** причина отказа — это
per-лид кастомное поле, эндпоинт №1 (список) его не отдаёт вообще (проверено
живым сравнением полей ответа). В отличие от Накладных (§2), тут нет
эквивалента `list-ajax`/`total-sum-ajax` с готовым агрегатом по причине —
дергать реальный лид приходится по одному.

### 6.2 Объём запросов — оценено и подтверждено живым прогоном 10.08.2026

Шаг 1 (список, `type_ids[]=3`, весь пайплайн 91, без ограничения по дате) —
**341 лид за всё время** существования воронки, 4 страницы по `limit=100`
→ **4 запроса**, ~16 секунд.

Из них с `created_at` в диапазоне 01.08.2026–10.08.2026 (частичный месяц,
10 дней) — **166 лидов** → **166 запросов** к `leads/details`, ~4с/запрос
(3с пауза + сеть) → **≈ 11 минут**. Это больше, чем Накладные (68 запросов,
§2.3), но того же порядка, что Товарная аналитика по общему времени (§2.5.1)
— и **не является проблемой** по правилу §0 («скорость не важна, важно не
попасть под блокировку»). Ожидаемо, что дальше в месяце объём будет расти
пропорционально дням (166 за 10 дней ⇒ ориентировочно 500-550 за полный
месяц) — как и везде, это нормально, не признак ошибки.

**Обязателен fire-and-forget + poll** (45-секундный потолок
`Runtime.evaluate`, та же ловушка, что в §2.5.1/§2.3) — НЕ `await`-ить цикл
из 166+ запросов одним вызовом `javascript_tool`.

### 6.3 Готовый скрипт v3 (консоль вкладки CRM)

**Шаг 1 — собрать список «отказных» лидов периода** (обычный `await`-вызов,
4 страницы укладываются в лимит одного вызова инструмента):
```js
async function crmGet(url, opts){
  await new Promise(r => setTimeout(r, 3000));
  const r = await fetch(url, Object.assign({headers:{'Accept':'application/json','X-Requested-With':'XMLHttpRequest'}}, opts||{}));
  return await r.json();
}
const log = [];
let page = 1, totalPages = 1;
const allLeads = [];
do {
  log.push(Date.now());
  const j = await crmGet(`/api/crm/leads/pineline/stage/leads_by_all_stages?pineline_id=91&type_ids%5B%5D=3&page=${page}&limit=100`);
  const grp = j.stages && j.stages[0];
  if (!grp) break;
  totalPages = grp.meta.total_pages;
  for (const l of grp.leads) {
    allLeads.push({id: l.id, manager_id: l.manager && l.manager.id, manager_name: l.manager && l.manager.full_name, created_at: l.create && l.create.created_at});
  }
  page++;
} while (page <= totalPages);

const gaps = log.slice(1).map((t,i)=>(t-log[i])/1000);
if (!gaps.every(g => g >= 2.9)) throw new Error('RATE LIMIT VIOLATION: ' + JSON.stringify(gaps));

// подставить реальный диапазон периода (YYYY-MM-DD, левая граница включительно,
// правая — первый день СЛЕДУЮЩЕГО дня после конца диапазона, т.к. created_at
// хранит время):
const DATE_FROM = '2026-08-01', DATE_TO_EXCLUSIVE = '2026-08-11';
window.__periodLostLeads = allLeads.filter(l => l.created_at && l.created_at >= DATE_FROM && l.created_at < DATE_TO_EXCLUSIVE);
JSON.stringify({totalPages, totalFetched: allLeads.length, gaps, periodCount: window.__periodLostLeads.length});
```

**Шаг 2 — fire-and-forget по каждому лиду периода** (глобальный
`__reasonAuditLog`, самопроверка на ВЕСЬ прогон целиком — тот же паттерн,
что в §2.3):
```js
window.__reasonProgress = 0;
window.__reasonTotal = window.__periodLostLeads.length;
window.__reasonDone = false;
window.__reasonError = null;
window.__reasonGaps = null;
window.__reasonAgg = null;
window.__reasonAuditLog = [];

(async () => {
  try {
    async function crmGet(url){
      await new Promise(r => setTimeout(r, 3000));
      window.__reasonAuditLog.push(Date.now());
      const r = await fetch(url, {headers:{'Accept':'application/json','X-Requested-With':'XMLHttpRequest'}});
      return await r.json();
    }
    const agg = {}; // manager_id -> {manager_name, reasons: {reason: count}, sample_size}
    for (const lead of window.__periodLostLeads) {
      const j = await crmGet(`/api/crm/leads/details?lead_id=${lead.id}`);
      const cf = (j.data && j.data.custom_fields || []).find(f => f.id === 9);
      const reason = (cf && cf.value) ? cf.value : 'Не указана';
      const mgrId = lead.manager_id || 'unknown';
      const mgrName = lead.manager_name || 'Неизвестно';
      if (!agg[mgrId]) agg[mgrId] = {manager_name: mgrName, reasons: {}, sample_size: 0};
      agg[mgrId].reasons[reason] = (agg[mgrId].reasons[reason] || 0) + 1;
      agg[mgrId].sample_size++;
      window.__reasonProgress++;
    }
    const gaps = window.__reasonAuditLog.slice(1).map((t,i) => (t - window.__reasonAuditLog[i]) / 1000);
    if (!gaps.every(g => g >= 2.9)) throw new Error('RATE LIMIT VIOLATION: ' + JSON.stringify(gaps));
    window.__reasonGaps = gaps;
    window.__reasonAgg = agg;
    window.__reasonDone = true;
  } catch (e) {
    window.__reasonError = String(e);
    window.__reasonDone = true;
  }
})();
JSON.stringify({started: true, total: window.__reasonTotal});
```

**Шаг 2b — опрос** (каждые ~60-90с, полный прогон на 150-550+ лидов ≈
10-40 минут):
```js
JSON.stringify({progress: window.__reasonProgress, total: window.__reasonTotal, done: window.__reasonDone, error: window.__reasonError, gaps: window.__reasonGaps});
```
Если `error` не `null` — остановиться, не импортировать частичные данные,
см. §0.

**Шаг 3 — забрать агрегат без обрезки:**
```js
document.body.innerHTML = '<pre id="__dump"></pre>';
document.getElementById('__dump').textContent = JSON.stringify(window.__reasonAgg);
'dumped, length=' + document.getElementById('__dump').textContent.length;
```
затем `get_page_text` на этом же табе. Дальше по нужному менеджеру
преобразовать `{manager_name, reasons:{...}, sample_size}` в payload из
начала §6 (`reasons` — массив `{reason, count}` из `Object.entries`) и
отправить построчно на Табе B — `manager_id` (UUID сайта) сопоставлять по
имени со списком `GET /api/v1/managers` (как в §2.3), НЕ по CRM `manager_id`
из ответа (числовое CRM-пространство, не совпадает с UUID сайта).

---

## 7. Планы продаж — оценка автоматизируемости (11.08.2026)

### 7.1 Где это в CRM

Раздел находится не в самом модуле «Лиды», а в общем сайдбаре сервисов:
`☰ → CRM → План продаж` → `https://torgstore.zymyran.com/service/crm/sales-plans`.
Раньше (сессия 07.08.2026, задачи #341-344) план выгружался вручную построчно
через UI без задокументированного механизма — 11.08.2026 найдены 2 реальных
JSON-эндпоинта за этим экраном.

### 7.2 Структура данных — ПО КАНАЛУ, не по менеджеру напрямую

Ключевое отличие от остальных 5 источников: план продаж задаётся на уровне
**канала** (по сути — воронки/подразделения × город, например «Каспи Алматы»,
«Каспи Шымкент», «Дилеры Астана», «TikTok продажи» и т.д. — 22 канала на
август 2026), а не на менеджера напрямую. Один менеджер может быть привязан
к нескольким каналам (см. уже существующую заметку в §5 п.5 «агрегация по
всем каналам менеджера») — значит план менеджера = сумма его прогресса по
всем каналам, где он указан ответственным.

### 7.2.1 🚨 ИСПРАВЛЕНИЕ (12.08.2026): план компании = 3 канала × Алматы, НЕ все 22 канала

**Ошибка, допущенная в этой же сессии:** первая попытка посчитать план
компании суммировала `sales_target` по ВСЕМ ~22-23 каналам pineline_id=91
(включая Каспи, Дилеров, Айдын Опт, Корпоративные продажи, Мастер продаж,
Customer service, Магазин Шымкент, Супер-дилеров Байсат и т.д.) — это дало
завышенные цифры (например, Июнь = 1,000,000,000 вместо реальных 325,000,000).
Пользователь дважды поправил:

1. «Мы работаем только с каналами - Инстаграм Тикток и розница» — считать
   только 3 ТИПА канала: `Розничные продажи`, `Инстаграм`, `TikTok продажи`
   (регэксп для матчинга: `/Розничн|Инстаграм|TikTok/i` — НЕ `/розниц|.../i`,
   т.к. «розниц» не подстрока «Розничные»: после «розн» буквы расходятся —
   «и-ц» vs «и-ч»).
2. «стоп сразу говорю только Алматы учитываем. Шымкент Астана пока нет» —
   из этих 3 типов канала брать ТОЛЬКО городской вариант «Алматы» (`city`
   в ответе `/api/crm/plans/monthly`), Шымкент/Астана-варианты исключить.

**Правильный набор на 11-12.08.2026 (id стабильны, проверять при каждом
прогоне через `/api/crm/plans/monthly`, т.к. могут пересоздаваться):**
`point_id 2` = «Розничные продажи»/Алматы, `point_id 51` = «Инстаграм»/Алматы,
`point_id 58` = «TikTok продажи»/Алматы. Итого **3 канала**, не 22-23 и не 8
(8 — это те же 3 типа, но по всем 3 городам сразу — тоже неверно, была
промежуточная ошибка перед финальной поправкой).

Company-wide план = сумма `sales_target` этих 3 point-ов (напрямую из
`points[]` ответа `/api/crm/plans/monthly`, без похода в
`manager-channels`). Личный план менеджера = сумма его `sales_target` из
`manager-channels` **только для этих 3 point_id**, не для всех каналов, где
он назначен.

**Важно:** сумма личных планов (только эти 3 канала, только Алматы) НЕ
обязана совпадать с company-wide суммой (это разные точки данных в CRM —
план на point и его разбивка по менеджерам иногда расходятся, сама CRM это
не сверяет). Не пытаться их искусственно уравнивать — грузить оба числа как
есть.

Раздел §7.3-§7.4 ниже и чек-лист §5 п.5 описывают более ранний (более общий,
но методологически неверный для company-wide плана) способ сбора «все N
каналов» — сам механизм эндпоинтов верный, но при импорте плана компании
и личных планов использовать фильтр из этого раздела (3 канала × Алматы),
а не весь список `points`.

### 7.3 Найденные эндпоинты и рабочий payload (взломано 11.08.2026, той же сессией)

**Эндпоинт 1 — сводка по всем каналам сразу:**
```
POST /api/crm/plans/monthly
```
Отдаёт ВСЕ 22 канала одним запросом: по каждому — `id`, `name.ru`, `city`,
`sales_target`, `current_sales`, плюс общий блок `global` (компания целиком:
`current_sales`, `sales_target`, `sales_forecast`, `old_target`).

**Эндпоинт 2 — разбивка канала по менеджерам:**
```
POST /api/crm/plans/manager-channels
```
По одному каналу — список менеджеров, отвечающих за него, с `id`, `name`,
`url`, `sales_target`, `current_sales` (доля канала на этого менеджера).

**Рабочий payload найден брутфорсом ("superset"-тело), а не перехватом.**
`{}` и `{month, year}` возвращали общий `400 The given data was invalid`
без детализации по полям; monkey-patch `window.fetch`/`XMLHttpRequest` тоже
не поймал реальные запросы (SPA держит свою ссылку на `fetch`, полученную до
патча — тот же паттерн, что и раньше на этом проекте, см. историю). Сработал
один запрос со всеми правдоподобными именами полей сразу — тратить время на
сужение до минимального набора не стал, суперсет надёжен и стабилен:

```js
const commonBody = {
  pineline_id: 91, month: 8, year: 2026, period: "2026-08",
  date: "2026-08-01", date_from: "2026-08-01", date_to: "2026-08-31"
};
// Эндпоинт 1:
await crmPost('/api/crm/plans/monthly', commonBody);   // → {global, points:[...22 канала]}
// Эндпоинт 2 (для каждого канала p из points):
await crmPost('/api/crm/plans/manager-channels',
  Object.assign({point_id: p.id, sales_point_id: p.id, channel_id: p.id, id: p.id}, commonBody));
```
`pineline_id: 91` — id воронки/подразделения, привязанной к плану продаж
(остальные месяцы/годы меняются в `commonBody`, `pineline_id` пока не
проверялся на других воронках — если план заведён на другой pineline,
возможно потребуется его найти отдельно).

### 7.4 Объём запросов, автоматизируемость и приведение к общему стандарту

**Полностью автоматизируемо чистым fetch — как остальные 5 источников.**
Никакой click-driven план не нужен, отменяет прежний вывод этого раздела.

Итого **23 запроса** (1 × monthly + 22 × manager-channels) с паузой ≥3с между
каждым (правило §0), фактически ~90 секунд на fire-and-forget-скрипт с
проверкой `gaps.every(g=>g>=2.9)` внутри — тот же v3-паттерн, что и у
остальных источников (см. §0). Проверенный прогон 11.08.2026: 23/23 без
ошибок, минимальный интервал 3.999с.

**Агрегация по менеджеру** — так как план задаётся на канал, а не на
менеджера напрямую (см. §7.2): суммировать `sales_target`/`current_sales` по
всем каналам, где менеджер встречается в ответе `manager-channels`. Прогон
11.08.2026 вернул 63 CRM-профиля (среди них есть не-менеджеры/боты с
доступом к каналам, посторонний персонал — фильтровать сопоставлением с
`GET /api/v1/managers` сайта, как во всех остальных источниках).

**Импорт на сайт** — тот же механизм, что уже существовал (см.
`backend/routers/period_targets.py`): `PUT /api/v1/period-targets` с
`period: "mgr:<UUID-сайта>:Август 2026"` и `plan: <сумма target по каналам>`
для личных планов, плюс отдельный `PUT` с `period: "Август 2026"` (без
префикса) и `plan: <global.sales_target>` для плана компании целиком.
Никакой новой бэкенд-логики не требуется — эндпоинт уже поддерживал
синтетический `mgr:`-префикс с 30.07.2026.

**Известный, ожидаемый пробел (не баг):** не все менеджеры сайта обязательно
назначены ответственными хоть за один канал в CRM в конкретном месяце — если
менеджер отсутствует в агрегации, значит в CRM у него в этом месяце нет
назначенного плана ни по одному каналу; план для него на сайте не
проставляется (не 0 — просто нет записи), это нужно перепроверять при каждом
прогоне, а не считать пропуском импорта.

---

## 5. ✅ Чек-лист «актуализировать данные за период X» — ВСЕ источники сразу

Когда пользователь просит «актуализировать», «обновить данные», «подтяни
свежие цифры» и т.п. — по умолчанию значит ВСЕ ниже, не только один источник.
Если какой-то источник физически не даёт актуализировать (например, слишком
рано для месячной товарной выгрузки) — явно сказать об этом пользователю,
а не тихо пропустить:

1. **Сквозная аналитика (реклама/UTM)** — §1. `POST /import/tree-json`.
2. **Накладные (касса/отгрузка)** — §2. `POST /invoices/import`.
3. **Скидки (без учёта уценки)** — §3.5. `POST /discounts/import`.
4. **Товарная аналитика** — §2.5. Ручной перезалив: `POST /import/products`
   (Excel). Автоматический ежедневный: §2.5.1, `POST /import/products-json`
   (DOM-скрипт, без файла) — тянет диапазон «1 число текущего месяца → сегодня»,
   апсерт идемпотентен, частичный месяц НЕ проблема (перезаливается заново
   каждый день по мере роста диапазона).
5. **Планы продаж** — §7 (см. ОБЯЗАТЕЛЬНО §7.2.1 — исправление методологии
   12.08.2026). `POST /api/crm/plans/monthly` даёт все ~22-23 канала, но для
   company-wide и личных планов брать **только 3**: `Розничные продажи`,
   `Инстаграм`, `TikTok продажи`, и только город **Алматы** (на 11-12.08.2026
   это `point_id` 2/51/58 — проверять по `city`/`name.ru` при каждом прогоне,
   id не гарантированы стабильными). Company-wide план = сумма `sales_target`
   этих 3 point-ов напрямую из `points[]`. Личный план менеджера = сумма его
   `sales_target` из `POST /api/crm/plans/manager-channels` для этих же 3
   point_id (не для всех каналов, где он назначен — старая версия этого пункта
   ошибочно суммировала весь конвейер). 9 запросов на 3 периода (1×monthly +
   3×manager-channels на период), не 23. Импорт — `PUT /api/v1/period-targets`
   (личные `mgr:<UUID>:<период>` + план компании без префикса). Сумма личных
   планов НЕ обязана совпадать с company-wide — не уравнивать искусственно.
6. **Причины отказа** — §6. `POST /decline-reasons/import`. Fire-and-forget
   (166 запросов на частичный август = ~11 минут, растёт пропорционально
   дням месяца) — тянет диапазон «1 число текущего месяца → сегодня» по
   воронке `pineline_id=91` («Отдел первичных продаж»), апсерт заменяет весь
   срез manager+period+pipeline целиком (не накопление).

После каждого источника — сверка по чек-листу того раздела (например §1.7
для рекламы). Только когда все применимые источники обновлены (или явно
помечены как «не применимо в этот раз») — считать актуализацию завершённой
и отчитаться пользователю с перечнем того, что реально обновилось.

---

## 4. Главный урок сессии 26.07.2026

Если в CRM уже есть готовое посчитанное поле (как `conversion` в
`report/tree`) — **брать его как есть**, а не изобретать свою формулу поверх
сырых `leads/deals/new_leads/...`. Кастомная логика имеет смысл только там,
где в CRM реально нет готового аналога (например, разбор «слито vs в
работе» через `not_implemented`, или ABC/Pareto по источникам — этого CRM не
считает вообще).
