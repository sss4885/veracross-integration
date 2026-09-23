"""Fixture-backed HTTP sequencing; all transports are replaced in memory."""
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from custom_components.veracross.api import VeracrossAPI, VeracrossAuthError

ROOT = Path(__file__).resolve().parents[1] / 'fixtures'
EMBED = (ROOT / 'fixture_grade_detail_tab.html').read_text()
OVERVIEW = (ROOT / 'fixture_overview.html').read_text()
RAW = (ROOT / 'fixture_la.json').read_text()


async def test_discover_real_chooser(hass):
    async def transport(method, url, **kwargs):
        if '/children/' in url:
            return 200, url, 'text/html', EMBED
        return 200, url, 'text/html', OVERVIEW
    with patch.object(VeracrossAPI, '_request', side_effect=transport):
        api = VeracrossAPI(hass, None, 'test-user', 'test-password', 'demo')
        api._logged_in = True
        children = await api.discover()
        assert [(c['student_id'], c['name']) for c in children] == [('900001', 'Student 1'), ('900002', 'Student 2')]
        assert children[0]['graded']['51001'] is True
        assert api.requests == 4  # parent, first overview, chooser, other overview


@pytest.mark.parametrize('rejected', [(403, 'text/plain', ''), (422, 'text/plain', ''), (200, 'text/html', '<html></html>')])
async def test_csrf_reuse_and_one_retry(hass, rejected):
    calls = []
    reject_next = False
    async def transport(method, url, **kwargs):
        nonlocal reject_next
        calls.append((url, kwargs))
        if '/children/' in url:
            return 200, url, 'text/html', EMBED
        if reject_next:
            reject_next = False
            return rejected[0], url, rejected[1], rejected[2]
        return 200, url, 'application/json', RAW
    with patch.object(VeracrossAPI, '_request', side_effect=transport):
        api = VeracrossAPI(hass, None, 'test-user', 'test-password', 'demo')
        api._logged_in = True
        await api.fetch_assignments('900001', '51001')
        await api.fetch_assignments('900001', '51002')
        assert len(calls) == 3
        assert calls[1][1]['headers']['X-CSRF-Token'] == calls[2][1]['headers']['X-CSRF-Token']
        reject_next = True
        await api.fetch_assignments('900001', '51002')
        assert len(calls) == 6
        assert '/children/900001/classes/51002' in calls[4][0]
        assert api.requests == 6


async def test_json_rejection_stops_after_retry(hass):
    async def transport(method, url, **kwargs):
        return (200, url, 'text/html', EMBED) if '/children/' in url else (403, url, '', '')
    with patch.object(VeracrossAPI, '_request', side_effect=transport):
        api = VeracrossAPI(hass, None, 'test-user', 'test-password', 'demo')
        api._logged_in = True
        with pytest.raises(VeracrossAuthError):
            await api.fetch_assignments('900001', '51001')
        assert api.requests == 4


async def test_grade_detail_two_hops_and_feedback(hass):
    doc = (ROOT / 'fixture_grade_detail_doc.html').read_text()
    calls = []
    async def transport(method, url, **kwargs):
        calls.append(url)
        if url.endswith('/feedback'):
            return 200, url, 'application/json', '{"feedback": [], "submissions": []}'
        return 200, url, 'text/html', doc if 'documents.veracross.com' in url else EMBED
    with patch.object(VeracrossAPI, '_request', side_effect=transport):
        api = VeracrossAPI(hass, None, 'test-user', 'test-password', 'demo')
        api._logged_in = True
        result = await api.fetch_grade_detail('900001', '51001')
        assert result['grade'] == 58.7
        assert len(calls) == 2 and calls[1].startswith('https://documents.veracross.com/')
        assert await api.fetch_feedback('900001', '51001') == []
        assert calls[-1].endswith('/enrollment/51001/feedback')


async def test_page_redirects_never_leave_veracross(hass):
    """A redirect off Veracross is refused, so cookies and the CSRF header stay on Veracross."""
    sent = []

    async def once(method, url, *, redirect_info=None, **kwargs):
        sent.append(url)
        if 'portals.veracross.com' in url:
            redirect_info['location'] = 'https://attacker.example/steal'
            return 302, url, 'text/html', ''
        return 200, url, 'text/html', 'should never be reached'

    api = VeracrossAPI(hass, None, 'test-user', 'test-password', 'demo')
    with patch.object(VeracrossAPI, '_request_once', side_effect=once):
        with pytest.raises(VeracrossAuthError):
            await api._request('GET', 'https://portals.veracross.com/demo/parent',
                               headers={'X-CSRF-Token': 'synthetic'})
    assert sent == ['https://portals.veracross.com/demo/parent']


async def test_page_redirects_within_veracross_are_followed(hass):
    async def once(method, url, *, redirect_info=None, **kwargs):
        if url.endswith('/old'):
            redirect_info['location'] = '/demo/new'
            return 302, url, 'text/html', ''
        return 200, url, 'text/html', 'ok'

    api = VeracrossAPI(hass, None, 'test-user', 'test-password', 'demo')
    with patch.object(VeracrossAPI, '_request_once', side_effect=once):
        status, final, _, body = await api._request('GET', 'https://portals.veracross.com/demo/old')
    assert (status, final, body) == (200, 'https://portals.veracross.com/demo/new', 'ok')
