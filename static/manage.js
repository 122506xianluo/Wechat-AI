'use strict';
const csrf=()=>document.querySelector('meta[name="csrf-token"]').content;
const el=(tag,text)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;return n};
async function api(path,data){const r=await fetch('/api/v1/'+path,{method:data===undefined?'GET':'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf()},body:data===undefined?undefined:JSON.stringify(data)});const x=await r.json();if(!r.ok||x.ok===false)throw Error(x.error||'请求失败');return x;}
const notice=t=>document.querySelector('#notice').textContent=t;
const btn=(text,fn)=>{const b=el('button',text);b.onclick=async()=>{try{await fn()}catch(e){notice(e.message)}};return b};
const content=document.querySelector('#content');
const pages={};let active='角色';
async function show(name){active=name;notice('');content.replaceChildren();try{await pages[name]()}catch(e){notice(e.message)}}
function table(items,fields,actions){const t=el('table'),head=el('tr');for(const f of fields)head.append(el('th',f));head.append(el('th','操作'));t.append(head);for(const row of items){const tr=el('tr');for(const f of fields)tr.append(el('td',String(row[f]??'')));const td=el('td');td.className='actions';for(const [title,fn] of actions(row))td.append(btn(title,fn));tr.append(td);t.append(tr)}content.append(t)}
async function editRole(r={}) {
  content.replaceChildren(el('h2', r.id ? '编辑角色' : '新建角色'));
  const form = el('form');
  const fields = {
    name: '名称', description: '说明', system_prompt: '系统提示词',
    model: '模型覆盖（留空继承）', temperature: '温度',
    max_tokens: '最大 tokens', max_reply_chars: '回复字符上限'
  };
  const inputs = {};
  const defaults = {temperature: 0.7, max_tokens: 600, max_reply_chars: 1200};
  for (const [key, title] of Object.entries(fields)) {
    const label = el('label', title);
    const input = el(key === 'system_prompt' ? 'textarea' : 'input');
    input.value = r[key] ?? defaults[key] ?? '';
    inputs[key] = input;
    label.append(input);
    form.append(label);
  }
  const knowledgeLabel = el('label', '知识库模式');
  const knowledgeMode = el('select');
  for (const [value, title] of [
    ['off', '关闭：不检索知识库'],
    ['auto', '自动：每轮检索已授权知识库'],
    ['tool', '工具：模型按需检索（需启用 tools 和搜索工具）']
  ]) {
    const option = el('option', title);
    option.value = value;
    knowledgeMode.append(option);
  }
  knowledgeMode.value = r.knowledge_mode ?? 'auto';
  inputs.knowledge_mode = knowledgeMode;
  knowledgeLabel.append(knowledgeMode);
  form.append(knowledgeLabel, el('p', '知识模式不等于访问授权；还需在知识库页明确设置允许访问的角色、聊天或成员。'));
  for (const [key, title] of [
    ['enabled', '启用'], ['user_selectable', '允许用户自行选择'], ['is_default', '全局默认角色']
  ]) {
    const label = el('label', title);
    const input = el('input');
    input.type = 'checkbox';
    input.checked = !!(r[key] ?? (key === 'enabled'));
    inputs[key] = input;
    label.append(input);
    form.append(label);
  }
  form.append(btn('保存', async () => {
    const data = {};
    for (const [key, input] of Object.entries(inputs)) {
      data[key] = input.type === 'checkbox' ? input.checked
        : ['temperature', 'max_tokens', 'max_reply_chars'].includes(key)
          ? Number(input.value) : input.value;
    }
    await api('roles' + (r.id ? '/' + r.id : ''), data);
    await show(active);
  }));
  form.onsubmit = event => event.preventDefault();
  content.append(form);
}
pages['角色']=async()=>{content.append(btn('新建角色',()=>editRole()));table((await api('roles')).items,['id','name','enabled','user_selectable','revision'],r=>[['编辑',()=>editRole(r)],['绑定',async()=>{const chat=prompt('聊天 ID（空为全局）');if(chat===null)return;const p=prompt('成员 ID（空为聊天）');if(p===null)return;await api('roles/'+r.id+'/bind',{chat_id:chat?Number(chat):null,principal_id:p?Number(p):null});notice('已绑定，下一轮生效')}],['修订/回滚',async()=>{const rows=(await api('roles/'+r.id+'/revisions')).items;const rev=prompt('可回滚版本：'+rows.map(x=>x.revision).join(', '));if(rev&&confirm('确认回滚？')){await api('roles/'+r.id+'/rollback',{revision:Number(rev)});await show(active)}}],['删除',async()=>{if(confirm('确认删除角色？')){const to=prompt('替代角色 ID（无绑定可留空）');if(to===null)return;await api('roles/'+r.id+'/delete',{replacement:to?Number(to):null});await show(active)}}]])};
pages['聊天/私聊用户']=async()=>{const q=el('input');q.placeholder='搜索备注或群名';content.append(q,btn('搜索',async()=>{const rows=(await api('chats?q='+encodeURIComponent(q.value))).items;renderChats(rows)}));renderChats((await api('chats')).items)};
function renderChats(rows){content.querySelector('table')?.remove();table(rows,['id','kind','name','approval','enabled','visibility','message_count','management_note'],r=>[['批准',async()=>{await api('chats/'+r.id,{approval:'approved'});await show(active)}],['拒绝',async()=>{await api('chats/'+r.id,{approval:'rejected'});await show(active)}],[r.enabled?'停用':'启用',async()=>{await api('chats/'+r.id,{enabled:!r.enabled});await show(active)}],['备注',async()=>{const n=prompt('管理备注',r.management_note);if(n!==null){await api('chats/'+r.id,{management_note:n});await show(active)}}],['合并',async()=>{const id=prompt('目标私聊 ID；停机后操作，不继承权限');if(id&&confirm('确认合并历史到目标用户？')){await api('chats/'+r.id+'/merge',{target:Number(id),confirm:true});await show(active)}}]])}
pages['群成员/权限']=async()=>{table((await api('principals')).principals||[],['id','kind','display_name','chat_id','status'],r=>[['批准',async()=>{await api('principals/'+r.id+'/status',{status:'active',confirm_identity:true});await show(active)}],['封禁',async()=>{await api('permissions',{principal_id:r.id,scope:r.chat_id?'chat':'global',chat_id:r.chat_id,access_level:'blocked'});await show(active)}],['权限',async()=>{const level=prompt('user / admin / blocked','user');if(level){await api('permissions',{principal_id:r.id,scope:r.chat_id?'chat':'global',chat_id:r.chat_id,access_level:level});await show(active)}}],['合并改名',async()=>{const to=prompt('旧成员 ID；停机后确认新昵称与旧成员同一人');if(to&&confirm('确认身份并合并？')){await api('members/'+r.id+'/merge',{target:Number(to),confirm:true});await show(active)}}]]);content.append(btn('同步群成员（需停止 Bot）',async()=>{const id=prompt('群聊天 ID');if(id&&confirm('将操作微信界面，确认同步？')){const r=await api('chats/'+id+'/sync-members',{confirm:true});notice(JSON.stringify(r))}}))};
pages['身份诊断']=async()=>table((await api('members/diagnostics')).items,['id','chat_id','principal_id','method','confidence','reason','created_at'],()=>[]);
pages['上下文']=async()=>{table((await api('contexts')).items,['id','chat_name','display_name','mode','message_count'],r=>[['历史',async()=>{const rows=(await api('contexts/'+r.id+'/history')).items;const p=el('pre',rows.map(x=>x.role+': '+x.content).join('\n'));content.append(p)}],['清空此范围',async()=>{if(confirm('只清空该范围，确认？')){await api('contexts/'+r.id+'/clear',{confirm:true});await show(active)}}]]);content.append(btn('设置群上下文模式',async()=>{const id=prompt('群 ID');if(!id)return;const mode=prompt('member / shared / hybrid','member');if(!mode)return;if(!confirm('确认修改？hybrid 会将群公共摘要分享给其他成员。'))return;await api('chats/'+id+'/context-mode',{mode,confirm:true});await show(active)}))};
pages['账户/备份']=async()=>{content.append(btn('退出登录',async()=>{await api('auth/logout',{});location.href='/login'}),btn('修改我的密码',async()=>{const current=prompt('当前密码');const password=prompt('新密码（至少12位）');if(current&&password){await api('auth/password',{current,password});location.href='/login'}}),btn('添加后台管理员（owner）',async()=>{const username=prompt('新管理员用户名');const password=prompt('初始密码（至少12位）');if(username&&password){await api('auth/accounts',{username,password});notice('已添加后台管理员')}}),btn('创建数据库备份（owner）',async()=>{await api('backups',{});await show(active)}));try{table((await api('backups')).items,['name','size_bytes'],()=>[])}catch(e){notice(e.message)}};
pages['任务队列']=async()=>table((await api('queue')).items,['id','chat_id','scope_id','job_type','state','attempts','last_error'],r=>[['重试',async()=>{await api('queue/'+r.id+'/retry',{});await show(active)}],['取消',async()=>{await api('queue/'+r.id+'/cancel',{});await show(active)}],['确认已发',async()=>{if(confirm('已亲自核对微信中的出站消息，确认已发送？')){await api('queue/'+r.id+'/mark_sent',{confirm:true});await show(active)}}],['确认未发并克隆',async()=>{if(confirm('已亲自检查微信消息和输入框，确认没有发送？将创建一个新任务。')&&confirm('再次确认：此操作可能造成重复回复，继续？')){await api('queue/'+r.id+'/clone',{confirm:true});await show(active)}}]]);
pages['附件']=async()=>{content.append(el('p','只保存已授权消息的附件，7天后删除文件与提取内容。微信菜单不可用时明确记录未支持，不猜测内容。'));table((await api('attachments')).items,['id','chat_id','principal_id','content_type','original_name','size_bytes','status','error'],()=>[])};
pages['模型能力']=async()=>{content.append(el('p','四项能力分别验证；检查会向你配置的服务发请求，可能计费。语音需要选择一段短音频作为样本。更换模型/地址后必须重新检查。'));const file=el('input');file.type='file';file.accept='.wav,.mp3,.m4a,.ogg,.flac';content.append(file);table((await api('capabilities')).items,['name','enabled','checked_at','error'],r=>[['检查（owner）',async()=>{if(!confirm('确认向配置的模型服务发送能力检查请求？'))return;const data={confirm:true};if(r.name==='transcription'){if(!file.files[0])throw Error('请选择短音频');data.sample=await file64(file.files[0],25*1024*1024);data.filename=file.files[0].name}notice('检查中…');const result=await api('capabilities/'+r.name+'/test',data);await show(active);notice(result.supported?'接口能力检查通过；实际微信附件读取仍需验收':('未通过：'+result.error))}]])};
async function file64(file,limit){if(file.size>limit)throw Error('文件超过大小限制');return new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result).split(',')[1]);reader.onerror=reject;reader.readAsDataURL(file)})}
pages['知识库']=async()=>{content.append(el('p','默认不授权任何聊天。授权行之间为 OR，同一行中角色/聊天/用户条件为 AND。源文件长期保留，聊天附件不会自动进入知识库。'));content.append(btn('创建知识库',async()=>{const name=prompt('名称');if(name){await api('knowledge-bases',{name});await show(active)}}));table((await api('knowledge-bases')).items,['id','name','description','document_count','revision'],r=>[['文档/上传',()=>knowledgeDocs(r)],['授权',()=>knowledgeBindings(r)],['提升收到的附件',async()=>{const id=prompt('附件 ID');if(id&&confirm('确认长期保留此文档，并允许此知识库的授权使用者检索？')){await api('knowledge-bases/'+r.id+'/promote',{attachment_id:id,confirm:true});await show(active)}}],['删除',async()=>{if(confirm('删除整个知识库、源文件和所有版本？')&&confirm('不可撤销，确认删除？')){await api('knowledge-bases/'+r.id+'/delete',{confirm:true});await show(active)}}]])};
async function knowledgeDocs(k){content.replaceChildren(el('h2',k.name+'：文档版本'));const f=el('input');f.type='file';f.accept='.pdf,.txt,.md,.docx';content.append(f,btn('上传/新版本',async()=>{if(!f.files[0])throw Error('请选择文档');notice('导入中…');await api('knowledge-bases/'+k.id+'/documents',{name:f.files[0].name,content:await file64(f.files[0],50*1024*1024)});await knowledgeDocs(k);notice('已保存，FTS立即可用；向量索引在Bot空闲时处理')}));table((await api('knowledge-bases/'+k.id+'/documents')).items,['id','name','version','active','size_bytes','status','index_state','last_error'],r=>[['重建向量',async()=>{await api('knowledge-documents/'+r.id+'/reindex',{});await knowledgeDocs(k)}],['删除版本',async()=>{if(confirm('确认删除此版本？')){await api('knowledge-bases/'+k.id+'/documents/'+r.id+'/delete',{confirm:true});await knowledgeDocs(k)}}]])}
async function knowledgeBindings(k){content.replaceChildren(el('h2',k.name+'：显式授权'));content.append(btn('添加授权',async()=>{const r=prompt('角色ID（可留空）');if(r===null)return;const c=prompt('聊天ID（可留空）');if(c===null)return;const p=prompt('用户/群成员ID（可留空）');if(p===null)return;await api('knowledge-bases/'+k.id+'/bindings',{role_id:r?Number(r):null,chat_id:c?Number(c):null,principal_id:p?Number(p):null});await knowledgeBindings(k)}));table((await api('knowledge-bases/'+k.id+'/bindings')).items,['id','role_id','chat_id','principal_id'],r=>[['撤销',async()=>{await api('knowledge-bases/'+k.id+'/bindings/'+r.id+'/delete',{});await knowledgeBindings(k)}]])}
pages['只读工具']=async()=>{content.append(el('p','默认全部关闭。先给角色授权，再到模型能力页检查 tools。每轮最多4次调用，没有Shell、任意网络、文件或桌面工具。'));table((await api('tools')).items,['name','description','roles'],r=>[['角色授权/撤销',async()=>{const role=prompt('角色ID');if(!role)return;const enabled=confirm('确定 = 启用；取消 = 撤销此角色的该工具');await api('tools/'+r.name,{role_id:Number(role),enabled});await show(active)}]]);content.append(el('h2','最近工具调用'));table((await api('tools/runs')).items,['id','principal_id','scope_id','tool_name','status','duration_ms','created_at'],()=>[])};
window.showManagementPage = function(name){
  return show(name);
};
window.managementPages = pages;
