"use strict";
const $ = (id) => document.getElementById(id);
let catalog = [], activeStarted = null, journalNext = null, journalLines = [];

async function api(path, options={}) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || response.statusText);
  return data;
}
function jsonPost(path, body={}) { return api(path,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)}); }
function cell(row,value){const td=document.createElement("td");if(value instanceof HTMLElement)td.appendChild(value);else td.textContent=String(value);row.appendChild(td)}
function bytes(value){let n=Number(value)||0;for(const unit of ["B","KiB","MiB","GiB"]){if(n<1024||unit==="GiB")return `${n.toFixed(unit==="B"?0:1)} ${unit}`;n/=1024}}
function duration(value){let n=Math.max(0,Math.floor(Number(value)||0));return [Math.floor(n/3600),Math.floor(n%3600/60),n%60].map(v=>String(v).padStart(2,"0")).join(":")}
function number(id){const value=Number($(id).value);return Number.isFinite(value)?value:0}

function renderCatalog(){
  const groups=[...new Set(catalog.map(x=>x.group))];
  $("testGroup").innerHTML=groups.map(x=>`<option>${x}</option>`).join("");
  renderTests();
}
function renderTests(){
  const tests=catalog.filter(x=>x.group===$("testGroup").value);
  $("testId").innerHTML=tests.map(x=>`<option value="${x.id}">${x.id} — ${x.name}</option>`).join("");
}
function sessionPayload(){
  const test=catalog.find(x=>x.id===$("testId").value)||{};
  return {session_number:$("sessionNumber").value,test_group:$("testGroup").value,test_id:$("testId").value,test_name:test.name||"",custom_name:$("customName").value,repeat_number:number("repeatNumber"),siren_type:$("sirenType").value,operator:$("operator").value,ego_speed_kph:number("egoSpeed"),source_speed_kph:number("sourceSpeed"),distance_m:number("distance"),temperature_c:number("temperature"),wind_speed_mps:number("windSpeed"),wind_direction_deg:number("windDirection"),precipitation:$("precipitation").value,road_surface:$("roadSurface").value,comment:$("comment").value};
}
async function startSession(){
  try{$("sessionFormState").textContent="Запуск...";await jsonPost("/api/sessions/start",sessionPayload());await updateState(true)}
  catch(error){$("sessionFormState").textContent=error.message}
}
async function stopSession(){
  try{$("sessionFormState").textContent="Остановка...";await jsonPost("/api/sessions/stop");await updateState(true);await updateUploads(true)}
  catch(error){$("sessionFormState").textContent=error.message}
}
function renderHistory(history){
  $("history").innerHTML="";
  history.forEach(item=>{const row=document.createElement("tr");[item.source_id||"—",item.session_number||"—",item.test_id||"—",item.repeat_number||"—",item.started_utc||"—",item.status||"—",item.log_name||"—"].forEach(x=>cell(row,x));$("history").appendChild(row)})
}
async function updateState(force=false){
  if(!force&&!$("sessions").classList.contains("active")&&!$("interfaces").classList.contains("active"))return;
  try{
    const state=await api("/api/state"), session=state.session, active=session.active;
    $("sourceBadge").textContent=`Source ${state.source_id}`;
    $("title").textContent=`Источник сирены — ${state.source_name}`;
    $("pageState").textContent=state.interfaces.nmea.running?"NMEA интерфейс активен":"NMEA не активен";
    $("recordIndicator").classList.toggle("active",Boolean(active));
    $("recordText").textContent=active?"Запись":"Остановлено";$("start").disabled=Boolean(active);$("stop").disabled=!active;
    if(active&&!activeStarted)activeStarted=Date.parse(active.started_utc);if(!active)activeStarted=null;
    $("timer").textContent=duration(activeStarted?(Date.now()-activeStarted)/1000:0);
    $("activeFile").textContent=active?.log_name||"—";$("activeSize").textContent=bytes(session.size);
    $("activeTrigger").textContent=session.trigger_active?"ON":"OFF";$("activeAudio").textContent=session.audio.blocks;
    $("activeNmea").textContent=`${state.interfaces.nmea.valid} / err ${state.interfaces.nmea.errors}`;
    renderHistory(session.history);renderInterfacesState(state.interfaces);
  }catch(error){$("pageState").textContent=error.message}
}
function action(text,handler,cls=""){const button=document.createElement("button");button.textContent=text;button.className=cls;button.onclick=handler;return button}
function renderLogs(logs){
  $("logs").innerHTML="";
  logs.forEach(log=>{const row=document.createElement("tr");cell(row,log.name);cell(row,bytes(log.size));cell(row,log.modified_utc||"—");cell(row,log.status||"local");cell(row,bytes(log.progress_bytes));cell(row,log.error||"—");const actions=document.createElement("div");actions.className="upload-actions";const download=document.createElement("a");download.href=`/api/uploads/local?name=${encodeURIComponent(log.name)}`;download.textContent="Скачать";actions.append(download);if(["uploading","upload_queued","canceling"].includes(log.status))actions.append(action("Отменить",()=>cancelUpload(log.name),"danger"));else actions.append(action("В S3",()=>uploadLog(log.name)));actions.append(action("Удалить",()=>deleteLog(log.name),"danger"));cell(row,actions);$("logs").appendChild(row)})
}
async function updateUploads(force=false){
  if(!force&&!$("uploads").classList.contains("active"))return;
  try{const state=await api("/api/uploads/state");renderLogs(state.logs);$("logsDir").value=state.local_dir;$("localFree").textContent=bytes(state.local_free_bytes);const s=state.settings;$("s3Endpoint").value=s.endpoint_url||"";$("s3Bucket").value=s.bucket||"";$("s3Region").value=s.region||"us-east-1";$("s3Prefix").value=s.prefix||"";$("s3Access").value=s.access_key_id||"";$("s3Auto").checked=Boolean(s.auto_upload);$("uploadsState").textContent=`${state.logs.length} файлов, S3 ${s.ready?"готов":"не настроен"}`}
  catch(error){$("uploadsState").textContent=error.message}
}
async function saveS3(){try{await jsonPost("/api/uploads/config",{endpoint_url:$("s3Endpoint").value,bucket:$("s3Bucket").value,region:$("s3Region").value,prefix:$("s3Prefix").value,access_key_id:$("s3Access").value,secret_access_key:$("s3Secret").value,session_token:$("s3Token").value,auto_upload:$("s3Auto").checked});$("s3Secret").value="";$("s3Token").value="";await updateUploads(true)}catch(e){$("uploadsState").textContent=e.message}}
async function uploadLog(name){try{await jsonPost("/api/uploads/upload",{name});await updateUploads(true)}catch(e){$("uploadsState").textContent=e.message}}
async function cancelUpload(name){try{await jsonPost("/api/uploads/cancel",{name});await updateUploads(true)}catch(e){$("uploadsState").textContent=e.message}}
async function deleteLog(name){if(!confirm(`Удалить ${name}?`))return;try{await jsonPost("/api/uploads/delete",{name});await updateUploads(true)}catch(e){$("uploadsState").textContent=e.message}}

