import pytest
from homeassistant.core import HomeAssistant
from custom_components.veracross import api as vapi

STEP1 = ('<form id="login-form" action="/demo/portals/login/user_lookup" method="post">'
         '<input type="hidden" name="authenticity_token" value="test-csrf-1"><input type="text" name="username">'
         '<input type="submit" name="commit" value="Next" id="recaptcha"></form>')
STEP2 = ('<form action="/demo/portals/login/password" method="post"><input type="hidden" name="authenticity_token" value="test-csrf-2">'
         '<input type="hidden" name="username" value="test-user"><input type="password" name="password"></form>')

def make(hass, responses, sent):
    api = vapi.VeracrossAPI(hass, None, "test-user", "test-password", "demo")
    it = iter(responses)
    async def fake(method, url, **kw):
        sent.append((method, url, kw.get("data")))
        return next(it)
    api._request = fake
    saved = {}
    async def save(pages): saved.update(pages)
    api._save_debug_pages = save
    return api, saved

async def test_two_step_login_ok(hass: HomeAssistant):
    sent = []
    api, saved = make(hass, [
        (200, "https://accounts.veracross.com/demo/portals/login", "text/html", STEP1),
        (200, "https://accounts.veracross.com/demo/portals/login/user_lookup", "text/html", STEP2),
        (200, "https://portals.veracross.com/demo/parent", "text/html", "<html>ok</html>"),
        (200, "https://portals.veracross.com/demo/parent", "text/html", "<html>ok</html>"),
    ], sent)
    await api.login()
    assert api._logged_in
    assert sent[1][0] == "POST" and sent[1][1].endswith("/demo/portals/login/user_lookup")
    assert sent[1][2]["username"] == "test-user" and sent[1][2]["authenticity_token"] == "test-csrf-1"
    assert sent[2][2]["password"] == "test-password" and sent[2][2]["authenticity_token"] == "test-csrf-2"
    assert not saved

async def test_bounce_back_to_username_is_captcha_not_bad_password(hass: HomeAssistant):
    sent = []
    api, saved = make(hass, [
        (200, "https://accounts.veracross.com/demo/portals/login", "text/html", STEP1),
        (422, "https://accounts.veracross.com/demo/portals/login/user_lookup", "text/html", STEP1),
    ], sent)
    with pytest.raises(vapi.VeracrossCaptchaError):
        await api.login()
    assert "login_step2.html" in saved
    assert all("test-password" not in body for body in saved.values())
    assert len(sent) == 2, "password must never be sent when blocked"

async def test_debug_files_never_retain_server_values(hass, tmp_path):
    """An echoed password, hidden token, script, or URL must never reach disk."""
    hass.config.config_dir = str(tmp_path)
    api = vapi.VeracrossAPI(hass, None, 'test-user', 'test-password', 'demo')
    body = '''<meta name="csrf-token" content="test-csrf-sensitive">
      <form action="/?password=test-password"><input name="username" value="test-user">
      <input name="password" type="password" value="test-password"><input type="hidden" value="test-cookie">
      </form><script>const credential = "test-password";</script>
      <p>test-user test-password test-api-key</p>'''
    await api._save_debug_pages({'login_step2.html': body})
    saved = (tmp_path / 'veracross_debug' / 'login_step2.html').read_text()
    for value in ('test-user', 'test-password', 'test-csrf-sensitive', 'test-cookie', 'test-api-key'):
        assert value not in saved
    assert '"has_password_input": true' in saved
    assert '"has_csrf": true' in saved


