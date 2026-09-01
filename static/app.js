const $=id=>document.getElementById(id);
let busy=false, pollTimer=null, taskId=null;

async function api(url, options={}) {
  const r=await fetch(url, options);
  const data=await r.json().catch(()=>({}));
  if(!r.ok) throw new Error(data.detail||"Ошибка сервера");
  return data;
}
function status(t){$("connection").textContent=t}
function msg(t, cls=""){$("message").className="message "+cls;$("message").textContent=t}
function duration(s){if(!s)return"—";s=Math.floor(s);let h=Math.floor(s/3600),m=Math.floor(s%3600/60),x=s%60;return h?`${h}:${String(m).padStart(2,"0")}:${String(x).padStart(2,"0")}`:`${m}:${String(x).padStart(2,"0")}`}
async function info(){
  const url=$("url").value.trim(); if(!url||busy)return;
  try{
    status("Получаю информацию…");
    const d=await api("/api/info",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({url})});
    $("preview").classList.remove("hidden");$("thumb").src=d.thumbnail||"";$("videoTitle").textContent=d.title||"Видео";
    $("videoMeta").textContent=`${d.uploader||"Источник"} · ${duration(d.duration)}`;
    $("thumbQuality").textContent=d.height>=2160?"4K":d.height>=1440?"2K":"Video";status("Готов");
  }catch(e){status("Ошибка");msg(e.message,"error")}
}
async function start(){
  const url=$("url").value.trim(); if(!url){msg("Сначала вставь ссылку.","error");return}
  busy=true;$("downloadBtn").disabled=true;$("progressBox").classList.remove("hidden");msg("");status("Скачивание…");
  $("progressBar").style.width="0%";$("percent").textContent="0%";$("speed").textContent="—";$("eta").textContent="--:--";
  try{
    const d=await api("/api/download",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({url,quality:$("quality").value,mode:$("mode").value})});
    taskId=d.id; poll();
  }catch(e){finishError(e.message)}
}
async function poll(){
  try{
    const d=await api("/api/progress/"+taskId);
    const p=Math.max(0,Math.min(100,Number(d.percent)||0));
    $("progressBar").style.width=p+"%";$("percent").textContent=Math.round(p)+"%";$("speed").textContent=d.speed||"—";$("eta").textContent=d.eta||"--:--";$("filename").textContent=d.filename||"…";
    $("progressStatus").textContent=d.status==="processing"?"Обработка…":"Скачивание…";
    if(d.status==="done"){ $("progressBar").style.width="100%";$("percent").textContent="100%";msg("✓ Скачивание завершено","success");status("Готово");busy=false;$("downloadBtn").disabled=false;window.location.href="/api/file/"+taskId;return}
    if(d.status==="error"){finishError(d.error||"Не удалось скачать файл");return}
    pollTimer=setTimeout(poll,500);
  }catch(e){finishError(e.message)}
}
function finishError(e){busy=false;$("downloadBtn").disabled=false;status("Ошибка");msg(e,"error")}
$("pasteBtn").onclick=async()=>{try{$("url").value=await navigator.clipboard.readText();info()}catch{}};
$("url").addEventListener("paste",()=>setTimeout(info,150));
$("url").addEventListener("keydown",e=>{if(e.key==="Enter")info()});
$("url").addEventListener("input",()=>{clearTimeout(window.infoTimer);window.infoTimer=setTimeout(()=>{if($("url").value.trim())info()},700)});
$("downloadBtn").onclick=start;
