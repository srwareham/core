"""Tests for the TP-Link component."""

from __future__ import annotations

import copy
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

from freezegun.api import FrozenDateTimeFactory
from kasa import AuthenticationError, Feature, KasaException, Module
import pytest

from homeassistant import setup
from homeassistant.components import tplink
from homeassistant.components.tplink.const import CONF_DEVICE_CONFIG, DOMAIN
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.const import (
    CONF_AUTHENTICATION,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_USERNAME,
    STATE_ON,
    STATE_UNAVAILABLE,
    EntityCategory,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from . import (
    CREATE_ENTRY_DATA_AUTH,
    CREATE_ENTRY_DATA_LEGACY,
    DEVICE_CONFIG_AUTH,
    IP_ADDRESS,
    MAC_ADDRESS,
    _mocked_device,
    _patch_connect,
    _patch_discovery,
    _patch_single_discovery,
)

from tests.common import MockConfigEntry, async_fire_time_changed


async def test_configuring_tplink_causes_discovery(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Test that specifying empty config does discovery."""
    with (
        patch("homeassistant.components.tplink.Discover.discover") as discover,
        patch("homeassistant.components.tplink.Discover.discover_single"),
    ):
        discover.return_value = {MagicMock(): MagicMock()}
        await async_setup_component(hass, tplink.DOMAIN, {tplink.DOMAIN: {}})
        await hass.async_block_till_done(wait_background_tasks=True)
        # call_count will differ based on number of broadcast addresses
        call_count = len(discover.mock_calls)
        assert discover.mock_calls

        freezer.tick(tplink.DISCOVERY_INTERVAL)
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert len(discover.mock_calls) == call_count * 2

        freezer.tick(tplink.DISCOVERY_INTERVAL)
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert len(discover.mock_calls) == call_count * 3


async def test_config_entry_reload(hass: HomeAssistant) -> None:
    """Test that a config entry can be reloaded."""
    already_migrated_config_entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_HOST: "127.0.0.1"}, unique_id=MAC_ADDRESS
    )
    already_migrated_config_entry.add_to_hass(hass)
    with _patch_discovery(), _patch_single_discovery(), _patch_connect():
        await async_setup_component(hass, tplink.DOMAIN, {tplink.DOMAIN: {}})
        await hass.async_block_till_done()
        assert already_migrated_config_entry.state is ConfigEntryState.LOADED
        await hass.config_entries.async_unload(already_migrated_config_entry.entry_id)
        await hass.async_block_till_done()
        assert already_migrated_config_entry.state is ConfigEntryState.NOT_LOADED


async def test_config_entry_retry(hass: HomeAssistant) -> None:
    """Test that a config entry can be retried."""
    already_migrated_config_entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_HOST: IP_ADDRESS}, unique_id=MAC_ADDRESS
    )
    already_migrated_config_entry.add_to_hass(hass)
    with (
        _patch_discovery(no_device=True),
        _patch_single_discovery(no_device=True),
        _patch_connect(no_device=True),
    ):
        await async_setup_component(hass, tplink.DOMAIN, {tplink.DOMAIN: {}})
        await hass.async_block_till_done()
        assert already_migrated_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_dimmer_switch_unique_id_fix_original_entity_still_exists(
    hass: HomeAssistant, entity_reg: er.EntityRegistry
) -> None:
    """Test no migration happens if the original entity id still exists."""
    config_entry = MockConfigEntry(domain=DOMAIN, data={}, unique_id=MAC_ADDRESS)
    config_entry.add_to_hass(hass)
    dimmer = _mocked_device(alias="My dimmer", modules=[Module.Light])
    rollout_unique_id = MAC_ADDRESS.replace(":", "").upper()
    original_unique_id = tplink.legacy_device_id(dimmer)
    original_dimmer_entity_reg = entity_reg.async_get_or_create(
        config_entry=config_entry,
        platform=DOMAIN,
        domain="light",
        unique_id=original_unique_id,
        original_name="Original dimmer",
    )
    rollout_dimmer_entity_reg = entity_reg.async_get_or_create(
        config_entry=config_entry,
        platform=DOMAIN,
        domain="light",
        unique_id=rollout_unique_id,
        original_name="Rollout dimmer",
    )

    with (
        _patch_discovery(device=dimmer),
        _patch_single_discovery(device=dimmer),
        _patch_connect(device=dimmer),
    ):
        await setup.async_setup_component(hass, DOMAIN, {})
        await hass.async_block_till_done(wait_background_tasks=True)

    migrated_dimmer_entity_reg = entity_reg.async_get_or_create(
        config_entry=config_entry,
        platform=DOMAIN,
        domain="light",
        unique_id=original_unique_id,
        original_name="Migrated dimmer",
    )
    assert migrated_dimmer_entity_reg.entity_id == original_dimmer_entity_reg.entity_id
    assert migrated_dimmer_entity_reg.entity_id != rollout_dimmer_entity_reg.entity_id


async def test_config_entry_wrong_mac_Address(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """Test config entry enters setup retry when mac address mismatches."""
    mismatched_mac = f"{MAC_ADDRESS[:-1]}0"
    already_migrated_config_entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_HOST: "127.0.0.1"}, unique_id=mismatched_mac
    )
    already_migrated_config_entry.add_to_hass(hass)
    with _patch_discovery(), _patch_single_discovery(), _patch_connect():
        await async_setup_component(hass, tplink.DOMAIN, {tplink.DOMAIN: {}})
        await hass.async_block_till_done()
        assert already_migrated_config_entry.state is ConfigEntryState.SETUP_RETRY

    assert (
        "Unexpected device found at 127.0.0.1; expected aa:bb:cc:dd:ee:f0, found aa:bb:cc:dd:ee:ff"
        in caplog.text
    )


async def test_config_entry_device_config(
    hass: HomeAssistant,
    mock_discovery: AsyncMock,
    mock_connect: AsyncMock,
) -> None:
    """Test that a config entry can be loaded with DeviceConfig."""
    mock_config_entry = MockConfigEntry(
        title="TPLink",
        domain=DOMAIN,
        data={**CREATE_ENTRY_DATA_AUTH},
        unique_id=MAC_ADDRESS,
    )
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.LOADED


async def test_config_entry_with_stored_credentials(
    hass: HomeAssistant,
    mock_discovery: AsyncMock,
    mock_connect: AsyncMock,
) -> None:
    """Test that a config entry can be loaded when stored credentials are set."""
    stored_credentials = tplink.Credentials("fake_username1", "fake_password1")
    mock_config_entry = MockConfigEntry(
        title="TPLink",
        domain=DOMAIN,
        data={**CREATE_ENTRY_DATA_AUTH},
        unique_id=MAC_ADDRESS,
    )
    auth = {
        CONF_USERNAME: stored_credentials.username,
        CONF_PASSWORD: stored_credentials.password,
    }

    hass.data.setdefault(DOMAIN, {})[CONF_AUTHENTICATION] = auth
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.LOADED
    config = DEVICE_CONFIG_AUTH
    assert config.credentials != stored_credentials
    config.credentials = stored_credentials
    mock_connect["connect"].assert_called_once_with(config=config)


async def test_config_entry_device_config_invalid(
    hass: HomeAssistant,
    mock_discovery: AsyncMock,
    mock_connect: AsyncMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test that an invalid device config logs an error and loads the config entry."""
    entry_data = copy.deepcopy(CREATE_ENTRY_DATA_AUTH)
    entry_data[CONF_DEVICE_CONFIG] = {"foo": "bar"}
    mock_config_entry = MockConfigEntry(
        title="TPLink",
        domain=DOMAIN,
        data={**entry_data},
        unique_id=MAC_ADDRESS,
    )
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.LOADED

    assert (
        f"Invalid connection type dict for {IP_ADDRESS}: {entry_data.get(CONF_DEVICE_CONFIG)}"
        in caplog.text
    )


@pytest.mark.parametrize(
    ("error_type", "entry_state", "reauth_flows"),
    [
        (tplink.AuthenticationError, ConfigEntryState.SETUP_ERROR, True),
        (tplink.KasaException, ConfigEntryState.SETUP_RETRY, False),
    ],
    ids=["invalid-auth", "unknown-error"],
)
async def test_config_entry_errors(
    hass: HomeAssistant,
    mock_discovery: AsyncMock,
    mock_connect: AsyncMock,
    error_type,
    entry_state,
    reauth_flows,
) -> None:
    """Test that device exceptions are handled correctly during init."""
    mock_connect["connect"].side_effect = error_type
    mock_config_entry = MockConfigEntry(
        title="TPLink",
        domain=DOMAIN,
        data={**CREATE_ENTRY_DATA_AUTH},
        unique_id=MAC_ADDRESS,
    )
    mock_config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is entry_state
    assert (
        any(mock_config_entry.async_get_active_flows(hass, {SOURCE_REAUTH}))
        == reauth_flows
    )


async def test_plug_auth_fails(hass: HomeAssistant) -> None:
    """Test a smart plug auth failure."""
    config_entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_HOST: "127.0.0.1"}, unique_id=MAC_ADDRESS
    )
    config_entry.add_to_hass(hass)
    device = _mocked_device(alias="my_plug", features=["state"])
    with _patch_discovery(device=device), _patch_connect(device=device):
        await async_setup_component(hass, tplink.DOMAIN, {tplink.DOMAIN: {}})
        await hass.async_block_till_done()

    entity_id = "switch.my_plug"
    state = hass.states.get(entity_id)
    assert state.state == STATE_ON
    device.update = AsyncMock(side_effect=AuthenticationError)

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
    await hass.async_block_till_done()
    state = hass.states.get(entity_id)
    assert state.state == STATE_UNAVAILABLE

    assert (
        len(
            hass.config_entries.flow.async_progress_by_handler(
                DOMAIN, match_context={"source": SOURCE_REAUTH}
            )
        )
        == 1
    )


