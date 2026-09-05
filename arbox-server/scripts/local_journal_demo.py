"""Local-only notification simulator; never included in release images.

Run with the published image and a read-only source mount (see deploy/README).
No real credentials are needed. Telegram and HA HTTP calls stay in this app.
"""
import asyncio
import json
import os
from pathlib import Path

import aiohttp
import uvicorn
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from app.settings import Settings
import app.notify as notify
from app.main import app, FRONTEND_DIR

settings = Settings(os.environ.get("DATA_DIR", "/data"))
settings._data["api_key"] = "local-demo"
settings.update({
    "telegram": {"enabled": False, "bot_token": "local-demo", "chat_id": "123", "kinds": []},
    "ha": {"enabled": False, "webhook_url": "http://127.0.0.1:8000/dev/ha", "kinds": []},
    "journal": {"level": "full"},
    "base_url": "http://localhost:8178",
})
notify.TG_API = "http://127.0.0.1:8000/dev/telegram"
messages = []
updates = asyncio.Queue()
update_id = 0


@app.middleware("http")
async def demo_health(request, call_next):
    if request.url.path == "/static/app.js":
        # Every demo tab must initialize its own in-memory API key. Relying
        # on /dev to seed localStorage broke direct links and isolated tabs,
        # and could retain a key entered for another server. This override
        # exists only in this launcher; production authentication is unchanged.
        source = (Path(FRONTEND_DIR) / "app.js").read_text()
        bootstrap = 'try { state.apiKey = localStorage.getItem("arbox_api_key"); } catch (e) {}'
        if source.count(bootstrap) != 1:
            raise RuntimeError("Demo API-key bootstrap no longer matches the frontend")
        source = source.replace(bootstrap, 'state.apiKey = "local-demo";')
        success = 'message.textContent = "הבדיקה נשלחה — פתחו את ההתראה וענו על השאלות. שום תשובה לא תישמר ביומן.";'
        source = source.replace(success, '''
    message.textContent = "ההודעה התקבלה בסימולטור המקומי — לא נשלחה לטלפון. ";
    const simulatorLink = document.createElement("a");
    simulatorLink.href = "/dev";
    simulatorLink.target = "_blank";
    simulatorLink.textContent = "פתח את ההודעה וענה כאן ↗";
    message.append(simulatorLink);
''')
        return Response(source,
                        media_type="application/javascript",
                        headers={"Cache-Control": "no-store"})
    response = await call_next(request)
    if request.url.path == "/api/health":
        payload = json.loads(b"".join([part async for part in response.body_iterator]))
        # UI access only. The actual Arbox client stays unconfigured.
        payload.update(configured=True, studio="סביבה מקומית — נתוני דוגמה", version="local-demo")
        return JSONResponse(payload)
    return response


@app.post("/dev/ha")
async def ha_notification(request: Request):
    messages.append({"channel": "ha", **await request.json()})
    return {"ok": True}


@app.api_route("/dev/telegram/botlocal-demo/{method}", methods=["GET", "POST"])
async def telegram(request: Request, method: str):
    if method == "getUpdates":
        try:
            update = await asyncio.wait_for(updates.get(), timeout=10)
            return {"ok": True, "result": [update]}
        except asyncio.TimeoutError:
            return {"ok": True, "result": []}
    payload = await request.json()
    if method == "sendMessage":
        messages.append({"channel": "telegram", "message": payload["text"],
                         "actions": [{"title": b["text"], "action": b.get("callback_data", "URI"), "uri": b.get("url")}
                                     for row in payload.get("reply_markup", {}).get("inline_keyboard", [])
                                     for b in row]})
    return {"ok": True}


@app.get("/dev/messages")
async def get_messages():
    return messages


