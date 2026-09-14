import { execFile } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import { readFile, writeFile, rename } from 'node:fs/promises';
import { readFileSync } from 'node:fs';
import { promisify } from 'node:util';

const exec = promisify(execFile);
const settingsPath = process.env.AWG_MANAGER_TELEGRAM_CONFIG ?? '/etc/awg-manager/telegram.json';
const pendingPath = process.env.AWG_MANAGER_PENDING ?? '/var/lib/openclaw/.openclaw/awg-manager-pending.json';
const namespace = 'awg';
const settings=JSON.parse(readFileSync(settingsPath,'utf8'));
let pending=(()=>{try{return JSON.parse(readFileSync(pendingPath,'utf8'));}catch{return {};}})();

const buttons = (rows) => ({telegram:{buttons:rows}});
const button = (text, payload, style) => ({text, callback_data:`${namespace}:${payload}`,...style?{style}:{}});

async function savePending() {
  const temporary = pendingPath + '.new';
  await writeFile(temporary, JSON.stringify(pending), {mode:0o600});
  await rename(temporary, pendingPath);
}

function authorized(senderId, channel='telegram') {
  return channel === 'telegram' && String(senderId) === String(settings.owner_id);
}

async function helper(args, timeout=30000) {
  try {
    const output = await exec('/usr/bin/sudo', ['-n', settings.helper, ...args], {timeout, maxBuffer:8*1024*1024});
    return JSON.parse(output.stdout);
  } catch (error) {
    let message;
    try { message=JSON.parse(error.stdout).error; } catch {}
    throw new Error(message||'Операция не выполнена. Подробности сохранены в журнале сервера.');
  }
}

function formatBytes(value=0) {
  const units=['Б','КБ','МБ','ГБ','ТБ']; let number=Number(value); let index=0;
  while(number>=1024 && index<units.length-1){number/=1024;index++;}
  return `${number.toFixed(index<2?0:1)} ${units[index]}`;
}

function activity(stamp) {
  if (!stamp) return 'никогда';
  const minutes=Math.max(0,Math.floor(Date.now()/1000-stamp)/60);
  if(minutes<1) return 'только что'; if(minutes<60) return `${Math.floor(minutes)} мин назад`;
  if(minutes<1440) return `${Math.floor(minutes/60)} ч назад`;
  return `${Math.floor(minutes/1440)} дн назад`;
}

function status(peer) {
  if (peer.effective_enabled) return 'разрешён';
  return ({manual:'заблокирован вручную',expired:'срок истёк',quota:'квота исчерпана'})[peer.block_reason] ?? 'заблокирован';
}

function interactiveView(view) {
  return {text:view.text,buttons:view.channelData?.telegram?.buttons??view.buttons??[]};
}

async function mainView() {
  const peers=await helper(['list']);
  const enabled=peers.filter(peer=>peer.effective_enabled).length;
  return {text:`VPN · AWG 3.1\nКлиентов: ${peers.length} · доступ разрешён: ${enabled}\n\nУправление работает без обращения к модели.`,
    channelData:buttons([
      [button('Клиенты', 'peers:0','primary'),button('Добавить', 'add','success')],
      [button('Расписания', 'schedules')],
      [button('Состояние','status'),button('Обновить','main')]
    ])};
}

async function peersView(page=0) {
  const peers=await helper(['list']); const size=8; const pages=Math.max(1,Math.ceil(peers.length/size));
  page=Math.max(0,Math.min(page,pages-1));
  const rows=peers.slice(page*size,(page+1)*size).map(peer=>[button(`${peer.effective_enabled?'●':'○'} ${peer.name} · ${peer.address}`,`peer:${peer.id}`)]);
  const navigation=[]; if(page>0) navigation.push(button('‹',`peers:${page-1}`)); navigation.push(button(`${page+1}/${pages}`,'noop')); if(page<pages-1) navigation.push(button('›',`peers:${page+1}`));
  if(navigation.length) rows.push(navigation);
  rows.push([button('Добавить','add','success'),button('Главная','main')]);
  return {text:peers.length?'Клиенты VPN\n● доступ разрешён · ○ доступ запрещён':'Клиентов пока нет.',channelData:buttons(rows)};
}

