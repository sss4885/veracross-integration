"""Network-free integration tests against Home Assistant's real registries/flows."""
import copy
from datetime import timedelta
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState, ConfigSubentry
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.veracross.api import VeracrossAPI, VeracrossAuthError
from custom_components.veracross.config_flow import student_data, class_key
from custom_components.veracross.parse import parse_children, parse_overview, parse_grade_detail, summarize, normalize, default_display_name

ROOT = Path(__file__).resolve().parents[1] / 'fixtures'
RAW = json.loads((ROOT / 'fixture_la.json').read_text())
CHILDREN = parse_children((ROOT / 'fixture_grade_detail_tab.html').read_text())
OVERVIEW = parse_overview((ROOT / 'fixture_overview.html').read_text())
DETAIL = parse_grade_detail((ROOT / 'fixture_grade_detail_doc.html').read_text())
for child in CHILDREN:
    child['graded'] = {c['id']: c['id'] in OVERVIEW and OVERVIEW[c['id']]['grade'] is not None for c in child['classes']}
ACCOUNT = {'username': 'test-user', 'password': 'test-password', 'school': 'demo'}


@pytest.fixture(autouse=True)
def isolated_config(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)


@pytest.fixture
def api_mock():
    with patch.object(VeracrossAPI, 'login', new_callable=AsyncMock) as login, \
         patch.object(VeracrossAPI, 'discover', new_callable=AsyncMock, return_value=CHILDREN) as discover, \
         patch.object(VeracrossAPI, 'fetch_overview', new_callable=AsyncMock, return_value=OVERVIEW) as overview, \
         patch.object(VeracrossAPI, 'fetch_assignments', new_callable=AsyncMock, return_value=RAW) as assignments, \
         patch.object(VeracrossAPI, 'fetch_grade_detail', new_callable=AsyncMock, return_value=DETAIL) as detail, \
         patch.object(VeracrossAPI, 'fetch_feedback', new_callable=AsyncMock, return_value=[]) as feedback, \
         patch.object(VeracrossAPI, '_request', side_effect=AssertionError('Network forbidden')):
        yield dict(login=login, discover=discover, overview=overview, assignments=assignments, detail=detail, feedback=feedback)


async def setup(hass, *, mode='separate', pin='1111', parent='9999', both=False):
    students = []
    for child in CHILDREN[:2 if both else 1]:
        data = student_data(child, ['51001'] if child['student_id'] == '900001' else [child['classes'][0]['id']])
        data.update(late_mode=mode, pin=pin if child['student_id'] == '900001' else '2222')
        students.append(ConfigSubentry(data=data, subentry_type='student', unique_id=child['student_id'], title=child['name']))
    entry = MockConfigEntry(domain='veracross', version=1, data=ACCOUNT, options={'parent_pin': parent, 'pin_lockout': True},
                            subentries_data=[s.as_dict() for s in students])
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return entry


async def call_pin(hass, pin):
    return await hass.services.async_call('veracross', 'enter_pin', {'pin': pin}, blocking=True, return_response=True)


