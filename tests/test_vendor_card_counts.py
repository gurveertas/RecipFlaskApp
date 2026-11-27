"""The vendor card and the Topic Engine must agree about what a vendor has.

The card read "14 certifications" for Check Point when 4 were retired — two of
them older versions of credentials already counted as current, so the same exam
was on the card twice. Cisco's read 15 when every Cisco row is a training
course and _vendor_cert_context correctly hands the Topic Engine none.

Overstated for 10 of 14 vendors, and worse than a wrong number: the screen and
the engine were describing different catalogues.
"""
from config.db import (Certification, CertificationStatus, Course, Vendor,
                       get_session)


def _stats_for(vendor_name):
    """The stats dict the vendor grid renders, for one vendor."""
    with get_session() as s:
        v = s.query(Vendor).filter_by(name=vendor_name).first()
        if v is None:
            return None
        sellable = [c for c in v.certifications
                    if c.status == CertificationStatus.CURRENT and c.is_credential]
        return {"certifications": len(sellable),
                "courses": sum(len(c.courses) for c in v.certifications),
                "rows": len(v.certifications)}


class TestOnlyCurrentCredentialsAreCounted:
    def test_a_retired_certification_is_not_advertised(self):
        with get_session() as s:
            v = Vendor(name="Cardtest Vendor")
            s.add(v); s.flush()
            s.add(Certification(vendor_id=v.id, name="Live Cert", exam_code="LC",
                                status=CertificationStatus.CURRENT))
            s.add(Certification(vendor_id=v.id, name="Old Cert", exam_code="OC",
                                status=CertificationStatus.RETIRED))
            s.commit()
            vid = v.id
        try:
            st = _stats_for("Cardtest Vendor")
            assert st["rows"] == 2          # both rows still exist
            assert st["certifications"] == 1   # only the current one counts
        finally:
            with get_session() as s:
                for c in s.query(Certification).filter_by(vendor_id=vid).all():
                    s.delete(c)
                s.delete(s.get(Vendor, vid))
                s.commit()

    def test_a_course_shaped_row_is_not_advertised_as_a_certification(self):
        # Cisco's card said 15 while the Topic Engine saw 0 credentials.
        with get_session() as s:
            v = Vendor(name="Coursey Vendor")
            s.add(v); s.flush()
            c = Certification(vendor_id=v.id, name="Implementing Widgets",
                              status=CertificationStatus.CURRENT)
            c.is_credential = False
            s.add(c); s.flush()
            s.add(Course(certification_id=c.id, name="Implementing Widgets",
                         url="https://www.rededucation.com/x/"))
            s.commit()
            vid = v.id
        try:
            st = _stats_for("Coursey Vendor")
            assert st["certifications"] == 0
            # ...but the course it teaches is still counted, which is the
            # honest other half of "0 certifications".
            assert st["courses"] == 1
        finally:
            with get_session() as s:
                for c in s.query(Certification).filter_by(vendor_id=vid).all():
                    for co in list(c.courses):
                        s.delete(co)
                    s.delete(c)
                s.delete(s.get(Vendor, vid))
                s.commit()


class TestTheCardAgreesWithTheTopicEngine:
    def test_both_apply_the_same_two_conditions(self):
        # If these ever drift again, the screen advertises a catalogue the
        # engine will not write about.
        import inspect
        from webapp import studio
        from webapp.pipeline import _vendor_cert_context
        card = inspect.getsource(studio.vendor_step)
        engine = inspect.getsource(_vendor_cert_context)
        for src, label in ((card, "vendor card"), (engine, "topic engine")):
            assert "CertificationStatus.CURRENT" in src, label
            assert "is_credential" in src, label
