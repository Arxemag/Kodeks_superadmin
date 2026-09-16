"""
Мини-админка таблицы reg_services (reg_number -> base_url) на Auth API (порт 8000).

UI:   GET  /admin/reg-services           — HTML-страница управления
API:  GET  /api/expert/reg-services      — список
      POST /api/expert/reg-services      — добавить/изменить {reg_number, base_url} (upsert)
      DELETE /api/expert/reg-services/{reg_number} — удалить

ЗАПИСЬ защищена токеном: POST и DELETE требуют заголовок X-Api-Token, равный ADMIN_API_TOKEN.
Чтение (GET списка и HTML-страница) открыто — по нему нельзя ничего сломать, а фронт может
брать оттуда хост кабинета по регу. Запись переписывает соответствие «рег -> каталог» для всей
системы, а порт 8000 бывает доступен снаружи, поэтому она закрыта.
Без ADMIN_API_TOKEN в .env Auth API не стартует (проверка в app.lifespan).
"""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from common.config import Settings, get_settings
from common.db import get_db
from common.logger import get_logger


logger = get_logger("auth.reg_admin")
router = APIRouter()


async def require_admin_token(
    x_api_token: str | None = Header(default=None, alias="X-Api-Token"),
    settings: Settings = Depends(get_settings),
) -> None:
    """
    Пускает дальше только с верным X-Api-Token. Сравнение через compare_digest,
    чтобы по времени ответа нельзя было подбирать токен посимвольно.
    """
    expected = (getattr(settings, "ADMIN_API_TOKEN", "") or "").strip()
    if not expected:
        # Штатно сюда не попасть: пустой токен ловится при старте приложения.
        logger.error("ADMIN_API_TOKEN пуст — запись в reg_services отклонена")
        raise HTTPException(status_code=503, detail="ADMIN_API_TOKEN не настроен")
    if not x_api_token or not secrets.compare_digest(x_api_token, expected):
        logger.warning("отклонена запись в reg_services: неверный или отсутствующий X-Api-Token")
        raise HTTPException(status_code=401, detail="Требуется корректный заголовок X-Api-Token")


class RegServiceIn(BaseModel):
    reg_number: str
    base_url: str


