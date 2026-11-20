from test_dialogs_ingress import ingress, payload  # noqa: F401


def test_memoryless_does_not_reuse_old_pdf_or_case(ingress):  # noqa: F811
    module, documents, rows = ingress
    first = module.register_turn(payload(module), [("old.pdf", b"%PDF-1.7\nold")])
    old_rows = dict(rows)
    fresh = module.register_turn(payload(module, source_message_id="fresh", attachment_ids=[], memoryless=True), [])
    assert fresh.record.doc_ids == () and fresh.record.active_doc_id == ""
    assert fresh.record.case_id != first.record.case_id
    new_doc = module.register_turn(payload(module, source_message_id="fresh-file", attachment_ids=["new"], memoryless=True),
                                    [("new.pdf", b"%PDF-1.7\nnew")])
    assert len(new_doc.record.doc_ids) == 1 and len(documents) == 2
    assert new_doc.record.case_id != fresh.record.case_id
    assert all(rows[key] == value for key, value in old_rows.items() if key[0] == "dialogs_turn")


def test_normal_followup_still_reuses_latest_document(ingress):  # noqa: F811
    module, _, _ = ingress
    first = module.register_turn(payload(module), [("report.pdf", b"%PDF-1.7\ncontent")])
    followup = module.register_turn(payload(module, source_message_id="followup", attachment_ids=[]), [])
    assert followup.record.doc_ids == first.record.doc_ids
    assert followup.record.case_id == first.record.case_id
