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

## 0. ⚠️ ОБЯЗАТЕЛЬНО ПЕРЕД ЛЮБЫМ ДЕЙСТВИЕМ В ЭТОЙ ВКЛАДКЕ — guard на rate-limit

26.07.2026 массовая выгрузка без паузы (~5 запросов/сек к zymyran.com) была
расценена сотрудниками как атака — доступ заблокировали. Правило теперь
жёсткое: **минимум 3 секунды между ЛЮБЫМИ запросами к torgstore.zymyran.com**,
и оно не должно зависеть от того, помнит агент об этом в моменте или нет.

**Перед первым запросом к CRM в этой вкладке (и после каждого reload/новой
навигации на zymyran.com — JS-состояние вкладки сбрасывается) вставить в
консоль этот снипет:**

```js
// CRM rate-limit guard — переживает reload (localStorage, а не память вкладки).
// Перехватывает fetch И XMLHttpRequest — то есть работает и для ручных fetch
// (fetchCompact ниже), и для запросов, которые UI сам шлёт при клике/наведении
// (например total-sum-ajax при смене фильтра на экране "Топ продаж").
(function(){
  const KEY='__crm_rl_last_ts', MIN_GAP=3000;
  async function throttle(){
    const last=Number(localStorage.getItem(KEY)||0), now=Date.now();
    const wait=MIN_GAP-(now-last);
    if(wait>0) await new Promise(r=>setTimeout(r,wait));
    localStorage.setItem(KEY,String(Date.now()));
  }
  window.__crmThrottle = throttle;
  const origFetch = window.fetch;
  window.fetch = async function(input, init){
    const url = typeof input==='string' ? input : (input && input.url) || '';
    if(url.includes('zymyran.com')) await throttle();
    return origFetch.call(this, input, init);
  };
  const OrigXHR = window.XMLHttpRequest;
  function PatchedXHR(){
    const xhr = new OrigXHR();
    const origOpen = xhr.open;
    xhr.open = function(method, url, ...rest){
      this.__isCrm = String(url).includes('zymyran.com');
      return origOpen.call(this, method, url, ...rest);
    };
    const origSend = xhr.send;
    xhr.send = function(...args){
      if(this.__isCrm){ throttle().then(()=>origSend.apply(this,args)); }
      else { origSend.apply(this,args); }
    };
    return xhr;
  }
  window.XMLHttpRequest = PatchedXHR;
  console.log('%c[CRM rate-limit guard armed] минимум 3с между запросами к zymyran.com, живёт до reload', 'color:#0a0;font-weight:bold');
})();
```

После этого `fetchCompact` (раздел 1.6) и любой клик по фильтрам/наведение на
странице «Топ продаж» (раздел 2) автоматически проходят через паузу — ничего
дополнительно помнить не нужно, пока не случится reload/навигация на новый URL
внутри zymyran.com (тогда guard нужно вставить заново).

Если это делает агент через браузерную автоматизацию (Claude in Chrome) —
первым шагом при любом взаимодействии с вкладкой torgstore.zymyran.com всегда
идёт вставка и выполнение этого снипета через `javascript_tool`, и только
потом — навигация/клики/остальные fetch.

---

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

Источник в CRM: «Аналитика → Топ продаж» → виджет «Менеджеры» (фильтр
город + канал). Один снимок на менеджера/период/канал/город.

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

### Готовый скрипт (консоль вкладки CRM, guard из §0 уже должен быть вставлен)

```js
const UCENKA_IDS = [27, 29, 228, 236, 243, 577];

async function totalSumRaw(crmManagerId, dateFrom, dateTo, shipmentPoint){
  const dr = encodeURIComponent(`${dateFrom} - ${dateTo}`); // формат ДД/ММ/ГГГГ
  const url = `/service/warehouse/products/requests/total-sum-ajax?search=&sortby=default&smart=&sku=&client_name=&type=any&date=other&date_other=${dr}&date_assembled=${dr}&date_completed=${dr}&date_delivered=${dr}&date_returned=${dr}&date_canceled=${dr}&date_debt=${dr}&invoice_sum_from=&invoice_sum_to=&client_id=&manager_id=${crmManagerId}&assembler_id=all&packager_id=all&courier_id=all&payments_method=0&discount_id=any&sale_channel=0&utm_source=0&utm_medium=&utm_campaign=&utm_term=&utm_content=&bill_id=0&service_point=0&shipment_point=${shipmentPoint}&return_warehouse=0&with_docs=all&promotion=all&via_source=0&page=0&path=/service/warehouse/products/requests`;
  const r = await fetch(url, {headers:{'Accept':'application/json','X-Requested-With':'XMLHttpRequest'}});
  const j = await r.json();
  const num = s => Number(String(s).replace(/[^\d.-]/g,'')) || 0;
  return { sale: num(j.totalSum), discount: num(j.totalSumWithoutDiscount) };
}

// Пример: Ерганат Аубакир (CRM id 566), Июль 2026
async function discountClean(crmManagerId, dateFrom, dateTo){
  const all = await totalSumRaw(crmManagerId, dateFrom, dateTo, 0);
  let ucenkaSale = 0, ucenkaDiscount = 0;
  for (const id of UCENKA_IDS){
    const u = await totalSumRaw(crmManagerId, dateFrom, dateTo, id); // throttle сам подождёт 3с
    ucenkaSale += u.sale; ucenkaDiscount += u.discount;
  }
  return {
    sale_amount: all.sale - ucenkaSale,
    discount_amount: all.discount - ucenkaDiscount,
    _debug: { all, ucenkaSale, ucenkaDiscount }
  };
}
// await discountClean(566, '01/07/2026', '31/07/2026')
```

Результат (`sale_amount`/`discount_amount`) передавать в `POST /discounts/import`
как раньше (раздел 2) — формат payload не меняется, меняется только то, ЧТО
подставляется в эти два поля (очищенное от уценки, а не сырое).

### Проверено 07.08.2026 (Ерганат Аубакир, июль 2026)

- Все склады: sale=44,584,633₸, discount=3,917,087₸
- Только «Уценка (Хаб)» (id 29): sale=134,550₸, discount=14,950₸
- Только «Уценка» (id 27): sale=0₸, discount=0₸ (у этого менеджера пусто)

Т.е. у этого конкретного менеджера уценка — маленькая доля (~0.3%), но по
другим менеджерам/периодам доля может быть куда больше — этим и объясняется,
зачем это разделять, а не просто списать как погрешность.

**Пока НЕ переделаны задним числом** уже загруженные скидки за июнь/июль/август
(они всё ещё считают уценку как часть скидки менеджера) — это отдельная задача,
делать только по явному запросу пользователя.

## 4. Главный урок сессии 26.07.2026

Если в CRM уже есть готовое посчитанное поле (как `conversion` в
`report/tree`) — **брать его как есть**, а не изобретать свою формулу поверх
сырых `leads/deals/new_leads/...`. Кастомная логика имеет смысл только там,
где в CRM реально нет готового аналога (например, разбор «слито vs в
работе» через `not_implemented`, или ABC/Pareto по источникам — этого CRM не
считает вообще).
