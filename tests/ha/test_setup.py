"""Public version policy and automatic card installation."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.veracross import async_migrate_entry, async_setup


async def test_setup_registers_bundled_card_once(hass):
    http = MagicMock()
    http.async_register_static_paths = AsyncMock()
    with patch.object(hass, 'http', http), \
         patch('custom_components.veracross.async_get_integration',
               AsyncMock(return_value=SimpleNamespace(version='0.2.0'))), \
         patch('custom_components.veracross.ha_frontend.add_extra_js_url') as add_url:
        assert await async_setup(hass, {})
        assert await async_setup(hass, {})
    http.async_register_static_paths.assert_awaited_once()
    config, = http.async_register_static_paths.call_args.args[0]
    assert config.url_path == '/veracross/veracross-card.js'
    assert Path(config.path).is_file()
    add_url.assert_called_once_with(hass, '/veracross/veracross-card.js?v=0.2.0')


async def test_public_entry_version_policy(hass):
    assert await async_migrate_entry(hass, SimpleNamespace(version=1))
    assert not await async_migrate_entry(hass, SimpleNamespace(version=2))

async def test_school_code_is_required_without_default(hass):
    import voluptuous as vol
    from custom_components.veracross.config_flow import VeracrossConfigFlow
    flow = VeracrossConfigFlow()
    flow.hass = hass
    result = await flow.async_step_user()
    schema = result['data_schema']
    field = next(key for key in schema.schema if key.schema == 'school')
    assert field.default is vol.UNDEFINED
    import pytest
    with pytest.raises(vol.Invalid):
        schema({'username': 'test-user', 'password': 'test-password'})
    with pytest.raises(vol.Invalid):
        schema({'username': 'test-user', 'password': 'test-password', 'school': ''})


async def test_remove_entry_without_loaded_coordinator(hass, tmp_path):
    from custom_components.veracross import async_remove_entry
    hass.config.config_dir = str(tmp_path)
    path = tmp_path / 'veracross' / 'unloaded-entry.db'
    path.parent.mkdir()
    for suffix in ('', '-wal', '-shm'):
        Path(str(path) + suffix).write_bytes(b'synthetic')
    await async_remove_entry(hass, SimpleNamespace(entry_id='unloaded-entry'))
    assert all(not Path(str(path) + suffix).exists() for suffix in ('', '-wal', '-shm'))
