(() => {
  'use strict';
  const form = document.getElementById('loginForm');
  const field = document.getElementById('password');
  const error = document.getElementById('loginError');
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    error.hidden = true;
    try {
      const res = await fetch('/api/login', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'woo-web' },
        body: JSON.stringify({ password: field.value }),
      });
      if (res.ok) {
        window.location.assign('/');
        return;
      }
      const body = await res.json().catch(() => ({}));
      error.textContent = body.error || 'Sign-in failed.';
    } catch {
      error.textContent = 'Could not reach the server. Check that it is still running.';
    }
    error.hidden = false;
    field.select();
  });
})();
