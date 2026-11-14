"""One-time 28 A alignment uses HA's public entry API, never private Store I/O."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import test_1fuseloader_ha as ha_fixtures
import test_1fuseloader_runtime as fixtures


def selected_options(**changes):
    return dict(meter_envelope_control=True, nominal_import_only_fallback=True,
                active_control=True, dry_run=False, overload_enabled=True,
                automatic_overload_cycles=True, grid_confirmation_seconds=30,
                **changes)


class ProfileMigrationTests(unittest.IsolatedAsyncioTestCase):
    def setup_entry(self, options=None, *, version=1, minor=1):
        self.entry=SimpleNamespace(version=version, minor_version=minor,
            data={"controlled_phase":"L1","device_id":"synthetic-charger"},
            options=dict(options or {}))
        self.updates=[]
        def update(entry, **changes):
            self.updates.append(deepcopy(changes))
            for key,value in changes.items():
                setattr(entry,key,value)
            return True
        self.history={"used_seconds":7002.,"failure_reason":"response_timeout",
            "automatic_cycle":{"used_seconds":75.,"state":"blocked"}}
        self.hass=SimpleNamespace(config_entries=SimpleNamespace(async_update_entry=update),
            data={"synthetic_own_ledger":deepcopy(self.history)})
        return self.entry

    async def test_selected_legacy_29_profile_tightens_once_in_single_public_update(self):
        entry=self.setup_entry(selected_options(overload_margin_amps=4.))
        before=deepcopy(entry.options);data=deepcopy(entry.data)
        with patch.object(fixtures.runtime,"OneFuseLoaderController",side_effect=AssertionError("No controller during migration")), \
                patch.object(fixtures.runtime,"Store",side_effect=AssertionError("No private Store migration")):
            self.assertTrue(await fixtures.runtime.async_migrate_entry(self.hass,entry))
        self.assertEqual(entry.minor_version,2)
        self.assertEqual(entry.options,{**before,"overload_margin_amps":3.})
        self.assertEqual(self.updates,[{"options":entry.options,"minor_version":2}])
        self.assertEqual(entry.data,data)
        self.assertEqual(self.hass.data["synthetic_own_ledger"],self.history)

    async def test_current_28_and_tighter_profiles_are_preserved(self):
        for margin in (3.,2.5,.5):
            with self.subTest(margin=margin):
                entry=self.setup_entry(selected_options(overload_margin_amps=margin))
                before=deepcopy(entry.options)
                self.assertTrue(await fixtures.runtime.async_migrate_entry(self.hass,entry))
                self.assertEqual(entry.options,before)
                self.assertEqual(entry.minor_version,2)

    async def test_nonmeter_profile_keeps_explicit_29_preference(self):
        entry=self.setup_entry({"overload_margin_amps":4.})
        self.assertTrue(await fixtures.runtime.async_migrate_entry(self.hass,entry))
        self.assertEqual(entry.options,{"overload_margin_amps":4.})
        self.assertEqual(entry.minor_version,2)

    async def test_different_buffer_profile_is_not_silently_aligned(self):
        entry=self.setup_entry(selected_options(buffer_amps=1.,overload_margin_amps=4.))
        before=deepcopy(entry.options)
        self.assertTrue(await fixtures.runtime.async_migrate_entry(self.hass,entry))
        self.assertEqual(entry.options,before)
        self.assertEqual(entry.minor_version,2)

    async def test_migration_does_not_expand_unspecified_legacy_options(self):
        entry=self.setup_entry({"meter_envelope_control":True,"overload_margin_amps":4.})
        self.assertTrue(await fixtures.runtime.async_migrate_entry(self.hass,entry))
        self.assertEqual(entry.options,{"meter_envelope_control":True,"overload_margin_amps":3.})
        self.assertNotIn("active_control",entry.options)
        self.assertNotIn("dry_run",entry.options)

    async def test_later_explicit_29_choice_and_future_minor_are_not_overwritten(self):
        for minor in (2,3):
            with self.subTest(minor=minor):
                entry=self.setup_entry(selected_options(overload_margin_amps=4.),minor=minor)
                before=deepcopy(entry.options)
                self.assertTrue(await fixtures.runtime.async_migrate_entry(self.hass,entry))
                self.assertEqual(entry.options,before)
                self.assertEqual(entry.minor_version,minor)
                self.assertEqual(self.updates,[])

    async def test_invalid_options_fail_before_any_entry_or_ledger_change(self):
        for invalid in ({"made_up_setting":True},{"overload_margin_amps":float("nan")},
                        {"main_fuse_amps":32.,"meter_envelope_control":True}):
            with self.subTest(invalid=invalid):
                entry=self.setup_entry(invalid)
                with self.assertRaises(ValueError):
                    await fixtures.runtime.async_migrate_entry(self.hass,entry)
                self.assertEqual(entry.minor_version,1)
                self.assertEqual(self.updates,[])
                self.assertEqual(self.hass.data["synthetic_own_ledger"],self.history)

    async def test_unsupported_future_major_is_rejected_without_downgrade(self):
        entry=self.setup_entry(selected_options(),version=2)
        self.assertFalse(await fixtures.runtime.async_migrate_entry(self.hass,entry))
        self.assertEqual(entry.version,2)
        self.assertEqual(self.updates,[])

    async def test_legacy_dataclass_and_validation_defaults_are_unchanged(self):
        self.assertEqual(fixtures.logic.RegulatorSettings().overload_margin_amps,4.)
        self.assertEqual(fixtures.const.DEFAULT_OPTIONS["overload_margin_amps"],4.)
        self.assertEqual(fixtures.const.validate_options({})["overload_margin_amps"],4.)


@unittest.skipUnless(ha_fixtures.HAS_HA,"Real Home Assistant is installed in the CI job")
class ProfileMigrationRealHATests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await ha_fixtures.RealHomeAssistantTests.asyncSetUp(self)
        from homeassistant.config_entries import ConfigEntries
        self.manager=ConfigEntries(self.hass,{})
        self.hass.config_entries=self.manager
        # Register using the public manager method, avoiding actual device
        # setup and unrelated issue-registry/storage work in this contract.
        with patch.object(self.manager,"async_setup",new_callable=AsyncMock,return_value=True), \
                patch.object(self.manager,"async_update_issues"), \
                patch.object(self.manager._store,"async_delay_save"):
            await self.manager.async_add(self.entry)

    async def asyncTearDown(self):
        await ha_fixtures.RealHomeAssistantTests.asyncTearDown(self)

    async def test_real_config_entry_updates_revision_and_ceiling_atomically(self):
        options={**dict(self.entry.options),**selected_options(overload_margin_amps=4.)}
        with patch.object(self.manager._store,"async_delay_save"):
            self.manager.async_update_entry(self.entry,options=options)
            before_data=dict(self.entry.data)
            with patch.object(self.manager,"async_update_entry",wraps=self.manager.async_update_entry) as update:
                self.assertTrue(await self.component.async_migrate_entry(self.hass,self.entry))
            self.assertEqual(update.call_count,1)
            self.assertEqual(self.entry.version,1)
            self.assertEqual(self.entry.minor_version,2)
            self.assertEqual(self.entry.options["overload_margin_amps"],3.)
            self.assertEqual(dict(self.entry.data),before_data)
            settings=self.component._settings_from_entry(self.entry)
            self.assertEqual(settings.main_fuse_amps+settings.overload_margin_amps,28.)

    async def test_real_minor_revision_preserves_later_explicit_option_choice(self):
        with patch.object(self.manager._store,"async_delay_save"):
            await self.component.async_migrate_entry(self.hass,self.entry)
            options={**dict(self.entry.options),**selected_options(overload_margin_amps=4.)}
            self.manager.async_update_entry(self.entry,options=options)
            with patch.object(self.manager,"async_update_entry",wraps=self.manager.async_update_entry) as update:
                self.assertTrue(await self.component.async_migrate_entry(self.hass,self.entry))
                update.assert_not_called()
            self.assertEqual(self.entry.minor_version,2)
            self.assertEqual(self.entry.options["overload_margin_amps"],4.)

    async def test_real_core_minor_dispatch_invokes_migration_once_before_controller_setup(self):
        integration=SimpleNamespace(domain="1fuseloader",
            async_get_component=AsyncMock(return_value=self.component))
        with patch.object(self.manager._store,"async_delay_save"):
            self.manager.async_update_entry(self.entry,
                options={**dict(self.entry.options),**selected_options(overload_margin_amps=4.)})
            with patch("homeassistant.config_entries.loader.async_get_integration",
                       new_callable=AsyncMock,return_value=integration), \
                    patch.object(self.component,"OneFuseLoaderController",
                                 side_effect=AssertionError("Core migrates before controller setup")), \
                    patch.object(self.component,"async_migrate_entry",
                                 wraps=self.component.async_migrate_entry) as migrate:
                self.assertTrue(await self.entry.async_migrate(self.hass))
                self.assertEqual(migrate.call_count,1)
                self.assertEqual(self.entry.minor_version,2)
                self.assertEqual(self.entry.options["overload_margin_amps"],3.)
                # Core recognizes the new schema revision on subsequent boot
                # rather than re-running this preference alignment.
                self.assertTrue(await self.entry.async_migrate(self.hass))
                self.assertEqual(migrate.call_count,1)

    async def test_new_entry_flow_advertises_minor_two_and_28_default_without_control_opt_in(self):
        flow=self.flow_module.OneFuseLoaderConfigFlow()
        flow.hass=self.hass
        flow.context={"source":"user"}
        values={**dict(self.entry.data),"device_id":"new-synthetic-device"}
        with patch.object(flow,"_input_errors",return_value={}), \
                patch.object(flow,"async_set_unique_id",new_callable=AsyncMock), \
                patch.object(flow,"_abort_if_unique_id_configured"):
            result=await flow.async_step_user(values)
        self.assertEqual(result["type"],"create_entry")
        self.assertEqual(result["version"],1)
        self.assertEqual(result["minor_version"],2)
        self.assertEqual(result["options"]["overload_margin_amps"],3.)
        self.assertFalse(result["options"]["active_control"])
        self.assertTrue(result["options"]["dry_run"])
        self.assertFalse(result["options"]["overload_enabled"])
        self.assertFalse(result["options"]["meter_envelope_control"])

    async def test_public_reconfigure_keeps_revision_and_later_option_preference(self):
        with patch.object(self.manager._store,"async_delay_save"):
            self.manager.async_update_entry(self.entry,minor_version=2,
                options={**dict(self.entry.options),"overload_margin_amps":4.})
            flow=self.flow_module.OneFuseLoaderConfigFlow()
            flow.hass=self.hass
            flow.context={"source":"reconfigure","entry_id":self.entry.entry_id}
            changed={**dict(self.entry.data),"l1_entity":"sensor.reconfigured_l1"}
            with patch.object(flow,"_input_errors",return_value={}), \
                    patch.object(flow,"async_set_unique_id",new_callable=AsyncMock), \
                    patch.object(flow,"_abort_if_unique_id_mismatch"), \
                    patch.object(self.manager,"async_reload",new_callable=AsyncMock,return_value=True):
                result=await flow.async_step_reconfigure(changed)
            self.assertEqual(result["type"],"abort")
            self.assertEqual(self.entry.data["l1_entity"],"sensor.reconfigured_l1")
            self.assertEqual(self.entry.minor_version,2)
            self.assertEqual(self.entry.options["overload_margin_amps"],4.)


if __name__=="__main__":
    unittest.main()
