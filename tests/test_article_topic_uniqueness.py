from types import SimpleNamespace

from config.db import Article, ArticleStatus, _engine
from webapp import pipeline
from webapp.studio import _preferred_article


def test_articles_have_a_unique_topic_index():
    index = next(
        item for item in Article.__table__.indexes
        if item.name == "uq_articles_topic_id"
    )
    assert index.unique
    assert [column.name for column in index.columns] == ["topic_id"]

    with _engine.connect() as connection:
        indexes = connection.exec_driver_sql("PRAGMA index_list(articles)").all()
    assert any(row[1] == "uq_articles_topic_id" and row[2] == 1
               for row in indexes)


def test_completed_article_is_preferred_over_later_empty_brief():
    completed = SimpleNamespace(id=5, status=ArticleStatus.DRAFT)
    duplicate = SimpleNamespace(id=6, status=ArticleStatus.BRIEF)

    assert _preferred_article([completed, duplicate]) is completed


def test_brief_creation_reuses_an_existing_article(monkeypatch):
    existing = SimpleNamespace(id=5)

    class Query:
        def filter_by(self, **kwargs):
            assert kwargs == {"topic_id": 27}
            return self

        def first(self):
            return existing

    class Session:
        def rollback(self):
            pass

        def query(self, model):
            assert model is Article
            return Query()

    monkeypatch.setattr(
        pipeline,
        "_create_brief_for_topic",
        lambda *args: (_ for _ in ()).throw(
            AssertionError("an existing topic must not generate another brief")),
    )

    assert pipeline.create_brief_for_topic(
        Session(), SimpleNamespace(id=27)) is existing
