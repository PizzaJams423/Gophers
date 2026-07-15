const chatLauncher = document.getElementById('chatLauncher');
const chatPanel = document.getElementById('chatPanel');
const chatClose = document.getElementById('chatClose');
const chatForm = document.getElementById('chatForm');
const chatInput = document.getElementById('chatInput');
const chatLog = document.getElementById('chatLog');
const API_BASE = 'https://gbot-vs3h.onrender.com';
const SESSION_KEY = 'gopherbot_session_id';

function getSessionId() {
  let id = localStorage.getItem(SESSION_KEY);
  if (!id) {
    id = (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2));
    localStorage.setItem(SESSION_KEY, id);
  }
  return id;
}

function setOpen(isOpen) {
  chatPanel.dataset.open = String(isOpen);
  chatLauncher.setAttribute('aria-expanded', String(isOpen));
  chatPanel.style.display = isOpen ? 'flex' : 'none';
  chatLauncher.classList.toggle('hidden', isOpen);
  document.body.style.overflow = isOpen ? 'hidden' : '';
  if (isOpen) setTimeout(() => chatInput.focus(), 50);
}

function appendUserMessage(text) {
  const row = document.createElement('div');
  row.className = 'chat-row user';
  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  bubble.textContent = text;
  row.appendChild(bubble);
  chatLog.appendChild(row);
  chatLog.scrollTop = chatLog.scrollHeight;
}

function appendBotMessage(text) {
  const row = document.createElement('div');
  row.className = 'chat-row bot';
  const wrap = document.createElement('div');
  wrap.className = 'bot-message-wrap';
  const avatar = document.createElement('img');
  avatar.src = 'image-28.png';
  avatar.alt = 'Gopherbot avatar';
  avatar.className = 'bot-avatar';
  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  bubble.textContent = text;
  wrap.appendChild(avatar);
  wrap.appendChild(bubble);
  row.appendChild(wrap);
  chatLog.appendChild(row);
  chatLog.scrollTop = chatLog.scrollHeight;
}

function appendTyping() {
  if (document.getElementById('typingRow')) return;
  const row = document.createElement('div');
  row.className = 'chat-row bot';
  row.id = 'typingRow';
  const wrap = document.createElement('div');
  wrap.className = 'bot-message-wrap';
  const avatar = document.createElement('img');
  avatar.src = 'image-28.png';
  avatar.alt = 'Gopherbot avatar';
  avatar.className = 'bot-avatar';
  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  bubble.textContent = 'Typing...';
  wrap.appendChild(avatar);
  wrap.appendChild(bubble);
  row.appendChild(wrap);
  chatLog.appendChild(row);
  chatLog.scrollTop = chatLog.scrollHeight;
}

function removeTyping() {
  const el = document.getElementById('typingRow');
  if (el) el.remove();
}

const FIELD_HINTS = {
  service: 'Try mentioning the service type, like LVP, painting, tile install, drywall, electrical, or general repair.',
  quantity: 'Include a size or amount, such as 200 sq ft, 15 linear ft, 3 doors, or 4 units.',
  hours: 'Add hours for hourly work, like 2 hours or 4.5 hours.',
  name: 'Tell me the client name.',
  phone: 'Add a phone number, like 423-555-1234.',
  email: 'Add an email address, like name@example.com.',
};

function formatErrorMessage(err, responseData, status) {
  const base = responseData?.detail || responseData?.error || responseData?.message || (typeof responseData === 'string' ? responseData : '') || err?.message || '';
  const extra = status ? ` (HTTP ${status})` : '';
  return (base || 'Request failed.') + extra;
}

async function sendToBot(message) {
  const res = await fetch(`${API_BASE}/api/chat`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Accept': 'application/json, text/plain, */*'
    },
    body: JSON.stringify({ session_id: getSessionId(), text: message })
  });

  const raw = await res.text();
  let data = null;
  try { data = raw ? JSON.parse(raw) : null; } catch { data = raw; }
  if (!res.ok) {
    throw new Error(formatErrorMessage(null, data, res.status));
  }
  return data;
}

function formatReply(data) {
  if (!data) return 'Sorry - I could not get a response.';
  if (typeof data === 'string') return data;
  if (data.reply) return data.reply;
  if (data.message) return data.message;
  if (data.detail) return data.detail;
  if (data.error) return data.error;

  if (data.status === 'need_more_info') {
    const fieldList = Array.isArray(data.missing) && data.missing.length ? ` Missing: ${data.missing.join(', ')}.` : '';
    const hint = data.missing && FIELD_HINTS[data.missing[0]] ? ` ${FIELD_HINTS[data.missing[0]]}` : '';
    return (data.question || 'I need a little more info.') + fieldList + hint;
  }

  if (data.status === 'complete' && data.estimate) {
    const e = data.estimate;
    let msg = `Estimate ready: $${e.low} to $${e.high}.`;
    if (e.service_label) msg += ` Service: ${e.service_label}.`;
    if (typeof e.quantity !== 'undefined' && e.unit) msg += ` Amount: ${e.quantity} ${e.unit}.`;
    msg += ` Labor: $${Number(e.labor ?? 0).toFixed(2)}.`;
    msg += ` Add-ons: $${Number(e.addons ?? 0).toFixed(2)}.`;
    msg += ` Final estimate: $${Number(e.final_estimate ?? 0).toFixed(2)}.`;
    if (e.notes && e.notes.length) {
      msg += `
Notes:
- ${e.notes.join('
- ')}`;
    }
    if (data.sheets_logged !== undefined) {
      msg += `
Sheets logged: ${data.sheets_logged ? 'yes' : 'no'}.`;
    }
    if (data.sheets_error) {
      msg += `
Sheets error: ${data.sheets_error}.`;
    }
    return msg;
  }

  return JSON.stringify(data, null, 2);
}

chatLauncher.addEventListener('click', () => setOpen(true));
chatClose.addEventListener('click', () => setOpen(false));
document.addEventListener('keydown', e => { if (e.key === 'Escape' && chatPanel.dataset.open === 'true') setOpen(false); });

chatForm.addEventListener('submit', async e => {
  e.preventDefault();
  const message = chatInput.value.trim();
  if (!message) return;
  appendUserMessage(message);
  chatInput.value = '';
  appendTyping();
  try {
    const data = await sendToBot(message);
    removeTyping();
    appendBotMessage(formatReply(data));
  } catch (err) {
    removeTyping();
    appendBotMessage('Sorry - the chat service is temporarily unavailable. Please try again.');
    console.error(err);
  }
});