async function peerView(id) {
  const peer=await helper(['get',id]);
  const expiry=peer.expires_at?new Date(peer.expires_at*1000).toLocaleString('ru-RU',{timeZone:'Europe/Moscow'}):'бессрочно';
  const quota=peer.quota_bytes?`${formatBytes(peer.quota_used)} / ${formatBytes(peer.quota_bytes)} (${peer.quota_period})`:'без лимита';
  const text=`${peer.name}\nIP: ${peer.address}\nДоступ: ${status(peer)}\nПоследняя активность: ${activity(peer.last_handshake)}\nТрафик: ↓ ${formatBytes(peer.total_rx)} · ↑ ${formatBytes(peer.total_tx)}\nСрок: ${expiry}\nКвота: ${quota}${peer.note?`\nЗаметка: ${peer.note}`:''}`;
  const exportRows=peer.exportable!==false
    ? [[button('QR',`export:${id}`),button('Скачать конфиг',`file:${id}`)]]
    : [[button('Перевыпустить ключ',`reissue-ask:${id}`,'danger')]];
  return {text,channelData:buttons([
    [button(peer.manual_enabled?'Заблокировать':'Разрешить',`toggle:${id}`,peer.manual_enabled?'danger':'success'),button('Обновить',`peer:${id}`)],
    ...exportRows,
    [button('Доступ и лимиты',`access:${id}`,'primary')],
    [button('Переименовать',`rename:${id}`),button('Дополнительно',`more:${id}`)],
    [button('‹ Клиенты','peers:0'),button('Главная','main')]
  ])};
}

async function accessView(id) {
  const peer=await helper(['get',id]);
  return {text:`Доступ и лимиты · ${peer.name}\nСрок: ${peer.expires_at?new Date(peer.expires_at*1000).toLocaleString('ru-RU',{timeZone:'Europe/Moscow'}):'бессрочно'}\nКвота: ${peer.quota_bytes?`${formatBytes(peer.quota_used)} / ${formatBytes(peer.quota_bytes)} (${peer.quota_period})`:'без лимита'}\nРасписаний: ${peer.schedules.length}`,
    channelData:buttons([
      [button('24 часа',`exp:${id}:1`),button('7 дней',`exp:${id}:7`),button('30 дней',`exp:${id}:30`)],
      [button('Своя дата',`exp-custom:${id}`),button('Бессрочно',`exp:${id}:none`)],
      [button('1 ГБ/день',`quota:${id}:1073741824:day`),button('5 ГБ/месяц',`quota:${id}:5368709120:month`)],
      [button('20 ГБ/месяц',`quota:${id}:21474836480:month`),button('Своя квота',`quota-custom:${id}`)],
      [button('Убрать квоту',`quota:${id}:none:lifetime`),button('Расписание',`schedule:${id}`)],
      [button('‹ Клиент',`peer:${id}`)]
    ])};
}

async function scheduleView(id) {
  const peer=await helper(['get',id]);
  const lines=peer.schedules.map(item=>`${item.action==='enable'?'Включить':'Отключить'}: ${item.cron} · id ${item.id}`);
  const rows=[
    [button('Ежедневно 08–23',`preset:${id}:daily`),button('Будни 08–23',`preset:${id}:weekdays`)],
    [button('Свой cron',`cron:${id}`)]
  ];
  for(const item of peer.schedules.slice(0,6)) rows.push([button(`Удалить ${item.action==='enable'?'вкл':'выкл'} ${item.cron}`,`unschedule:${id}:${item.id}`,'danger')]);
  rows.push([button('‹ Доступ',`access:${id}`)]);
  return {text:`Расписание · ${peer.name}\nЧасовой пояс: Europe/Moscow\n${lines.length?lines.join('\n'):'Правил пока нет.'}\n\nCron: минута час день месяц день-недели. Ручная блокировка, срок и квота имеют приоритет.`,channelData:buttons(rows)};
}

