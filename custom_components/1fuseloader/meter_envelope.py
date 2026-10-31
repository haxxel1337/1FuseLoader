"""Standalone meter control: one electrical Episode and five explicit states.

Acquisition/SDK boundaries are separate from policy. No selected-policy path
reads legacy reduction, retry or confirmation owners.
"""
from __future__ import annotations
from copy import deepcopy
from dataclasses import replace
from math import ceil, floor, isfinite
import importlib.util
from pathlib import Path
import sys
_name=__name__+'_state'
if _name not in sys.modules:
    _spec=importlib.util.spec_from_file_location(_name,Path(__file__).with_name('meter_state.py'))
    _module=importlib.util.module_from_spec(_spec);sys.modules[_name]=_module;_spec.loader.exec_module(_module)
_state=sys.modules[_name]
ControlState,ElectricalEpisode,LeaseState,RecoveryState=(_state.ControlState,_state.ElectricalEpisode,_state.LeaseState,_state.RecoveryState)
def number(value):
    if isinstance(value,bool): return None
    try:
        value=float(value)
        return value if isfinite(value) else None
    except (ValueError,TypeError,OverflowError): return None
class ElectricalSupervisor:
    def __init__(self,cause_type): self.cause_type=cause_type
    def zero(self,policy,r,inp,cause,detail=None):
        if not isinstance(cause,self.cause_type): raise TypeError('A typed electrical stop cause is required')
        policy.stop(r,inp,cause)
        d=policy.decision(r,inp,'meter_fault',0,command='set_limit',cause=cause,reason=detail or cause.value)
        policy.zero_permits[d.plan_id]=cause.value
        while len(policy.zero_permits)>64: policy.zero_permits.pop(next(iter(policy.zero_permits)))
        return d
