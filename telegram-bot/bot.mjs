import plugin from '../openclaw-plugin/index.mjs';
import {readFileSync,writeFileSync,renameSync} from 'node:fs';

const settingsPath=process.env.AWG_MANAGER_TELEGRAM_CONFIG??'/etc/awg-manager/telegram.json';
const settings=JSON.parse(readFileSync(settingsPath,'utf8'));
const token=readFileSync(settings.token_file,'utf8').trim();
const apiRoot=`https://api.telegram.org/bot${token}`;
const offsetPath='/var/lib/awg-telegram-bot/offset';
let offset=(()=>{try{return Number(readFileSync(offsetPath,'utf8'))||0;}catch{return 0;}})();
let command;
let interactive;
let beforeDispatch;

plugin.register({
  registerCommand(value){if(value.name==='vpn') command=value;},
  registerInteractiveHandler(value){if(value.channel==='telegram'&&value.namespace==='awg') interactive=value.handler;},
  on(name,handler){if(name==='before_dispatch') beforeDispatch=handler;}
});

async function telegram(method,body) {
  const response=await fetch(`${apiRoot}/${method}`,{method:'POST',headers:{'content-type':'application/json'},
    body:JSON.stringify(body),signal:AbortSignal.timeout(35000)});
  const result=await response.json();
  if(!result.ok) throw new Error(`${method}: ${result.description??'Telegram API error'}`);
  return result.result;
}

function rows(payload) { return payload?.channelData?.telegram?.buttons??payload?.buttons??[]; }

async function sendPayload(payload,messageThreadId=settings.thread_id) {
  if(!payload?.text) return;
  const body={chat_id:String(settings.owner_id),text:payload.text};
  if(messageThreadId) body.message_thread_id=Number(messageThreadId);
  const keyboard=rows(payload); if(keyboard.length) body.reply_markup={inline_keyboard:keyboard};
  await telegram('sendMessage',body);
}

async function editPayload(message,payload) {
  const body={chat_id:String(settings.owner_id),message_id:message.message_id,text:payload.text};
  const keyboard=rows(payload); if(keyboard.length) body.reply_markup={inline_keyboard:keyboard};
  await telegram('editMessageText',body);
}

async function handleMessage(message) {
  if(String(message.from?.id)!==String(settings.owner_id) || String(message.chat?.id)!==String(settings.owner_id) || !message.text) return;
  const threadId=message.message_thread_id??settings.thread_id;
  if(/^\/(?:vpn|start)(?:@\w+)?$/i.test(message.text.trim())) {
    await sendPayload(await command.handler({senderId:String(message.from.id),channelId:'telegram'}),threadId);
    return;
  }
  const result=await beforeDispatch({content:message.text,channel:'telegram',senderId:String(message.from.id)},
    {channelId:'telegram',senderId:String(message.from.id),conversationId:String(threadId??message.chat.id)});
  if(result?.handled && result.text) await sendPayload({text:result.text},threadId);
}

async function handleCallback(query) {
  await telegram('answerCallbackQuery',{callback_query_id:query.id});
  if(String(query.from?.id)!==String(settings.owner_id) || String(query.message?.chat?.id)!==String(settings.owner_id) || !query.data?.startsWith('awg:')) return;
  const ctx={auth:{isAuthorizedSender:true},senderId:String(query.from.id),
    conversationId:String(query.message?.message_thread_id??query.message?.chat?.id),
    callback:{payload:query.data.slice(4)},respond:{
      async editMessage(payload){await editPayload(query.message,payload);},
      async reply(payload){await sendPayload(payload,query.message?.message_thread_id);}
    }};
  await interactive(ctx);
}

async function poll() {
  while(true) {
    try {
      const updates=await telegram('getUpdates',{offset,timeout:25,allowed_updates:['message','callback_query']});
      for(const update of updates) {
        offset=update.update_id+1;
        // At-most-once execution: a restart must never replay a destructive action.
        writeFileSync(offsetPath+'.new',String(offset),{mode:0o600});
        renameSync(offsetPath+'.new',offsetPath);
        if(update.message) await handleMessage(update.message);
        if(update.callback_query) await handleCallback(update.callback_query);
      }
    } catch(error) {
      console.error(new Date().toISOString(),String(error.message).replaceAll(token,'[redacted]'));
      await new Promise(resolve=>setTimeout(resolve,3000));
    }
  }
}

if(!command||!interactive||!beforeDispatch) throw new Error('Plugin handlers were not registered');
await poll();