async function statusView() {
  const peers=await helper(['list']);
  const disk=await exec('/usr/bin/df',['-h','/']);
  return {text:`Состояние VPS\nVPN отвечает. Клиентов: ${peers.length}\n\n${disk.stdout.trim()}`,channelData:buttons([[button('Обновить','status'),button('Главная','main')]])};
}

async function sendConfig(id, qr=false) {
  const data=await helper(['export',id]);
  const token=(await readFile(settings.token_file,'utf8')).trim();
  const form=new FormData(); form.set('chat_id',String(settings.owner_id));
  if(settings.thread_id) form.set('message_thread_id',String(settings.thread_id));
  if(qr) {
    if(data.config.length>2900) throw new Error('Этот конфиг слишком большой для QR. Используй «Скачать конфиг».');
    const image=await new Promise((resolve,reject)=>{
      const child=execFile('/usr/bin/qrencode',['-t','PNG','-o','-'],{encoding:'buffer',timeout:10000,maxBuffer:1048576},(error,output)=>error?reject(error):resolve(output));
      child.stdin.end(data.config);
    });
    form.set('photo',new Blob([image],{type:'image/png'}),`${data.name}.png`);
  } else {
    form.set('document',new Blob([data.config],{type:'text/plain'}),`${data.name}.conf`);
  }
  const method=qr?'sendPhoto':'sendDocument';
  const response=await fetch(`https://api.telegram.org/bot${token}/${method}`,{method:'POST',body:form,signal:AbortSignal.timeout(30000)});
  const result=await response.json(); if(!result.ok) throw new Error('Telegram не принял файл.');
}

async function sendTelegramButtons(text, rows) {
  const token=(await readFile(settings.token_file,'utf8')).trim();
  const body={chat_id:String(settings.owner_id),text,reply_markup:{inline_keyboard:rows}};
  if(settings.thread_id) body.message_thread_id=Number(settings.thread_id);
  const response=await fetch(`https://api.telegram.org/bot${token}/sendMessage`,{
    method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body),signal:AbortSignal.timeout(30000)
  });
  const result=await response.json(); if(!result.ok) throw new Error('Telegram не принял сообщение.');
}

// The same Telegram forum topic is represented differently for a button callback
// and a following text message. There is one authorized owner, so one pending slot
// per sender is both sufficient and reliable across topics.
function pendingKey(senderId) { return String(senderId); }
async function setPending(ctx, value, prompt) {
  pending[pendingKey(ctx.senderId)]={...value,expires:Date.now()+10*60*1000};
  await savePending();
  await ctx.respond.reply({text:prompt});
}