@router.get("/api/expert/reg-services")
async def list_reg_services(
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Список всех записей reg_services."""
    tbl = settings.DB_TABLE_REG_SERVICES
    res = await db.execute(text(f"SELECT reg_number, base_url FROM {tbl} ORDER BY reg_number"))
    return [{"reg_number": r[0], "base_url": r[1]} for r in res]


@router.post("/api/expert/reg-services", dependencies=[Depends(require_admin_token)])
async def upsert_reg_service(
    body: RegServiceIn,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Добавить или изменить reg -> base_url (upsert по reg_number)."""
    tbl = settings.DB_TABLE_REG_SERVICES
    reg = (body.reg_number or "").strip()
    url = (body.base_url or "").strip().rstrip("/")
    if not reg or not url:
        raise HTTPException(status_code=400, detail="reg_number и base_url обязательны")
    await db.execute(
        text(f"""
            INSERT INTO {tbl} (reg_number, base_url) VALUES (:r, :u)
            ON CONFLICT (reg_number) DO UPDATE SET base_url = EXCLUDED.base_url
        """),
        {"r": reg, "u": url},
    )
    await db.commit()
    logger.info("reg_services upsert reg=%r base_url=%r", reg, url)
    return {"reg_number": reg, "base_url": url}


@router.delete("/api/expert/reg-services/{reg_number}", dependencies=[Depends(require_admin_token)])
async def delete_reg_service(
    reg_number: str,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Удалить запись по reg_number."""
    tbl = settings.DB_TABLE_REG_SERVICES
    res = await db.execute(text(f"DELETE FROM {tbl} WHERE reg_number = :r"), {"r": reg_number})
    await db.commit()
    logger.info("reg_services delete reg=%r rowcount=%s", reg_number, res.rowcount)
    return {"deleted": reg_number, "rowcount": res.rowcount}


@router.get("/admin/reg-services", response_class=HTMLResponse, include_in_schema=False)
async def reg_admin_page() -> HTMLResponse:
    """HTML-страница управления таблицей reg_services."""
    return HTMLResponse(_HTML)


_HTML = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>reg_services — управление</title>
<style>
  body{font-family:system-ui,Arial,sans-serif;margin:24px;color:#222;background:#fafafa}
  h1{font-size:20px} table{border-collapse:collapse;width:100%;max-width:900px;background:#fff}
  th,td{border:1px solid #ddd;padding:8px;text-align:left;font-size:14px}
  th{background:#f0f0f0} input{width:100%;box-sizing:border-box;padding:6px;font-size:14px}
  button{padding:6px 12px;font-size:14px;cursor:pointer;border:1px solid #bbb;border-radius:4px;background:#fff}
  button.save{border-color:#2a7;color:#176} button.del{border-color:#c44;color:#a22}
  .add{margin:16px 0;max-width:900px;display:flex;gap:8px} .add input{flex:1}
  #msg{margin:10px 0;min-height:18px;font-size:14px} .ok{color:#176}.err{color:#a22}
  .reg{width:140px}
</style></head>
<body>
<h1>reg_services — справочник reg → base_url каталога</h1>
<div class="add">
  <input id="token" type="password" placeholder="X-Api-Token (нужен только для сохранения и удаления)">
</div>
<div id="msg"></div>
<div class="add">
  <input id="newReg" class="reg" placeholder="reg (напр. 465302)">
  <input id="newUrl" placeholder="base_url (напр. https://cabinet-03.kodeks.expert)">
  <button class="save" onclick="addRow()">Добавить</button>
  <button onclick="load()">Обновить</button>
</div>
<table><thead><tr><th class="reg">reg</th><th>base_url</th><th style="width:170px">Действия</th></tr></thead>
<tbody id="rows"></tbody></table>
<script>
const API="/api/expert/reg-services";
function msg(t,ok){const m=document.getElementById("msg");m.textContent=t;m.className=ok?"ok":"err";}
// Токен держим только в пределах вкладки: чтение справочника открыто, он нужен лишь на запись.
function token(){return (document.getElementById("token").value||"").trim();}
function authHeaders(extra){const h=Object.assign({},extra||{});const t=token();if(t)h["X-Api-Token"]=t;return h;}
function saveToken(){try{sessionStorage.setItem("regAdminToken",token());}catch(e){}}
function restoreToken(){try{const t=sessionStorage.getItem("regAdminToken");if(t)document.getElementById("token").value=t;}catch(e){}}
async function load(){
  try{const r=await fetch(API);const data=await r.json();
    const tb=document.getElementById("rows");tb.innerHTML="";
    data.forEach(row=>{
      const tr=document.createElement("tr");
      tr.innerHTML=`<td>${row.reg_number}</td>
        <td><input value="${(row.base_url||'').replace(/"/g,'&quot;')}" id="u_${row.reg_number}"></td>
        <td><button class="save" onclick="save('${row.reg_number}')">Сохранить</button>
            <button class="del" onclick="del('${row.reg_number}')">Удалить</button></td>`;
      tb.appendChild(tr);
    });
    msg(`Загружено записей: ${data.length}`,true);
  }catch(e){msg("Ошибка загрузки: "+e,false);}
}
async function save(reg){
  const url=document.getElementById("u_"+reg).value.trim();
  await upsert(reg,url);
}
async function addRow(){
  const reg=document.getElementById("newReg").value.trim();
  const url=document.getElementById("newUrl").value.trim();
  if(!reg||!url){msg("Заполните reg и base_url",false);return;}
  await upsert(reg,url);
  document.getElementById("newReg").value="";document.getElementById("newUrl").value="";
}
async function upsert(reg,url){
  if(!token()){msg("Укажите X-Api-Token — без него запись запрещена",false);return;}
  try{const r=await fetch(API,{method:"POST",headers:authHeaders({"Content-Type":"application/json"}),
        body:JSON.stringify({reg_number:reg,base_url:url})});
    if(r.status===401){throw new Error("неверный X-Api-Token");}
    if(!r.ok){throw new Error((await r.json()).detail||r.status);}
    saveToken();
    msg(`Сохранено: ${reg}`,true);load();
  }catch(e){msg("Ошибка сохранения: "+e,false);}
}
async function del(reg){
  if(!confirm("Удалить "+reg+"?"))return;
  if(!token()){msg("Укажите X-Api-Token — без него удаление запрещено",false);return;}
  try{const r=await fetch(API+"/"+encodeURIComponent(reg),{method:"DELETE",headers:authHeaders()});
    if(r.status===401){throw new Error("неверный X-Api-Token");}
    if(!r.ok)throw new Error(r.status);
    msg(`Удалено: ${reg}`,true);load();
  }catch(e){msg("Ошибка удаления: "+e,false);}
}
restoreToken();
load();
</script>
</body></html>
"""