@pytest.mark.parametrize('action', [
    'https://foreign.invalid/login', 'http://accounts.veracross.com/login',
    'https://veracross.com.foreign.invalid/login', 'https://veracross.com/login',
])
@pytest.mark.parametrize('step', [1, 2])
async def test_untrusted_form_action_never_posted(hass, action, step):
    sent = []
    first = STEP1 if step == 2 else STEP1.replace('/demo/portals/login/user_lookup', action)
    responses = [(200, 'https://accounts.veracross.com/login', 'text/html', first)]
    if step == 2:
        responses.append((200, 'https://accounts.veracross.com/login', 'text/html',
                          STEP2.replace('/demo/portals/login/password', action)))
    api, _ = make(hass, responses, sent)
    with pytest.raises(vapi.VeracrossAuthError, match='Untrusted'):
        await api.login()
    assert len(sent) == step
    assert all(url != action for _, url, _ in sent)


@pytest.mark.parametrize('location', ['https://foreign.invalid/collect', 'http://portals.veracross.com/parent'])
async def test_login_redirect_to_untrusted_host_not_followed(hass, location):
    api, _ = make(hass, [], [])
    sent = []
    async def request(method, url, **kwargs):
        assert kwargs['allow_redirects'] is False
        sent.append((method, url))
        if method == 'GET':
            return 200, url, 'text/html', STEP1
        kwargs['redirect_info']['location'] = location
        return 302, url, 'text/html', ''
    api._request = request
    with pytest.raises(vapi.VeracrossAuthError, match='Untrusted'):
        await api.login()
    assert len(sent) == 2
    assert not api._logged_in


@pytest.mark.parametrize('status', [301, 302, 303, 307, 308])
async def test_login_redirect_credential_semantics(hass, status):
    api, _ = make(hass, [], [])
    sent = []
    async def request(method, url, **kwargs):
        assert kwargs['allow_redirects'] is False
        sent.append((method, url, kwargs.get('data')))
        if len(sent) == 1:
            kwargs['redirect_info']['location'] = '/next'
            return status, url, 'text/html', ''
        return 200, url, 'text/html', 'done'
    api._request = request
    if status in (307, 308):
        with pytest.raises(vapi.VeracrossAuthError, match='replay'):
            await api._login_request('POST', 'https://accounts.veracross.com/login', data={'password': 'secret'})
        assert len(sent) == 1
    else:
        await api._login_request('POST', 'https://accounts.veracross.com/login', data={'password': 'secret'})
        assert sent[1] == ('GET', 'https://accounts.veracross.com/next', None)


async def test_login_redirect_limit(hass):
    api, _ = make(hass, [], [])
    sent = []
    async def request(method, url, **kwargs):
        sent.append(url)
        kwargs['redirect_info']['location'] = '/next'
        return 302, url, 'text/html', ''
    api._request = request
    with pytest.raises(vapi.VeracrossAuthError, match='excessive'):
        await api._login_request('GET', 'https://accounts.veracross.com/login')
    assert len(sent) == 11


async def test_diagnostic_write_error_logs_type_only(hass, caplog):
    import logging
    from unittest.mock import AsyncMock, patch
    api = vapi.VeracrossAPI(hass, None, 'test-user', 'test-password', 'demo')
    with caplog.at_level(logging.DEBUG, logger=vapi.__name__), patch.object(
        hass, 'async_add_executor_job', AsyncMock(side_effect=OSError('private-path-marker'))
    ):
        await api._save_debug_pages({'login_step1.html': STEP1})
    assert 'OSError' in caplog.text
    assert 'private-path-marker' not in caplog.text


async def test_transport_captures_redirect_without_following(hass):
    from unittest.mock import AsyncMock, MagicMock
    response = MagicMock(status=302, url='https://accounts.veracross.com/login')
    response.headers = {'Location': 'https://foreign.invalid/collect', 'Content-Type': 'text/html'}
    response.text = AsyncMock(return_value='')
    session = MagicMock()
    session.request.return_value.__aenter__ = AsyncMock(return_value=response)
    api = vapi.VeracrossAPI(hass, session, 'test-user', 'test-password', 'demo')
    with pytest.raises(vapi.VeracrossAuthError, match='Untrusted'):
        await api._login_request('POST', str(response.url), data={'password': 'test-password'})
    session.request.assert_called_once_with('POST', str(response.url),
                                           allow_redirects=False, data={'password': 'test-password'})