async def test_config_and_subentry_flows(hass, api_mock):
    r = await hass.config_entries.flow.async_init('veracross', context={'source': 'user'})
    r = await hass.config_entries.flow.async_configure(r['flow_id'], ACCOUNT)
    assert r['step_id'] == 'students'
    r = await hass.config_entries.flow.async_configure(r['flow_id'], {'students': ['900001']})
    assert r['step_id'] == 'classes'
    r = await hass.config_entries.flow.async_configure(r['flow_id'], {'classes': ['51001', '51002']})
    assert r['type'] == 'create_entry'
    entry = r['result']
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(entry.subentries) == 1
    manager = hass.config_entries.subentries
    r = await manager.async_init((entry.entry_id, 'student'), context={'source': 'user'})
    r = await manager.async_configure(r['flow_id'], {'student_id': '900002'})
    assert r['step_id'] == 'classes'
    r = await manager.async_configure(r['flow_id'], {'classes': [CHILDREN[1]['classes'][0]['id']]})
    assert r['type'] == 'create_entry', r
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(entry.subentries) == 2
    sub = next(s for s in entry.subentries.values() if s.unique_id == '900001')
    r = await manager.async_init((entry.entry_id, 'student'), context={'source': 'reconfigure', 'subentry_id': sub.subentry_id})
    assert r['step_id'] == 'settings'
    values = {'late_mode': 'counted', 'pin': '12', 'upcoming_days': 10}
    r = await manager.async_configure(r['flow_id'], values)
    assert r['errors']['pin'] == 'invalid_pin'
    la = next(c for c in sub.data['classes'] if c['id'] == '51001')
    math = next(c for c in sub.data['classes'] if c['id'] == '51002')
    values.update(pin='3333')
    values['classes_order'] = ['sensor.veracross_student_1_english']
    r = await manager.async_configure(r['flow_id'], values)
    assert r['step_id'] == 'names'
    r = await manager.async_configure(r['flow_id'], {class_key(la): 'Literature'})
    assert r['type'] == 'abort' and r['reason'] == 'reconfigure_successful'
    await hass.async_block_till_done(wait_background_tasks=True)
    assert sub.data['late_mode'] == 'counted' and sub.data['pin'] == '3333'
    assert er.async_get(hass).async_get('sensor.veracross_student_1_english').name == 'Literature'
    assert not next(c for c in sub.data['classes'] if c['id'] == '51002')['show']
    assert sub.data['classes'][0]['id'] == '51001'
    assert hass.states.get('sensor.veracross_student_1_english') is not None


@pytest.mark.parametrize(('mode', 'count', 'listed', 'late'), [('separate', 1, 1, 1), ('listed', 1, 2, 0), ('counted', 2, 2, 0)])
async def test_refresh_diff_attention_and_throttle(hass, api_mock, mode, count, listed, late):
    entry = await setup(hass, mode=mode)
    coord = entry.runtime_data
    assert hass.states.get('sensor.veracross_student_1_english').state == '91.25'
    assert hass.states.get('button.veracross_student_1_refresh') is not None
    assert hass.states.get('sensor.veracross_student_1_summary').state == str(count)
    view = coord.views['900001']
    assert len(view['needs_attention']) == listed and len(view['late']) == late
    assert view['classes'][0]['late_count'] == 1
    version = view['data_version']
    api_mock['detail'].reset_mock()
    api_mock['feedback'].reset_mock()
    await coord.async_refresh()
    assert not api_mock['detail'].called and not api_mock['feedback'].called
    assert coord.views['900001']['data_version'] == version
    events = []
    hass.bus.async_listen('veracross_assignment_changed', events.append)
    changed = copy.deepcopy(RAW)
    changed['assignments'][0]['raw_score'] = '8'
    api_mock['assignments'].return_value = changed
    await coord.async_refresh()
    await hass.async_block_till_done()
    assert coord.views['900001']['data_version'] == version + 1
    assert len(events) == 1 and events[0].data['change'] == 'graded'
    assert api_mock['detail'].call_count == 1
    before = api_mock['assignments'].call_count
    await hass.services.async_call('button', 'press', {'entity_id': 'button.veracross_student_1_refresh'}, blocking=True)
    assert api_mock['assignments'].call_count == before
    sensor = hass.states.get('sensor.veracross_student_1_english')
    assert 'assignments' not in sensor.attributes and 'categories' not in sensor.attributes


async def test_pin_lockout_reload(hass, api_mock):
    entry = await setup(hass)
    assert hass.states.get('sensor.veracross_active_student').state == 'none'
    assert await call_pin(hass, '1111') == {'active': '900001', 'student_name': 'Student 1'}
    assert hass.states.get('sensor.veracross_active_student').state == '900001'
    assert (await call_pin(hass, '9999'))['active'] == 'all'
    await hass.services.async_call('veracross', 'lock', blocking=True)
    assert hass.states.get('sensor.veracross_active_student').state == 'none'
    for _ in range(5):
        with pytest.raises(ServiceValidationError, match='wrong_pin'):
            await call_pin(hass, '4444')
    with pytest.raises(ServiceValidationError, match='locked_out'):
        await call_pin(hass, '1111')
    coord = entry.runtime_data
    assert coord.locked_out_until > dt_util.utcnow()
    coord.locked_out_until = dt_util.utcnow() - timedelta(seconds=1)
    await call_pin(hass, '1111')
    before = api_mock['overview'].call_count
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get('sensor.veracross_active_student').state == 'none'
    assert api_mock['overview'].call_count == before


