import { ce, $ } from '../utils/dom.js';
import { queryRag } from '../services/ragMock.js';

export function initChat(){
  const sendBtn = $('#sendBtn');
  const input = $('#chatInput');
  const feed = $('#chatFeed');

  sendBtn.addEventListener('click', ()=> {
    const text = input.value.trim();
    if(!text) return;
    appendUserMsg(text);
    input.value='';
    setTimeout(()=> {
      const r = queryRag(text);
      appendBotMsg(r.text + (r.hits.length ? ` (source: ${r.source})` : ''));
    }, 400);
  });
}

function appendUserMsg(text){
  const feed = $('#chatFeed');
  const b = document.createElement('div');
  b.className='msg user';
  b.innerText=text;
  feed.appendChild(b);
  feed.scrollTop = feed.scrollHeight;
}
function appendBotMsg(text){
  const feed = $('#chatFeed');
  const b = document.createElement('div');
  b.className='msg bot';
  b.innerText=text;
  feed.appendChild(b);
  feed.scrollTop = feed.scrollHeight;
}