@app.post("/dev/tap")
async def tap(request: Request):
    global update_id
    body = await request.json()
    if body["channel"] == "ha":
        async with aiohttp.ClientSession() as session:
            async with session.post("http://127.0.0.1:8000/api/ha/callback",
                                    headers={"X-Api-Key": "local-demo"},
                                    json={"action": body["action"], "reply_text": body.get("text")}) as response:
                return JSONResponse(await response.json(), status_code=response.status)
    update_id += 1
    if body.get("action"):
        update = {"callback_query": {"id": str(update_id), "data": body["action"],
                                     "message": {"chat": {"id": 123}}}}
    else:
        update = {"message": {"chat": {"id": 123}, "text": body.get("text", "")}}
    await updates.put({"update_id": update_id, **update})
    return {"ok": True}


@app.get("/dev", response_class=HTMLResponse)
async def demo_page():
    return """<!doctype html><html lang="he" dir="rtl"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>בדיקת משוב מקומית</title><style>
body{font:16px system-ui;background:#f4f6f9;color:#17243b;max-width:960px;margin:32px auto;padding:0 20px}
header{margin-bottom:24px}a{color:#2458a6}main{display:grid;grid-template-columns:1fr 1fr;gap:20px}
article{background:white;border:1px solid #d9e0ea;border-radius:16px;padding:18px;margin:12px 0}
p{white-space:pre-wrap;line-height:1.6}button,input{font:inherit;padding:10px;margin:4px;border:1px solid #ccd6e3;border-radius:8px}
button{cursor:pointer;background:#edf3ff}small{color:#58677d}@media(max-width:650px){main{grid-template-columns:1fr}}
</style><header><h1>🧪 בדיקת משוב מקומית</h1>
<p>סימולטור בלבד. ההודעות נשארות כאן — אין חיבור לטלפון, לחשבון Arbox או לשרת הפעיל.</p>
<a href="/settings" target="_blank">פתח הגדרות ← מעקב ← נסה בטלגרם / ב־HA</a>
<p><small>הכפתורים מפעילים את מסלולי ההתראות והתגובות של השרת. תצוגת הטלפון בפועל יכולה להיות שונה.</small></p></header>
<main><section><h2>Telegram</h2><div id="telegram"></div>
<form id="textForm"><input id="text" placeholder="טקסט לאחר לחיצה על מה עשיתי"><button>שלח</button></form></section>
<section><h2>Home Assistant</h2><div id="ha"></div></section></main><p id="status" role="status"></p>
<script>
let count=0;
async function tap(body){const r=await fetch('/dev/tap',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
if(!r.ok)document.querySelector('#status').textContent='הפעולה נכשלה: '+await r.text();}
async function refresh(){try{const rows=await(await fetch('/dev/messages')).json();
if(rows.length<count){document.getElementById('telegram').replaceChildren();document.getElementById('ha').replaceChildren();count=0;}
for(const row of rows.slice(count)){
const card=document.createElement('article'),p=document.createElement('p');p.textContent=row.message;card.append(p);
for(const action of row.actions||[]){const b=document.createElement('button');b.textContent=action.title;b.onclick=async()=>{
if(action.action==='URI'&&action.uri){window.open(action.uri,'_blank','noopener');return;}
let text;if(action.behavior==='textInput'){text=prompt(action.textInputPlaceholder||'מה עשית?');if(text===null)return;}
b.disabled=true;try{await tap({channel:row.channel,action:action.action,text});}finally{b.disabled=false;}};card.append(b);}
document.getElementById(row.channel).append(card);}count=rows.length;}catch(e){document.querySelector('#status').textContent=e.message;}}
document.querySelector('#textForm').onsubmit=async(e)=>{e.preventDefault();await tap({channel:'telegram',text:document.querySelector('#text').value});document.querySelector('#text').value='';};
refresh();setInterval(refresh,750);
</script></html>"""


if __name__ == "__main__":
    # Register development endpoints ahead of the production SPA catch-all.
    app.router.routes.sort(key=lambda route: not getattr(route, "path", "").startswith("/dev"))
    uvicorn.run(app, host="0.0.0.0", port=8000)