async def test_websocket_access(hass, api_mock):
    await setup(hass, both=True)
    # HA authenticates connections before dispatch. Exercise the actual registered
    # command schemas/handlers without a local HTTP server (network forbidden).
    from homeassistant.components.websocket_api import ActiveConnection
    connection = MagicMock(spec=ActiveConnection)
    connection.user = MagicMock(is_admin=True)
    async def request(kind, **data):
        connection.reset_mock()
        handler, schema = hass.data['websocket_api'][kind]
        message = {'id': 1, 'type': kind, **data}
        handler(hass, connection, schema(message) if schema else message)
        if connection.send_error.called:
            return {'success': False, 'error': {'code': connection.send_error.call_args.args[1]}}
        return {'success': True, 'result': connection.send_result.call_args.args[1]}
    response = await request('veracross/student', student_id='900001')
    assert response['error']['code'] == 'unauthorized'
    assert (await request('veracross/students'))['result'] == []
    await call_pin(hass, '1111')
    assert (await request('veracross/student', student_id='900001'))['result']['name'] == 'Student 1'
    assert not (await request('veracross/student', student_id='900002'))['success']
    assert (await request('veracross/students'))['result'] == []
    await call_pin(hass, '9999')
    assert (await request('veracross/student', student_id='900002'))['result']['name'] == 'Student 2'
    assert len((await request('veracross/students'))['result']) == 2


async def test_two_auth_failures_start_reauth(hass, api_mock):
    entry = await setup(hass)
    api_mock['overview'].side_effect = VeracrossAuthError('Expired')
    await entry.runtime_data.async_refresh()
    assert entry.runtime_data._auth_failures == 1
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert any(f['context']['source'] == 'reauth' for f in hass.config_entries.flow.async_progress())
    assert entry.state is ConfigEntryState.LOADED


async def test_options_parent_pin_rediscovery_and_purge(hass, api_mock):
    # Start with a subset of synthetic classes so discovery can add more classes.
    original = copy.deepcopy(CHILDREN)
    with patch(f'{__name__}.CHILDREN', [{**original[0], 'classes': [next(c for c in original[0]['classes'] if c['id'] == '51001')]}]):
        entry = await setup(hass)
    sub, = entry.subentries.values()
    options = hass.config_entries.options
    r = await options.async_init(entry.entry_id)
    r = await options.async_configure(r['flow_id'], {'parent_pin': '1111', 'pin_lockout': True, 'rediscover': False})
    assert r['errors']['parent_pin'] == 'duplicate_pin'
    r = await options.async_configure(r['flow_id'], {'parent_pin': '12', 'pin_lockout': True, 'rediscover': False})
    assert r['errors']['parent_pin'] == 'invalid_pin'
    r = await options.async_configure(r['flow_id'], {'parent_pin': '9999', 'pin_lockout': False, 'rediscover': True})
    assert r['type'] == 'create_entry'
    await hass.async_block_till_done(wait_background_tasks=True)
    sub, = entry.subentries.values()
    assert len(sub.data['classes']) == len(CHILDREN[0]['classes'])
    assert all(not c['show'] for c in sub.data['classes'] if c['id'] != '51001')
    for _ in range(6):
        with pytest.raises(ServiceValidationError, match='wrong_pin'):
            await call_pin(hass, '4444')
    assert (await call_pin(hass, '9999'))['active'] == 'all'
    await hass.services.async_call('veracross', 'purge', {'before': '2030-01-19'}, blocking=True)
    assert hass.states.get('sensor.veracross_student_1_english').state == '91.25'


async def test_feedback_change_and_calculated_fallback(hass, api_mock):
    api_mock['overview'].return_value = {'51001': {'grade': None, 'letter': None}}
    entry = await setup(hass)
    coord = entry.runtime_data
    row = coord.views['900001']['classes'][0]
    assert row['grade'] == 58.7 and row['grade_source'] == 'calculated'
    raw = copy.deepcopy(RAW)
    raw['assignments'][0]['num_feedback'] = 1
    api_mock['assignments'].return_value = raw
    await coord.async_refresh()
    api_mock['feedback'].assert_awaited_once_with('900001', '51001')
    version = coord.views['900001']['data_version']
    await coord.async_refresh()
    assert api_mock['feedback'].call_count == 1
    assert coord.views['900001']['data_version'] == version


