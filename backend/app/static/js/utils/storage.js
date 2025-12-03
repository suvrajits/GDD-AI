const KEY = 'ggai_pins_v1';
export function savePin(obj){
  const cur = JSON.parse(localStorage.getItem(KEY)||'[]');
  cur.unshift(obj);
  localStorage.setItem(KEY, JSON.stringify(cur.slice(0,50)));
}
export function loadPins(){ return JSON.parse(localStorage.getItem(KEY)||'[]'); }
export function clearPins(){ localStorage.removeItem(KEY); }
