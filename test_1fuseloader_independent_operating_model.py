"""Independent synthetic plant, transport and scheduler regression model.

No household records, network or running Home Assistant are used. The actual
controller and Store wrapper run against public synthetic fixtures. The plant
owns physical current independently; status, meter samples, provider clocks,
local native receipts, HTTP acceptance and finite TTL are separate events.

The healthy oracle is an operation-level property, not a chosen planner mode:
under a known 5 A house/at-most16 A car and healthy desired charging, a positive
accepted offer must not be interrupted by a software zero. Independent source,
manual-pause and hard-peak controls ensure this does not disable protection.
An additional ordinary-load family raises the house to8.1/8.2 A and requires
finite lower offers, including an independently delivered partial native batch.
SDK queue/device behavior remains simulated and needs separate adapter tests.
"""
from __future__ import annotations
import asyncio
from collections import Counter
import heapq
import importlib
import inspect
import itertools
import logging
import math
import random
import traceback
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import test_1fuseloader_disconnected_readiness_runtime as ready
import test_1fuseloader_runtime as fixtures

logic=fixtures.logic
EPOCH=1_700_000_000.


class PhysicalLease:
    """Reference plant TTL: expiry belongs to a request generation, not a cap."""
    def __init__(self):
        self.generation=None
        self.cap=0.
    def install(self,generation,cap):
        self.generation,self.cap=generation,float(cap)
    def expire(self,generation):
        if generation!=self.generation:
            return False
        self.cap=0.
        return True


