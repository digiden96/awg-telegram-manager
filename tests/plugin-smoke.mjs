import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import plugin from '../openclaw-plugin/index.mjs';

const settings=JSON.parse(readFileSync(process.env.AWG_MANAGER_TELEGRAM_CONFIG??'/etc/awg-manager/telegram.json','utf8'));
const owner=String(settings.owner_id);

const registrations={commands:[],interactive:null,hook:null};
plugin.register({
  registerCommand(value){registrations.commands.push(value);},
  registerInteractiveHandler(value){registrations.interactive=value;},
  on(name,handler){if(name==='before_dispatch') registrations.hook=handler;}
});
assert.equal(registrations.commands.length,1);
assert.equal(registrations.commands[0].name,'vpn');
assert.equal(registrations.interactive.namespace,'awg');
const denied=await registrations.commands[0].handler({senderId:'1',channelId:'telegram'});
assert.match(denied.text,/запрещён/);
const menu=await registrations.commands[0].handler({senderId:owner,channelId:'telegram'});
assert.ok(menu.channelData.telegram.buttons.length>=3);
for(const row of menu.channelData.telegram.buttons) for(const button of row) assert.ok(button.callback_data.length<=64);

let edited;
const callbackContext=(payload)=>({
  auth:{isAuthorizedSender:true},senderId:owner,conversationId:'smoke-test',
  callback:{payload},respond:{
    async editMessage(value){edited=value;},
    async reply(value){throw new Error(`Unexpected reply: ${value.text}`);}
  }
});
await registrations.interactive.handler(callbackContext('peers:0'));
const peerButton=edited.buttons.flat().find(item=>item.callback_data?.startsWith('awg:peer:'));
assert.ok(peerButton,'At least one peer is required for the server smoke test');
await registrations.interactive.handler(callbackContext(peerButton.callback_data.slice(4)));
for(const row of edited.buttons) for(const item of row) {
  assert.ok(item.callback_data.length<=64);
  assert.ok(!item.callback_data.includes('${'),'Callback contains an uninterpolated identifier');
}
console.log('OpenClaw plugin registration, authorization, menu and callback sizes passed');
