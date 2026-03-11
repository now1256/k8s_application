(function () {
  const TOKEN_KEY = 'ticketing_access_token';

  function getToken() {
    return localStorage.getItem(TOKEN_KEY);
  }

  function setToken(token) {
    localStorage.setItem(TOKEN_KEY, token);
  }

  function clearToken() {
    localStorage.removeItem(TOKEN_KEY);
  }

  async function fetchMe() {
    const token = getToken();
    if (!token) return null;

    const response = await fetch('/api/auth/me', {
      headers: {
        Authorization: `Bearer ${token}`,
      },
    });

    if (!response.ok) {
      clearToken();
      return null;
    }

    return response.json();
  }

  async function renderAuthArea(targetId) {
    const root = document.getElementById(targetId);
    if (!root) return;

    const user = await fetchMe();
    if (user) {
      root.innerHTML = `
        <span class="auth-name">${user.name}님</span>
        <button class="auth-btn" id="logoutButton">로그아웃</button>
      `;

      const logoutButton = document.getElementById('logoutButton');
      if (logoutButton) {
        logoutButton.addEventListener('click', () => {
          clearToken();
          window.location.reload();
        });
      }
      return;
    }

    root.innerHTML = `
      <a class="auth-link" href="/login.html">로그인</a>
      <a class="auth-link" href="/signup.html">회원가입</a>
    `;
  }

  window.TicketingAuth = {
    getToken,
    setToken,
    clearToken,
    fetchMe,
    renderAuthArea,
  };
})();