async def test_stale_startup_full_refresh_and_reauth_finish(hass, api_mock):
    entry = await setup(hass)
    old = (dt_util.utcnow() - timedelta(hours=13)).isoformat()
    await entry.runtime_data.db('set_meta', 'last_success:900001', old)
    api_mock['detail'].reset_mock()
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    api_mock['detail'].assert_awaited_once_with('900001', '51001')
    r = await hass.config_entries.flow.async_init('veracross', context={'source': 'reauth', 'entry_id': entry.entry_id}, data=entry.data)
    assert r['step_id'] == 'reauth_confirm'
    r = await hass.config_entries.flow.async_configure(r['flow_id'], {'password': 'updated'})
    assert r['reason'] == 'reauth_successful'
    await hass.async_block_till_done(wait_background_tasks=True)
    assert entry.data['password'] == 'updated' and entry.state is ConfigEntryState.LOADED


async def test_unload_during_refresh(hass, api_mock):
    import asyncio
    entry = await setup(hass)
    started = asyncio.Event()
    async def blocked(student_id):
        started.set()
        await asyncio.Event().wait()
    api_mock['overview'].side_effect = blocked
    task = hass.async_create_task(entry.runtime_data.async_refresh())
    await started.wait()
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert task.done()



def defaults(flow):
    return {str(k): k.default() for k in flow['data_schema'].schema
            if callable(getattr(k, 'default', None))}


async def reconfigure(hass, entry):
    sub = next(s for s in entry.subentries.values() if s.unique_id == '900001')
    return await hass.config_entries.subentries.async_init(
        (entry.entry_id, 'student'), context={'source': 'reconfigure', 'subentry_id': sub.subentry_id})


async def finish_names(hass, flow, values=None):
    result = await hass.config_entries.subentries.async_configure(flow['flow_id'], values or defaults(flow))
    assert result['reason'] == 'reconfigure_successful'
    await hass.async_block_till_done(wait_background_tasks=True)


async def test_reorder_hide_and_overview_only(hass, api_mock):
    entry = await setup(hass)
    sub, = entry.subentries.values()
    registry = er.async_get(hass)
    entity = lambda cid: registry.async_get_entity_id('sensor', 'veracross', f'{entry.entry_id}_900001_{cid}')
    original = [c['id'] for c in sub.data['classes']]
    assert all(entity(cid) for cid in original)
    # First show three fixture classes, then reorder and remove English.
    flow = await reconfigure(hass, entry)
    config = flow['data_schema'].schema[next(k for k in flow['data_schema'].schema if str(k) == 'classes_order')].config
    assert config['multiple'] and config['reorder']
    assert config['include_entities'] == [entity(cid) for cid in original]
    values = defaults(flow)
    values['classes_order'] = [entity(cid) for cid in ['51001', '51002', '51003']]
    flow = await hass.config_entries.subentries.async_configure(flow['flow_id'], values)
    await finish_names(hass, flow)
    flow = await reconfigure(hass, entry)
    previous = [c['id'] for c in sub.data['classes']]
    values = defaults(flow)
    values['classes_order'] = [entity('51003'), entity('51002')]
    flow = await hass.config_entries.subentries.async_configure(flow['flow_id'], values)
    assert [k.split(':')[0] for k in defaults(flow)] == ['51003', '51002']
    await finish_names(hass, flow)
    assert [c['id'] for c in sub.data['classes']] == ['51003', '51002'] + [c for c in previous if c not in ('51003', '51002')]
    assert [c['id'] for c in sub.data['classes'] if c['show']] == ['51003', '51002']
    assert registry.async_get(entity('51001')).hidden_by is er.RegistryEntryHider.INTEGRATION
    for mock in api_mock.values():
        mock.reset_mock()
    await entry.runtime_data.async_refresh()
    for method in ('assignments', 'detail', 'feedback'):
        assert all(call.args[1] != '51001' for call in api_mock[method].call_args_list)
    api_mock['overview'].assert_awaited_once_with('900001')
    state = hass.states.get(entity('51001'))
    assert state.state == str(OVERVIEW['51001']['grade'])
    assert state.attributes['shown'] is False
    assert state.attributes['teacher'] == OVERVIEW['51001']['teacher']
    assert state.attributes['letter'] == OVERVIEW['51001']['letter']
    assert state.attributes['needs_attention_count'] == 0
    assert state.attributes['period'] is None
    view = entry.runtime_data.views['900001']
    assert [c['id'] for c in view['classes']] == ['51003', '51002']
    assert all(i['class_id'] != '51001' for key in ('assignments', 'needs_attention', 'late') for i in view[key])
    summary = hass.states.get('sensor.veracross_student_1_summary')
    assert [c['id'] for c in summary.attributes['classes']] == ['51003', '51002']
    assert int(summary.state) == sum(c['needs_attention_count'] for c in view['classes'])
    # Integration hiding clears when selected again; a user's own hide survives.
    registry.async_update_entity(entity('51002'), hidden_by=er.RegistryEntryHider.USER)
    flow = await reconfigure(hass, entry)
    values = defaults(flow)
    values['classes_order'] = [entity('51003')]
    flow = await hass.config_entries.subentries.async_configure(flow['flow_id'], values)
    await finish_names(hass, flow)
    assert registry.async_get(entity('51002')).hidden_by is er.RegistryEntryHider.USER
    flow = await reconfigure(hass, entry)
    values = defaults(flow)
    values['classes_order'] = [entity('51001'), entity('51002')]
    flow = await hass.config_entries.subentries.async_configure(flow['flow_id'], values)
    await finish_names(hass, flow)
    assert registry.async_get(entity('51001')).hidden_by is None
    assert registry.async_get(entity('51002')).hidden_by is er.RegistryEntryHider.USER
    assert hass.states.get(entity('51001')).attributes['shown'] is True