async function interactive(ctx) {
  if(!ctx.auth.isAuthorizedSender || !authorized(ctx.senderId)) { await ctx.respond.reply({text:'Доступ запрещён.'}); return {handled:true}; }
  const parts=ctx.callback.payload.split(':'); const action=parts[0];
  try {
    if(action==='noop') return {handled:true};
    if(action==='main') await ctx.respond.editMessage(interactiveView(await mainView()));
    else if(action==='peers') await ctx.respond.editMessage(interactiveView(await peersView(Number(parts[1]||0))));
    else if(action==='peer') await ctx.respond.editMessage(interactiveView(await peerView(parts[1])));
    else if(action==='access') await ctx.respond.editMessage(interactiveView(await accessView(parts[1])));
    else if(action==='schedule') await ctx.respond.editMessage(interactiveView(await scheduleView(parts[1])));
    else if(action==='status') await ctx.respond.editMessage(interactiveView(await statusView()));
    else if(action==='schedules') await ctx.respond.editMessage({text:'Расписание настраивается отдельно для каждого клиента.',buttons:[[button('Клиенты','peers:0'),button('Главная','main')]]});
    else if(action==='add') await setPending(ctx,{action:'add'},'Отправь имя нового устройства одним сообщением. Допустимы латинские буквы, цифры, дефис и подчёркивание. Отмена: отмена');
    else if(action==='rename') await setPending(ctx,{action:'rename',peer:parts[1]},'Отправь новое имя одним сообщением. Отмена: отмена');
    else if(action==='exp-custom') await setPending(ctx,{action:'expiry',peer:parts[1]},'Отправь дату и время по Москве: ДД.ММ.ГГГГ ЧЧ:ММ. Отмена: отмена');
    else if(action==='quota-custom') await setPending(ctx,{action:'quota',peer:parts[1]},'Отправь квоту, например: 10 GB month. Период: day, month или lifetime. Отмена: отмена');
    else if(action==='cron') await setPending(ctx,{action:'cron',peer:parts[1]},'Отправь правило: enable 0 8 * * 1-5 или disable 0 23 * * *. Часовой пояс Europe/Moscow. Отмена: отмена');
    else if(action==='toggle') { const peer=await helper(['get',parts[1]]); await helper(['enable',parts[1],peer.manual_enabled?'false':'true']); await ctx.respond.editMessage(interactiveView(await peerView(parts[1]))); }
    else if(action==='exp') { const value=parts[2]==='none'?'none':new Date(Date.now()+Number(parts[2])*86400000).toISOString(); await helper(['expiry',parts[1],value]); await ctx.respond.editMessage(interactiveView(await accessView(parts[1]))); }
    else if(action==='quota') { await helper(['quota',parts[1],parts[2],parts[3]]); await ctx.respond.editMessage(interactiveView(await accessView(parts[1]))); }
    else if(action==='preset') { const rules=parts[2]==='daily'?[['enable','0 8 * * *'],['disable','0 23 * * *']]:[['enable','0 8 * * 1-5'],['disable','0 23 * * 1-5']]; for(const rule of rules) await helper(['schedule-add',parts[1],...rule]); await ctx.respond.editMessage(interactiveView(await scheduleView(parts[1]))); }
    else if(action==='unschedule') { await helper(['schedule-delete',parts[1],parts[2]]); await ctx.respond.editMessage(interactiveView(await scheduleView(parts[1]))); }
    else if(action==='export' || action==='file') { await sendConfig(parts[1],action==='export'); await ctx.respond.reply({text:action==='export'?'QR отправлен в топик VPN.':'Конфигурация отправлена в топик VPN.'}); }
    else if(action==='more') await ctx.respond.editMessage({text:'Дополнительные действия. Удаление безвозвратно отзывает ключ клиента.',buttons:[[button('Удалить клиента',`delete-ask:${parts[1]}`,'danger')],[button('‹ Клиент',`peer:${parts[1]}`)]]});
    else if(action==='reissue-ask') {
      const nonce=randomBytes(5).toString('hex');
      pending[`reissue:${nonce}`]={peer:parts[1],expires:Date.now()+120000};
      await savePending();
      await ctx.respond.editMessage({text:'Старый ключ перестанет работать. Перевыпустить конфигурацию клиента?',buttons:[[button('Перевыпустить',`reissue:${parts[1]}:${nonce}`,'danger'),button('Отмена',`peer:${parts[1]}`)]]});
    }
    else if(action==='reissue') {
      const key=`reissue:${parts[2]}`; const confirmation=pending[key];
      if(!confirmation || confirmation.peer!==parts[1] || confirmation.expires<Date.now()) throw new Error('Подтверждение устарело. Открой карточку заново.');
      delete pending[key]; await savePending();
      await helper(['reissue',parts[1],'CONFIRM']);
      await ctx.respond.editMessage(interactiveView(await peerView(parts[1])));
    }
    else if(action==='delete-ask') { const nonce=randomBytes(5).toString('hex'); pending[`delete:${nonce}`]={peer:parts[1],expires:Date.now()+120000}; await savePending(); await ctx.respond.editMessage({text:'Удалить клиента без возможности восстановить его ключ?',buttons:[[button('Удалить',`delete:${parts[1]}:${nonce}`,'danger'),button('Отмена',`peer:${parts[1]}`)]]}); }
    else if(action==='delete') { const confirmation=pending[`delete:${parts[2]}`]; if(!confirmation || confirmation.peer!==parts[1] || confirmation.expires<Date.now()) throw new Error('Подтверждение устарело. Открой карточку заново.'); delete pending[`delete:${parts[2]}`]; await savePending(); await helper(['delete',parts[1],'CONFIRM']); await ctx.respond.editMessage(interactiveView(await peersView(0))); }
    return {handled:true};
  } catch(error) { await ctx.respond.reply({text:`Ошибка: ${error.message}`}); return {handled:true}; }
}