async def test_update_attrs_fails_in_init(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test a smart plug auth failure."""
    config_entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_HOST: "127.0.0.1"}, unique_id=MAC_ADDRESS
    )
    config_entry.add_to_hass(hass)
    light = _mocked_device(modules=[Module.Light], alias="my_light")
    light_module = light.modules[Module.Light]
    p = PropertyMock(side_effect=KasaException)
    type(light_module).color_temp = p
    light.__str__ = lambda _: "MockLight"
    with _patch_discovery(device=light), _patch_connect(device=light):
        await async_setup_component(hass, tplink.DOMAIN, {tplink.DOMAIN: {}})
        await hass.async_block_till_done()

    entity_id = "light.my_light"
    entity = entity_registry.async_get(entity_id)
    assert entity
    state = hass.states.get(entity_id)
    assert state.state == STATE_UNAVAILABLE
    assert "Unable to read data for MockLight None:" in caplog.text


async def test_update_attrs_fails_on_update(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test a smart plug auth failure."""
    config_entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_HOST: "127.0.0.1"}, unique_id=MAC_ADDRESS
    )
    config_entry.add_to_hass(hass)
    light = _mocked_device(modules=[Module.Light], alias="my_light")
    light_module = light.modules[Module.Light]

    with _patch_discovery(device=light), _patch_connect(device=light):
        await async_setup_component(hass, tplink.DOMAIN, {tplink.DOMAIN: {}})
        await hass.async_block_till_done()

    entity_id = "light.my_light"
    entity = entity_registry.async_get(entity_id)
    assert entity
    state = hass.states.get(entity_id)
    assert state.state == STATE_ON

    p = PropertyMock(side_effect=KasaException)
    type(light_module).color_temp = p
    light.__str__ = lambda _: "MockLight"
    freezer.tick(5)
    async_fire_time_changed(hass)
    entity = entity_registry.async_get(entity_id)
    assert entity
    state = hass.states.get(entity_id)
    assert state.state == STATE_UNAVAILABLE
    assert f"Unable to read data for MockLight {entity_id}:" in caplog.text
    # Check only logs once
    caplog.clear()
    freezer.tick(5)
    async_fire_time_changed(hass)
    entity = entity_registry.async_get(entity_id)
    assert entity
    state = hass.states.get(entity_id)
    assert state.state == STATE_UNAVAILABLE
    assert f"Unable to read data for MockLight {entity_id}:" not in caplog.text