async def test_names_registry_defaults_payload_and_events(hass, api_mock):
    entry = await setup(hass)
    sub, = entry.subentries.values()
    cls = next(c for c in sub.data['classes'] if c['id'] == '51001')
    entity_id = 'sensor.veracross_student_1_english'
    registry = er.async_get(hass)
    flow = await reconfigure(hass, entry)
    flow = await hass.config_entries.subentries.async_configure(flow['flow_id'], defaults(flow))
    await finish_names(hass, flow, {class_key(cls): '  Literature  '})
    assert registry.async_get(entity_id).name == 'Literature'
    assert registry.async_get(entity_id).original_name == 'Veracross Student 1 English'
    registry.async_update_entity(entity_id, name='My English')
    await hass.async_block_till_done()
    assert hass.states.get('sensor.veracross_student_1_summary').attributes['classes'][0]['name'] == 'My English'
    await call_pin(hass, '1111')
    connection = MagicMock()
    handler, schema = hass.data['websocket_api']['veracross/student']
    handler(hass, connection, schema({'id': 1, 'type': 'veracross/student', 'student_id': '900001'}))
    assert connection.send_result.call_args.args[1]['classes'][0]['name'] == 'My English'
    events = []
    hass.bus.async_listen('veracross_assignment_changed', events.append)
    changed = copy.deepcopy(RAW)
    changed['assignments'][0]['raw_score'] = '8'
    api_mock['assignments'].return_value = changed
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert events and all(e.data['class'] == 'My English' for e in events)
    flow = await reconfigure(hass, entry)
    flow = await hass.config_entries.subentries.async_configure(flow['flow_id'], defaults(flow))
    assert defaults(flow)[class_key(cls)] == 'My English'
    await finish_names(hass, flow, {class_key(cls): default_display_name(cls['portal_name'])})
    assert registry.async_get(entity_id).name is None
    assert entry.runtime_data.views['900001']['classes'][0]['name'] == 'English'


async def test_legacy_name_migrates_once(hass, api_mock):
    child = CHILDREN[0]
    data = student_data(child, ['51001'])
    next(c for c in data['classes'] if c['id'] == '51001')['name'] = 'Literature'
    sub = ConfigSubentry(data=data, subentry_type='student', unique_id=child['student_id'], title=child['name'])
    entry = MockConfigEntry(domain='veracross', version=1, data=ACCOUNT, subentries_data=[sub.as_dict()])
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    registry = er.async_get(hass)
    entity_id = 'sensor.veracross_student_1_english'
    assert registry.async_get(entity_id).name == 'Literature'
    registry.async_update_entity(entity_id, name=None)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert registry.async_get(entity_id).name is None


