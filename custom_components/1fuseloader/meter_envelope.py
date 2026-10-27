"""Explicit opt-in measured-meter electrical supervisor.

Normal EV estimate convergence has no stop authority here. The electrical
supervisor uses the validated meter and actual finite accepted command state.
This is an operational envelope, not a fuse-temperature model.
"""
from __future__ import annotations
from copy import deepcopy
from dataclasses import dataclass, replace
from math import ceil, floor, isfinite


def number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if isfinite(value) else None
    except (ValueError, TypeError, OverflowError):
        return None


@dataclass(frozen=True)
class ElectricalPeak:
    source_id: object
    origin: float
    deadline: float
    grid_source_ts: float
    previous_authority: int
    interrupted: bool = False


class ElectricalSupervisor:
    """The sole constructor of an actual zero request in this policy."""
    def __init__(self, cause_type):
        self.cause_type = cause_type

    def zero(self, policy, regulator, inp, cause, detail=None):
        if not isinstance(cause, self.cause_type):
            raise TypeError("A typed electrical stop cause is required")
        regulator._restart_required = True
        policy.recovering = True
        policy.start_since = None
        if cause in {self.cause_type.PEAK_RESPONSE_TIMEOUT,
                     self.cause_type.GRID_CONFIRMATION_TIMEOUT,
                     self.cause_type.GRID_CEILING,
                     self.cause_type.HARD_LIMIT,
                     self.cause_type.OVERLOAD_BUDGET}:
            cycle = regulator._ensure_automatic_cycle()
            cycle['state'] = 'exhausted' if cause == self.cause_type.OVERLOAD_BUDGET else 'blocked'
            cycle['failure_reason'] = cycle['failure_reason'] or cause.value
            regulator._overload_failure_reason = regulator._overload_failure_reason or cause.value
        decision=policy.decision(regulator, inp, 'meter_fault', 0, command='set_limit',
            cause=cause, reason=detail or cause.value)
        policy.zero_permits[decision.plan_id]=cause.value
        while len(policy.zero_permits)>64:
            policy.zero_permits.pop(next(iter(policy.zero_permits)))
        return decision


