"use strict";
const $ = (selector) => document.querySelector(selector);
const csrf = $('meta[name="csrf-token"]').content;
let snapshot = null, page = 1, tab = 'overview', settingsLoaded = false, toastTimer;
const titles = {
  overview: ['Backup overview', 'Keep an eye on your clips, queue, and cloud storage.'],
  clips: ['Clip history', 'Every discovered clip has a place in the queue.'],
  activity: ['Activity log', 'See what the backup worker is doing on your server.'],
  settings: ['Backup settings', 'Choose your storage limits and transfer schedule.'],
  setup: ['Connections', 'Connect your accounts and choose your local storage.']
};
const labels = {download:'To download', upload:'To upload', uploaded:'Uploaded', expired:'Expired', aged_out:'Outside window', oversized:'Over limit'};
const formatNumber = (n) => Number(n || 0).toLocaleString();
const formatGB = (bytes) => (Number(bytes || 0) / 1e9).toLocaleString(undefined, {maximumFractionDigits:3}) + ' GB';
function formatBytes(bytes){if(bytes>=1e9)return formatGB(bytes);if(bytes>=1e6)return (bytes/1e6).toFixed(1)+' MB';if(bytes>=1000)return (bytes/1000).toFixed(1)+' KB';return bytes+' B';}
function date(value) { return value ? new Date(typeof value === 'number' ? value * 1000 : value).toLocaleString(undefined, {month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}) : '—'; }
function toast(message, error=false) {const el=$('#toast'); el.textContent=message; el.classList.toggle('error',error); el.classList.remove('hidden'); clearTimeout(toastTimer); toastTimer=setTimeout(()=>el.classList.add('hidden'),error?10000:5000);}
async function api(path, data) {
  const response=await fetch(path, data===undefined?{cache:'no-store'}:{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(data)});
  let result; try {result=await response.json();} catch {throw new Error('The local server returned an unexpected response.');}
  if(!response.ok) throw new Error(result.error || 'The operation failed.');
  return result;
}
async function action(button, fn) {
  button.disabled=true;
  try {await fn(); await refresh();} catch(error) {toast(error.message,true);} finally {button.disabled=false;}
}
function navigate() {
  tab=location.hash.slice(1); if(!titles[tab]) tab='overview';
  document.querySelectorAll('[data-page]').forEach(el=>el.classList.toggle('hidden',el.dataset.page!==tab));
  document.querySelectorAll('[data-tab]').forEach(el=>el.classList.toggle('active',el.dataset.tab===tab));
  $('#page-title').textContent=titles[tab][0]; $('#page-description').textContent=titles[tab][1];
  $('#breadcrumb').textContent=({overview:'Overview',clips:'Clip history',activity:'Activity log',settings:'Backup settings',setup:'Connections'})[tab];
  if(tab==='clips') loadClips(); if(tab==='activity') loadLogs();
}
window.addEventListener('hashchange',navigate);
function cell(row,text,className='') {const td=document.createElement('td');td.textContent=text;if(className)td.className=className;row.append(td);return td;}
function makeRows(target,jobs,history) {
  target.replaceChildren();
  if(!jobs.length) {const row=document.createElement('tr'),td=document.createElement('td');td.colSpan=history?6:4;td.className='empty';const strong=document.createElement('strong');strong.textContent='No clips to show yet';td.append(strong,'Clips will appear after the worker completes a scan.');row.append(td);target.append(row);return;}
  jobs.forEach(job=>{
    const row=document.createElement('tr'),td=cell(row,'');
    const name=document.createElement('div');name.className='clip-name';
    name.textContent=job.name.replace(/^\d{4}-\d\d-\d\d_\d\d-\d\d-\d\d_/,'').replace(/_[a-f0-9]{64}\.mp4$/,'');name.title=job.name;
    const sub=document.createElement('div');sub.className='clip-meta';sub.textContent=job.expected_size?formatBytes(job.expected_size)+' · MP4':'MP4 recording';
    if(job.missing_since){sub.textContent='Missing from Blink’s latest clip list';sub.classList.add('problem');}
    else if(job.last_error){sub.textContent=job.last_error;sub.classList.add('problem');}
    sub.title=sub.textContent;td.append(name,sub);
    cell(row,date(job.recorded));const badge=document.createElement('span');badge.className='badge '+(job.missing_since&&job.stage==='download'?'missing':job.stage);badge.textContent=labels[job.stage]||job.stage;cell(row,'').append(badge);
    cell(row,(job.download_attempts||0)+(job.upload_attempts||0));
    if(history){const pending=['download','upload'].includes(job.stage),next=job.stage==='download'?job.next_download:job.next_upload;cell(row,pending?(next>Date.now()/1000?date(next):'Next cycle'):'—');const last=cell(row,'');if(pending){const button=document.createElement('button');button.type='button';button.className='button secondary small';button.textContent='Retry';button.addEventListener('click',()=>action(button,async()=>{await api('/api/retry',{key:job.key});toast('Clip scheduled for the next cycle.');await loadClips();}));last.append(button);}}
    target.append(row);
  });
}
async function loadClips() {try{const result=await api('/api/clips?'+new URLSearchParams({page,search:$('#clip-search').value,stage:$('#clip-filter').value}));makeRows($('#clips-body'),result.jobs,true);$('#clip-total').textContent=formatNumber(result.total)+' clips';$('#page-number').textContent='Page '+page;$('#prev-page').disabled=page<=1;$('#next-page').disabled=page*50>=result.total;}catch(error){toast(error.message,true);}}
async function loadRecent(){const result=await api('/api/clips');makeRows($('#recent-body'),result.jobs.slice(0,6),false);}
async function loadLogs(){try{const result=await api('/api/logs');const parent=$('#logs');parent.replaceChildren();if(!result.logs.length){const p=document.createElement('p');p.className='muted';p.textContent='No activity yet. Start the backup to see its progress here.';parent.append(p);}result.logs.forEach(log=>{const title=document.createElement('h3');title.textContent=log.name==='backup.log'?'Transfer activity':'Startup & connection messages';const pre=document.createElement('pre');pre.textContent=log.lines.join('\n')||'No messages yet.';parent.append(title,pre);});}catch(error){toast(error.message,true);}}
function fillSettings(settings) {const form=$('#settings-form');form.elements.cloud_gb.value=settings.max_bytes/1e9;form.elements.local_gb.value=(settings.max_staging_bytes??1e10)/1e9;['poll_seconds','download_delay_seconds','max_clips_per_cycle'].forEach(key=>form.elements[key].value=settings[key]);$('#settings-folder').value=settings.folder_name||'Blink Rolling Backup';settingsLoaded=true;}
function renderStatus(s) {
  snapshot=s;const count=s.counts||{},r=s.report||{},w=s.worker;
  const pending=(count.download||0)+(count.upload||0),missing=r.missing_from_latest_manifest||0;
  $('#count-uploaded').textContent=formatNumber(count.uploaded);$('#count-download').textContent=formatNumber(count.download);$('#count-upload').textContent=formatNumber(count.upload);$('#count-missing').textContent=formatNumber(missing);$('#nav-pending').textContent=formatNumber(pending);
  $('#cloud-used').textContent=r.drive_bytes==null?'—':formatGB(r.drive_bytes);$('#cloud-limit').textContent=' / '+formatGB(s.settings.max_bytes);$('#cloud-bar').style.width=Math.min(100,100*(r.drive_bytes||0)/s.settings.max_bytes)+'%';
  $('#local-used').textContent=formatGB(s.staging_bytes);$('#local-limit').textContent=' / '+formatGB(s.settings.max_staging_bytes??1e10);$('#local-bar').style.width=Math.min(100,100*s.staging_bytes/(s.settings.max_staging_bytes??1e10))+'%';
  $('#last-check').textContent=r.checked_at?'Last scan '+date(r.checked_at):'No scan yet';
  const state=w.stopping?'Stopping':w.running?'Running':w.restart_enabled?'Restarting':'Stopped';$('#worker-state').textContent=state;
  const toggle=$('#worker-toggle');toggle.textContent=w.stopping?'Stopping…':w.running?'Stop backup':w.restart_enabled?'Stop automatic retries':'Start backup';toggle.disabled=w.stopping;
  $('#setup-banner').classList.toggle('hidden',s.configured);
  const health=$('.health-card');health.classList.remove('warning','danger');let title,description,icon='✓';
  const stale=r.checked_at&&Date.now()/1000-r.checked_at>Math.max(300,s.settings.poll_seconds*3);
  if(!s.configured){title='Ready for your first backup';description='Connect Google Drive and Blink, then select your Sync Module.';icon='⌁';}
  else if(w.stopping){title='Finishing the current operation';description='The worker will stop safely between transfers. Pending clips remain queued.';}
  else if(!w.running){title=w.restart_enabled?'Worker will retry automatically':'Backup is stopped';description=w.restart_enabled?'The worker exited. Check Activity for sign-in or connection errors; another start is scheduled.':'Start the worker to scan for new clips and resume pending transfers.';health.classList.add('warning');icon='Ⅱ';}
  else if(missing||(r.cycle_errors||[]).length||r.manifest_ok===false){title='Some backups need attention';description=missing?missing+' known clips are absent from Blink’s latest list. See Clip history for unresolved items.':(r.cycle_errors||[]).join(' · ')||'The latest Blink scan failed. The worker will retry.';health.classList.add('danger');icon='!';}
  else if(stale){title='Waiting for the next completed scan';description='A transfer or large batch may be in progress. Check Activity if the scan time stays unchanged.';health.classList.add('warning');icon='…';}
  else if(pending){title='Your backup queue is moving';description=pending+' clips are pending download or upload. Each failed transfer keeps its own retry schedule.';}
  else if(!r.checked_at){title='Connecting to your accounts';description='The first scan is starting. Connection messages appear in Activity.';icon='…';}
  else{title='No pending transfers';description='All discovered eligible clips are processed. The worker is watching for new recordings.';}
  $('#health-title').textContent=title;$('#health-description').textContent=description;$('#health-icon').textContent=icon;
  const driveLink=$('#drive-link');driveLink.classList.toggle('hidden',!s.settings.folder_id);if(s.settings.folder_id)driveLink.href='https://drive.google.com/drive/folders/'+encodeURIComponent(s.settings.folder_id);
  if(!settingsLoaded)fillSettings(s.settings);
  $('#settings-hint').textContent=w.running?'Stop the worker before saving changes.':'Changes take effect on the next worker start.';$('#save-settings').disabled=w.running;
  $('#google-status').textContent=s.auth.google_saved?(s.settings.folder_id?'Authorization saved · folder selected':'Authorization saved · check folder'):'Not connected';
  $('#blink-status').textContent=s.auth.needs_code?'Verification code required':s.auth.blink_saved?'Credentials saved on this computer':'Not connected';
  $('#blink-code-form').classList.toggle('hidden',!s.auth.needs_code);$('#blink-form').classList.toggle('hidden',s.auth.needs_code);
  $('#module-current').textContent=s.settings.module_id?'Selected module ID: '+s.settings.module_id:'No module selected';
  const available=s.auth.modules;if(available.length){const select=$('#module-select'),signature=JSON.stringify(available);if(select.dataset.signature!==signature){select.replaceChildren();available.forEach(m=>{const option=document.createElement('option');option.value=m.id;option.textContent=m.name+' · '+m.id;select.append(option);});select.dataset.signature=signature;if(available.some(m=>m.id===s.settings.module_id))select.value=s.settings.module_id;}}
  if(!s.google_pending)$('#google-auth-link').classList.add('hidden');
  document.querySelectorAll('[data-page="setup"] input,[data-page="setup"] select,[data-page="setup"] button').forEach(el=>el.disabled=w.running);
  document.querySelectorAll('.retry-all').forEach(el=>el.disabled=!pending);
}
async function refresh(){try{const s=await api('/api/status');renderStatus(s);$('#connection-error').classList.add('hidden');if(tab==='overview')await loadRecent();if(tab==='clips')await loadClips();if(tab==='activity')await loadLogs();}catch(error){$('#connection-error').classList.remove('hidden');}}
$('#worker-toggle').addEventListener('click',event=>action(event.currentTarget,async()=>{const stop=snapshot?.worker.running||snapshot?.worker.restart_enabled;await api(stop?'/api/stop':'/api/start',{});toast(stop?'Stop requested. The current transfer may take a moment.':'Backup worker started.');}));
document.querySelectorAll('.retry-all').forEach(button=>button.addEventListener('click',()=>action(button,async()=>{const r=await api('/api/retry',{});toast(r.affected+' pending clips scheduled for the next cycle.');})));
let searchTimer;$('#clip-search').addEventListener('input',()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>{page=1;loadClips();},250);});$('#clip-filter').addEventListener('change',()=>{page=1;loadClips();});$('#prev-page').addEventListener('click',()=>{page--;loadClips();});$('#next-page').addEventListener('click',()=>{page++;loadClips();});$('#refresh-logs').addEventListener('click',loadLogs);
$('#settings-form').addEventListener('submit',event=>{event.preventDefault();const form=event.currentTarget;action($('#save-settings'),async()=>{await api('/api/settings',{max_bytes:Math.round(Number(form.elements.cloud_gb.value)*1e9),max_staging_bytes:Math.round(Number(form.elements.local_gb.value)*1e9),poll_seconds:Number(form.elements.poll_seconds.value),download_delay_seconds:Number(form.elements.download_delay_seconds.value),max_clips_per_cycle:Number(form.elements.max_clips_per_cycle.value)});toast('Settings saved. They apply the next time the worker starts.');});});
$('#google-client').addEventListener('change',async event=>{const input=event.target,file=input.files[0];if(!file)return;try{if(file.size>100000)throw new Error('Choose the small JSON client file downloaded from Google Cloud.');const client=JSON.parse(await file.text());await api('/api/setup/google-client',{client});toast('Google Desktop client saved. You can now connect Drive.');await refresh();}catch(error){toast(error.message,true);}finally{input.value='';}});
$('#google-connect').addEventListener('click',event=>action(event.currentTarget,async()=>{const result=await api('/api/setup/google',{});const link=$('#google-auth-link');link.href=result.url;link.classList.remove('hidden');toast('Select “Continue to Google” to authorize your account in a new tab.');}));
$('#google-folder').addEventListener('click',event=>action(event.currentTarget,async()=>{await api('/api/setup/google-folder',{});toast('Drive folder is ready.');}));
$('#blink-form').addEventListener('submit',event=>{event.preventDefault();const form=event.currentTarget;action(form.querySelector('button'),async()=>{try{const result=await api('/api/setup/blink',{email:form.elements.email.value,password:form.elements.password.value});toast(result.needs_code?'Enter the verification code Blink sent you.':'Blink connected. Select your Sync Module below.');}finally{form.elements.password.value='';}});});
$('#blink-code-form').addEventListener('submit',event=>{event.preventDefault();const form=event.currentTarget;action(form.querySelector('button'),async()=>{await api('/api/setup/blink-code',{code:form.elements.code.value});form.elements.code.value='';toast('Blink verified. Choose your Sync Module below.');});});
$('#load-modules').addEventListener('click',event=>action(event.currentTarget,async()=>{const result=await api('/api/setup/modules',{});toast(result.needs_code?'Blink needs verification again. Enter the code above.':result.modules.length+' modules with local storage found.');}));
$('#save-module').addEventListener('click',event=>action(event.currentTarget,async()=>{await api('/api/setup/module',{module_id:$('#module-select').value});toast('Sync Module selected. You can start the backup.');}));
$('#cancel-setup').addEventListener('click',event=>action(event.currentTarget,async()=>{await api('/api/setup/cancel',{});toast('Unfinished sign-in canceled. Existing saved connections remain.');}));
navigate();refresh();setInterval(refresh,5000);