async def test_settings_validate_selection(hass, api_mock):
    entry = await setup(hass, both=True)
    flow = await reconfigure(hass, entry)
    values = defaults(flow)
    values['classes_order'] = []
    result = await hass.config_entries.subentries.async_configure(flow['flow_id'], values)
    assert result['errors'] == {'classes_order': 'select_class'}
    values['classes_order'] = ['sensor.veracross_student_1_english'] * 2
    result = await hass.config_entries.subentries.async_configure(flow['flow_id'], values)
    assert result['errors'] == {'classes_order': 'invalid_class'}
    other = next(s for s in entry.subentries.values() if s.unique_id == '900002')
    cid = other.data['classes'][0]['id']
    values['classes_order'] = [er.async_get(hass).async_get_entity_id('sensor', 'veracross', f'{entry.entry_id}_900002_{cid}')]
    # The selector itself rejects entities outside include_entities.
    import voluptuous as vol
    with pytest.raises(vol.Invalid):
        result['data_schema'](values)


async def test_overlapping_entries_are_isolated_and_removal_deletes_only_one(hass, api_mock, tmp_path):
    first = await setup(hass)
    altered = copy.deepcopy(RAW)
    altered['assignments'][0]['assignment_notes'] = 'Second account only'
    api_mock['assignments'].return_value = altered
    api_mock['overview'].return_value = {**OVERVIEW, '51001': {'grade': 82.5, 'letter': 'B', 'teacher': 'Teacher Z'}}
    second = await setup(hass, pin='3333', parent='8888')
    a, b = first.runtime_data, second.runtime_data
    for coord, grade, notes in [(a, 91.25, RAW['assignments'][0]['assignment_notes']),
                                (b, 82.5, 'Second account only')]:
        stored = await coord.db('load_student', '900001')
        assert stored['classes']['51001']['grade'] == grade
        assert next(i for i in stored['assignments']['51001'] if i['id'] == 61001)['notes'] == notes
        assert coord.views['900001']['classes'][0]['grade'] == grade
    paths = [tmp_path / 'veracross' / f'{entry.entry_id}.db' for entry in (first, second)]
    assert paths[0] != paths[1] and all(p.exists() for p in paths)
    # Reload uses only this entry's cache, even though the other has identical IDs.
    await hass.config_entries.async_reload(first.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert first.runtime_data.views['900001']['classes'][0]['grade'] == 91.25
    assert await hass.config_entries.async_remove(first.entry_id)
    assert all(not Path(str(paths[0]) + suffix).exists() for suffix in ('', '-wal', '-shm'))
    assert paths[1].exists()
    assert (await b.db('load_student', '900001'))['classes']['51001']['grade'] == 82.5


async def test_upcoming_window_uses_synthetic_today(hass, api_mock):
    entry = await setup(hass)
    with patch('custom_components.veracross.coordinator.dt_util.now',
               return_value=dt_util.parse_datetime('2030-01-18T12:00:00+00:00')):
        await entry.runtime_data.async_build_views()
    # Jan 18 and Jan 19 fall in the seven-day window; Jan 29 does not.
    assert entry.runtime_data.views['900001']['classes'][0]['upcoming_count'] == 2


async def test_refresh_logs_do_not_expose_student_or_error_contents(hass, api_mock, caplog):
    entry = await setup(hass)
    caplog.clear()
    api_mock['overview'].side_effect = VeracrossAuthError('private-error-marker')
    await entry.runtime_data.async_refresh()
    assert 'student_id=900001' in caplog.text
    assert 'Student 1' not in caplog.text
    assert 'private-error-marker' not in caplog.text


def test_discrepancy_log_is_debug_and_has_only_class_id(caplog):
    import logging
    from custom_components.veracross.coordinator import VeracrossCoordinator
    with caplog.at_level(logging.DEBUG, logger='custom_components.veracross.coordinator'):
        VeracrossCoordinator._merge_grade({'grade': 62.4}, {'grade': 91.25}, None,
                                          'private-class-marker', '51001')
    record, = [r for r in caplog.records if 'grades differ' in r.message]
    assert record.levelno == logging.DEBUG
    assert record.message == 'Official and calculated grades differ for class_id=51001'