function renderInterfacesState(state){
  const c=state.config,n=c.nmea,g=c.siren_trigger,a=c.audio;
  if(document.activeElement?.tagName!=="INPUT"&&document.activeElement?.tagName!=="SELECT"){
    $("nmeaType").value=n.type;$("usbDevice").value=n.usb_device;$("usbBaud").value=n.usb_baud;$("uartDevice").value=n.uart_device;$("uartBaud").value=n.uart_baud;$("udpBind").value=n.udp_bind;$("udpPort").value=n.udp_port;
    $("gpioPin").value=g.gpio_bcm;$("gpioPull").value=g.pull;$("gpioDebounce").value=g.debounce_ms;$("gpioMock").checked=Boolean(g.mock);
    $("audioEnabled").checked=Boolean(a.enabled);$("alsaDevice").value=a.alsa_device;$("audioRate").value=a.sample_rate_hz;$("audioChannels").value=a.channels;$("audioFormat").value=a.sample_format;$("audioBytes").value=a.bytes_per_sample;$("audioBlock").value=a.block_frames;
  }
  $("triggerLamp").parentElement.classList.toggle("active",state.trigger.active);$("triggerState").textContent=state.trigger.active?"ON":"OFF";
  const fix=state.nmea.last_fix||{};$("fixPosition").textContent=fix.latitude_deg===undefined?"—":`${fix.latitude_deg.toFixed(8)}, ${fix.longitude_deg.toFixed(8)}`;$("fixAltitude").textContent=fix.altitude_m===undefined?"—":`${fix.altitude_m.toFixed(2)} м`;$("fixSpeed").textContent=fix.speed_mps===undefined?"—":`${(fix.speed_mps*3.6).toFixed(2)} км/ч`;$("fixHeading").textContent=fix.heading_rad===undefined?"—":`${(fix.heading_rad*180/Math.PI).toFixed(1)}°`;$("fixSatellites").textContent=fix.satellites??"—";$("nmeaCounters").textContent=`${state.nmea.valid} / ${state.nmea.errors}`;$("nmeaStatus").textContent=state.nmea.error||`${state.nmea.running?"работает":"остановлен"}, ${state.nmea.bytes} байт`;
}
function interfacesPayload(){return{nmea:{type:$("nmeaType").value,usb_device:$("usbDevice").value,usb_baud:number("usbBaud"),uart_device:$("uartDevice").value,uart_baud:number("uartBaud"),udp_bind:$("udpBind").value,udp_port:number("udpPort")},siren_trigger:{gpio_bcm:number("gpioPin"),pull:$("gpioPull").value,debounce_ms:number("gpioDebounce"),mock:$("gpioMock").checked},audio:{enabled:$("audioEnabled").checked,alsa_device:$("alsaDevice").value,sample_rate_hz:number("audioRate"),channels:number("audioChannels"),sample_format:$("audioFormat").value,bytes_per_sample:number("audioBytes"),block_frames:number("audioBlock")}}}
async function saveInterfaces(){try{$("interfacesState").textContent="Применение...";await jsonPost("/api/interfaces",interfacesPayload());$("interfacesState").textContent="Применено";await updateState(true)}catch(e){$("interfacesState").textContent=e.message}}
async function simulate(active){try{await jsonPost("/api/interfaces/simulate-trigger",{active});await updateState(true)}catch(e){$("interfacesState").textContent=e.message}}
async function updateJournal(force=false){if(!force&&(!$("journal").classList.contains("active")||!$("journalAuto").checked))return;try{const path=journalNext===null?"/api/journal":`/api/journal?after=${journalNext-1}`;const data=await api(path);journalNext=data.next_sequence;journalLines.push(...data.lines);journalLines=journalLines.slice(-500);$("journalOutput").textContent=journalLines.map(x=>`${x.time} ${x.level.padEnd(7)} ${x.message}`).join("\n");$("journalOutput").scrollTop=$("journalOutput").scrollHeight}catch(_){}}

