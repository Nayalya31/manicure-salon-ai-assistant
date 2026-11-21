(() => {
  const csrfToken = document.querySelector('meta[name="csrf-token"]').content;
  const messages = document.querySelector('#messages');
  const form = document.querySelector('#chat-form');
  const input = document.querySelector('#message-input');
  const suggestions = document.querySelector('#suggestions');
  const errorBox = document.querySelector('#error');
  const confirmation = document.querySelector('#confirmation');
  const confirmButton = document.querySelector('#confirm-submit');
  const newButton = document.querySelector('#new-chat');
  let busy = false;
  let submissionKey = null;

  const api = async (url, body = {}) => {
    const response = await fetch(url, {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken},
      body: JSON.stringify(body),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || 'Не удалось выполнить запрос.');
    return data;
  };

  const addMessage = (role, text, pending = false) => {
    const node = document.createElement('div');
    node.className = `message message--${role}${pending ? ' message--pending' : ''}`;
    node.textContent = text;
    messages.append(node);
    messages.scrollTop = messages.scrollHeight;
    return node;
  };

  const setBusy = (value) => {
    busy = value;
    messages.setAttribute('aria-busy', String(value));
    input.disabled = value;
    form.querySelector('button').disabled = value;
  };

  const showError = (text) => {
    errorBox.textContent = text;
    errorBox.hidden = false;
  };

  const start = async () => {
    setBusy(true);
    errorBox.hidden = true;
    confirmation.hidden = true;
    submissionKey = null;
    messages.replaceChildren();
    const wait = addMessage('assistant', 'Начинаю новый диалог…', true);
    try {
      const data = await api('/api/conversations');
      wait.remove();
      addMessage('assistant', data.greeting);
    } catch (error) {
      wait.remove();
      showError(error.message);
    } finally {
      setBusy(false);
      input.focus();
    }
  };

  const send = async (text) => {
    if (busy || !text.trim()) return;
    errorBox.hidden = true;
    confirmation.hidden = true;
    addMessage('user', text.trim());
    setBusy(true);
    const wait = addMessage('assistant', 'Печатаю…', true);
    try {
      const data = await api('/api/chat', {message: text.trim()});
      wait.remove();
      addMessage('assistant', data.reply);
      if (data.awaiting_confirmation) {
        confirmation.hidden = false;
        submissionKey = submissionKey || crypto.randomUUID();
      }
      suggestions.hidden = true;
    } catch (error) {
      wait.remove();
      showError(error.message);
    } finally {
      setBusy(false);
      input.focus();
    }
  };

  form.addEventListener('submit', (event) => {
    event.preventDefault();
    const text = input.value;
    input.value = '';
    send(text);
  });
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); form.requestSubmit(); }
  });
  suggestions.addEventListener('click', (event) => {
    if (event.target.matches('button')) send(event.target.textContent);
  });
  confirmButton.addEventListener('click', async () => {
    if (busy || !submissionKey) return;
    setBusy(true);
    errorBox.hidden = true;
    try {
      const data = await api('/api/submit', {idempotency_key: submissionKey});
      confirmation.hidden = true;
      addMessage('assistant', data.message);
      input.disabled = true;
      form.querySelector('button').disabled = true;
    } catch (error) {
      showError(error.message);
      setBusy(false);
    }
  });
  newButton.addEventListener('click', start);
  start();
})();
