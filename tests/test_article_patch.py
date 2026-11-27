"""A targeted fix returns a PATCH, not an article.

Asking for the whole article back and asking nicely for the untouched parts to
be reproduced verbatim does not work and cannot be made to work by wording:
the model complies mostly, and "mostly" is what the editor sees as "I fixed one
check and an unrelated one went amber".

The patch format removes the question rather than answering it. The draft is
numbered block by block, the model returns edits keyed to those numbers, and a
block it does not name is not rewritten — it is not in the response at all.
These tests hold that guarantee, and the anchor check that is the one thing
standing between a block number and the wrong paragraph.
"""
import json

import pytest

from synthesis.brief import (Edit, apply_patch, number_blocks,
                             parse_patch_response, split_blocks)

DRAFT = """Prisma Access is Palo Alto Networks' cloud-delivered security platform.

## What the certification covers

The exam covers deployment, policy and troubleshooting.

## Who should take it

Network engineers already working with NGFW deployments."""

BLOCKS = split_blocks(DRAFT)


def response(edits, **fields):
    body = {"edits": edits}
    body.update(fields)
    return json.dumps(body)


class TestUntouchedBlocksCannotChange:
    """The whole point of the format."""

    def test_a_block_the_patch_does_not_name_is_byte_identical(self):
        ok, patch = parse_patch_response(response([
            {"block": 3, "anchor": "The exam covers deployment",
             "action": "replace", "text": "The exam covers deployment [S1]."}
        ]), BLOCKS)
        assert ok
        out = split_blocks(apply_patch(BLOCKS, patch.edits))
        assert out[0] == BLOCKS[0]
        assert out[1] == BLOCKS[1]
        assert out[3] == BLOCKS[3]
        assert out[4] == BLOCKS[4]

    def test_only_the_named_block_differs(self):
        ok, patch = parse_patch_response(response([
            {"block": 1, "anchor": "Prisma Access is Palo Alto",
             "action": "replace", "text": "Rewritten opening."}
        ]), BLOCKS)
        out = split_blocks(apply_patch(BLOCKS, patch.edits))
        assert [i for i, (a, b) in enumerate(zip(BLOCKS, out)) if a != b] == [0]


class TestTheAnchorCatchesAWrongBlockNumber:
    """A number is easy for the model to copy, which is why the format uses
    one — but an off-by-one would rewrite a paragraph nobody complained
    about, which is the exact failure the format exists to prevent."""

    def test_an_edit_naming_the_wrong_block_is_discarded(self):
        ok, patch = parse_patch_response(response([
            {"block": 2, "anchor": "The exam covers deployment",   # that is 3
             "action": "replace", "text": "Should never be applied."},
            {"block": 3, "anchor": "The exam covers deployment",
             "action": "replace", "text": "Applied."},
        ]), BLOCKS)
        assert ok
        assert [e.block for e in patch.edits] == [3]
        assert "anchor" in patch.rejected[0]
        assert "Should never be applied" not in apply_patch(BLOCKS, patch.edits)

    def test_a_block_number_off_the_end_is_discarded(self):
        ok, patch = parse_patch_response(response([
            {"block": 99, "anchor": "whatever this says",
             "action": "replace", "text": "x"}]), BLOCKS)
        assert not ok
        assert "does not exist" in patch

    def test_leading_markdown_does_not_count_as_a_mismatch(self):
        # The model quotes the prose and skips the "## ". That is a
        # formatting detail, not the wrong block.
        ok, patch = parse_patch_response(response([
            {"block": 2, "anchor": "What the certification covers",
             "action": "replace", "text": "## What this certification covers"}
        ]), BLOCKS)
        assert ok and [e.block for e in patch.edits] == [2]

    def test_an_anchor_too_short_to_discriminate_is_waved_through(self):
        # Rejecting on a 3-character anchor would throw away good edits for
        # a field that could not have proved anything either way.
        ok, patch = parse_patch_response(response([
            {"block": 3, "anchor": "The", "action": "replace", "text": "New."}
        ]), BLOCKS)
        assert ok and len(patch.edits) == 1


