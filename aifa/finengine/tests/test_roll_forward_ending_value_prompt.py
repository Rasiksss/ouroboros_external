from finengine.skills.finengine2_prompts import build_extraction_prompt, prompt_profile_for_cell


def test_extraction_prompt_disambiguates_closing_carrying_amount_in_roll_forwards():
    prompt = build_extraction_prompt(
        prompt_profile_for_cell("rsbu_nf_ru"),
        target_enums=["TRADEMARK_CARRYING_AMOUNT"],
        metric_blocks_json="[]",
        schema_lines=['"TRADEMARK_CARRYING_AMOUNT": {}'],
        split_request=True,
        company_type="non_financial",
    )

    assert "closing/end-of-period group" in prompt
    assert "closing carrying amount" in prompt
    assert "not opening carrying amount" in prompt
