"""LinkedIn sign-in against a stand-in for its current login page: inputs with
generated ids, found by what they are (autocomplete), the form rendered twice
with one copy hidden, and a button that isn't a submit. Every request is
answered locally; nothing reaches LinkedIn. The credentials are invented."""

import asyncio

import pytest

from harvey.integrations import linkedin as li

LOGIN_PAGE = """<!doctype html><html><body>
<div style="display:none">
  <input id="_R_hidden_user" type="email" autocomplete="username">
  <input id="_R_hidden_pass" type="password" autocomplete="current-password">
</div>
<div>
  <input id="_R_user" type="email" autocomplete="username webauthn">
  <input id="_R_pass" type="password" autocomplete="current-password">
  <button type="button">Sign in</button>
</div>
<script>
  document.getElementById('_R_pass').addEventListener('keydown', e => {
    if (e.key !== 'Enter') return;
    const ok = document.getElementById('_R_user').value === 'jane@example.com'
      && e.target.value === 'not-a-real-password';
    location.href = ok ? 'https://www.linkedin.com/feed/' : 'https://www.linkedin.com/checkpoint/challenge';
  });
</script></body></html>"""


def _run(coro):
    return asyncio.run(coro)


async def _login(monkeypatch, tmp_path, page_html, password="not-a-real-password"):
    monkeypatch.setattr(li, "COOKIES_PATH", tmp_path / "cookies.json")
    monkeypatch.setattr(li, "ACTIVITY_PATH", tmp_path / "activity.json")
    monkeypatch.setattr(li, "FORM_TIMEOUT_MS", 2000)

    async def no_delay(self, *a, **k):
        return None
    monkeypatch.setattr(li.LinkedInAutomation, "_random_delay", no_delay)
    bot = li.LinkedInAutomation("jane@example.com", password)
    try:
        await bot.start()
    except Exception as e:                      # no Chromium installed here
        pytest.skip(f"Playwright Chromium unavailable: {e}")

    async def serve(route):
        url = route.request.url
        body = page_html if "/login" in url else "<html><body>ok</body></html>"
        await route.fulfill(status=200, content_type="text/html", body=body)
    await bot.context.route("**/*", serve)
    try:
        return await bot.login(), bot.page.url
    finally:
        await bot.stop()


def test_signs_in_on_the_current_markup(monkeypatch, tmp_path):
    ok, url = _run(_login(monkeypatch, tmp_path, LOGIN_PAGE))
    assert ok is True and "/feed" in url


def test_a_security_check_is_reported_not_retried(monkeypatch, tmp_path):
    ok, url = _run(_login(monkeypatch, tmp_path, LOGIN_PAGE, password="wrong"))
    assert ok is False and "checkpoint" in url


def test_a_page_with_no_form_fails_fast(monkeypatch, tmp_path, caplog):
    ok, _ = _run(_login(monkeypatch, tmp_path, "<html><body>Something else</body></html>"))
    assert ok is False
    assert "harvey linkedin login" in caplog.text


def test_the_session_expiry_is_read_without_its_value(monkeypatch, tmp_path):
    path = tmp_path / "cookies.json"
    monkeypatch.setattr(li, "COOKIES_PATH", path)
    assert li.LinkedInAutomation.session_expires() is None
    path.write_text('[{"name": "bcookie", "value": "x"}, '
                    '{"name": "li_at", "value": "secret", "expires": 1893456000}]')
    assert li.LinkedInAutomation.session_expires() == 1893456000.0