function parseExpiry(text) {
  const match=text.match(/^(\d{2})\.(\d{2})\.(\d{4})\s+(\d{2}):(\d{2})$/); if(!match) throw new Error('Нужен формат ДД.ММ.ГГГГ ЧЧ:ММ');
  const iso=`${match[3]}-${match[2]}-${match[1]}T${match[4]}:${match[5]}:00+03:00`;
  const stamp=Date.parse(iso); if(!Number.isFinite(stamp) || stamp<Date.now()+60000) throw new Error('Дата должна быть в будущем'); return new Date(stamp).toISOString();
}

async function beforeDispatch(event, ctx) {
  if(!authorized(ctx.senderId,ctx.channelId) || event.content?.startsWith('/')) return;
  const key=pendingKey(ctx.senderId); const request=pending[key]; if(!request) return;
  delete pending[key]; await savePending();
  if(request.expires<Date.now()) return {handled:true,text:'Время ввода истекло. Открой /vpn и повтори действие.'};
  const text=(event.content??'').trim(); if(text.toLowerCase()==='отмена') return {handled:true,text:'Действие отменено.'};
  try {
    if(request.action==='add') {
      const peer=await helper(['add',text]);
      try {
        await sendTelegramButtons(`Клиент ${peer.name} создан. Что отправить?`,[
          [button('Показать QR',`export:${peer.id}`,'primary'),button('Скачать конфиг',`file:${peer.id}`)],
          [button('Открыть клиента',`peer:${peer.id}`)]
        ]);
        return {handled:true};
      } catch {
        return {handled:true,text:`Клиент ${peer.name} создан. Открой /vpn → Клиенты, чтобы получить QR или конфиг.`};
      }
    }
    if(request.action==='rename') { const peer=await helper(['rename',request.peer,text]); return {handled:true,text:`Клиент переименован в ${peer.name}.`}; }
    if(request.action==='expiry') { await helper(['expiry',request.peer,parseExpiry(text)]); return {handled:true,text:'Срок доступа установлен.'}; }
    if(request.action==='quota') { const match=text.match(/^(\d+(?:[.,]\d+)?)\s*(mb|gb|tb)\s+(day|month|lifetime)$/i); if(!match) throw new Error('Пример: 10 GB month'); const powers={mb:2,gb:3,tb:4}; const bytes=Math.round(Number(match[1].replace(',','.'))*1024**powers[match[2].toLowerCase()]); await helper(['quota',request.peer,String(bytes),match[3].toLowerCase()]); return {handled:true,text:'Квота установлена. Учёт включает входящий и исходящий трафик через VPN.'}; }
    if(request.action==='cron') { const match=text.match(/^(enable|disable)\s+(.+)$/i); if(!match) throw new Error('Пример: disable 0 23 * * *'); const result=await helper(['schedule-add',request.peer,match[1].toLowerCase(),match[2]]); return {handled:true,text:`Правило добавлено. Следующие срабатывания:\n${result.next.map(value=>new Date(value).toLocaleString('ru-RU',{timeZone:'Europe/Moscow'})).join('\n')}`}; }
  } catch(error) { pending[key]={...request,expires:Date.now()+10*60*1000}; await savePending(); return {handled:true,text:`Ошибка: ${error.message}\nПопробуй ещё раз или отправь «отмена».`}; }
}

export default {
  id:'awg-telegram-manager', name:'AmneziaWG Telegram Manager',
  register(api) {
    api.registerCommand({name:'vpn',description:'Управление AmneziaWG',channels:['telegram'],requireAuth:true,handler:async ctx=>authorized(ctx.senderId,ctx.channelId??ctx.channel)?await mainView():{text:'Доступ запрещён.'}});
    api.registerInteractiveHandler({channel:'telegram',namespace,handler:interactive});
    api.on('before_dispatch',beforeDispatch,{priority:100});
  }
};