class RegulatorPlant:
    """Independent additive plant, transport delivery and sparse event cache.

    The plant never calls a regulator helper to decide physical current.
    Source timestamps, local receipts and status are delivered independently.
    A zero is reported as a liveness candidate, not automatically a code defect.
    """
    def __init__(self, ready, grid_delay, ev_delay, status_delay, apply_delay, provider_quantum=0.):
        self.params = dict(ready=ready, grid_delay=grid_delay, ev_delay=ev_delay,
                           status_delay=status_delay, apply_delay=apply_delay,
                           provider_quantum=provider_quantum)
        settings = dict(active_control=True, dry_run=False, nominal_import_only_fallback=True,
                        bounded_nominal_shedding=True, peak_only_response=True,
                        automatic_overload_cycles=True, disconnected_readiness=ready,
                        overload_enabled=True, measurement_settle_seconds=25,
                        grid_confirmation_seconds=30, max_sensor_age_seconds=120,
                        enable_soft_guard=False, enable_pause_resume=False)
        self.r = logic.OneFuseRegulator(logic.RegulatorSettings(**settings))
        self.r.import_overload_state(dict(version=1, used_seconds=4000., exposure_active=False,
            transition_pending=False, minimum_held=False, failure_reason="response_timeout"))
        self.q, self.order, self.seq = [], 0, 0
        self.physical_lease = PhysicalLease()
        self.request_generation = 0
        self.interruptions = []
        self.status = "disconnected" if ready else "awaiting_start"
        self.grid = 5.02
        self.gt = 0.
        self.ev, self.et, self.es = .02, None, None
        self.vector, self.sequences = (), ((182, 0), (183, 0), (184, 0), (185, 0))
        self.cap, self.demand, self.truth = 0., .02, .0
        self.physical_status = "awaiting_start"
        self.first_positive = None
        self.stop = None
        self.trace = []
        self.max_truth_grid = 5.
        self.now = 0.

    def add(self, at, kind, value=None):
        self.order += 1
        heapq.heappush(self.q, (float(at), self.order, kind, value))

    def physical(self):
        draw = max(0., min(self.cap, self.demand))
        self.max_truth_grid = max(self.max_truth_grid, 5.+draw)
        if self.truth >= 1 and draw < 1 and self.demand >= 1:
            self.interruptions.append(dict(at=self.now,draw=draw,demand=self.demand))
        if draw != self.truth:
            self.truth = draw
            self.seq += 1
            self.add(self.now+self.params["ev_delay"], "ev", (draw, self.now, self.seq))
        state = "charging" if draw >= 1. else "awaiting_start"
        if state != self.physical_status:
            self.physical_status = state
            self.add(self.now+self.params["status_delay"], "status", state)

    def evaluate(self, event):
        inp = logic.RegulatorInput(now_ts=self.now, wall_now_ts=EPOCH+self.now,
            status=self.status, grid_l1=self.grid, charger_current=self.ev,
            grid_report_ts=EPOCH+self.gt,
            charger_report_ts=None if self.et is None else EPOCH+self.et,
            data_valid=True, charger_current_trusted=True, charger_native_committed=True,
            control_source_id="synthetic-selected-source", charger_report_family="native_terminal_receipt",
            charger_report_provenance=("synthetic-receiver", "max_terminals", 182, 183),
            charger_increase_ready=True, charger_publication_pending=False,
            charger_event_source_known=self.es is not None,
            charger_event_source_ts=None if self.es is None else EPOCH+self.es,
            charger_observation_report_times=self.vector, charger_observation_sequences=self.sequences)
        d = self.r.evaluate(inp)
        row = dict(at=self.now, event=event, raw=self.grid, raw_source=self.gt,
                   ev=self.ev, ev_receipt=self.et, ev_source=self.es, status=self.status,
                   physical_ev=self.truth, physical_grid=5.+self.truth,
                   mode=d.mode, cap=d.current_p1, command=d.command, send=d.should_call_service,
                   reason=d.reason)
        self.trace.append(row)
        if d.should_call_service:
            # b35 lacked the optional dispatch-input API. Retaining this
            # public signature compatibility permits the SAME oracle to
            # demonstrate RED on that source, rather than fail on TypeError.
            kwargs={"inp":inp} if "inp" in inspect.signature(self.r.begin_submission).parameters else {}
            if not self.r.begin_submission(d, self.now, **kwargs):
                return
            if d.command == "set_limit":
                if d.current_p1 == 0 and self.first_positive is not None:
                    self.stop = row
                    return
                if d.current_p1 > 0 and self.first_positive is None:
                    self.first_positive = self.now
                    # Connect only after a real Ready acknowledgement.
                    if self.params["ready"]:
                        self.add(self.now+3., "status", "awaiting_start")
                    start = max(self.now, 60.)
                    for offset, draw in ((4., 2.), (15., 6.4), (24., 10.), (30., 16.)):
                        self.add(start+offset, "demand", draw)
                self.request_generation += 1
                self.add(self.now+self.params["apply_delay"], "apply",
                    (self.request_generation, d.current_p1, self.now+d.ttl_seconds))
            self.r.acknowledge(d, self.now)

    def run(self):
        for at in range(0, 151, 5):
            self.add(at, "capture_grid")
            self.add(at+.1, "tick")
        while self.q and self.stop is None and self.q[0][0] <= 150.2:
            self.now, _, kind, value = heapq.heappop(self.q)
            if kind == "capture_grid":
                self.add(self.now+self.params["grid_delay"], "grid", (5.+self.truth, self.now))
                continue
            if kind == "demand":
                self.demand = value
                self.physical()
                continue
            if kind == "apply":
                generation, cap, expires = value
                self.physical_lease.install(generation, cap)
                self.cap = self.physical_lease.cap
                self.add(expires, "lease_expire", generation)
                self.physical()
                continue
            if kind == "lease_expire":
                if self.physical_lease.expire(value):
                    self.cap = self.physical_lease.cap
                    self.physical()
                continue
            if kind == "ev":
                self.ev, self.es, seq = value
                quantum = self.params["provider_quantum"]
                if quantum:
                    self.es = math.floor(self.es/quantum)*quantum
                self.et = self.now
                self.vector = ((182, EPOCH+self.now), (183, EPOCH+self.now))
                self.sequences = ((182, seq), (183, seq), (184, 0), (185, 0))
            elif kind == "grid":
                self.grid, self.gt = value
            elif kind == "status":
                self.status = value
            self.evaluate(kind)
        return {"parameters":self.params, "initial_positive_at":self.first_positive,
                "max_physical_grid":self.max_truth_grid, "stop_candidate":self.stop,
                "physical_interruptions":self.interruptions,
                "trace":self.trace if self.stop else self.trace[-5:]}



class VirtualTimeouts:
    """Only runtime's wait_for uses virtual deadlines; actual tasks stay real."""
    def __init__(self,sim): self.sim=sim
    def __getattr__(self,name): return getattr(asyncio,name)
    async def wait_for(self,awaitable,timeout):
        task=asyncio.ensure_future(awaitable)
        expired=False
        def expire():
            nonlocal expired
            if not task.done():
                expired=True
                task.cancel()
        self.sim.add(self.sim.now+timeout,'callback',expire)
        try: return await task
        except asyncio.CancelledError:
            if expired: raise TimeoutError from None
            raise


