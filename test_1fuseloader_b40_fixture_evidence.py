"""Explicit mapped-L1 metadata for existing opt-in synthetic fixtures.

This is fixture data only. Real adapter mapping/clock behavior is exercised
separately by test_1fuseloader_b40_controller's actual ProductUpdate callbacks.
"""


class MappedL1Fixture:
    observation_id=183
    output_phase=10
    mapping_epoch=1
    mapping_sequence=1
    error=None

    def __init__(self,case,adopted,value_reader=None):
        self.case=case
        self.adopted=adopted
        self.value_reader=value_reader

    @property
    def current(self):
        native=self.case.native
        if self.value_reader is not None:
            return self.value_reader()
        # These older scalar fixtures specify a terminal value only for their
        # declared winning IDs. A nonwinner MAX does not invent a T3 value.
        return native.current if 183 in native.observation_ids else None

    @property
    def received_at(self):
        return dict(getattr(self.case.native,'observation_report_times',())).get(183)

    @property
    def sequence(self):
        return dict(getattr(self.case.native,'observation_sequences',())).get(183,0)

    @property
    def evidence_credit(self):
        credits=getattr(self.case.native,'observation_evidence_credit',None)
        return dict(credits).get(183,False) if credits is not None else True

    @property
    def source_identity(self):
        return self.case.native.source_identity

    @property
    def mapping_adopted_at(self):
        return self.case.wall(self.adopted)

    mapping_source_at=mapping_adopted_at
    mapping_source_highwater_at=mapping_adopted_at

    @property
    def source_at(self):
        return dict(getattr(self.case.native,'source_timestamps',())).get(183)

    source_highwater_at=source_at

    @property
    def committed(self):
        return bool(self.current is not None and self.received_at is not None
            and self.received_at>self.mapping_adopted_at and self.sequence>0
            and self.evidence_credit is True and getattr(self.case.native,'error',None) is None)


def install(case,*,adopted,value_reader=None):
    case.adapter.native_controlled_l1_observation=MappedL1Fixture(case,adopted,value_reader)
