"""Private bounded native telemetry has no control or periodic-I/O authority."""
from copy import deepcopy
import json
import unittest
from unittest.mock import Mock, patch

import test_1fuseloader_clock_provenance_adapter as raw
import test_1fuseloader_disconnected_readiness_runtime as ready
import test_1fuseloader_runtime as fixtures


class PassiveAdapterTests(unittest.IsolatedAsyncioTestCase):
    health = staticmethod(raw.ConductorClockTests.health)
    make_charger = raw.ConductorClockTests.make_charger
    assert_no_commands = raw.ConductorClockTests.assert_no_commands
    setUp = raw.ConductorClockTests.setUp

    async def test_raw_allowlist_keeps_actual_clocks_and_excludes_private_payload(self):
        seen = []
        before = {key:len(value) for key,value in self.connection.handlers.items()}
        self.client.set_telemetry_callback(seen.append)
        self.assertEqual(before, {key:len(value) for key,value in self.connection.handlers.items()})
        records = [{"mid":self.charger.id,"id":key,"value":"16.2",
            "timestamp":self.now.isoformat(),"secret":"PRIVATE_SENTINEL"}
            for key in (48,111,112,113,114,182,183,184,185)]
        records += [{"mid":self.charger.id,"id":109,"value":"3"},
            {"mid":self.charger.id,"id":100,"value":"C"},
            {"mid":"other-selected-device","id":114,"value":"99"},
            {"mid":self.charger.id,"id":999,"value":"PRIVATE_SENTINEL"}]
        for handler in self.connection.handlers["ProductUpdate"]:
            await handler(records)
        self.assertEqual(len(seen),1)
        serialized = json.dumps([event.public() for event in seen[0]])
        self.assertNotIn("PRIVATE_SENTINEL",serialized)
        self.assertNotIn(self.charger.id,serialized)
        self.assertEqual({event.observation_id for event in seen[0]},
            {48,100,109,111,112,113,114,182,183,184,185})
        event = seen[0][0]
        self.assertEqual(event.source_at_utc,self.now.isoformat())
        self.assertEqual(event.received_at_utc,self.now.isoformat())
        self.assertEqual(event.received_monotonic,self.mono)
        self.assert_no_commands()

    async def test_invalid_diagnostic_is_fixed_code_and_callback_failure_is_inert(self):
        seen=[]
        self.client.set_telemetry_callback(seen.append)
        self.client._receive_telemetry_observations([
            {"mid":self.charger.id,"id":114,"value":"PRIVATE_REMOTE_TEXT"},
            {"mid":self.charger.id,"id":109,"value":True}],self.charger.id)
        self.assertEqual(len(seen[0]),2)
        self.assertTrue(all(event.value is None and event.error=="invalid_diagnostic_number"
            for event in seen[0]))
        self.client.set_telemetry_callback(Mock(side_effect=RuntimeError("PRIVATE")))
        self.client._receive_telemetry_observations(
            {"mid":self.charger.id,"id":114,"value":0},self.charger.id)
        self.assert_no_commands()


class PassiveControllerTests(unittest.IsolatedAsyncioTestCase):
    update_entry = ready.DisconnectedReadinessRuntimeTests.update_entry
    wall = ready.DisconnectedReadinessRuntimeTests.wall
    schedule = ready.DisconnectedReadinessRuntimeTests.schedule
    active_timers = ready.DisconnectedReadinessRuntimeTests.active_timers
    asyncSetUp = ready.DisconnectedReadinessRuntimeTests.asyncSetUp
    asyncTearDown = ready.DisconnectedReadinessRuntimeTests.asyncTearDown
    set_native = ready.DisconnectedReadinessRuntimeTests.set_native
    snapshot = ready.DisconnectedReadinessRuntimeTests.snapshot
    install = ready.DisconnectedReadinessRuntimeTests.install

    def event(self,key,value):
        return fixtures.runtime.NativeTelemetryObservation(key,value,None,
            self.wall().isoformat(),self.now,1,1)

    async def test_one_hundred_normal_events_have_no_disk_control_or_notify_effect(self):
        c=self.controller
        c._queue_incident_journal=Mock()
        before=deepcopy(c.regulator.export_overload_state())
        with patch.object(c,"_read_input",side_effect=AssertionError("diagnostic read input")), \
                patch.object(c,"notify") as notify, \
                patch.object(c.regulator,"evaluate",side_effect=AssertionError("diagnostic evaluate")):
            for index in range(100):
                c._handle_passive_telemetry((self.event(182,16.1),self.event(114,16.)))
            c._handle_passive_telemetry((object(),))
        c._queue_incident_journal.assert_not_called()
        notify.assert_not_called()
        c._handle_source_change.assert_not_called()
        self.assertIsNone(c._passive_journal_unsub)
        self.assertEqual(len(c._native_telemetry_events),128)
        self.assertEqual(c.regulator.export_overload_state(),before)
        self.assertEqual(self.adapter.calls,[])

    async def test_pilot_zero_records_without_own_command_and_flushes_only_twice(self):
        c=self.controller
        c._queue_incident_journal=Mock()
        c._handle_passive_telemetry((self.event(114,16.),))
        c._handle_passive_telemetry((self.event(114,0.),))
        self.assertEqual(c._queue_incident_journal.call_count,1)
        marker=c._pilot_zero_observations[-1]
        self.assertEqual(marker["previous_output_amps"],16.)
        self.assertIsNone(marker["last_attempt"])
        timers=self.active_timers()
        self.assertEqual(len(timers),1)
        original_due=timers[0].due
        for _ in range(100):
            c._handle_passive_telemetry((self.event(182,7.),self.event(114,0.)))
        for _ in range(3):
            c._handle_passive_telemetry((self.event(114,16.),self.event(114,0.)))
        self.assertEqual(c._queue_incident_journal.call_count,1)
        self.assertEqual(len(c._pilot_zero_observations),4)
        self.assertEqual(self.active_timers()[0].due,original_due)
        self.now=original_due
        await timers[0].callback(None)
        self.assertEqual(c._queue_incident_journal.call_count,2)
        c._handle_passive_telemetry((self.event(182,7.1),))
        self.assertIsNone(c._passive_journal_unsub)
        self.assertEqual(self.adapter.calls,[])
        self.assertNotIn("native_events",c.diagnostic_attributes())
        self.assertNotIn("pilot_zero_observations",c.diagnostic_attributes())


if __name__ == "__main__":
    unittest.main()
