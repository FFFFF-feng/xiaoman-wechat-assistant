(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const states = {ready:'授权文件正常',missing:'授权缺失',mismatch:'授权不一致',unknown:'暂时无法核对',issued:'待使用',queued:'排队中',starting:'正在生成二维码',awaiting_scan:'等待扫码',needs_code:'等待配对数字',connected:'已接入',failed:'连接失败',cancelled:'已取消',expired:'已过期',revoked:'已撤销'};
  const actions = {issue_invite:'生成邀请',cancel_invite:'取消邀请',rename_member:'修改成员备注',revoke_member:'撤销成员'};
  let session = '', expiryTimer, refreshTimer, activeView = 'members', rows = [], overview = {}, loading = false, modalAction = null, freshId = null;
  history.replaceState(null, '', location.pathname);
  function node(tag, text, cls) { const item = document.createElement(tag); if (text !== undefined) item.textContent = text; if (cls) item.className = cls; return item; }
  function date(value) { return new Date(value).toLocaleString('zh-CN', {timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}); }
  function notice(text, error = false) { $('notice').textContent = text; $('notice').classList.toggle('error-notice', error); $('notice').hidden = !text; }
  function clearLogin(message = '') {
    session = ''; clearTimeout(expiryTimer); clearInterval(refreshTimer);
    rows = []; overview = {}; freshId = null; loading = false;
    $('password').value = ''; $('invite-link').value = ''; $('invite-code').value = '';
    $('fresh-invite').hidden = true; $('workspace').hidden = true; $('logout').hidden = true; $('login-view').hidden = false;
    $('list-container').replaceChildren(); $('login-error').textContent = message;
    if ($('action-dialog').open) $('action-dialog').close(); modalAction = null;
  }
  async function api(route, body, login = false) {
    const captured = session;
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 20000);
    try {
      const response = await fetch('/api/manage' + route, {method: body === undefined ? 'GET' : 'POST',
        headers:{...(body === undefined ? {} : {'Content-Type':'application/json'}),...(!login ? {Authorization:'Bearer '+captured}: {})},
        body:body === undefined ? undefined : JSON.stringify(body),signal:controller.signal,credentials:'omit',cache:'no-store',redirect:'error'});
      const value = await response.json().catch(() => ({}));
      if (!login && captured !== session) throw new Error('页面已退出，旧请求结果不再显示');
      if (response.status === 401 && !login) { clearLogin('登录已过期，请重新登录'); throw new Error('登录已过期，请重新登录'); }
      if (!response.ok) throw new Error(typeof value.detail === 'string' ? value.detail : '操作未完成，请刷新状态后再试');
      return value;
    } catch (error) {
      if (error.name === 'AbortError' || error instanceof TypeError) throw new Error(body === undefined ? '网络暂时不可用，请稍后刷新' : '操作结果暂时无法确认，请先刷新记录，不要重复提交');
      throw error;
    } finally { clearTimeout(timer); }
  }
  function authButton(text, action, key, danger = false) { const button = node('button',text,danger?'quiet danger':'quiet'); button.dataset.action=action;button.dataset.key=String(key);return button; }
  function badge(state) { return node('span',states[state]||'未知状态','badge '+state); }
  function memberTable(items) {
    const table=node('table',undefined,'data-table'),head=node('thead'),tr=node('tr');
    ['成员备注','授权文件','历史任务','操作'].forEach(text=>tr.append(node('th',text))); head.append(tr);table.append(head);
    const body=node('tbody');
    for(const value of items) {
      const row=node('tr'),name=node('td'),status=node('td'),tasks=node('td',String(value.task_count)+' 项','task-cell'),buttons=node('td');
      name.append(node('div',value.label,'row-title'));
      name.append(node('div',value.authorization==='ready'?'本人微信授权记录已找到':'未自动改绑；原任务归属保持不变','row-note'));
      name.append(node('div','成员标识尾码 '+value.member_id.slice(-8),'row-note'));
      status.append(badge(value.authorization));const group=node('div',undefined,'row-actions');
      group.append(authButton('修改备注','rename',value.member_id),authButton('撤销接入','revoke',value.member_id,true));buttons.append(group);
      row.append(name,status,tasks,buttons);body.append(row);
    }
    table.append(body);return table;
  }
  function inviteTable(items) {
    const table=node('table',undefined,'data-table'),head=node('thead'),tr=node('tr');
    ['体验者备注','状态','创建 / 到期（北京时间）','操作'].forEach(text=>tr.append(node('th',text)));head.append(tr);table.append(head);
    const body=node('tbody');
    for(const value of items) {
      const row=node('tr'),name=node('td'),status=node('td'),times=node('td',undefined,'time-cell'),buttons=node('td');
      name.append(node('div',value.label,'row-title'),node('div','邀请编号 '+value.id+' · 原链接不保存到记录','row-note'));
      status.append(badge(value.state));times.append(node('div',date(value.created_at)),node('div','到期 '+date(value.expires_at),'row-note'));
      if(['issued','queued','starting','awaiting_scan','needs_code'].includes(value.state)) buttons.append(authButton('取消邀请','cancel-invite',value.id,true));
      else buttons.append(node('span',value.state==='connected'?'到成员页管理':'无需操作','row-note'));
      row.append(name,status,times,buttons);body.append(row);
    }
    table.append(body);return table;
  }
  function render() {
    const query=$('search').value.trim().toLowerCase(),abnormal=$('only-abnormal').checked;
    const filtered=rows.filter(v=>(!query || [v.label||'',states[v.authorization||v.state]||'',actions[v.action]||''].some(t=>t.toLowerCase().includes(query)))&&!(activeView==='members'&&abnormal&&v.authorization==='ready'));
    const container=$('list-container');container.replaceChildren();
    if(!filtered.length) {container.append(node('div',query||abnormal?'没有符合筛选条件的记录':activeView==='members'?'还没有成员。先创建专属邀请，再让体验者本人扫码。':activeView==='invitations'?'还没有邀请。点击“新建邀请”，生成一人一份的链接。':'管理操作会记录在这里。','empty'));return;}
    if(activeView==='members') container.append(memberTable(filtered));
    else if(activeView==='invitations') container.append(inviteTable(filtered));
    else {const list=node('ul',undefined,'event-list');filtered.forEach(v=>{const row=node('li'),text=node('div',actions[v.action]||'管理操作');if(v.label)text.append(node('div',v.label,'row-note'));row.append(text,node('time',date(v.created_at)));list.append(row);});container.append(list);}
  }
  async function refresh() {
    if(!session||loading) return;loading=true;$('refresh').disabled=true;const selected=activeView;
    try {
      const value=await api('/overview');if(!session)return;
      overview=value;$('nav-count').textContent=value.members.length;$('member-count').textContent=value.members.length+' / '+value.limit;
      $('waiting-count').textContent=value.waiting_count;$('worker-state').textContent=value.worker_online?'扫码服务在线':'扫码服务暂未在线';$('worker-state').className=value.worker_online?'online':'offline';
      $('reminder-state').textContent=value.reminder_worker_online?'提醒投递器在线':'提醒投递器待检查';$('reminder-state').className=value.reminder_worker_online?'online':'offline';
      let values=value.members;if(selected==='invitations')values=(await api('/invitations')).invitations;if(selected==='events')values=(await api('/events')).events;
      if(!session||selected!==activeView)return;rows=values;render();$('updated').textContent='更新于 '+date(new Date().toISOString());
    }catch(error){if(session)notice(error.message,true);}finally{loading=false;$('refresh').disabled=false;if(session&&selected!==activeView)refresh();}
  }
  function view(name) {
    activeView=name;$('search').value='';$('only-abnormal').checked=false;rows=[];
    document.querySelectorAll('[data-view]').forEach(button=>{if(button.dataset.view===name)button.setAttribute('aria-current','page');else button.removeAttribute('aria-current');});
    const titles={members:['成员','核对备注与授权文件状态，管理谁可以使用小满。'],invitations:['邀请记录','查看最近100条邀请。链接只在签发时显示，不保存原始凭证。'],events:['操作记录','查看最近50条管理操作，不显示聊天内容。']};
    $('page-title').textContent=titles[name][0];$('page-description').textContent=titles[name][1];$('abnormal-filter').hidden=name!=='members';render();
    refresh();
  }
  function dialog(kind, value) {
    modalAction={kind,value};
    if(kind==='create')modalAction.request_id=Array.from(crypto.getRandomValues(new Uint8Array(16)),v=>v.toString(16).padStart(2,'0')).join('');
    $('dialog-error').textContent='';$('action-label').value=value?.label||'';
    $('label-fields').hidden=!['create','rename'].includes(kind);$('action-label').required=['create','rename'].includes(kind);$('dialog-submit').disabled=false;
    $('dialog-submit').className=kind==='revoke'||kind==='cancel-invite'?'danger':'primary';
    const descriptions={create:['新建专属邀请','写一个体验者备注。链接30分钟有效，收到链接的人必须用本人微信扫码；已接入的人不要重复扫码。','生成邀请'],rename:['修改成员备注','只修改管理备注，不会修改微信昵称、头像或任务归属。','保存备注'],revoke:['撤销这位成员的接入？','撤销后：新消息不再处理，旧日程链接失效，未开始的提醒会被拦截。历史任务和聊天记录保留。\n重新扫码的授权不自动继承原任务；授权缺失时不要用撤销代替身份修复。','确认撤销接入'],'cancel-invite':['取消这份邀请？','未完成的扫码将停止，这份链接不能继续接入。已经接入的成员须到成员页管理。','确认取消邀请']};
    const content=descriptions[kind];$('dialog-title').textContent=content[0];$('dialog-description').textContent=(value?.label?'对象：'+value.label+'\n\n':'')+content[1];$('dialog-submit').textContent=content[2];
    $('action-dialog').showModal();if(!$('label-fields').hidden)$('action-label').focus();
  }
  async function copy(input) {
    const value=$(input).value;if(!value)return;
    try {await navigator.clipboard.writeText(value);notice(input==='invite-link'?'完整邀请链接已复制，只发给这一位本人':'邀请码已复制，请勿公开分享');}
    catch {const field=$(input);const type=field.type;field.type='text';field.focus();field.select();try{if(!document.execCommand('copy'))throw new Error();notice('已复制，请只发给体验者本人');}catch{notice('自动复制不可用，已选中文字，请手动复制');}field.type=type;}
  }
  $('login-form').addEventListener('submit',async event=>{
    event.preventDefault();const password=$('password').value,username=$('username').value.trim();$('password').value='';$('login-button').disabled=true;$('login-error').textContent='';
    try{const value=await api('/login',{username,password},true);session=value.session;$('login-view').hidden=true;$('workspace').hidden=false;$('logout').hidden=false;notice('');
      expiryTimer=setTimeout(()=>clearLogin('登录一小时有效，请重新登录'),value.expires_in*1000);refreshTimer=setInterval(refresh,30000);view('members');}
    catch(error){$('login-error').textContent=error.message;}finally{$('login-button').disabled=false;}
  });
  $('logout').addEventListener('click',async()=>{try{await api('/logout',{});}catch{}finally{clearLogin('已退出管理后台');}});
  document.querySelectorAll('[data-view]').forEach(button=>button.addEventListener('click',()=>view(button.dataset.view)));
  $('new-invite').addEventListener('click',()=>dialog('create'));$('refresh').addEventListener('click',()=>{notice('');refresh();});$('search').addEventListener('input',render);$('only-abnormal').addEventListener('change',render);
  $('list-container').addEventListener('click',event=>{const button=event.target.closest('button[data-action]');if(!button)return;
    const value=button.dataset.action==='cancel-invite'?rows.find(v=>String(v.id)===button.dataset.key):rows.find(v=>v.member_id===button.dataset.key);if(value)dialog(button.dataset.action,value);
  });
  for(const id of ['dialog-close','dialog-cancel'])$(id).addEventListener('click',()=>{if(!$('dialog-submit').disabled)$('action-dialog').close();});
  $('action-dialog').addEventListener('cancel',event=>{if($('dialog-submit').disabled)event.preventDefault();});
  $('action-form').addEventListener('submit',async event=>{
    event.preventDefault();if(!modalAction||$('dialog-submit').disabled)return;const {kind,value}=modalAction;const label=$('action-label').value.trim();
    if(['create','rename'].includes(kind)&&!label){$('dialog-error').textContent='请填写备注';return;}$('dialog-submit').disabled=true;$('dialog-error').textContent='';
    try {
      if(kind==='create') {
        const request_id=modalAction.request_id;
        const result=await api('/invitations',{label,request_id});freshId=result.id;
        $('invite-link').value=location.origin+'/join#invite='+result.invite;$('invite-code').value=result.invite;$('invite-code').type='password';$('show-code').textContent='显示';
        $('fresh-label').textContent=result.label;$('fresh-expiry').textContent='北京时间 '+date(result.expires_at)+' 到期；每人重新生成一次。';$('join-address').textContent=location.origin+'/join';$('fresh-invite').hidden=false;
        activeView='invitations';view('invitations');notice('邀请已生成。请复制完整链接给这一位体验者本人。');
      } else if(kind==='rename') {await api('/members/'+encodeURIComponent(value.member_id)+'/label',{label});notice('备注已更新，身份和任务归属未改变');}
      else if(kind==='revoke') {await api('/members/'+encodeURIComponent(value.member_id)+'/revoke',{});notice('成员接入已撤销，历史记录保留');}
      else {await api('/invitations/'+value.id+'/cancel',{});if(freshId===value.id){$('fresh-invite').hidden=true;$('invite-link').value='';$('invite-code').value='';freshId=null;}notice('邀请已取消');}
      $('action-dialog').close();modalAction=null;refresh();
    }catch(error){if(session)$('dialog-error').textContent=error.message;}finally{$('dialog-submit').disabled=false;}
  });
  $('copy-link').addEventListener('click',()=>copy('invite-link'));$('copy-code').addEventListener('click',()=>copy('invite-code'));
  $('show-code').addEventListener('click',()=>{const hidden=$('invite-code').type==='password';$('invite-code').type=hidden?'text':'password';$('show-code').textContent=hidden?'隐藏':'显示';});
  $('dismiss-invite').addEventListener('click',()=>{if(confirm('收起后无法从记录找回原链接。确认已复制保存？')){$('fresh-invite').hidden=true;$('invite-link').value='';$('invite-code').value='';freshId=null;}});
})();
