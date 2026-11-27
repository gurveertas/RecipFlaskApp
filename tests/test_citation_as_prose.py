"""A citation tag is a pointer, never a word in the sentence.

The tags are stripped at the reader boundary, so a sentence that leans on one
grammatically breaks the moment it is published. Three articles shipped this
way: two published "According to, the exam consists of..." and one reached a
WordPress draft as a link labelled "S1", because the link form survived the
stripper entirely.

The prompt asked for "according to <source name> [S2]" and the model kept the
shape while dropping the name — the half that carries the meaning.
"""


class TestTagsDoingAWordsJob:
    def test_a_trailing_pointer_is_the_good_form(self, qa):
        c = qa("The exam runs for 120 minutes [S1].")["citation_as_prose"]
        assert c.status == "pass"

    def test_according_to_a_bare_tag_fails(self, qa):
        c = qa("According to [S1], the exam runs for 120 minutes.")["citation_as_prose"]
        assert c.status == "fail"
        assert "According to [S1]" in c.detail

    def test_naming_the_source_before_the_tag_passes(self, qa):
        c = qa("According to Pearson VUE [S1], the exam runs for 120 "
               "minutes.")["citation_as_prose"]
        assert c.status == "pass"

    def test_a_tag_as_a_links_visible_text_fails(self, qa):
        # The worst shape: strip_citation_markers knew only the bracket form,
        # so this one published as a link labelled S1.
        c = qa("According to [S1](https://www.pearsonvue.com/), the release "
               "is significant.")["citation_as_prose"]
        assert c.status == "fail"

    def test_a_tag_as_the_sentence_subject_fails(self, qa):
        c = qa("[S1] states that the exam runs for 120 minutes.")["citation_as_prose"]
        assert c.status == "fail"

    def test_a_preposition_straight_into_a_tag_fails(self, qa):
        # "...described in [S3]." publishes as "...described in."
        c = qa("The steps are described in [S3].")["citation_as_prose"]
        assert c.status == "fail"

    def test_naming_the_document_keeps_the_same_sentence_legal(self, qa):
        c = qa("The steps are described in the migration procedure "
               "[S3].")["citation_as_prose"]
        assert c.status == "pass"

    def test_a_product_name_is_not_a_citation(self, qa):
        # "Objects for S3-compatible storage" is Amazon S3, not source 3. A
        # check that cannot tell them apart would fail an article for naming
        # a product, so only bracketed tags count.
        c = qa("Use Objects for S3-compatible storage [S5].")["citation_as_prose"]
        assert c.status == "pass"

    def test_faq_answers_are_checked_too(self, qa):
        # They reach the reader through the same boundary as the body.
        c = qa("The exam runs for 120 minutes [S1].",
               faq=[{"question": "How long is it?",
                     "answer": "According to [S1], 120 minutes."}])["citation_as_prose"]
        assert c.status == "fail"

    def test_it_blocks_rather_than_warns(self, qa):
        # Reader-facing breakage, and mechanical to fix — the same call
        # heading_length makes.
        c = qa("According to [S1], the exam runs for 120 minutes.")["citation_as_prose"]
        assert c.status == "fail"
