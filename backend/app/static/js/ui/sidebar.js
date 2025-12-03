import { ce, $ } from '../utils/dom.js';

const tabs = [
  {id:'new', label:'New Concept'},
  {id:'visual', label:'Visual Ideas'},
  {id:'loops', label:'Game Loops'},
  {id:'mon', label:'Monetisation'},
  {id:'mech', label:'Mechanics'}
];

export function initSidebar(){ 
  const container = $('#sidebar'); // your sidebar element id
  tabs.forEach(t=>{
    const btn = ce('button','side-btn');
    btn.dataset.tab = t.id;
    btn.innerText = t.label;
    btn.addEventListener('click', ()=> {
      document.querySelectorAll('.panel').forEach(p=>p.classList.add('hidden'));
      const p = document.getElementById('panel-'+t.id);
      if(p) p.classList.remove('hidden');
    });
    container.appendChild(btn);
  });
}