async def test_feature_no_category(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test a strip unique id."""
    already_migrated_config_entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_HOST: "127.0.0.1"}, unique_id=MAC_ADDRESS
    )
    already_migrated_config_entry.add_to_hass(hass)
    dev = _mocked_device(
        alias="my_plug",
        features=["led"],
    )
    dev.features["led"].category = Feature.Category.Unset
    with _patch_discovery(device=dev), _patch_connect(device=dev):
        await async_setup_component(hass, tplink.DOMAIN, {tplink.DOMAIN: {}})
        await hass.async_block_till_done()

    entity_id = "switch.my_plug_led"
    entity = entity_registry.async_get(entity_id)
    assert entity
    assert entity.entity_category == EntityCategory.DIAGNOSTIC
    assert "Unhandled category Category.Unset, fallback to DIAGNOSTIC" in caplog.text


@pytest.mark.parametrize(
    ("identifier_base", "expected_message", "expected_count"),
    [
        pytest.param("C0:06:C3:42:54:2B", "Replaced", 1, id="success"),
        pytest.param("123456789", "Unable to replace", 3, id="failure"),
    ],
)
async def test_unlink_devices(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    caplog: pytest.LogCaptureFixture,
    identifier_base,
    expected_message,
    expected_count,
) -> None:
    """Test for unlinking child device ids."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**CREATE_ENTRY_DATA_LEGACY},
        entry_id="123456",
        unique_id="any",
        version=1,
        minor_version=2,
    )
    entry.add_to_hass(hass)

    # Setup initial device registry, with linkages
    mac = "C0:06:C3:42:54:2B"
    identifiers = [
        (DOMAIN, identifier_base),
        (DOMAIN, f"{identifier_base}_0001"),
        (DOMAIN, f"{identifier_base}_0002"),
    ]
    device_registry.async_get_or_create(
        config_entry_id="123456",
        connections={
            (dr.CONNECTION_NETWORK_MAC, mac.lower()),
        },
        identifiers=set(identifiers),
        model="hs300",
        name="dummy",
    )
    device_entries = dr.async_entries_for_config_entry(device_registry, entry.entry_id)

    assert device_entries[0].connections == {
        (dr.CONNECTION_NETWORK_MAC, mac.lower()),
    }
    assert device_entries[0].identifiers == set(identifiers)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    device_entries = dr.async_entries_for_config_entry(device_registry, entry.entry_id)

    assert device_entries[0].connections == {(dr.CONNECTION_NETWORK_MAC, mac.lower())}
    # If expected count is 1 will be the first identifier only
    expected_identifiers = identifiers[:expected_count]
    assert device_entries[0].identifiers == set(expected_identifiers)
    assert entry.version == 1
    assert entry.minor_version == 3

    msg = f"{expected_message} identifiers for device dummy (hs300): {set(identifiers)}"
    assert msg in caplog.text