class MeterEnvelopePolicy:
    def __init__(self,decision_type,cause_type):
        self.decision_type,self.causes=decision_type,cause_type
        self.supervisor=ElectricalSupervisor(cause_type)
        self.state=ControlState.NORMAL;self.lease=LeaseState();self.recovery=RecoveryState()
        self.episode=None;self.next_episode_id=1;self.operational_fault=None;self.history=[]
        self.legacy_normal_history=None;self.started=False;self.l1=None;self.last_ev_error=None
        self.recovered_cycle_id=None
        self.zero_permits={};self.feedback_base_cap=self.feedback_source=None
        self.ordinary_decrease_grid=self.ordinary_decrease_cap=self.ordinary_decrease_raw=None
        self.peak_complete_grid=None

    def limit_due(self,r,now,cap):
        return (r.last_target!=(cap,0,0,60) or r.last_set_limit_ts is None
            or now-r.last_set_limit_ts>=max(1,min(r.settings.resend_limit_every_seconds,30)))

    def diagnostics(self,r):
        e=self.episode;c=r._ensure_automatic_cycle()
        return dict(state=self.state.value,episode_id=e.episode_id if e else None,
            episode_phase=e.phase.value if e else None,response_deadline=e.deadline if e and e.unresolved else None,
            grid_deadline=e.grid_deadline if e and e.phase==ControlState.GRID_WAIT else None,
            operational_fault=self.operational_fault,recovery_stage=self.recovery.stage,recovery_required=self.recovery.required,
            cycle_id=c['cycle_id'],used_seconds=c['used_seconds'],remaining_seconds=max(0.,r.settings.overload_budget_seconds-c['used_seconds']),
            historical_failure=c['failure_reason'],incident_count=len(self.history))

    def remember(self,r,reason):
        c=r._ensure_automatic_cycle();eid=self.episode.episode_id if self.episode else None
        if not self.history or (self.history[-1]['reason'],self.history[-1].get('episode_id'),self.history[-1]['resolved'])!=(reason,eid,False):
            self.history.append(dict(reason=reason,episode_id=eid,cycle_id=c['cycle_id'],used_seconds=c['used_seconds'],resolved=False))
            self.history=self.history[-64:]

    def authority_deadline(self,now):
        values=[expiry for _,expiry in self.possible_positive.values() if expiry>now]
        if self.cold_unknown_pending and self.cold_upper_until is not None: values.append(self.cold_upper_until)
        if self.lease.expires is not None and self.lease.cap>0: values.append(self.lease.expires)
        return max(values,default=now)

    def stop(self,r,inp,cause):
        now=number(inp.now_ts) or 0.;raw=number(inp.grid_l1)
        repeat=bool(cause in {self.causes.PEAK_RESPONSE_TIMEOUT,self.causes.GRID_CONFIRMATION_TIMEOUT,
            self.causes.GRID_CEILING,self.causes.OVERLOAD_BUDGET,self.causes.OVERLOAD_NOT_ADMITTED}
            and self.recovery.required and self.lease.cap==0 and raw is not None
            and raw<=r.settings.main_fuse_amps and self.critical_error(r,inp) is None)
        if not repeat: self.recovery=RecoveryState(True,'await_authority',old_authority_until=self.authority_deadline(now))
        self.recovery.required=True;self.operational_fault=cause.value;self.state=ControlState.STOP_RECOVERY
        if self.episode is not None and self.episode.unresolved: self.episode.failure=self.episode.failure or cause.value
        c=r._ensure_automatic_cycle();budget=number(r.settings.overload_budget_seconds)
        c['state']='exhausted' if budget is not None and c['used_seconds']>=budget else 'blocked'
        c['failure_reason']=c['failure_reason'] or cause.value;r._overload_failure_reason=r._overload_failure_reason or cause.value
        self.remember(r,cause.value)

    def export_state(self):
        e=self.episode
        return dict(version=1,state=self.state.value,next_episode_id=self.next_episode_id,operational_fault=self.operational_fault,
            recovery_required=self.recovery.required,recovery_stage=self.recovery.stage,history=deepcopy(self.history),
            recovered_cycle_id=self.recovered_cycle_id,
            episode_active=bool(e and e.unresolved),episode_id=e.episode_id if e else None,
            episode_source=e.source_id if e else None,episode_previous=e.previous_authority if e else None,
            episode_failure=e.failure if e else None,legacy_normal_history=deepcopy(self.legacy_normal_history))

    def restore_state(self,r,state):
        c=r._ensure_automatic_cycle();saved=state.get('meter_control');self.legacy_normal_history=deepcopy(state.get('normal_control'))
        if saved is not None:
            valid=bool(isinstance(saved,dict) and type(saved.get('version')) is int and saved['version']==1 and saved.get('state') in {v.value for v in ControlState}
                and type(saved.get('next_episode_id')) is int and 1<=saved['next_episode_id']<=2**53-1
                and type(saved.get('recovery_required')) is bool and type(saved.get('episode_active')) is bool
                and (saved.get('operational_fault') is None or isinstance(saved['operational_fault'],str)
                    and 0<len(saved['operational_fault'].encode('utf-8'))<=160)
                and saved.get('recovery_stage') in {'none','complete','await_authority','await_normal_headroom',
                    'await_normal_stability','await_normal_acceptance','await_normal_response'}
                and (saved.get('operational_fault') is None or saved['recovery_required'])
                and (saved.get('recovered_cycle_id') is None or type(saved['recovered_cycle_id']) is int
                    and 1<=saved['recovered_cycle_id']<=c['cycle_id'])
                and (saved.get('episode_id') is None or type(saved['episode_id']) is int
                    and 1<=saved['episode_id']<saved['next_episode_id'])
                and (not saved['episode_active'] or saved.get('episode_id') is not None
                    and r._trial_identity(saved.get('episode_source')) is not None
                    and type(saved.get('episode_previous')) in (int,float)
                    and number(saved['episode_previous']) is not None and 0<saved['episode_previous']<=40)
                and (not saved['episode_active'] or c['source_id'] is None
                    or r._trial_identity(saved['episode_source'])==r._trial_identity(c['source_id']))
                and (saved.get('episode_failure') is None or isinstance(saved['episode_failure'],str)
                    and 0<len(saved['episode_failure'].encode('utf-8'))<=160)
                and isinstance(saved.get('history'),list) and len(saved['history'])<=64
                and all(isinstance(item,dict) and set(item)<= {'reason','episode_id','cycle_id','used_seconds','resolved'}
                    and isinstance(item.get('reason'),str) and 0<len(item['reason'].encode('utf-8'))<=160
                    and type(item.get('resolved')) is bool
                    and type(item.get('cycle_id')) is int and item['cycle_id']>=1
                    and (item.get('episode_id') is None or type(item['episode_id']) is int
                        and 1<=item['episode_id']<saved['next_episode_id'])
                    and type(item.get('used_seconds')) in (int,float) and number(item['used_seconds']) is not None
                    and item['used_seconds']>=0 for item in saved['history']))
            if not valid:
                self.operational_fault='invalid_standalone_state';self.recovery=RecoveryState(True,'await_authority');self.state=ControlState.STOP_RECOVERY
                return False
            self.next_episode_id=saved['next_episode_id'];self.history=deepcopy(saved['history'])
            self.recovered_cycle_id=saved.get('recovered_cycle_id')
            self.legacy_normal_history=deepcopy(saved.get('legacy_normal_history',self.legacy_normal_history))
            self.operational_fault=saved['operational_fault'];needed=(saved['recovery_required'] or saved['episode_active']
                or saved['state'] in {'REDUCING','GRID_WAIT','MINIMUM_HOLD'})
            if saved['episode_active']:
                self.operational_fault=self.operational_fault or saved.get('episode_failure') or 'interrupted_episode_after_restore'
                self.episode=ElectricalEpisode(saved['episode_id'],r._trial_identity(saved['episode_source']),
                    None,None,None,saved['episode_previous'],interrupted=True,failure=self.operational_fault)
            if needed and self.operational_fault is None:
                self.operational_fault='interrupted_episode_after_restore'
            if c['state']=='blocked' and not needed:
                self.operational_fault=c['failure_reason'] or 'unresolved_persisted_operational_fault'
                needed=True
            self.recovery=RecoveryState(needed,'await_authority' if needed else 'none')
        elif c['state']=='blocked' or state.get('peak_response_pending') or state.get('transition_pending'):
            self.operational_fault=c['failure_reason'] or state.get('peak_response_failure') or 'interrupted_operational_state'
            self.recovery=RecoveryState(True,'await_authority')
        if self.operational_fault: self.remember(r,self.operational_fault)
        self.state=ControlState.STOP_RECOVERY if self.recovery.required else ControlState.NORMAL
        return True

    def import_boundary(self,r,snapshot):
        accepted=snapshot.get('accepted') if snapshot else None
        if accepted is not None and (self.lease.origin is None or accepted['origin']>=self.lease.origin):
            same_normal=(self.lease.cap==accepted['cap'] and self.lease.source_id==accepted['source_id']
                and self.lease.normal_origin==accepted.get('normal_origin') and self.accepted_l1_floor is not None
                and accepted['floor'] is not None and self.accepted_l1_floor['key']==accepted['floor']['key'])
            self.lease.cap=accepted['cap'];self.lease.origin=accepted['origin'];self.lease.expires=accepted['expires']
            self.lease.source_id=accepted['source_id']
            if not same_normal: self.accepted_l1_floor=deepcopy(accepted['floor'])
            self.lease.normal_origin=accepted.get('normal_origin')
            self.upper=max(self.upper,accepted['cap'])
            if accepted['cap']==0:
                self.zero_l1_floor=deepcopy(accepted['floor']);self.lease.zero_dispatch=accepted['origin']
        if snapshot:
            for cap,expiry in snapshot.get('possible',()):
                self.possible_positive[('handoff',expiry,cap)]=(cap,expiry)
        incoming=snapshot.get('episode') if snapshot else None
        if incoming and (self.episode is None or not self.episode.unresolved):
            self.episode=ElectricalEpisode(self.next_episode_id,**incoming);self.next_episode_id+=1
        if snapshot and snapshot.get('operational_fault') and self.operational_fault is None:
            self.operational_fault=snapshot['operational_fault'];self.recovery=RecoveryState(True,'await_authority');self.remember(r,self.operational_fault)

    def prepare_handoff(self,r): self.import_boundary(r,r._meter_handoff_snapshot())
    def initialize(self,r,inp,now):
        if self.started: return
        self.started=True
        for record in (*r._retired_dispatch_records.values(),*r._dispatch_records.values()):
            cap=record.get('target',(0,0,0))[0];at=number(record.get('now'))
            if cap>0 and at is not None:
                ttl=number(record.get('ttl')) or max(60.,r.settings.normal_ttl_seconds,r.settings.soft_ttl_seconds)
                self.possible_positive[('adopt',at,cap)]=(cap,at+ttl)
        boundary=r._meter_handoff_snapshot()
        if boundary.get('accepted') is not None and boundary['accepted']['cap']>0:
            self.import_boundary(r,boundary)
            self.upper=max(16.,self.lease.cap)
        else:
            self.upper=16.;self.cold_unknown_pending=True;self.cold_upper_until=now+60.
            candidate=int(floor(min(r.settings.max_charge_amps,r.settings.main_fuse_amps-r.settings.buffer_amps-number(inp.grid_l1))))
            if candidate>=7: self.start_since=now;self.start_cap=candidate
        self.import_boundary(r,boundary)
        if self.recovery.required and self.recovery.old_authority_until is None: self.recovery.old_authority_until=self.authority_deadline(now)

    def observe_input(self,r,inp,ev_valid):
        r._last_input_now_ts=number(inp.now_ts);r._last_input_wall_now_ts=number(inp.wall_now_ts)
        r._last_input_grid_ts=number(inp.grid_report_ts);r._last_input_charger_ts=number(inp.charger_report_ts)
        r._last_input_control_source_id=r._trial_identity(inp.control_source_id);r._last_input_status=(inp.status or '').strip().lower()
        self.last_ev_error=inp.meter_ev_health.reason if inp.meter_ev_health and not inp.meter_ev_health.valid else None

    def observe_ledger(self,r,inp,good):
        s=r.settings;c=r._ensure_automatic_cycle();now,wall,gt,raw=map(number,(inp.now_ts,inp.wall_now_ts,inp.grid_report_ts,inp.grid_l1))
        if now is None or r._overload_last_eval_ts is not None and now<r._overload_last_eval_ts: return self.causes.CLOCK_INVALID
        if r._overload_last_eval_ts is not None and r._overload_exposure_active:
            delta=now-r._overload_last_eval_ts;r._overload_used_seconds+=delta;c['used_seconds']+=delta
        r._overload_last_eval_ts=now
        if good: r._overload_exposure_active=raw>s.main_fuse_amps
        if not good or not 0<=wall-gt<=min(15.,s.max_sensor_age_seconds): r._break_automatic_continuity();return None
        source=r._trial_identity(inp.control_source_id)
        if c['source_id'] is not None and r._trial_identity(c['source_id'])!=source:
            r._break_automatic_continuity();return self.causes.CONTROL_SOURCE_CHANGED
        previous=r._automatic_last_gt
        if r._automatic_observed_source!=source: r._break_automatic_continuity();previous=None
        if previous is not None and gt<previous: return self.causes.GRID_INVALID
        if previous is not None and gt==previous: return None
        if previous is not None and gt-previous>min(15.,s.max_sensor_age_seconds): r._break_automatic_continuity()
        r._automatic_last_gt,r._automatic_last_now,r._automatic_observed_source=gt,now,source
        if c['source_id'] is None: c['source_id']=source
        if raw<23.:
            r._automatic_qualification_gt=r._automatic_qualification_since=None
            if r._automatic_cooling_gt is None: r._automatic_cooling_gt,r._automatic_cooling_since=gt,now
            if gt-r._automatic_cooling_gt>=1800. and now-r._automatic_cooling_since>=1800.:
                if c['used_seconds']>0 or c['state']!='ready': c.update(cycle_id=c['cycle_id']+1,state='ready',used_seconds=0.,failure_reason=None)
                r._automatic_cooling_gt,r._automatic_cooling_since=gt,now
            elif c['state']=='qualifying': c['state']='ready'
            return None
        r._automatic_cooling_gt=r._automatic_cooling_since=None
        if c['used_seconds']>=s.overload_budget_seconds:
            c['state']='exhausted';r._automatic_qualification_gt=r._automatic_qualification_since=None;return None
        if raw<=s.main_fuse_amps:
            r._automatic_qualification_gt=r._automatic_qualification_since=None
            if c['state']=='qualifying': c['state']='ready'
            return None
        if self.recovery.required or not s.overload_enabled or c['state']=='active': return None
        if not self.lease.live(now) or (inp.status or '').strip().lower()!='charging': return None
        if r._automatic_qualification_gt is None: r._automatic_qualification_gt,r._automatic_qualification_since=gt,now;c['state']='qualifying'
        if gt-r._automatic_qualification_gt>=30. and now-r._automatic_qualification_since>=30.: c['state']='active'
        return None

    def zero_current_evidence(self,r,inp):
        if self.critical_error(r,inp) is not None: return None
        e=r._controlled_l1_projection(inp)
        return e.current if e is not None and e.current<=.5 and r._controlled_l1_after(e,self.zero_l1_floor) else None

    def observe_positive_authority(self,r,inp,now,live):
        self.possible_positive={key:value for key,value in self.possible_positive.items() if value[1]>now}
        if self.cold_unknown_pending and now>=self.cold_upper_until: self.cold_unknown_pending=False
        off=self.zero_current_evidence(r,inp) is not None
        if off:
            self.possible_positive.clear();self.cold_unknown_pending=False
            if self.recovery.required: self.recovery.old_authority_until=now
        active=any(now<expiry for _,expiry in self.possible_positive.values()) or self.cold_unknown_pending
        if not live and not active:
            self.upper=0.;self.episode=None
        if self.recovery.required and self.recovery.old_authority_until is None: self.recovery.old_authority_until=self.authority_deadline(now)
        return active

    def normal_certificate(self,r,inp,cap):
        raw=number(inp.grid_l1)
        return bool(cap>0 and self.ev_estimate(r,inp) and 0<self.l1.current<=cap+.5
            and r._controlled_l1_after(self.l1,self.accepted_l1_floor)
            and raw-self.l1.current+cap<=r.settings.main_fuse_amps-r.settings.buffer_amps)

    def observe_recovery(self,r,inp,now,uncertain):
        if not self.recovery.required: return
        raw=number(inp.grid_l1);normal=r.settings.main_fuse_amps-r.settings.buffer_amps
        cap=self.lease.cap if self.lease.live(now) else 0;candidate=int(floor(min(r.settings.max_charge_amps,normal-raw)))
        accepted_start=bool(self.recovery.normal_dispatch is not None and cap>0
            and cap==self.recovery.normal_cap and self.lease.origin>=self.recovery.normal_dispatch)
        headroom=(raw<=normal and (candidate>=7 or accepted_start)) or (raw<=normal and self.normal_certificate(r,inp,cap))
        if not headroom:
            self.start_since=self.start_cap=None;self.recovery.normal_dispatch=self.recovery.normal_cap=self.recovery.normal_floor=None
            self.recovery.stage='await_normal_headroom';return
        if self.start_since is None: self.start_since=now;self.start_cap=candidate if cap==0 else cap
        ended=self.recovery.old_authority_until is not None and now>=self.recovery.old_authority_until
        if not ended: self.recovery.stage='await_authority';return
        if now-self.start_since<r.settings.restart_stable_seconds: self.recovery.stage='await_normal_stability';return
        if self.recovery.normal_dispatch is None: self.recovery.stage='await_normal_acceptance';return
        self.recovery.stage='await_normal_response';e=self.l1
        if (cap>0 and e is not None and 0<e.current<=cap+.5 and r._controlled_l1_after(e,self.recovery.normal_floor)
                and self.ev_estimate(r,inp) and raw-e.current+cap<=normal):
            for event in self.history:
                if not event['resolved']: event['resolved']=True
            self.operational_fault=None;self.recovery=RecoveryState(False,'complete')
            c=r._ensure_automatic_cycle();c['state']='exhausted' if c['used_seconds']>=r.settings.overload_budget_seconds else 'ready'
            self.recovered_cycle_id=c['cycle_id']
            if self.episode is not None and self.episode.failure: self.episode=None

    def observe_inflight(self,r,inp,decision):
        if self.critical_error(r,inp) is not None: return
        raw,now,wall,gt=map(number,(inp.grid_l1,inp.now_ts,inp.wall_now_ts,inp.grid_report_ts))
        self.upper=max(self.upper,decision.current_p1)
        if 28.<raw<=r.settings.overload_transition_limit_amps and (self.episode is None or not self.episode.unresolved) and not (self.minimum_verified and decision.current_p1<=7):
            origin=now-(wall-gt);self.episode=ElectricalEpisode(self.next_episode_id,inp.control_source_id,origin,origin+r.settings.overload_response_seconds,gt,self.upper)
            self.next_episode_id+=1

    def quiet_normal_bound(self,r,inp):
        """Independent whole-RAW proof, never stale EV subtraction or restart."""
        if not r._meter_settings_valid(r.settings):
            return None
        now,wall,gt,raw=map(number,(inp.now_ts,inp.wall_now_ts,inp.grid_report_ts,inp.grid_l1))
        if (None in (now,wall,gt,raw) or self.critical_error(r,inp) is not None
                or not 0<=wall-gt<=min(15.,r.settings.max_sensor_age_seconds)
                or not self.lease.live(now) or self.lease.cap<=0
                or self.recovery.required or self.operational_fault or self.episode is not None
                or self.cold_unknown_pending or not r.settings.active_control or r.settings.dry_run
                or not r.settings.enable_dynamic_limit
                or (inp.status or '').strip().lower() not in {'charging','start_charging','ready_to_charge','awaiting_start',
                    'awaiting_load_balancing','connected','buffering'}
                or self.lease.source_id!=inp.control_source_id):
            return None
        upper=max(self.upper,self.lease.cap,*(cap for cap,expiry in self.possible_positive.values() if expiry>now))
        budget=min(23.,r.settings.main_fuse_amps-r.settings.buffer_amps)
        if raw+upper>budget:
            return None
        return upper,budget

    def note_dispatch(self,r,d,record):
        if d.command=='set_limit' and d.current_p1>0:
            self.zero_l1_floor=None;self.lease.zero_dispatch=None
            self.possible_positive[d.plan_id]=(d.current_p1,record['now']+d.ttl_seconds);self.upper=max(self.upper,d.current_p1)
            if d.current_p1>7: self.minimum_verified=False
            if self.recovery.required and not d.meter_overload_authority: self.recovery.candidate_plan=d.plan_id
        elif d.command=='set_limit' and d.current_p1==0 and self.zero_l1_floor is None:
            self.zero_l1_floor=deepcopy(record.get('controlled_l1_floor'));self.lease.zero_dispatch=record['now']

    def acknowledge(self,r,d,now,dispatch):
        origin=dispatch['now'];old=self.lease.cap
        r.last_action_ts=now;r.last_set_limit_ts=origin;r.last_command='set_limit';r.last_target=(d.current_p1,0,0,d.ttl_seconds)
        self.lease.cap=d.current_p1;self.lease.origin=origin;self.lease.expires=origin+d.ttl_seconds
        if d.current_p1<=0: return
        self.lease.source_id=dispatch.get('source_id');r._accepted_control_source_id=self.lease.source_id
        startup_proved=bool(self.start_since is not None and origin-self.start_since>=r.settings.restart_stable_seconds
            and self.recovery.old_authority_until is not None and origin>=self.recovery.old_authority_until)
        new_floor=dispatch.get('controlled_l1_floor')
        normal_floor=not d.meter_overload_authority and new_floor is not None
        operating_scope_missing=bool(normal_floor and (self.accepted_l1_floor is None
            or new_floor['key']!=self.accepted_l1_floor['key']))
        if old!=d.current_p1:
            r._last_limit_change_ts=origin;self.accepted_l1_floor=deepcopy(new_floor)
            self.lease.normal_origin=origin
        elif operating_scope_missing and (not self.recovery.required or startup_proved):
            # Phase context may settle after the first positive command. Only
            # this accepted SDK boundary can establish its missing/new scope;
            # an ordinary renewal in the same scope must keep the old floor.
            self.accepted_l1_floor=deepcopy(new_floor);self.lease.normal_origin=origin
        if (not d.meter_overload_authority and number(d.house_load_l1) is not None
                and d.house_load_l1+d.current_p1<=r.settings.main_fuse_amps-r.settings.buffer_amps):
            self.lease.normal_house=d.house_load_l1
        normal_decrease=bool(old>d.current_p1>0 and not d.meter_overload_authority
            and d.mode.removeprefix('dry_run_') in {'meter_normal','meter_normal_decrease'})
        if d.meter_ordinary_decrease or normal_decrease:
            # Only a meter sample after this actual SDK boundary can observe
            # the lower offer. New EV telemetry alone cannot reuse its old RAW.
            self.ordinary_decrease_grid=max(number(d.meter_basis_grid_ts) or 0.,number(dispatch.get('wall')) or 0.)
            self.ordinary_decrease_cap=d.current_p1
            self.ordinary_decrease_raw=number(d.grid_l1)
        elif old!=d.current_p1: self.ordinary_decrease_grid=self.ordinary_decrease_cap=self.ordinary_decrease_raw=None
        if self.episode is not None and self.episode.unresolved and d.current_p1==7 and self.minimum_dispatch is None:
            self.minimum_dispatch=origin;self.minimum_l1_floor=deepcopy(dispatch.get('controlled_l1_floor'))
        recovery_scope_missing=bool(normal_floor and (self.recovery.normal_floor is None
            or new_floor['key']!=self.recovery.normal_floor['key']))
        if (self.recovery.required and startup_proved and not d.meter_overload_authority
                and (self.recovery.normal_dispatch is None or old!=d.current_p1 or recovery_scope_missing)):
            self.recovery.normal_dispatch=origin;self.recovery.normal_cap=d.current_p1
            self.recovery.normal_floor=deepcopy(dispatch.get('controlled_l1_floor'));self.recovery.stage='await_normal_response'

    @property
    def response_ts(self): return self.episode.response_ts if self.episode else None
    @response_ts.setter
    def response_ts(self,value):
        if self.episode is not None: self.episode.response_ts=value
        if self.episode is not None and value is not None: self.episode.phase=ControlState.GRID_WAIT

    @property
    def response_at(self): return self.episode.response_at if self.episode else None
    @response_at.setter
    def response_at(self,value):
        if self.episode is not None: self.episode.response_at=value

    @property
    def confirmation_deadline(self): return self.episode.grid_deadline if self.episode else None
    @confirmation_deadline.setter
    def confirmation_deadline(self,value):
        if self.episode is not None: self.episode.grid_deadline=value

    @property
    def minimum_dispatch(self): return self.episode.minimum_dispatch if self.episode else None
    @minimum_dispatch.setter
    def minimum_dispatch(self,value):
        if self.episode is not None: self.episode.minimum_dispatch=value

    @property
    def minimum_l1_floor(self): return self.episode.minimum_floor if self.episode else None
    @minimum_l1_floor.setter
    def minimum_l1_floor(self,value):
        if self.episode is not None: self.episode.minimum_floor=value

    @property
    def confirmation_grid_floor(self): return self.episode.grid_floor if self.episode else None
    @confirmation_grid_floor.setter
    def confirmation_grid_floor(self,value):
        if self.episode is not None: self.episode.grid_floor=value

    @property
    def confirmation_value(self): return self.episode.current_value if self.episode else None
    @confirmation_value.setter
    def confirmation_value(self,value):
        if self.episode is not None: self.episode.current_value=value

    @property
    def confirmation_ceiling(self): return self.episode.ceiling if self.episode else None
    @confirmation_ceiling.setter
    def confirmation_ceiling(self,value):
        if self.episode is not None: self.episode.ceiling=value

    @property
    def upper(self): return self.lease.upper
    @upper.setter
    def upper(self,value): self.lease.upper=value

    @property
    def minimum_verified(self): return self.lease.minimum_verified
    @minimum_verified.setter
    def minimum_verified(self,value): self.lease.minimum_verified=value

    @property
    def accepted_l1_floor(self): return self.lease.accepted_floor
    @accepted_l1_floor.setter
    def accepted_l1_floor(self,value): self.lease.accepted_floor=value

    @property
    def zero_l1_floor(self): return self.lease.zero_floor
    @zero_l1_floor.setter
    def zero_l1_floor(self,value): self.lease.zero_floor=value

    @property
    def cold_upper_until(self): return self.lease.cold_until
    @cold_upper_until.setter
    def cold_upper_until(self,value): self.lease.cold_until=value

    @property
    def cold_unknown_pending(self): return self.lease.cold_pending
    @cold_unknown_pending.setter
    def cold_unknown_pending(self,value): self.lease.cold_pending=value

    @property
    def possible_positive(self): return self.lease.possible_positive
    @possible_positive.setter
    def possible_positive(self,value): self.lease.possible_positive=value

    @property
    def start_since(self): return self.recovery.stable_since
    @start_since.setter
    def start_since(self,value): self.recovery.stable_since=value

    @property
    def start_cap(self): return self.recovery.stable_cap
    @start_cap.setter
    def start_cap(self,value): self.recovery.stable_cap=value

    @property
    def recovering(self): return self.recovery.required
    @recovering.setter
    def recovering(self,value): self.recovery.required=value


    def decision(self, r, inp, mode, cap, *, command=None, cause=None,
                 reason='', house=None, overload=False, response_deadline=None,
                 confirmation_deadline=None, no_write_deadline=None, basis='meter_envelope_feedback',
                 ordinary_decrease=False, normal_retention=False):
        s, now = r.settings, number(inp.now_ts)
        self.state=({'meter_peak_response':ControlState.REDUCING,'meter_grid_wait':ControlState.GRID_WAIT,
            'meter_overload':ControlState.MINIMUM_HOLD,'meter_fault':ControlState.STOP_RECOVERY,
            'meter_recovery_wait':ControlState.STOP_RECOVERY}.get(mode,
            ControlState.STOP_RECOVERY if self.recovery.required else ControlState.NORMAL))
        if now is None:
            now = number(r._last_input_now_ts) or 0.
        raw, ev = number(inp.grid_l1), number(inp.charger_current)
        r._plan_sequence += 1
        should = bool(s.active_control is True and s.dry_run is False and command)
        if command == 'set_limit':
            should = should and (cause==self.causes.CONFIGURATION or self.limit_due(r,now,cap))
            if mode=='meter_peak_response' and self.minimum_dispatch is None:
                should=bool(s.active_control is True and s.dry_run is False)
            if cap > 0 and r.last_target and cap > r.last_target[0]:
                should = should and (r._last_limit_change_ts is None or
                    now-r._last_limit_change_ts >= s.min_seconds_between_actions)
        d = self.decision_type(mode=('dry_run_'+mode if s.dry_run else mode),
            reason=reason, command=command,
            service='easee.set_circuit_dynamic_limit' if command else None,
            current_p1=cap, current_p2=0, current_p3=0, ttl_seconds=60,
            available_l1=cap, house_load_l1=(max(0.,raw-(ev or 0.)) if house is None and raw is not None else house or 0.),
            grid_l1=raw, charger_current=ev or 0., paused_by_us=r.paused_by_us,
            should_call_service=bool(should), dry_run=s.dry_run is not False,
            overload_used_seconds=number(r.effective_overload_used_seconds) or 0.,
            overload_remaining_seconds=max(0.,(number(r.effective_overload_budget_seconds) or 0.)-(number(r.effective_overload_used_seconds) or 0.)),
            overload_lifetime_used_seconds=r._overload_used_seconds,
            overload_historical_failure_reason=r._overload_failure_reason,
            overload_failure_latched=(r._overload_failure_reason is not None
                if cause==self.causes.CONFIGURATION else self.operational_fault is not None or r.effective_overload_used_seconds>=s.overload_budget_seconds),
            startup_charge_amps=7 if cause==self.causes.CONFIGURATION else r.startup_current,
            plan_id=r._plan_sequence, committed_current_p1=r.last_target[0] if r.last_target else None,
            command_urgency=('zero' if cause else 'protective_reduction' if command and r.last_target and cap<r.last_target[0] else 'normal'),
            house_load_basis=basis,
            nominal_response_deadline_ts=response_deadline,
            nominal_response_remaining_seconds=max(0.,response_deadline-now) if response_deadline is not None else None,
            grid_confirmation_deadline_ts=confirmation_deadline,
            grid_confirmation_remaining_seconds=max(0.,confirmation_deadline-now) if confirmation_deadline is not None else None,
            normal_continuity_deadline_ts=no_write_deadline,
            meter_envelope=True, meter_zero_cause=cause.value if cause else None,
            meter_state=mode, meter_control_state=self.state.value,
            meter_episode_id=self.episode.episode_id if self.episode else None,
            meter_operational_fault=self.operational_fault,meter_recovery_stage=self.recovery.stage, meter_normal_retention=bool(cap>0 and not cause and
                (normal_retention or raw is not None and raw<=s.main_fuse_amps)),
            meter_overload_authority=overload, meter_ordinary_decrease=ordinary_decrease,
            meter_basis_grid_ts=number(inp.grid_report_ts),
            controlled_l1_current=self.l1.current if self.l1 is not None else None,
            controlled_l1_used=basis=='eligible_mapped_l1_then_meter' or mode in {'meter_grid_wait'})
        r._last_decision = d
        return d


    def critical_error(self, r, inp):
        C, s = self.causes, r.settings
        explicit = any(value is not None for value in
            (inp.meter_grid_health,inp.meter_actuator_health,inp.meter_ev_health))
        if explicit:
            if inp.meter_grid_health is None or inp.meter_grid_health.valid is not True:
                return C.GRID_INVALID
            if inp.meter_actuator_health is None or inp.meter_actuator_health.valid is not True:
                return C.ACTUATOR_INVALID
            if inp.meter_ev_health is None:
                return C.ACTUATOR_INVALID
        elif not inp.data_valid or inp.data_error:
            return C.GRID_INVALID
        raw, now, wall, gt = map(number,(inp.grid_l1,inp.now_ts,inp.wall_now_ts,inp.grid_report_ts))
        if now is None:
            return C.CLOCK_INVALID
        if raw is None or raw<0 or None in (wall,gt) or not 0<=wall-gt<=s.max_sensor_age_seconds:
            return C.GRID_INVALID
        source = r._trial_identity(inp.control_source_id)
        if source is None:
            return C.CONTROL_SOURCE_CHANGED
        if r.last_target and r.last_target[0]>0 and source != r._trial_identity(r._accepted_control_source_id):
            return C.CONTROL_SOURCE_CHANGED
        status=(inp.status or '').strip().lower()
        allowed={'charging','start_charging','ready_to_charge','awaiting_start','awaiting_load_balancing',
            'connected','buffering','paused','completed','disconnected','awaiting_smart_start',
            'awaiting_scheduled_start','awaiting_authorization','de_authorizing','authenticating',
            'paused_due_to_equalizer','searching_for_master','waiting_for_schedule','stop_charging'}
        if status not in allowed:
            return C.ACTUATOR_INVALID
        return None


    def ev_estimate(self, r, inp, ev_valid=None):
        """Only independently mapped L1 may be subtracted from RAW L1."""
        raw,gt,wall=map(number,(inp.grid_l1,inp.grid_report_ts,inp.wall_now_ts))
        e=self.l1
        ev,et=(e.current,e.received_at_ts) if e is not None else (None,None)
        return bool(e is not None and 1<=ev<=16.5
            and 0<=wall-et<=r.settings.max_sensor_age_seconds
            and gt>et and abs(gt-et)<=r.settings.measurement_settle_seconds
            and ev<=raw+1)


    def retention(self,r,inp,proposed=None):
        now=number(inp.now_ts)
        if now is None: return None
        if not r.last_target or r.last_target[0]<=0: return None
        cap=r.last_target[0]
        shadow=deepcopy(r)
        p=shadow._get_meter_envelope_policy()
        d=p.evaluate(shadow,inp)
        e=p.episode
        reducing=bool(e is not None and e.unresolved and e.phase==ControlState.REDUCING
            and e.deadline is not None and now<e.deadline)
        grid_wait=bool(e is not None and e.phase==ControlState.GRID_WAIT and cap<=7
            and e.grid_deadline is not None and now<e.grid_deadline)
        minimum_hold=bool(e is not None and e.phase==ControlState.MINIMUM_HOLD and cap<=7
            and p.minimum_verified and number(inp.grid_l1)<=r.settings.main_fuse_amps+r.settings.overload_margin_amps)
        episode_hold=bool((reducing or grid_wait or minimum_hold) and e.failure is None and not e.interrupted
            and p.critical_error(shadow,inp) is None and number(inp.grid_l1)<=r.settings.overload_transition_limit_amps
            and p.operational_fault is None and not p.recovery.required)
        if d.meter_zero_cause or (not episode_hold and (not d.meter_normal_retention or d.meter_overload_authority)):
            return None
        template=proposed or d
        return replace(template,mode='meter_normal_hold',reason='Retaining only the actual live meter-envelope lease during local I/O; no command or renewal.',
            command=None,service=None,should_call_service=False,current_p1=cap,current_p2=0,current_p3=0,
            meter_envelope=True,meter_zero_cause=None,meter_state='meter_normal_hold',
            meter_normal_retention=True,meter_overload_authority=False,
            meter_episode_retention=episode_hold,
            normal_continuity_deadline_ts=r.last_set_limit_ts+r.last_target[3],
            nominal_response_deadline_ts=e.deadline if episode_hold and reducing else None,
            grid_confirmation_deadline_ts=e.grid_deadline if episode_hold and grid_wait else None)


    def evaluate(self, r, inp):
        s,C=r.settings,self.causes
        if not r._meter_settings_valid(s):
            return self.supervisor.zero(self,r,inp,C.CONFIGURATION)
        problem=self.critical_error(r,inp)
        now,raw,wall,gt=map(number,(inp.now_ts,inp.grid_l1,inp.wall_now_ts,inp.grid_report_ts))
        ledger_error=self.observe_ledger(r,inp,problem is None)
        if ledger_error: problem=problem or ledger_error
        if problem: return self.supervisor.zero(self,r,inp,problem)
        ev_valid=bool((inp.meter_ev_health is None or inp.meter_ev_health.valid is True)
            and inp.charger_current_trusted is True and number(inp.charger_current) is not None
            and number(inp.charger_current)>=0)
        self.observe_input(r,inp,ev_valid)
        self.l1=r._controlled_l1_projection(inp)
        ev=self.l1.current if self.l1 is not None else None
        status=r._last_input_status
        self.initialize(r,inp,now)
        live=self.lease.live(now)
        if self.lease.cap>0 and not live:
            return self.supervisor.zero(self,r,inp,C.LEASE_EXPIRED)
        uncertain_positive=self.observe_positive_authority(r,inp,now,live)
        self.observe_recovery(r,inp,now,uncertain_positive)
        cap=self.lease.cap if live else 0
        self.upper=max(self.upper,cap)
        if raw>min(38.,s.overload_transition_limit_amps):
            return self.supervisor.zero(self,r,inp,C.HARD_LIMIT)
        manual={'paused','awaiting_smart_start','awaiting_scheduled_start','awaiting_authorization',
            'de_authorizing','authenticating','paused_due_to_equalizer','waiting_for_schedule','stop_charging'}
        if status in manual:
            return self.decision(r,inp,'meter_wait',cap,
                reason='Charger/user pause, authorization or schedule owns the session; no positive write or resume.')
        if inp.meter_product_data_quiet:
            bound=self.quiet_normal_bound(r,inp)
            if bound is None:
                return self.supervisor.zero(self,r,inp,C.ACTUATOR_INVALID,
                    detail='Product data is quiet and independent whole-meter normal retention is unavailable.')
            if (cap==self.ordinary_decrease_cap and self.ordinary_decrease_grid is not None
                    and gt<=self.ordinary_decrease_grid):
                return self.decision(r,inp,'meter_normal_hold',cap,normal_retention=True,
                    no_write_deadline=self.lease.expires,
                    reason='The old pre-decrease meter packet retains only its original finite lower lease; quiet data grants no renewal.')
            return replace(self.decision(r,inp,'meter_quiet_normal',cap,command='set_limit',house=raw,
                basis='whole_raw_quiet_normal_retention',normal_retention=True,
                reason='Product data is quiet; fresh whole-meter load plus all possible current fits the normal budget. Renewing only the existing normal cap.'),
                meter_quiet_normal_retention=True,meter_quiet_upper_amps=bound[0],meter_quiet_normal_budget_amps=bound[1])
        if self.recovering and cap==0:
            candidate=int(floor(min(s.max_charge_amps,s.main_fuse_amps-s.buffer_amps-raw)))
            if raw>s.main_fuse_amps or candidate<r.startup_current:
                self.start_since=self.start_cap=None
            else:
                if self.start_since is None:
                    self.start_since=now;self.start_cap=candidate
                self.start_cap=min(self.start_cap,candidate)
                if uncertain_positive or now-self.start_since<s.restart_stable_seconds:
                    return self.decision(r,inp,'meter_recovery_wait',0,
                        reason='Waiting for original possible-positive expiry or genuine off evidence and the existing continuous normal restart interval; zero renewals add no authority.')
        if status in {'completed','disconnected'}:
            candidate=int(floor(min(s.max_charge_amps,s.main_fuse_amps-s.buffer_amps-raw)))
            if (s.disconnected_readiness and candidate>=r.startup_current
                    and raw+max(self.upper,candidate)<=s.main_fuse_amps-s.buffer_amps):
                return self.decision(r,inp,'meter_ready',candidate,command='set_limit',house=raw,
                    reason='Passive whole-RAW allocation for a completed/disconnected vehicle; no start/resume.')
            return self.decision(r,inp,'meter_wait',cap,
                reason='Passive readiness disabled or lacks whole-RAW headroom; no session start.')
        if self.episode is not None and raw<=s.main_fuse_amps and self.normal_certificate(r,inp,cap):
            self.episode=None
        peak=self.episode if self.episode is not None and self.episode.unresolved else None
        retained_minimum=bool(cap==r.startup_current and self.l1 is not None
            and r._controlled_l1_after(self.l1,self.accepted_l1_floor))
        if retained_minimum:
            if 1<=ev<=r.startup_current+.5:
                self.minimum_verified=True
            elif ev>r.startup_current+.5:
                self.minimum_verified=False
        if peak is not None and (peak.interrupted or peak.failure):
            if raw>s.main_fuse_amps: return self.supervisor.zero(self,r,inp,C.PEAK_RESPONSE_TIMEOUT)
            peak=None
        if (raw>28. and live and not self.recovery.required and peak is None and not (cap==r.startup_current and self.minimum_verified)
                and (self.peak_complete_grid is None or gt>self.peak_complete_grid)):
            ordinary=bool(self.ev_estimate(r,inp) and r._controlled_l1_after(self.l1,self.accepted_l1_floor)
                and raw-self.l1.current+7<=s.main_fuse_amps-s.buffer_amps)
            if (not s.overload_enabled and not ordinary
                    or r._ensure_automatic_cycle()['used_seconds']>=s.overload_budget_seconds):
                return self.supervisor.zero(self,r,inp,C.OVERLOAD_NOT_ADMITTED if not s.overload_enabled else C.OVERLOAD_BUDGET)
            origin=now-(wall-gt)
            peak=self.episode=ElectricalEpisode(self.next_episode_id,inp.control_source_id,origin,
                origin+min(10.,s.overload_response_seconds),gt,int(max(cap,self.upper)))
            self.next_episode_id+=1
            self.response_ts=self.response_at=self.confirmation_deadline=None
            self.minimum_dispatch=None;self.minimum_l1_floor=None
            self.confirmation_value=self.confirmation_grid_floor=self.confirmation_ceiling=None
            peak.overload_authority=s.overload_enabled
            peak.ceiling=s.main_fuse_amps+s.overload_margin_amps if s.overload_enabled else s.main_fuse_amps
        minimum=r.startup_current
        if (peak is not None and cap==minimum and raw<=s.main_fuse_amps
                and gt>peak.grid_source_ts):
            # A genuinely newer measured normal total has continuation
            # authority. It grants neither a timely EV response nor a new
            # response window: an unresolved return above28 still uses the
            # original, possibly already expired, electrical deadline.
            if (self.ev_estimate(r,inp) and ev<=minimum+.5
                    and r._controlled_l1_after(self.l1,self.accepted_l1_floor)):
                self.upper=max(minimum,ev)
                self.minimum_verified=True
                self.episode=None
                self.response_ts=self.response_at=self.confirmation_deadline=None
                peak=None
            else:
                return self.decision(r,inp,'meter_normal',minimum,command='set_limit',
                    reason='Fresh normal meter feedback retains the actual minimum; unresolved peak evidence and its original deadline remain intact.')
        if peak is not None:
            if not s.overload_enabled and peak.overload_authority and raw>s.main_fuse_amps:
                ordinary=bool(self.ev_estimate(r,inp) and r._controlled_l1_after(self.l1,self.accepted_l1_floor)
                    and raw-self.l1.current+7<=s.main_fuse_amps-s.buffer_amps)
                if not ordinary: return self.supervisor.zero(self,r,inp,C.OVERLOAD_NOT_ADMITTED)
                peak.overload_authority=False;peak.ceiling=s.main_fuse_amps
            context=inp.controlled_l1
            if (self.minimum_l1_floor is not None and context is not None
                    and (not r._controlled_l1_context(inp)
                        or r._controlled_l1_key(context)!=self.minimum_l1_floor['key'])):
                # Mapping changes withdraw the old response; they never buy a
                # second10s or a replacement30s confirmation interval.
                peak.phase=ControlState.REDUCING
                self.response_ts=self.response_at=None
            genuine=bool(self.l1 is not None and 1<=ev<=minimum+.5 and cap==minimum
                and self.minimum_dispatch is not None
                and r._controlled_l1_after(self.l1,self.minimum_l1_floor))
            et=self.l1.received_at_ts if self.l1 is not None else None
            at=now-(wall-et) if genuine else None
            if genuine and self.response_ts is None and self.minimum_dispatch<at<peak.deadline:
                self.response_ts=et;self.response_at=at
                self.confirmation_deadline=min(at+s.grid_confirmation_seconds,
                    r.last_set_limit_ts+r.last_target[3]) if s.grid_confirmation_seconds else peak.deadline
                self.confirmation_grid_floor=self.response_ts
                self.confirmation_value=ev
                self.confirmation_ceiling=peak.ceiling if peak.ceiling is not None else s.main_fuse_amps+s.overload_margin_amps
            if self.response_ts is None:
                if peak.deadline is None or now>=peak.deadline:
                    self.episode.failure='response_timeout'
                    return self.supervisor.zero(self,r,inp,C.PEAK_RESPONSE_TIMEOUT)
                return self.decision(r,inp,'meter_peak_response',minimum,command='set_limit',
                    response_deadline=peak.deadline,overload=peak.overload_authority and raw>s.main_fuse_amps,
                    reason='Measured RAW peak requests the configured minimum inside the original electrical response deadline.')
            if now>=self.confirmation_deadline:
                return self.supervisor.zero(self,r,inp,C.GRID_CONFIRMATION_TIMEOUT)
            ceiling=self.confirmation_ceiling
            if gt>self.response_ts and raw>ceiling:
                return self.supervisor.zero(self,r,inp,C.GRID_CEILING)
            if (genuine and abs(ev-self.confirmation_value)>.1
                    and et>self.confirmation_grid_floor):
                self.confirmation_grid_floor=et
                self.confirmation_value=ev
            if gt>self.confirmation_grid_floor and genuine:
                if ev>raw+1 or raw-ev+minimum>ceiling:
                    return self.supervisor.zero(self,r,inp,C.GRID_CEILING)
                peak.phase=ControlState.MINIMUM_HOLD
                self.peak_complete_grid=gt
                self.upper=max(minimum,ev or 0.)
                self.minimum_verified=True
                self.response_ts=self.response_at=self.confirmation_deadline=None
            else:
                return self.decision(r,inp,'meter_grid_wait',minimum,
                    confirmation_deadline=self.confirmation_deadline,overload=peak.overload_authority and raw>s.main_fuse_amps,
                    reason='Timely genuine minimum response; waiting for the later meter inside the separate original confirmation phase.')
        cycle=r._ensure_automatic_cycle()
        if self.ordinary_decrease_grid is not None and gt>self.ordinary_decrease_grid:
            self.ordinary_decrease_grid=self.ordinary_decrease_cap=self.ordinary_decrease_raw=None
        if (live and peak is None and cap==self.ordinary_decrease_cap and self.ordinary_decrease_grid is not None
                and gt<=self.ordinary_decrease_grid
                and self.ordinary_decrease_raw is not None and raw<=self.ordinary_decrease_raw
                and (raw<=s.main_fuse_amps or cycle['used_seconds']<s.overload_budget_seconds)):
            return self.decision(r,inp,'meter_normal_hold',cap,normal_retention=True,
                no_write_deadline=r.last_set_limit_ts+r.last_target[3],
                reason='The same pre-decrease meter packet cannot claim another response or stop; retain only its actual finite lower lease.')
        if s.main_fuse_amps<raw<=28. and live:
            if (status=='charging' and self.ev_estimate(r,inp)
                    and r._controlled_l1_after(self.l1,self.accepted_l1_floor)):
                house=max(0.,raw-ev)
                lower=int(floor(min(s.max_charge_amps,s.main_fuse_amps-s.buffer_amps-house)))
                if minimum<=lower<cap and house+lower<=s.main_fuse_amps-s.buffer_amps:
                    return self.decision(r,inp,'meter_normal_decrease',lower,command='set_limit',
                        house=house,basis='eligible_mapped_l1_then_meter',ordinary_decrease=True,
                        reason='Genuine native current followed by the meter permits a strictly lower normal offer; no overload authority is used.')
        if raw>s.main_fuse_amps:
            if cycle['used_seconds']>=s.overload_budget_seconds:
                return self.supervisor.zero(self,r,inp,C.OVERLOAD_BUDGET)
            if not s.overload_enabled or self.recovery.required or self.operational_fault:
                return self.supervisor.zero(self,r,inp,C.OVERLOAD_NOT_ADMITTED)
            if raw>s.main_fuse_amps+s.overload_margin_amps:
                return self.supervisor.zero(self,r,inp,C.GRID_CEILING)
            if not live:
                return self.decision(r,inp,'meter_wait',0,reason='No new session starts from overload headroom.')
            return self.decision(r,inp,'meter_overload',minimum,command='set_limit',overload=True,
                reason='Measured overload uses only the configured minimum and the preserved cumulative qualification/active budget.')
        if not s.active_control or s.dry_run or not s.enable_dynamic_limit:
            return self.decision(r,inp,'meter_monitor',cap,reason='Automatic control is disabled.')
        allowed_start={'charging','start_charging','ready_to_charge','awaiting_start','awaiting_load_balancing','connected','buffering'}
        coherent=live and self.ev_estimate(r,inp) and status=='charging'
        basis_label='meter_envelope_feedback'
        if coherent:
            self.feedback_base_cap=self.feedback_source=None
            desired=int(floor(min(s.max_charge_amps,s.main_fuse_amps-s.buffer_amps-(raw-ev))))
            if r._controlled_l1_after(self.l1,self.accepted_l1_floor):
                self.upper=max(cap,ev)
                if cap==minimum and ev<=minimum+.5:
                    self.minimum_verified=True
            basis=max(0.,raw-ev)
            basis_label='eligible_mapped_l1_then_meter'
            if self.normal_certificate(r,inp,cap): self.lease.normal_house=basis
            if desired>cap and not r._controlled_l1_after(self.l1,self.accepted_l1_floor):
                desired=cap
        elif live:
            # A measured normal total may retain/lower the actual accepted
            # lease. This deliberately uses meter feedback, not RAW+whole proof.
            if raw<=s.main_fuse_amps-s.buffer_amps:
                self.feedback_base_cap=self.feedback_source=None
                desired=cap
            else:
                if self.feedback_base_cap is None or self.feedback_source!=inp.control_source_id:
                    self.feedback_base_cap=cap;self.feedback_source=inp.control_source_id
                desired=min(cap,max(minimum,self.feedback_base_cap-int(ceil(
                    raw-(s.main_fuse_amps-s.buffer_amps)))))
            raw_candidate=int(floor(min(s.max_charge_amps,s.main_fuse_amps-s.buffer_amps-raw)))
            if raw_candidate>cap and raw+max(self.upper,raw_candidate)<=s.main_fuse_amps-s.buffer_amps:
                desired=raw_candidate
                basis_label='whole_raw_independent_new_authority'
            basis=raw
        else:
            desired=int(floor(min(s.max_charge_amps,s.main_fuse_amps-s.buffer_amps-raw)))
            basis=raw
        desired=max(minimum,min(s.max_charge_amps,desired)) if live else desired
        if status not in allowed_start and status!='disconnected':
            # A circuit allocation never resumes a manual pause or schedule.
            return self.decision(r,inp,'meter_wait',cap,reason='Vehicle/charger owns pause, schedule or completion; no start/resume is issued.')
        if not live:
            if desired<minimum:
                self.start_since=self.start_cap=None
                return self.decision(r,inp,'meter_wait',0,reason='Waiting for complete normal whole-RAW startup headroom.')
            if raw+max(self.upper,desired)>s.main_fuse_amps-s.buffer_amps:
                return self.decision(r,inp,'meter_wait',0,
                    reason='Cold startup preserves all possible prior authority until its existing finite lease horizon ends.')
            if status=='disconnected' and s.disconnected_readiness:
                return self.decision(r,inp,'meter_ready',desired,command='set_limit',house=basis,
                    reason='Passive whole-RAW circuit readiness; no vehicle start/resume.')
            if self.start_since is None:
                self.start_since=now;self.start_cap=desired
            self.start_cap=min(self.start_cap,desired)
            if now-self.start_since<s.restart_stable_seconds:
                return self.decision(r,inp,'meter_wait',0,reason='Cold/critical restart waits for the existing continuous normal startup interval.')
            desired=min(desired,self.start_cap)
        return self.decision(r,inp,'meter_normal',desired,command='set_limit',house=basis,
            basis=basis_label,
            reason='Normal meter-envelope control; ordinary EV convergence is diagnostic and cannot request a stop.')
