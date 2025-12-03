import { ce, $ } from '../utils/dom.js';
import { savePin, loadPins } from '../utils/storage.js';

const AGENTS = [
  {id:'director', name:'Game Director', desc:'Vision owner'},
  {id:'systems', name:'Systems Designer', desc:'Mechanics & balance'},
  {id:'ux', name:'UX Director', desc:'Flow & HUD'},
  {id:'pm', name:'Product Manager', desc:'KPI & monetisation'}
];

export function renderAgentCards(){
  const container = $('#agentCards');
  AGENTS.forEach(a=>{
    const card = ce('div','agent-card');
    card.innerHTML = `<h4>${a.name}</h4><p>${a.desc}</p>`;
    const pin = ce('button','btn-pin'); pin.innerText='Pin';
    pin.addEventListener('click', ()=> {
      savePin({title: a.name, text: a.desc, ts: Date.now()});
      alert('Pinned: ' + a.name);
    });
    card.appendChild(pin);
    container.appendChild(card);
  });
}