class TestApplyingEdits:
    def test_insert_after_places_the_block_below_its_anchor(self):
        out = split_blocks(apply_patch(BLOCKS, [
            Edit(block=1, action="insert_after", text="A new second block.")]))
        assert out[0] == BLOCKS[0]
        assert out[1] == "A new second block."
        assert out[2] == BLOCKS[1]

    def test_delete_removes_the_block(self):
        out = split_blocks(apply_patch(BLOCKS, [Edit(block=1, action="delete")]))
        assert len(out) == len(BLOCKS) - 1
        assert out[0] == BLOCKS[1]

    def test_several_edits_do_not_shift_each_other(self):
        # Applying low-numbered edits first would move the block a
        # higher-numbered edit is aiming at, and the wrong paragraph changes.
        out = split_blocks(apply_patch(BLOCKS, [
            Edit(block=1, action="replace", text="FIRST"),
            Edit(block=3, action="insert_after", text="INSERTED"),
            Edit(block=5, action="replace", text="LAST"),
        ]))
        assert out[0] == "FIRST"
        assert out[3] == "INSERTED"
        assert out[-1] == "LAST"

    def test_an_insert_and_a_replace_on_one_block_both_land(self):
        out = split_blocks(apply_patch(BLOCKS, [
            Edit(block=3, action="replace", text="REPLACED"),
            Edit(block=3, action="insert_after", text="ADDED"),
        ]))
        assert out[2] == "REPLACED"
        assert out[3] == "ADDED"


class TestNonBodyFields:
    def test_null_means_keep_what_the_article_has(self):
        ok, patch = parse_patch_response(response(
            [{"block": 1, "anchor": "Prisma Access is Palo",
              "action": "replace", "text": "x"}],
            meta_title=None, cta=None), BLOCKS)
        assert patch.meta_title is None and patch.cta is None

    def test_a_field_only_change_needs_no_edits_at_all(self):
        # title_readability lives in the SEO title, not the body. Demanding a
        # body edit alongside it is what made that check unfixable.
        ok, patch = parse_patch_response(response(
            [], meta_title="The Complete Prisma Access SSE Guide 2026"), BLOCKS)
        assert ok
        assert patch.edits == []
        assert patch.meta_title.startswith("The Complete")

    def test_an_empty_response_is_a_failure(self):
        ok, why = parse_patch_response(response([]), BLOCKS)
        assert not ok and "No usable edit" in why

    def test_bad_json_is_a_failure_not_a_crash(self):
        ok, why = parse_patch_response("I'm afraid I can't do that", BLOCKS)
        assert not ok and "valid JSON" in why


class TestMechanicalCorrectionsStillApply:
    def test_edit_text_goes_through_the_same_acronym_fixer(self):
        # parse_article_response applies these at the single point every write
        # path funnels through. The patch path is a fourth write path and must
        # not be the one that skips them.
        ok, patch = parse_patch_response(response([
            {"block": 3, "anchor": "The exam covers deployment",
             "action": "replace", "text": "The ccse exam covers deployment."}
        ]), BLOCKS, acronyms={"CCSE"})
        assert "CCSE" in patch.edits[0].text

    def test_the_seo_title_is_fixed_and_clamped(self):
        ok, patch = parse_patch_response(response(
            [], meta_title="x" * 200), BLOCKS)
        assert len(patch.meta_title) <= 60


class TestNumbering:
    def test_the_numbers_shown_are_the_numbers_applied(self):
        # If these ever drift, every edit lands one block off.
        shown = number_blocks(BLOCKS)
        for i, b in enumerate(BLOCKS, 1):
            assert f"[{i}] {b.splitlines()[0]}" in shown

    def test_blank_runs_do_not_create_phantom_blocks(self):
        assert split_blocks("a\n\n\n\nb") == ["a", "b"]
        assert split_blocks("a\n\n   \n\nb") == ["a", "b"]