class ControllerPlant:
    def __init__(self,**options):
        self.opts=dict(ready=False,grid_delay=0.,ev_delay=8.,status_delay=8.,
                      apply_delay=.2,http_delay=.2,store_delay=0.,provider_quantum=1.,fault=None,
                      direct_full=False,direct_draw=16.,house_steps=(),responds=True,
                      budget=1800,horizon=260.2,proof_noise=None,fast_profile=False,ramp_down=False,
                       drop_grid_samples=(),native_offset=0.,secondary_delay=0.,
                       fresh_verification_at=None)
        self.opts.update(options)
        self.now=100.
        self.q=[]; self.serial=0; self.sequence=0; self.request_sequence=0
        self.physical_lease=PhysicalLease()
        self.external_pause=False
        self.draw=0.; self.cap=0.; self.demand=.02; self.house=5.
        self.physical_status='awaiting_start'
        self.first_positive=None; self.first_zero=None; self.trace=[]
        self.max_grid=5.; self.counts=Counter(); self.last_signature=None
        self.errors=[]
        self.interruptions=[]
        self.native_components={}

    def spawn(self,coroutine):
        task=asyncio.create_task(coroutine)
        def completed(done):
            if done.cancelled(): return
            error=done.exception()
            if error is not None:
                self.errors.append(dict(at=self.now,type=type(error).__name__,message=str(error),
                    traceback=''.join(traceback.format_exception(type(error),error,error.__traceback__)),
                    recent_trace=list(self.trace[-25:])))
        task.add_done_callback(completed)
        return task

    def add(self,at,kind,value=None):
        self.serial+=1
        heapq.heappush(self.q,(float(at),self.serial,kind,value))

    async def sleep(self,delay):
        if not delay:
            await asyncio.sleep(0)
            return
        future=asyncio.get_running_loop().create_future()
        self.add(self.now+delay,'callback',lambda: future.set_result(None) if not future.done() else None)
        await future

    def physical(self):
        current=0. if self.external_pause else max(0.,min(self.cap,self.demand))
        self.max_grid=max(self.max_grid,self.house+current)
        if self.draw>=1 and current<1 and self.demand>=1:
            self.interruptions.append(dict(at=self.now,draw=current,demand=self.demand,
                                           manual_pause=self.external_pause))
        if current!=self.draw:
            self.draw=current
            self.sequence+=1
            self.add(self.now+self.opts['ev_delay'],'native',(current,self.now,self.sequence))
        status='paused' if self.external_pause else 'charging' if current>=1 else 'awaiting_start'
        if status!=self.physical_status:
            self.physical_status=status
            self.add(self.now+self.opts['status_delay'],'status',status)

    def pulse(self):
        self.circuit.attributes['state_latestPulse']=self.case.wall().isoformat()
        self.circuit.last_reported=self.case.wall()

    async def setup(self):
        self.case=ready.DisconnectedReadinessRuntimeTests()
        await self.case.asyncSetUp()
        self.case.configure(enabled=self.opts['ready'],overload_budget_seconds=self.opts['budget'])
        self.case.now=self.now
        self.c=self.case.controller
        self.r=self.c.regulator
        self.case.hass.async_create_task=self.spawn
        self.case.publish(self.now,'disconnected' if self.opts['ready'] else 'awaiting_start',
                          5.,.02,receipt=None,changed=90.)
        self.circuit=SimpleNamespace(state='0',last_reported=self.case.wall(),attributes={
            'unit_of_measurement':'A','state_dynamicCircuitCurrentP1':0,
            'state_dynamicCircuitCurrentP2':0,'state_dynamicCircuitCurrentP3':0,
            'state_latestPulse':self.case.wall().isoformat()})
        self.case.states['sensor.circuit']=self.circuit
        registry=SimpleNamespace(async_get=lambda _:None)
        self.registry_patch=patch.object(fixtures.runtime,'er',SimpleNamespace(
            async_get=lambda _:registry,async_entries_for_device=lambda *_:[SimpleNamespace(
                platform='easee',disabled_by=None,entity_id='sensor.circuit')]))
        self.registry_patch.start()
        self.async_patch=patch.object(fixtures.runtime,'asyncio',VirtualTimeouts(self))
        self.async_patch.start()
        # Real coalescing, cancellation and in-flight source validation.
        self.c._handle_source_change=fixtures.runtime.OneFuseLoaderController._handle_source_change.__get__(self.c)
        backend=self.c._budget_store.store
        save=backend.async_save
        async def stored(payload):
            self.counts['store_writes_started']+=1
            await self.sleep(self.opts['store_delay'])
            await save(payload)
            self.counts['store_writes_completed']+=1
        backend.async_save=stored
        self.r.import_overload_state(dict(version=1,used_seconds=4000.,exposure_active=False,
            transition_pending=False,minimum_held=False,failure_reason='response_timeout'))
        # Establish persisted state via the actual public Store wrapper.
        self.c._budget_store.clock=lambda:self.case.epoch+self.now
        self.case.adapter.async_execute=self.execute
        self.case.adapter.async_execute_with_dispatch=self.execute_with_dispatch
        self.case.adapter.reported_limit_receipts=(None,None,None)
        for at in range(100,int(self.opts['horizon'])+1,5):
            self.add(at,'capture_grid')
            self.add(at+.1,'watchdog')
            self.add(at+.05,'pulse')
        if self.opts['fault']:
            self.add(200.,'fault',self.opts['fault'])
        for at,house in self.opts['house_steps']:
            self.add(at,'house',house)
        if self.opts['proof_noise']:
            self.add(205.,'proof_noise',self.opts['proof_noise'])
        if self.opts['fresh_verification_at'] is not None:
            self.add(self.opts['fresh_verification_at'],'fresh_verification')
        self.start=self.spawn(self.c.async_start())
        await self.pump()

    async def execute_with_dispatch(self,decision,on_dispatch):
        # This is the simulated SDK boundary, never a planner proposal. The
        # callback captures the input/counters immediately before transmission.
        if on_dispatch() is not True:
            adapter_api=importlib.import_module(fixtures.PACKAGE+'.easee_adapter')
            raise adapter_api.CommandNotSent('synthetic_dispatch_refused')
        return await self.execute(decision)

    async def execute(self,decision):
        self.case.adapter.calls.append(decision)
        self.counts['http_requests_started']+=1
        self.request_sequence+=1
        request=self.request_sequence
        self.trace.append(dict(at=self.now,event='HTTP_BEGIN',cap=decision.current_p1,
                               command=decision.command,mode=decision.mode,reason=decision.reason))
        if decision.command=='set_limit':
            self.add(self.now+self.opts['apply_delay'],'apply',
                (request,decision.current_p1,self.now+decision.ttl_seconds))
            if decision.current_p1==0 and self.first_positive is not None and self.first_zero is None:
                inp=self.c._read_input()
                self.first_zero=dict(at=self.now,mode=decision.mode,reason=decision.reason,
                    raw=inp.grid_l1,ev=inp.charger_current,status=inp.status,
                    provider=inp.charger_event_source_ts,receipt=inp.charger_report_ts,
                    accepted_cap=self.r.last_target[0] if self.r.last_target else None,
                    true_grid=self.house+self.draw,true_ev=self.draw)
        await self.sleep(self.opts['http_delay'])
        self.counts['http_replies']+=1
        return SimpleNamespace(http_status=200,correlation_ids=())

    async def pump(self):
        for _ in range(35):
            await asyncio.sleep(0)
        if self.r.last_target and self.r.last_target[0]>0 and self.first_positive is None:
            self.first_positive=self.now
            if self.opts['ready']: self.add(self.now+3.,'status','awaiting_start')
            start=max(self.now,160.)
            profile=((4.,self.opts['direct_draw']),) if self.opts['direct_full'] else ((4.,2.),(15.,6.4),(24.,10.),(30.,16.))
            if self.opts['fast_profile']:
                profile=((4.,2.),(4.2,8.),(4.4,16.))
            for offset,draw in profile:
                self.add(start+offset,'demand',draw)
        d=self.c.last_decision
        if d is not None:
            signature=(d.mode,d.current_p1,d.command,d.should_call_service,d.reason)
            if signature!=self.last_signature:
                inp=self.c._read_input()
                self.trace.append(dict(at=self.now,event='PLAN',mode=d.mode,cap=d.current_p1,
                    command=d.command,send=d.should_call_service,reason=d.reason,raw=inp.grid_l1,
                    ev=inp.charger_current,status=inp.status,source=inp.charger_event_source_ts,
                    receipt=inp.charger_report_ts,truth_ev=self.draw,truth_grid=self.house+self.draw,
                    budget_pending=self.c._budget_store.verification_pending,
                    budget_error=self.c._budget_error))
                self.last_signature=signature

    async def event(self,kind,value):
        self.case.now=self.now
        self.counts[kind]+=1
        if kind=='capture_grid':
            if self.now not in self.opts['drop_grid_samples']:
                self.add(self.now+self.opts['grid_delay'],'grid',(self.house+self.draw,self.now))
        elif kind=='grid':
            self.case.grid(*value)
            self.c._handle_source_change(None)
        elif kind=='native':
            current,source,seq=value
            if self.opts['secondary_delay']:
                # Distinct terminals can publish one physical sample in
                # separate callbacks. Updating a lower primary can expose
                # the slightly smaller OLD secondary MAX until its delivery.
                self.add(self.now,'native_component',(182,current,source,seq))
                self.add(self.now+self.opts['secondary_delay'],'native_component',
                         (183,current,source,seq))
                await self.pump()
                return
            current+=self.opts['native_offset']
            self.case.native_vector(float(current),(182,183),{182:self.now,183:self.now},
                {182:seq,183:seq},self.now)
            self.case.native.observation_evidence_credit=tuple((key,True) for key in (182,183,184,185))
            quantum=self.opts['provider_quantum']
            stamp=math.floor(source/quantum)*quantum if quantum else source
            self.case.native.source_timestamps=tuple((key,self.case.wall(stamp)) for key in (182,183,184,185))
            self.pulse()
            self.c._handle_source_change(None)
        elif kind=='native_component':
            key,current,source,seq=value
            reported=current+self.opts['native_offset']-(.002 if key==183 else 0.)
            self.native_components[key]=(max(0.,reported),self.now,source,seq)
            maximum=max(item[0] for item in self.native_components.values())
            winners=tuple(key for key,item in self.native_components.items() if item[0]==maximum)
            reports={key:item[1] for key,item in self.native_components.items()}
            sequences={key:item[3] for key,item in self.native_components.items()}
            self.case.native_vector(maximum,winners,reports,sequences,self.now)
            self.case.native.observation_evidence_credit=tuple((key,True) for key in (182,183,184,185))
            quantum=self.opts['provider_quantum']
            self.case.native.source_timestamps=tuple((key,self.case.wall(
                math.floor(item[2]/quantum)*quantum if quantum else item[2]))
                for key,item in self.native_components.items())
            self.pulse()
            self.c._handle_source_change(None)
        elif kind=='status':
            state=self.case.states['sensor.status']
            if state.state!=value: state.last_changed=self.case.wall()
            state.state='paused' if self.external_pause else value
            state.last_reported=self.case.wall()
            self.pulse()
            self.c._handle_source_change(None)
        elif kind=='demand':
            self.demand=value
            self.physical()
        elif kind=='house':
            self.house=value
            self.physical()
        elif kind=='fresh_verification':
            # A new public verification gate over the SAME loaded backend
            # must perform real save/readback; no pending/durable flag is set.
            previous=self.c._budget_store
            self.c._budget_store=type(previous)(previous.store,
                clock=previous.clock,read_back=previous.store.async_load)
        elif kind=='proof_noise':
            if value=='matching_get':
                # A new GET receipt contains the SAME old7 A observation.
                self.case.install(self.case.snapshot(current=7.,status='charging',observed=164.))
            elif value=='nonwinning_native':
                # One conductor advances while the unchanged winning7 A
                # retains its pre-dispatch receipt, provider time and counter.
                native=self.case.native
                old_receipt=(native.received_at.timestamp()-self.case.epoch)
                old_source=dict(native.source_timestamps)[183]
                old_sequence=dict(native.observation_sequences)[183]
                self.case.native_vector(7.,(183,),{182:self.now,183:old_receipt},
                    {182:old_sequence+1,183:old_sequence},self.now)
                self.case.native.source_timestamps=((182,self.case.wall()),(183,old_source),(184,None),(185,None))
            elif value=='suppressed_winner':
                # The adapter explicitly labels an ambiguous seen-value
                # return as numeric state only. New receipts/counters cannot
                # override that producer contract and forge a minimum event.
                old_sequence=dict(self.case.native.observation_sequences)[183]
                self.case.native_vector(7.,(183,),{182:self.now,183:self.now},
                    {182:old_sequence+1,183:old_sequence+1},self.now)
                self.case.native.observation_evidence_credit=((182,True),(183,False),(184,True),(185,True))
            self.c._handle_source_change(None)
        elif kind=='apply':
            request,cap,expires=value
            physical_cap=(self.cap if not self.opts['responds'] and 0<cap<self.cap else cap)
            if self.opts['ramp_down'] and cap==7 and self.cap>9 and self.opts['responds']:
                physical_cap=9.
                self.add(self.now+.2,'physical_tail',(request,7.))
            self.physical_lease.install(request,physical_cap)
            self.cap=self.physical_lease.cap
            self.add(expires,'lease_expire',request)
            self.circuit.state=str(cap)
            self.circuit.attributes.update(state_dynamicCircuitCurrentP1=cap,
                state_dynamicCircuitCurrentP2=0,state_dynamicCircuitCurrentP3=0)
            self.case.adapter.reported_limit_receipts=(self.now,self.now,self.now)
            self.pulse()
            self.physical()
            self.c._handle_source_change(None)
        elif kind=='physical_tail':
            request,cap=value
            if self.physical_lease.generation==request:
                self.physical_lease.install(request,cap)
                self.cap=self.physical_lease.cap
                self.physical()
        elif kind=='lease_expire':
            if self.physical_lease.expire(value):
                self.cap=self.physical_lease.cap
                self.physical()
        elif kind=='pulse': self.pulse()
        elif kind=='watchdog': self.spawn(self.c.async_tick())
        elif kind=='callback': value()
        elif kind=='fault':
            if value=='source': self.case.adapter.telemetry_block_reason='synthetic_source_fault'
            elif value=='manual_pause':
                self.external_pause=True
                self.physical()
                self.case.states['sensor.status'].state='paused'
                self.case.states['sensor.status'].last_changed=self.case.wall()
            elif value=='hard_peak':
                self.house=39.
                self.case.grid(39.)
            self.c._handle_source_change(None)
        await self.pump()

    async def drive(self,horizon):
        loops=0
        while loops<15000:
            loops+=1
            active=[t for t in self.case.active_timers() if not t.cancelled]
            timer=min(active,key=lambda t:t.due) if active else None
            next_event=self.q[0][0] if self.q else float('inf')
            next_timer=timer.due if timer else float('inf')
            at=min(next_event,next_timer)
            if at>horizon: break
            self.now=max(self.now,at)
            self.case.now=self.now
            if next_timer<=next_event:
                timer.cancelled=True
                self.counts['absolute_timer_callback']+=1
                await timer.callback(self.case.wall())
                await self.pump()
            else:
                _,_,kind,value=heapq.heappop(self.q)
                await self.event(kind,value)
        if loops>=15000: raise RuntimeError('Virtual callback loop did not converge')

    async def run(self):
        await self.setup()
        try:
            await self.drive(self.opts['horizon'])
            result=dict(parameters=self.opts,first_positive_ack=self.first_positive,
                first_zero=self.first_zero,physical_max_grid=self.max_grid,
                counters=dict(self.counts),trace=self.trace,background_errors=self.errors)
            result['physical_interruptions']=list(self.interruptions)
        finally:
            try:
                stopped=asyncio.create_task(self.case.asyncTearDown())
                await self.pump()
                await self.drive(self.opts['horizon']+40.)
                await stopped
            finally:
                self.async_patch.stop()
                self.registry_patch.stop()
        return result