class MeterEnvelopePolicy:
    def __init__(self, decision_type, cause_type):
        self.decision_type = decision_type
        self.causes = cause_type
        self.supervisor = ElectricalSupervisor(cause_type)
        self.upper = 0.
        self.started = False
        self.recovering = False
        self.start_since = None
        self.start_cap = None
        self.response_ts = None
        self.response_at = None
        self.confirmation_deadline = None
        self.minimum_dispatch = None
        self.minimum_floor = None
        self.peak_complete_grid = None
        self.last_ev_error = None
        self.minimum_verified = False
        self.feedback_base_cap = None
        self.feedback_source = None
        self.zero_permits = {}
        self.cold_upper_until = None
        self.cold_unknown_pending = False
        self.ordinary_decrease_grid = None
        self.ordinary_decrease_cap = None
        self.adopted_phase = None
        self.confirmation_ceiling = None
        self.confirmation_value = None
        self.confirmation_grid_floor = None

    def prepare_handoff(self):
        """Re-adopt actual electrical phase on every explicit policy handoff."""
        self.started=False
        self.minimum_verified=False
        self.minimum_dispatch=self.minimum_floor=None
        self.response_ts=self.response_at=self.confirmation_deadline=None
        self.confirmation_value=self.confirmation_grid_floor=None
        self.confirmation_ceiling=self.adopted_phase=None
        self.ordinary_decrease_grid=self.ordinary_decrease_cap=None
        self.feedback_base_cap=self.feedback_source=None

    def adopt_minimum_phase(self,r,inp,now,wall):
        """Carry a genuine legacy peak's actual dispatch and fixed grid phase."""
        window=r._minimum_confirmation
        owner=window.owner if window is not None else None
        if owner=='nominal':
            accepted=number(r._normal_shed_accepted_at)
            floor=r._normal_shed_ev_floor
            response_at=number(r._normal_shed_response_monotonic)
        else:
            accepted=number(r._overload_minimum_accepted_at)
            floor=r._overload_minimum_ev_floor
            response_at=number(r._overload_minimum_response_monotonic)
        self.minimum_dispatch=accepted
        self.minimum_floor=floor
        if (window is not None and owner in {'overload','nominal'}
                and window.source_id==inp.control_source_id
                and window.cap==r.startup_current and r.last_target[0]==r.startup_current
                and accepted is not None):
            self.adopted_phase=owner
            self.response_ts=window.first_ev_ts
            self.response_at=response_at if response_at is not None else now-(wall-window.first_ev_ts)
            self.confirmation_deadline=window.deadline
            self.confirmation_grid_floor=window.floor
            self.confirmation_value=window.value
            self.confirmation_ceiling=(r.settings.main_fuse_amps-r.settings.buffer_amps
                if owner=='nominal' else r.settings.main_fuse_amps+r.settings.overload_margin_amps)

    def retire_minimum_phase(self,r):
        r._clear_overload_transition();r._clear_minimum_confirmation('overload')
        if self.adopted_phase=='nominal':
            r._clear_bounded_nominal_shedding()
        self.adopted_phase=None

    def decision(self, r, inp, mode, cap, *, command=None, cause=None,
                 reason='', house=None, overload=False, response_deadline=None,
                 confirmation_deadline=None, no_write_deadline=None, basis='meter_envelope_feedback',
                 ordinary_decrease=False, normal_retention=False):
        s, now = r.settings, number(inp.now_ts)
        if now is None:
            now = number(r._last_input_now_ts) or 0.
        raw, ev = number(inp.grid_l1), number(inp.charger_current)
        r._plan_sequence += 1
        should = bool(s.active_control is True and s.dry_run is False and command)
        if command == 'set_limit':
            should = should and (cause==self.causes.CONFIGURATION or r._should_send_limit(now, cap, 60))
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
                if cause==self.causes.CONFIGURATION else r.effective_overload_failure_reason is not None),
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
            meter_state=mode, meter_normal_retention=bool(cap>0 and not cause and
                (normal_retention or raw is not None and raw<=s.main_fuse_amps)),
            meter_overload_authority=overload, meter_ordinary_decrease=ordinary_decrease,
            meter_basis_grid_ts=number(inp.grid_report_ts))
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

    def observe_ledger(self, r, inp, good):
        """Existing ledger schema and exact RAW qualification/cooling rules."""
        s=r.settings; cycle=r._ensure_automatic_cycle()
        now,wall,gt,raw=map(number,(inp.now_ts,inp.wall_now_ts,inp.grid_report_ts,inp.grid_l1))
        if not r._observe_overload_time(now,raw,good):
            return self.causes.CLOCK_INVALID
        source=r._trial_identity(inp.control_source_id)
        if not good or not 0<=wall-gt<=min(15.,s.max_sensor_age_seconds):
            r._break_automatic_continuity()
            if cycle['state'] in {'qualifying','active'}:
                cycle['state']='blocked';cycle['failure_reason']=cycle['failure_reason'] or 'source_control_invalid'
            return None
        if cycle['source_id'] is not None and r._trial_identity(cycle['source_id'])!=source:
            r._break_automatic_continuity();cycle['state']='blocked';cycle['failure_reason']='source_binding_changed'
            return None
        previous=r._automatic_last_gt
        if r._automatic_observed_source != source:
            r._break_automatic_continuity();previous=None
        if previous is not None and gt<previous:
            return self.causes.GRID_INVALID
        if previous is not None and gt==previous:
            return None
        if previous is not None and gt-previous>min(15.,s.max_sensor_age_seconds):
            r._break_automatic_continuity()
        r._automatic_last_gt,r._automatic_last_now,r._automatic_observed_source=gt,now,source
        if cycle['source_id'] is None: cycle['source_id']=source
        if raw<23.:
            r._automatic_qualification_gt=r._automatic_qualification_since=None
            if r._automatic_cooling_gt is None:
                r._automatic_cooling_gt,r._automatic_cooling_since=gt,now
            if gt-r._automatic_cooling_gt>=1800. and now-r._automatic_cooling_since>=1800.:
                if cycle['used_seconds']>0 or cycle['state']!='ready' or cycle['failure_reason']:
                    cycle.update(cycle_id=cycle['cycle_id']+1,state='ready',used_seconds=0.,failure_reason=None)
                r._automatic_cooling_gt,r._automatic_cooling_since=gt,now
            elif cycle['state']=='qualifying': cycle['state']='ready'
            return None
        r._automatic_cooling_gt=r._automatic_cooling_since=None
        if cycle['used_seconds']>=s.overload_budget_seconds:
            cycle['state']='exhausted'
            r._automatic_qualification_gt=r._automatic_qualification_since=None
            return None
        if raw<=s.main_fuse_amps:
            r._automatic_qualification_gt=r._automatic_qualification_since=None
            if cycle['state']=='qualifying': cycle['state']='ready'
            return None
        if cycle['failure_reason'] is not None or cycle['state']=='active': return None
        live=bool(r.last_target and r.last_target[0]>0 and r.last_set_limit_ts is not None
            and 0<=now-r.last_set_limit_ts<r.last_target[3]
            and inp.status=='charging' and s.active_control and not s.dry_run)
        if not live: return None
        if r._automatic_qualification_gt is None:
            r._automatic_qualification_gt,r._automatic_qualification_since=gt,now;cycle['state']='qualifying'
        if gt-r._automatic_qualification_gt>=30. and now-r._automatic_qualification_since>=30.:
            cycle['state']='active'
        return None

    def observe_input(self, r, inp, ev_valid):
        # The legacy parser validates native counters; its error is estimate
        # quality here, not permission to reinterpret a fresh RAW as a fault.
        observed=replace(inp,data_valid=ev_valid,data_error=None,
            charger_current_trusted=ev_valid and inp.charger_current_trusted)
        r._observe_ev_clock(observed)
        r._last_input_now_ts=number(inp.now_ts)
        r._last_input_wall_now_ts=number(inp.wall_now_ts)
        r._last_input_grid_ts=number(inp.grid_report_ts)
        r._last_input_charger_ts=number(inp.charger_report_ts)
        r._last_input_control_source_id=r._trial_identity(inp.control_source_id)
        r._last_input_status=(inp.status or '').strip().lower()
        r._last_input_current_trusted=ev_valid and inp.charger_current_trusted
        r._last_input_native_committed=inp.charger_native_committed
        r._last_input_snapshot_verified=inp.charger_snapshot_verified
        self.last_ev_error=(inp.meter_ev_health.reason if inp.meter_ev_health and not inp.meter_ev_health.valid else r._ev_clock_error)

    def ev_estimate(self, r, inp, ev_valid):
        raw,ev,gt,et,wall=map(number,(inp.grid_l1,inp.charger_current,inp.grid_report_ts,inp.charger_report_ts,inp.wall_now_ts))
        return bool(ev_valid and r._ev_clock_error is None and inp.charger_native_committed is True
            and not inp.charger_snapshot_verified and not inp.charger_publication_pending
            and inp.charger_increase_ready is True and ev is not None and 1<=ev<=16.5
            and et is not None and 0<=wall-et<=r.settings.max_sensor_age_seconds
            and gt>et and abs(gt-et)<=r.settings.measurement_settle_seconds
            and ev<=raw+1 and r._ev_established_value==ev
            and r._event_source_usable())

    def observe_inflight(self,r,inp,decision):
        """Remember the FIRST real high meter report while an SDK awaits.

        The request already entered the SDK; its possible upper is real even
        before HTTP acceptance. Benign telemetry never resets this deadline.
        """
        if self.critical_error(r,inp) is not None:
            return
        raw,now,wall,gt=map(number,(inp.grid_l1,inp.now_ts,inp.wall_now_ts,inp.grid_report_ts))
        self.upper=max(self.upper,decision.current_p1,r.last_target[0] if r.last_target else 0)
        if (28.<raw<=min(38.,r.settings.overload_transition_limit_amps)
                and r._peak_response is None and not (self.minimum_verified
                    and self.upper<=r.startup_current+.5)):
            origin=now-(wall-gt)
            r._peak_response=ElectricalPeak(inp.control_source_id,origin,
                origin+min(10.,r.settings.overload_response_seconds),gt,int(ceil(self.upper)))
            self.response_ts=self.response_at=self.confirmation_deadline=None
            self.minimum_dispatch=self.minimum_floor=None

    def evaluate(self, r, inp):
        s,C=r.settings,self.causes
        try:
            r._trial_policy(replace(s,active_control=True,dry_run=False))
            numeric_valid=(type(s.active_control) is bool and type(s.dry_run) is bool
                and type(s.min_charge_amps) is int and type(s.max_charge_amps) is int
                and type(s.startup_charge_amps) is int and s.startup_charge_amps==7
                and s.min_seconds_between_actions>=0 and s.restart_stable_seconds>=0
                and s.resend_limit_every_seconds>0 and s.max_sensor_age_seconds>0
                and s.controlled_phase=='L1' and s.unused_phase_current==0)
        except (ValueError,TypeError,OverflowError):
            numeric_valid=False
        if not (numeric_valid and s.nominal_import_only_fallback and s.peak_only_response and s.automatic_overload_cycles
                and s.overload_enabled and s.enable_dynamic_limit and s.max_charge_amps<=16
                and s.normal_ttl_seconds==60):
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
        ev_valid=ev_valid and r._ev_clock_error is None
        ev=number(inp.charger_current) if ev_valid else None
        status=r._last_input_status
        live=bool(r.last_target and r.last_target[0]>0 and r.last_set_limit_ts is not None
            and 0<=now-r.last_set_limit_ts<r.last_target[3])
        if r.last_target and r.last_target[0]>0 and not live:
            return self.supervisor.zero(self,r,inp,C.LEASE_EXPIRED)
        if not self.started:
            self.upper=max(self.upper,r._committed_current_upper(),r._peak_whole_authority(),
                r.last_target[0] if r.last_target else 16)
            if not live:
                # No live authority is restored. Every cold controller retains
                # the maximum possible previous16 A for the existing finite
                #60s lease horizon; no smaller persisted cap can erase it.
                self.cold_unknown_pending=True;self.cold_upper_until=now+60.
                candidate=int(floor(min(s.max_charge_amps,s.main_fuse_amps-s.buffer_amps-raw)))
                if candidate>=r.startup_current:
                    self.start_since=now;self.start_cap=candidate
            elif r._peak_response is not None:
                # An opt-in handoff is not a new peak or a new7 A request.
                # Adopt only the existing genuine electrical minimum phase;
                # ordinary legacy measurement owners remain outside this path.
                self.adopt_minimum_phase(r,inp,now,wall)
            self.started=True
        if not live and self.cold_unknown_pending and now>=self.cold_upper_until:
            self.upper=0.;self.cold_unknown_pending=False
        cap=r.last_target[0] if live else 0
        self.upper=max(self.upper,cap)
        if raw>min(38.,s.overload_transition_limit_amps):
            return self.supervisor.zero(self,r,inp,C.HARD_LIMIT)
        manual={'paused','awaiting_smart_start','awaiting_scheduled_start','awaiting_authorization',
            'de_authorizing','authenticating','paused_due_to_equalizer','waiting_for_schedule','stop_charging'}
        if status in manual:
            return self.decision(r,inp,'meter_wait',cap,
                reason='Charger/user pause, authorization or schedule owns the session; no positive write or resume.')
        if status in {'completed','disconnected'}:
            candidate=int(floor(min(s.max_charge_amps,s.main_fuse_amps-s.buffer_amps-raw)))
            if (s.disconnected_readiness and candidate>=r.startup_current
                    and raw+max(self.upper,candidate)<=s.main_fuse_amps-s.buffer_amps):
                return self.decision(r,inp,'meter_ready',candidate,command='set_limit',house=raw,
                    reason='Passive whole-RAW allocation for a completed/disconnected vehicle; no start/resume.')
            return self.decision(r,inp,'meter_wait',cap,
                reason='Passive readiness disabled or lacks whole-RAW headroom; no session start.')
        peak=r._peak_response
        retained_minimum=bool(cap==r.startup_current and ev_valid and ev is not None
            and inp.charger_native_committed and not inp.charger_publication_pending
            and inp.charger_increase_ready and r._ev_clock_error is None
            and r._ev_advanced_since(r._accepted_control_ev_ts,'_accepted_control_ev_ts'))
        if retained_minimum:
            if 1<=ev<=r.startup_current+.5:
                self.minimum_verified=True
            elif ev>r.startup_current+.5:
                self.minimum_verified=False
        if peak is not None and raw+self.upper<=s.main_fuse_amps-s.buffer_amps:
            r._peak_response=None;r._peak_response_failure=None
            self.retire_minimum_phase(r)
            r._overload_hold_active=False
            self.response_ts=self.response_at=self.confirmation_deadline=None
            self.confirmation_value=self.confirmation_grid_floor=self.confirmation_ceiling=None
            peak=None
        if peak is not None and (peak.interrupted or r._peak_response_failure):
            return self.supervisor.zero(self,r,inp,C.PEAK_RESPONSE_TIMEOUT)
        if (raw>28. and live and peak is None and not (cap==r.startup_current and self.minimum_verified)
                and (self.peak_complete_grid is None or gt>self.peak_complete_grid)):
            origin=now-(wall-gt)
            peak=r._peak_response=ElectricalPeak(inp.control_source_id,origin,
                origin+min(10.,s.overload_response_seconds),gt,int(max(cap,self.upper)))
            self.response_ts=self.response_at=self.confirmation_deadline=None
            self.minimum_dispatch=None;self.minimum_floor=None
            self.confirmation_value=self.confirmation_grid_floor=self.confirmation_ceiling=None
        minimum=r.startup_current
        if (peak is not None and cap==minimum and raw<=s.main_fuse_amps
                and gt>peak.grid_source_ts):
            # A genuinely newer measured normal total has continuation
            # authority. It grants neither a timely EV response nor a new
            # response window: an unresolved return above28 still uses the
            # original, possibly already expired, electrical deadline.
            if (self.ev_estimate(r,inp,ev_valid) and ev<=minimum+.5
                    and r._ev_advanced_since(r._accepted_control_ev_ts,'_accepted_control_ev_ts')):
                self.upper=max(minimum,ev)
                self.minimum_verified=True
                r._peak_response=None;r._peak_response_failure=None
                self.retire_minimum_phase(r)
                r._overload_hold_active=False
                self.response_ts=self.response_at=self.confirmation_deadline=None
                peak=None
            else:
                return self.decision(r,inp,'meter_normal',minimum,command='set_limit',
                    reason='Fresh normal meter feedback retains the actual minimum; unresolved peak evidence and its original deadline remain intact.')
        if peak is not None:
            genuine=bool(ev_valid and ev is not None and 1<=ev<=minimum+.5 and cap==minimum
                and self.minimum_dispatch is not None and r._last_input_charger_ts is not None
                and r._ev_advanced_since(self.minimum_floor,'_accepted_control_ev_ts'))
            at=now-(wall-r._last_input_charger_ts) if genuine else None
            if genuine and self.response_ts is None and self.minimum_dispatch<at<peak.deadline:
                self.response_ts=r._last_input_charger_ts;self.response_at=at
                self.confirmation_deadline=min(at+s.grid_confirmation_seconds,
                    r.last_set_limit_ts+r.last_target[3]) if s.grid_confirmation_seconds else peak.deadline
                self.confirmation_grid_floor=self.response_ts
                self.confirmation_value=ev
                self.confirmation_ceiling=s.main_fuse_amps+s.overload_margin_amps
            if self.response_ts is None:
                if now>=peak.deadline:
                    r._peak_response_failure='response_timeout'
                    return self.supervisor.zero(self,r,inp,C.PEAK_RESPONSE_TIMEOUT)
                return self.decision(r,inp,'meter_peak_response',minimum,command='set_limit',
                    response_deadline=peak.deadline,overload=raw>s.main_fuse_amps,
                    reason='Measured RAW peak requests the configured minimum inside the original electrical response deadline.')
            if now>=self.confirmation_deadline:
                return self.supervisor.zero(self,r,inp,C.GRID_CONFIRMATION_TIMEOUT)
            ceiling=self.confirmation_ceiling
            if gt>self.response_ts and raw>ceiling:
                return self.supervisor.zero(self,r,inp,C.GRID_CEILING)
            if (genuine and abs(ev-self.confirmation_value)>.1
                    and r._last_input_charger_ts>self.confirmation_grid_floor):
                self.confirmation_grid_floor=r._last_input_charger_ts
                self.confirmation_value=ev
            if gt>self.confirmation_grid_floor and genuine:
                if ev>raw+1 or raw-ev+minimum>ceiling:
                    return self.supervisor.zero(self,r,inp,C.GRID_CEILING)
                r._peak_response=None;r._peak_response_failure=None
                self.retire_minimum_phase(r)
                r._overload_hold_active=raw>s.main_fuse_amps
                self.peak_complete_grid=gt
                self.upper=max(minimum,ev or 0.)
                self.minimum_verified=True
                self.response_ts=self.response_at=self.confirmation_deadline=None
            else:
                return self.decision(r,inp,'meter_grid_wait',minimum,
                    confirmation_deadline=self.confirmation_deadline,overload=raw>s.main_fuse_amps,
                    reason='Timely genuine minimum response; waiting for the later meter inside the separate original confirmation phase.')
        cycle=r._ensure_automatic_cycle()
        if raw<=s.main_fuse_amps:
            r._overload_hold_active=False
            self.ordinary_decrease_grid=self.ordinary_decrease_cap=None
        if s.main_fuse_amps<raw<=28. and live:
            if (cap==self.ordinary_decrease_cap and self.ordinary_decrease_grid is not None
                    and gt<=self.ordinary_decrease_grid
                    and cycle['used_seconds']<s.overload_budget_seconds):
                return self.decision(r,inp,'meter_normal_hold',cap,normal_retention=True,
                    no_write_deadline=r.last_set_limit_ts+r.last_target[3],
                    reason='The same pre-decrease meter packet cannot claim another response or stop; retain only its actual finite lower lease.')
            if (status=='charging' and self.ev_estimate(r,inp,ev_valid)
                    and r._ev_advanced_since(r._accepted_control_ev_ts,'_accepted_control_ev_ts')):
                house=max(0.,raw-ev)
                lower=int(floor(min(s.max_charge_amps,s.main_fuse_amps-s.buffer_amps-house)))
                if minimum<=lower<cap and house+lower<=s.main_fuse_amps-s.buffer_amps:
                    return self.decision(r,inp,'meter_normal_decrease',lower,command='set_limit',
                        house=house,basis='eligible_native_then_meter',ordinary_decrease=True,
                        reason='Genuine native current followed by the meter permits a strictly lower normal offer; no overload authority is used.')
        if raw>s.main_fuse_amps:
            if cycle['used_seconds']>=s.overload_budget_seconds:
                return self.supervisor.zero(self,r,inp,C.OVERLOAD_BUDGET)
            if cycle['failure_reason'] or cycle['state'] in {'blocked','exhausted'}:
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
        coherent=self.ev_estimate(r,inp,ev_valid) and status=='charging'
        basis_label='meter_envelope_feedback'
        if coherent:
            self.feedback_base_cap=self.feedback_source=None
            desired=int(floor(min(s.max_charge_amps,s.main_fuse_amps-s.buffer_amps-(raw-ev))))
            if r._ev_advanced_since(r._accepted_control_ev_ts,'_accepted_control_ev_ts'):
                self.upper=max(cap,ev)
                if cap==minimum and ev<=minimum+.5:
                    self.minimum_verified=True
            basis=max(0.,raw-ev)
            basis_label='eligible_native_then_meter'
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

    def acknowledge(self,r,d,now,dispatch):
        origin=dispatch['now'] if dispatch is not None else now
        old=r.last_target[0] if r.last_target else None
        r.last_action_ts=now;r.last_set_limit_ts=origin;r.last_command='set_limit'
        r.last_target=(d.current_p1,0,0,60)
        if d.current_p1<=0:
            self.recovering=True;self.start_since=self.start_cap=None
            self.cold_unknown_pending=True;self.cold_upper_until=origin+60.
            r._committed_normal=None
            return
        r._accepted_control_source_id=r._last_input_control_source_id
        if d.meter_ordinary_decrease:
            self.ordinary_decrease_grid=d.meter_basis_grid_ts
            self.ordinary_decrease_cap=d.current_p1
        elif old!=d.current_p1:
            self.ordinary_decrease_grid=self.ordinary_decrease_cap=None
        if old!=d.current_p1:
            r._last_limit_change_ts=origin
            baselines=dispatch.get('ev_owner',{}).get('baselines',{}) if dispatch else {}
            r._accepted_control_ev_ts=next(iter(baselines.values()),r._last_input_charger_ts)
            r._pin_ev_owner('_accepted_control_ev_ts',wall_floor=r._ack_wall(now))
            if d.current_p1>r.startup_current:
                self.minimum_verified=False
            if d.house_load_basis!='meter_envelope_feedback':
                self.feedback_base_cap=self.feedback_source=None
        self.upper=max(self.upper,d.current_p1)
        r._restart_required=False
        if r._peak_response is not None and d.current_p1==r.startup_current and self.minimum_dispatch is None:
            self.minimum_dispatch=origin;self.minimum_floor=r._accepted_control_ev_ts
            # Even a same-cap minimum request needs its actual original dispatch
            # floor; later TTL acknowledgements never move that response clock.
            r._pin_ev_owner('_accepted_control_ev_ts',wall_floor=r._ack_wall(now))
        r._committed_normal=None

    def retention(self,r,inp,proposed=None):
        if not r.last_target or r.last_target[0]<=0: return None
        cap=r.last_target[0]
        shadow=deepcopy(r)
        d=shadow._get_meter_envelope_policy().evaluate(shadow,inp)
        if d.meter_zero_cause or not d.meter_normal_retention or d.meter_overload_authority:
            return None
        template=proposed or d
        return replace(template,mode='meter_normal_hold',reason='Retaining only the actual live meter-envelope lease during local I/O; no command or renewal.',
            command=None,service=None,should_call_service=False,current_p1=cap,current_p2=0,current_p3=0,
            meter_envelope=True,meter_zero_cause=None,meter_state='meter_normal_hold',
            meter_normal_retention=True,meter_overload_authority=False,
            normal_continuity_deadline_ts=r.last_set_limit_ts+r.last_target[3],
            nominal_response_deadline_ts=None,grid_confirmation_deadline_ts=None)
