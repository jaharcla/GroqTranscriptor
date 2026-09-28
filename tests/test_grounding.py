from pathlib import Path

from courseai_lectures.files import Lecture
from courseai_lectures.grounding import CourseGrounder


class FakeNotion:
    def __init__(self):
        self.requested = []

    def request(self, method, route, body=None):
        self.requested.append((method, route))
        if route == "pages/11111111-1111-1111-1111-111111111111":
            return {
                "id": "11111111-1111-1111-1111-111111111111",
                "properties": {
                    "Course": {
                        "type": "title",
                        "title": [{"plain_text": "KIN 120 — Quantitative Analysis"}],
                    },
                    "Modules": {
                        "type": "relation",
                        "relation": [{"id": "module-1"}],
                    },
                    "Assessments": {
                        "type": "relation",
                        "relation": [{"id": "wrong-assessment"}],
                    },
                },
            }
        if route == "pages/module-1":
            return {
                "id": "module-1",
                "properties": {
                    "Name": {
                        "type": "title",
                        "title": [{"plain_text": "2D Vectors"}],
                    }
                },
            }
        raise AssertionError(f"Unexpected Notion request: {method} {route}")

    def children(self, parent):
        if parent == "11111111-1111-1111-1111-111111111111":
            return [
                {
                    "id": "course-text",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [{"plain_text": "Vectors, components, and force systems."}]
                    },
                    "has_children": False,
                },
                {
                    "id": "course-notes",
                    "type": "child_page",
                    "child_page": {"title": "Current lecture notes"},
                    "has_children": True,
                },
            ]
        if parent == "course-notes":
            return [
                {
                    "id": "notes-text",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [{"plain_text": "Resolve vectors into x and y components."}]
                    },
                    "has_children": False,
                }
            ]
        if parent == "module-1":
            return [
                {
                    "id": "module-text",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [{"plain_text": "Unit vectors and graphical addition."}]
                    },
                    "has_children": False,
                }
            ]
        raise AssertionError(f"Unexpected Notion children request: {parent}")


def configured(config):
    config.review_dir = config.state.parent / "reviews"
    config.context_dir = config.state.parent / "review-context"
    config.materials_dir = config.state.parent / "Materials"
    config.notion_context_enabled = True
    config.notion_context_cache_minutes = 30
    config.notion_context_max_chars = 18000
    config.notion_context_relations = ("Modules", "Syllabus")
    return config


def test_grounding_is_scoped_to_mapped_course_and_allowlisted_relations(config):
    configured(config)
    material_dir = config.materials_dir / "KIN120"
    material_dir.mkdir(parents=True)
    (material_dir / "current-slides.txt").write_text(
        "Vector components and resultant force.",
        encoding="utf-8",
    )
    notion = FakeNotion()
    grounder = CourseGrounder(config, notion)
    lecture = Lecture("KIN120", "2026-09-28", "Lecture")

    context = grounder.context(lecture)
    joined = "\n".join(item["text"] for item in context["sources"])

    assert "Quantitative Analysis" in joined
    assert "Resolve vectors" in joined
    assert "Unit vectors" in joined
    assert "resultant force" in joined
    assert not any("wrong-assessment" in route for _method, route in notion.requested)
    assert context["course_page_id"] == config.courses["KIN120"]
    assert all(item["id"] for item in context["sources"])


def test_grounding_cache_survives_notion_refresh_failure(config):
    configured(config)
    lecture = Lecture("KIN120", "2026-09-28", "Lecture")
    grounder = CourseGrounder(config, FakeNotion())
    first = grounder.context(lecture)
    assert first["sources"]

    class BrokenNotion:
        def request(self, *args, **kwargs):
            raise RuntimeError("offline")

        def children(self, *args, **kwargs):
            raise RuntimeError("offline")

    config.notion_context_cache_minutes = 0
    cached = CourseGrounder(config, BrokenNotion()).context(lecture)

    assert cached["sources"]
    assert any("stale cached" in warning for warning in cached["warnings"])


def test_asr_prompt_is_short_and_course_specific(config):
    configured(config)
    context = {
        "sources": [
            {
                "id": "material:slides.txt",
                "type": "local_lecture_material",
                "title": "Cardiovascular Physiology Slides",
                "text": "electrocardiogram depolarization repolarization photoplethysmography",
            }
        ]
    }
    prompt = CourseGrounder(config, FakeNotion()).asr_prompt(
        Lecture("KIN104", "2026-09-28", "Cardiovascular"),
        context,
    )
    assert "KIN104" in prompt
    assert "electrocardiogram" in prompt
    assert len(prompt) <= 900
