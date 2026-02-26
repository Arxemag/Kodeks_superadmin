# POST /admin/dirs — соответствие параметров каталога и кода

Ожидаемые параметры каталога (пример) и откуда они берутся в нашем коде.

---

## Системные параметры (без привязки к кабинетам)

| Параметр | Пример значения | В нашем коде |
|----------|-----------------|--------------|
| setup2 | (пусто или значение) | Из формы каталога; в `SETUP_DIR_DEFAULTS` (cabinet_acl) при POST /admin/dir не задаётся, в ответе попадает в full_form. В `_BASE_PARAMS_FALLBACK` (admin_dirs): `""` если нет в форме. |
| n | 2 | Форма каталога или fallback `"2"`. |
| set | (пусто) | `_BASE_PARAMS_FALLBACK["set"] = ""`. |
| cmd | save | Всегда добавляется в `build_admin_dirs_form`: `form["cmd"] = "save"`. |
| path, action, del, base | (если каталог отдаёт в форме) | Не добавляем в admin_dirs; берём из full_form (ответ каталога после POST /admin/dir с setup2). В init_company при отсутствии в форме подставляем только **base** из acl_form_base. |

---

## Блок «печать / сохранение / копирование»

| Параметр | Пример значения | В нашем коде |
|----------|-----------------|--------------|
| acl_unstoragero | (пусто) | `_BASE_PARAMS_FALLBACK`: `""`. |
| acl_unstorage | (пусто) | `_BASE_PARAMS_FALLBACK`: `""`. |
| acl_unstoragefull | (пусто) | `_BASE_PARAMS_FALLBACK`: `""`. |
| acl_unprint | (пусто) | `_BASE_PARAMS_FALLBACK`: `""`. |
| unprinttext | Установлен административный запрет… | `_BASE_PARAMS_FALLBACK`: полный текст. |
| acl_unsave | (пусто) | `_BASE_PARAMS_FALLBACK`: `""`. |
| unsavetext | Установлен административный запрет… | `_BASE_PARAMS_FALLBACK`: полный текст. |
| acl_uncopy | (пусто) | `_BASE_PARAMS_FALLBACK`: `""`. |
| uncopytext | Установлен административный запрет… | `_BASE_PARAMS_FALLBACK`: полный текст. |
| uncopylength | 0 | `_BASE_PARAMS_FALLBACK`: `"0"`. |

---

## Общие ACL и опции

| Параметр | Пример значения | В нашем коде |
|----------|-----------------|--------------|
| packprocess | on | `_BASE_PARAMS_FALLBACK`: `"on"`. |
| acl_packprocess | {"kw":["1","8"]} | Из формы; если нет — `_EMPTY_ACL_JSON` (`{"kw":[]}`). |
| acl_infoboardsedit | {"kw":["1","530"]} | Из формы; если нет — `_EMPTY_ACL_JSON`. |
| inspectionactuallinks | on | `_BASE_PARAMS_FALLBACK`: `"on"`. |
| acl_inspectactuallinks | {"kw":["1","4","8","15","321"]} | Из формы; если нет — `_EMPTY_ACL_JSON`. |
| desiredtab | -1 | `_BASE_PARAMS_FALLBACK`: `"-1"`. |
| desiredtabas | -1 | `_BASE_PARAMS_FALLBACK`: `"-1"`. |

---

## Кабинеты (acl_infoboard_*, infoboard_*)

Для каждого кабинета (например p000a, p000b, …) каталог ожидает:

| Параметр | Когда есть доступ | Когда нет доступа | В нашем коде |
|----------|-------------------|-------------------|--------------|
| acl_infoboard_{id} | {"kw":["1","321"]} | (пусто или {"kw":[]}) | Из full_form; для кабинетов из acl_overrides подставляем `acl_to_json(group_ids)`. |
| acl_infoboard_{id}_view | то же или пусто | (пусто) | Аналогично; при наличии acl_overrides ставим то же значение, что и без _view. |
| infoboard_{id} | on | (нет поля или пусто) | Ставим `"on"` только если в ACL есть группы (`has_groups`). |
| infoboard_{id}_view | on | (нет поля) | То же. |

- Список кабинетов берётся из **формы каталога** (все ключи `acl_infoboard_*` из full_form) плюс ключи из **acl_overrides** (только те, что есть в форме в init_company).
- **extra_board_ids** в init_company не передаём — в форму не добавляем кабинеты, которых нет в ответе каталога.

---

## Откуда берётся форма

1. **init_company / add_cabinet_group:**  
   GET /admin/dirs → GET /admin/dir?n=X → POST /admin/dir с setup2=«Настройки сервисов» и дефолтами из `SETUP_DIR_DEFAULTS` (cabinet_acl). В ответе — HTML формы с полями выше.

2. **Парсинг:**  
   `parse_full_form_from_html(setup_html)` даёт full_form (все input). При отсутствии acl-полей — fallback `parse_acl_from_html(setup_html)` (init_company).

3. **Подстановка:**  
   `build_admin_dirs_form(full_form, acl_overrides_safe, extra_board_ids=None)` дополняет форму из `_BASE_PARAMS_FALLBACK` при отсутствии ключа и подставляет только наши acl_infoboard_* / infoboard_* для кабинетов из формы.

4. **Отправка:**  
   urlencode списка пар (имя, значение), без удаления «пустых» полей. Заголовки: Referer, Origin, Content-Type: application/x-www-form-urlencoded.

---

## Что проверить при 500 или порче DOCS

- В теле запроса должны быть **все** системные поля из таблиц выше (хотя бы с пустыми значениями), чтобы каталог не воспринял отсутствие поля как сброс.
- Не должны появляться **лишние** имена кабинетов (acl_infoboard_*), которых не было в форме каталога — для этого в init_company не передаём extra_board_ids и фильтруем acl_overrides по form_board_keys.
- Для кабинетов **без** групп: в каталоге иногда приходит пустая строка; мы, если в форме значения нет, подставляем `{"kw":[]}`. Если каталог ждёт строго пустую строку, можно будет поменять fallback на `""` только для acl_infoboard_* (сейчас не меняем, чтобы не ломать другие сценарии).

Если пришлёте сохранённое тело запроса (admin_dirs_5xx_request.txt) и список полей из браузера при успешном сохранении — можно построчно сверить отличия.

---

## Замечание по вашему списку

В перечне параметров каталога **нет** полей `path`, `action`, `del`, `base`. Значит, для **итогового** POST /admin/dirs каталог может их не ожидать (они могли относиться к предыдущему шагу POST /admin/dir с setup2).

Сейчас мы в init_company подставляем **base** в full_form, если его нет в форме. Если каталог при POST /admin/dirs действительно не ждёт base и из‑за него отдаёт 500, можно отключить подстановку base для этого запроса (оставлять в теле только то, что вернул каталог в форме).