document.querySelectorAll(".tab").forEach(tab=>tab.onclick=()=>{document.querySelectorAll(".tab,.view").forEach(x=>x.classList.remove("active"));tab.classList.add("active");$(tab.dataset.view).classList.add("active");if(tab.dataset.view==="uploads")updateUploads(true);if(tab.dataset.view==="interfaces")updateState(true);if(tab.dataset.view==="journal")updateJournal(true)});
$("testGroup").onchange=renderTests;$("start").onclick=startSession;$("stop").onclick=stopSession;$("uploadsRefresh").onclick=()=>updateUploads(true);$("s3Save").onclick=saveS3;$("s3Test").onclick=async()=>{try{const x=await jsonPost("/api/uploads/test");$("uploadsState").textContent=`S3 OK: ${x.bucket}`}catch(e){$("uploadsState").textContent=e.message}};$("logsDirSave").onclick=async()=>{try{await jsonPost("/api/uploads/local-dir",{path:$("logsDir").value});await updateUploads(true)}catch(e){$("uploadsState").textContent=e.message}};$("interfacesRefresh").onclick=()=>updateState(true);$("interfacesSave").onclick=saveInterfaces;$("triggerOn").onclick=()=>simulate(true);$("triggerOff").onclick=()=>simulate(false);$("journalRefresh").onclick=()=>updateJournal(true);$("journalCopy").onclick=()=>navigator.clipboard.writeText($("journalOutput").textContent);$("journalClear").onclick=async()=>{await jsonPost("/api/journal/clear");journalLines=[];journalNext=null;$("journalOutput").textContent=""};
(async()=>{const data=await api("/api/sessions/catalog");catalog=data.tests;renderCatalog();await updateState(true);await updateUploads(true)})();setInterval(()=>updateState(),1000);setInterval(()=>updateUploads(),1500);setInterval(()=>updateJournal(),1000);