class IndependentRegulatorModelTests(unittest.TestCase):
    def test_old_ttl_generation_cannot_expire_an_identical_renewed_offer(self):
        plant=PhysicalLease()
        plant.install(1,16)
        plant.install(2,16)
        self.assertFalse(plant.expire(1))
        self.assertEqual(plant.cap,16)
        self.assertTrue(plant.expire(2))
        self.assertEqual(plant.cap,0)

    def test_healthy_native_grid_status_permutations_do_not_send_zero(self):
        choices=list(itertools.product((False,True),(0.,2.,5.,8.),(0.,2.,5.,8.),
                                       (0.,2.,5.,8.),(0.,2.,5.)))
        random.Random(1337).shuffle(choices)
        for choice in choices:
            result=RegulatorPlant(*choice).run()
            with self.subTest(parameters=result['parameters']):
                self.assertIsNotNone(result['initial_positive_at'])
                self.assertLessEqual(result['max_physical_grid'],21.)
                self.assertFalse(result['physical_interruptions'],result['physical_interruptions'])
                self.assertIsNone(result['stop_candidate'],result['stop_candidate'])

    def test_provider_precision_does_not_turn_normal_authority_into_a_stop(self):
        for quantum,delay in itertools.product((0.,1.),(0.,.001,.2,1.)):
            result=RegulatorPlant(False,0.,8.,8.,delay,quantum).run()
            with self.subTest(provider_quantum=quantum,physical_delay=delay):
                self.assertIsNotNone(result['initial_positive_at'])
                self.assertLessEqual(result['max_physical_grid'],21.)
                self.assertFalse(result['physical_interruptions'],result['physical_interruptions'])
                self.assertIsNone(result['stop_candidate'],result['stop_candidate'])


class IndependentControllerModelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.loop=asyncio.get_running_loop()
        self.previous_debug=self.loop.get_debug()
        self.loop.set_debug(False)
        self.logger=logging.getLogger('onefuse_runtime_tests.runtime')
        self.was_disabled=self.logger.disabled
        self.logger.disabled=True
    async def asyncTearDown(self):
        self.logger.disabled=self.was_disabled
        self.loop.set_debug(self.previous_debug)

    async def test_every_deadline_http_store_and_source_event_preserves_healthy_charge(self):
        cases=[dict(ready=rd,grid_delay=grid,ev_delay=native,status_delay=status,
                    apply_delay=apply,http_delay=http,store_delay=0.)
               for rd,grid,native,status,apply,http in itertools.product(
                   (False,True),(0.,2.,5.,8.),(0.,2.,5.,8.),(0.,8.),(.2,2.),(0.,.2,2.))]
        # The Store delay exceeds the runtime2s bound, while actual write/readback
        # still succeeds. This includes the FIRST7 A proposal, not just a hold
        # whose7 A acknowledgement has already been accepted.
        cases += [dict(ready=rd,http_delay=http,store_delay=2.5)
                  for rd,http in itertools.product((False,True),(0.,.2,2.))]
        totals={name:0 for name in ('absolute_timer_callback','http_requests_started','store_writes_completed')}
        for options in cases:
            result=await ControllerPlant(**options).run()
            for name in totals: totals[name]+=result['counters'].get(name,0)
            with self.subTest(parameters=result['parameters']):
                self.assertIsNotNone(result['first_positive_ack'])
                self.assertLessEqual(result['physical_max_grid'],21.)
                self.assertFalse(result['physical_interruptions'],result['physical_interruptions'])
                self.assertFalse(result['background_errors'],result['background_errors'][:1])
                self.assertIsNone(result['first_zero'],result['first_zero'])
        self.assertTrue(all(totals.values()),totals)

    async def test_ordinary_high_normal_load_and_partial_native_batch_do_not_stop(self):
        # The physical load stays below25 A, but initially exceeds the23 A
        # normal buffer. The car obeys each lower offer after a finite delay.
        # A small measurement offset and separate conductor callbacks expose
        # an older MAX briefly; neither is a new physical response certificate.
        cases=[dict(direct_full=True,house_steps=((200.,house),),
                    native_offset=.264,secondary_delay=.1,apply_delay=apply,
                    ev_delay=native,http_delay=http,provider_quantum=precision)
               for house,native,http,precision,apply in itertools.product(
                   (8.1,8.2),(0.,8.),(.2,2.),(0.,1.),(.2,16.))]
        cases += [dict(direct_full=True,house_steps=((200.,house),),
                       native_offset=.264,secondary_delay=.002,apply_delay=16.,
                       ev_delay=8.,http_delay=.2,provider_quantum=1.)
                  for house in (8.1,8.2)]
        cases += [dict(direct_full=True,house_steps=((200.,8.2),),
                       native_offset=.264,secondary_delay=.1,apply_delay=.2,
                       ev_delay=8.,http_delay=.2,provider_quantum=1.,store_delay=2.5,
                       fresh_verification_at=213.1)]
        for options in cases:
            plant=ControllerPlant(**options)
            result=await plant.run()
            with self.subTest(parameters=options):
                self.assertIsNotNone(result['first_positive_ack'])
                self.assertLessEqual(result['physical_max_grid'],25.)
                self.assertFalse(result['background_errors'],result['background_errors'][:1])
                self.assertFalse(result['physical_interruptions'],result['physical_interruptions'])
                self.assertIsNone(result['first_zero'],result['first_zero'])
                decreases=[row['cap'] for row in result['trace']
                    if row['event']=='HTTP_BEGIN' and row['at']>=200. and 0<row['cap']<16]
                self.assertTrue(decreases,result['trace'][-12:])
                exposure=plant.r.export_overload_state()
                self.assertEqual(exposure['used_seconds'],4000.)
                self.assertEqual(exposure['automatic_cycle']['used_seconds'],0.)

    async def test_source_fault_manual_pause_and_real_hard_peak_still_preempt(self):
        for fault in ('source','manual_pause','hard_peak'):
            result=await ControllerPlant(fault=fault,http_delay=.2,
                provider_quantum=0.,apply_delay=2.).run()
            with self.subTest(fault=fault):
                self.assertIsNotNone(result['first_positive_ack'])
                self.assertFalse(result['background_errors'],result['background_errors'][:1])
                if fault!='manual_pause':
                    self.assertIsNotNone(result['first_zero'])
                    self.assertGreaterEqual(result['first_zero']['at'],200.)
                later=[row for row in result['trace'] if row['event']=='HTTP_BEGIN' and row['at']>=200.]
                self.assertFalse(any(row['cap']>0 or row['command']=='resume' for row in later),later)

    async def test_real_peak_can_reduce_to_seven_with_coarse_provider_time(self):
        # At200 the independently settled16 A draw plus a22 A house is38 A.
        # The actual request reduces the plant to7 A in.2s; its genuine native
        # receipt8s later is still BEFORE the original source-aged10s deadline.
        # Provider whole-second metadata can legitimately precede HTTP ACK.
        for http in (.2,2.):
            result=await ControllerPlant(direct_full=True,house_steps=((199.9,22.),),
                grid_delay=0.,http_delay=http).run()
            with self.subTest(http=http):
                self.assertAlmostEqual(result['physical_max_grid'],38.)
                self.assertFalse(result['physical_interruptions'],result['physical_interruptions'])
                self.assertFalse(result['background_errors'],result['background_errors'][:1])
                self.assertIsNone(result['first_zero'],result['first_zero'])
                after_peak=[row for row in result['trace'] if row['event']=='HTTP_BEGIN' and row['at']>=200.]
                self.assertTrue(any(row['cap']==7 for row in after_peak),after_peak)
                self.assertFalse(any(row['cap']>7 or row['command']=='resume' for row in after_peak),after_peak)

    async def test_fast_genuine_ramps_can_share_a_provider_second(self):
        cases=(dict(fast_profile=True),dict(direct_full=True,
               house_steps=((199.9,22.),),ramp_down=True))
        for options in cases:
            result=await ControllerPlant(**options).run()
            with self.subTest(options=options):
                self.assertFalse(result['background_errors'],result['background_errors'][:1])
                self.assertFalse(result['physical_interruptions'],result['physical_interruptions'])
                self.assertIsNone(result['first_zero'],result['first_zero'])

    async def test_minimum_receipt_after_original_peak_deadline_cannot_gain_time(self):
        # Two seconds of grid delivery plus.2s application plus8s of native
        # delivery means the minimum arrives at210.2, AFTER deadline210.
        # Physical success in this hidden plant is not timely observable proof.
        for http in (.2,2.):
            result=await ControllerPlant(direct_full=True,house_steps=((199.9,22.),),
                grid_delay=2.,http_delay=http).run()
            with self.subTest(http=http):
                self.assertFalse(result['background_errors'],result['background_errors'][:1])
                self.assertIsNotNone(result['first_zero'])
                self.assertGreaterEqual(result['first_zero']['at'],210.-1e-6)
                self.assertLessEqual(result['first_zero']['at'],210.+1e-6)

    async def test_pre_minimum_high_grid_waits_but_genuinely_later_high_grid_rejects(self):
        old=await ControllerPlant(direct_full=True,house_steps=((199.9,22.),),
            drop_grid_samples=(205.,),http_delay=.2).run()
        self.assertFalse(old['background_errors'],old['background_errors'][:1])
        self.assertFalse(old['physical_interruptions'],old['physical_interruptions'])
        self.assertIsNone(old['first_zero'],old['first_zero'])
        # An independently new205 meter sample remains38 A even though the
        # actual7 A reduction occurred at200.2. It must not borrow30s.
        new=await ControllerPlant(direct_full=True,
            house_steps=((199.9,22.),(204.9,31.)),http_delay=.2).run()
        self.assertFalse(new['background_errors'],new['background_errors'][:1])
        self.assertIsNotNone(new['first_zero'])
        self.assertGreaterEqual(new['first_zero']['at'],205.)
        self.assertLessEqual(new['first_zero']['at'],208.2+1e-6)

    async def test_actual_nonresponse_and_new_grid_above_twenty_nine_still_stop(self):
        cases=(dict(responds=False),dict(house_steps=((199.9,22.),(215.,23.))))
        for options in cases:
            fields=dict(direct_full=True,house_steps=((199.9,22.),),http_delay=.2)
            fields.update(options)
            result=await ControllerPlant(**fields).run()
            with self.subTest(options=options):
                self.assertFalse(result['background_errors'],result['background_errors'][:1])
                self.assertIsNotNone(result['first_zero'])
                self.assertGreaterEqual(result['first_zero']['at'],200.)

    async def test_pre_dispatch_minimum_get_and_nonwinning_updates_cannot_create_response(self):
        for noise in (None,'matching_get','nonwinning_native','suppressed_winner'):
            result=await ControllerPlant(direct_full=True,direct_draw=7.,
                house_steps=((199.9,22.),),proof_noise=noise,http_delay=.2).run()
            with self.subTest(noise=noise):
                self.assertFalse(result['background_errors'],result['background_errors'][:1])
                prior=[row for row in result['trace'] if row['event']=='HTTP_BEGIN'
                       and row['at']<200. and row['cap']>0]
                self.assertTrue(prior)
                self.assertEqual(prior[-1]['cap'],16,prior)
                self.assertIsNotNone(result['first_zero'])
                # The7 A state predates dispatch of the new lower offer;
                # neither a GET nor a different conductor starts another30s.
                self.assertGreaterEqual(result['first_zero']['at'],210.-1e-6)
                self.assertLessEqual(result['first_zero']['at'],210.+1e-6)

    async def test_automatic_budget_and_complete_lease_reserve_are_not_refunded(self):
        result=await ControllerPlant(direct_full=True,house_steps=((199.9,22.),),
            http_delay=.2,horizon=2005.).run()
        self.assertFalse(result['background_errors'],result['background_errors'][:1])
        self.assertIsNotNone(result['first_zero'])
        # Runtime can stop up to90s early to reserve a complete final lease;
        # it cannot stop this healthy established minimum merely for cache age.
        self.assertGreaterEqual(result['first_zero']['at'],200.+1800.-90.-1e-6)
        self.assertLessEqual(result['first_zero']['at'],200.+1800.+1e-6)


if __name__=='__main__':
    unittest.main()
