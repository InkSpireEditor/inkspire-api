# -*- coding: utf-8 -*-
"""The scan: what counts as a story, what counts as a chapter, and the ids.

Chapter names, statuses and summaries come from each file's own header, which
`test_ink.py` covers on its own.

These tests go straight to the Scanner, with no HTTP and no account. The routes over
it are in test_files.py.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

import pytest
import yaml

from inkspire_api.fs import (
    Conflict,
    Malformed,
    NotFound,
    StorageError,
    derive_id,
    free_path,
    slugify,
)
from inkspire_api.storage import (
    CHAPTER_SUFFIX,
    SPACE,
    DataRootMissing,
    Scanner,
)

from tests.conftest import make_story


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "stories").mkdir()
    return tmp_path


@pytest.fixture
def scanner(root: Path) -> Scanner:
    return Scanner(root)


def only_story(scanner: Scanner):
    stories = list(scanner.tree().stories.values())
    assert len(stories) == 1, stories
    return stories[0]


# --- ids -------------------------------------------------------------------


def test_an_id_is_sixteen_hex_characters() -> None:
    story_id = derive_id(SPACE, "stories/example-story")
    assert len(story_id) == 16
    assert set(story_id) <= set("0123456789abcdef")


def test_the_same_path_always_derives_the_same_id() -> None:
    assert derive_id(SPACE, "stories/a") == derive_id(SPACE, "stories/a")


def test_different_paths_derive_different_ids() -> None:
    assert derive_id(SPACE, "stories/a") != derive_id(SPACE, "stories/b")


def test_the_same_path_in_another_space_derives_another_id() -> None:
    """Two roots, so a relative path alone does not say which file is meant."""
    assert derive_id(SPACE, "notes.ink") != derive_id("notes", "notes.ink")


def test_a_chapter_id_is_not_its_story_id() -> None:
    story = "stories/example-story"
    assert derive_id(SPACE, story) != derive_id(SPACE, f"{story}/chapters/first.ink")


def test_an_id_survives_a_new_scanner(root: Path) -> None:
    """Ids are derived, so nothing has to be stored for them to stay valid."""
    make_story(root, "example-story", chapters={"first.ink": ""})
    first = only_story(Scanner(root))
    second = only_story(Scanner(root))
    assert first.id == second.id
    assert [c.id for c in first.chapters] == [c.id for c in second.chapters]


# --- what is a story, what is a chapter ------------------------------------


def test_a_directory_without_a_manifest_is_not_a_story(root: Path, scanner: Scanner) -> None:
    make_story(root, "not-a-story", manifest=False, chapters={"first.ink": ""})
    assert scanner.tree().stories == {}
    assert scanner.tree().chapters == {}


def test_a_story_is_named_from_its_manifest(root: Path, scanner: Scanner) -> None:
    make_story(root, "example-story", title="Example Story", synopsis="In one line.")
    story = only_story(scanner)
    assert (story.name, story.summary) == ("Example Story", "In one line.")
    assert story.slug == "example-story"


def test_a_story_with_no_title_is_named_after_its_directory(root: Path, scanner: Scanner) -> None:
    story_dir = make_story(root, "example-story")
    (story_dir / "story.yaml").write_text("synopsis: ''\n", encoding="utf-8")
    assert only_story(scanner).name == "example-story"


def test_a_chapter_is_named_after_its_file(root: Path, scanner: Scanner) -> None:
    make_story(root, "example-story", chapters={"first-chapter.ink": ""})
    assert [c.name for c in only_story(scanner).chapters] == ["first-chapter"]


def test_a_chapter_its_header_titles_is_named_by_it(root: Path, scanner: Scanner) -> None:
    make_story(
        root,
        "example-story",
        chapters={"first-chapter.ink": "---\ntitle: First Chapter\n---\nOnce.\n"},
    )
    assert [c.name for c in only_story(scanner).chapters] == ["First Chapter"]


def test_a_chapter_carries_the_status_and_summary_its_header_gives_it(
    root: Path, scanner: Scanner
) -> None:
    make_story(
        root,
        "example-story",
        chapters={
            "first.ink": "---\nstatus: draft\nsummary: She opens it.\n---\nOnce.\n"
        },
    )
    chapter = only_story(scanner).chapters[0]
    assert (chapter.status, chapter.summary) == ("draft", "She opens it.")


def test_a_chapter_with_no_header_has_no_status_or_summary(
    root: Path, scanner: Scanner
) -> None:
    make_story(root, "example-story", chapters={"first.ink": "Once.\n"})
    chapter = only_story(scanner).chapters[0]
    assert (chapter.status, chapter.summary) == ("", "")


def test_a_chapter_whose_header_is_broken_is_named_by_its_file(
    root: Path, scanner: Scanner
) -> None:
    """A header nobody can parse must not take the chapter out of the tree."""
    make_story(
        root, "example-story", chapters={"first.ink": "---\ntitle: [oops\n---\nOnce.\n"}
    )
    assert [c.name for c in only_story(scanner).chapters] == ["first"]


def test_only_ink_files_are_chapters(root: Path, scanner: Scanner) -> None:
    make_story(root, "example-story", chapters={"first.ink": "", "notes.txt": ""})
    assert [c.filename for c in only_story(scanner).chapters] == ["first.ink"]


def test_a_lorebook_and_a_timeline_are_not_chapters(root: Path, scanner: Scanner) -> None:
    story_dir = make_story(root, "example-story", chapters={"first.ink": ""})
    (story_dir / "lorebook").mkdir()
    (story_dir / "lorebook" / "lorebook.yaml").write_text("{}\n", encoding="utf-8")
    (story_dir / "timeline.yaml").write_text("{}\n", encoding="utf-8")

    story = only_story(scanner)
    assert [c.filename for c in story.chapters] == ["first.ink"]
    assert len(scanner.tree().chapters) == 1


def test_a_story_with_no_chapters_directory_still_lists(root: Path, scanner: Scanner) -> None:
    story_dir = make_story(root, "example-story")
    (story_dir / "chapters").rmdir()
    assert only_story(scanner).chapters == ()


def test_a_symlinked_chapter_is_not_listed(root: Path, scanner: Scanner) -> None:
    """A link is how a file outside the repository would get an id."""
    outside = root.parent / "outside.ink"
    outside.write_text("elsewhere", encoding="utf-8")
    story_dir = make_story(root, "example-story", chapters={"first.ink": ""})
    (story_dir / "chapters" / "linked.ink").symlink_to(outside)

    assert [c.filename for c in only_story(scanner).chapters] == ["first.ink"]


def test_a_symlinked_story_directory_is_not_listed(root: Path, scanner: Scanner) -> None:
    elsewhere = root.parent / "elsewhere"
    (elsewhere / "chapters").mkdir(parents=True)
    (elsewhere / "story.yaml").write_text("title: Elsewhere\n", encoding="utf-8")
    (root / "stories" / "linked").symlink_to(elsewhere)

    assert scanner.tree().stories == {}


def test_an_unparseable_manifest_does_not_hide_the_chapters(root: Path, scanner: Scanner) -> None:
    story_dir = make_story(root, "example-story", chapters={"first.ink": ""})
    (story_dir / "story.yaml").write_text("title: [unclosed\n", encoding="utf-8")

    story = only_story(scanner)
    assert story.name == "example-story"
    assert [c.filename for c in story.chapters] == ["first.ink"]


def test_a_missing_repository_reads_as_no_stories(tmp_path: Path) -> None:
    """An installation whose repository is not cloned yet answers, rather than failing."""
    assert Scanner(tmp_path / "absent").tree().stories == {}


def test_stories_and_chapters_are_indexed_by_id(root: Path, scanner: Scanner) -> None:
    make_story(root, "one", chapters={"a.ink": ""})
    make_story(root, "two", chapters={"b.ink": "", "c.ink": ""})

    tree = scanner.tree()
    assert len(tree.stories) == 2
    assert len(tree.chapters) == 3
    for chapter_id, chapter in tree.chapters.items():
        assert chapter.id == chapter_id
        assert chapter.story_id in tree.stories


def test_an_unknown_id_is_not_found(root: Path, scanner: Scanner) -> None:
    with pytest.raises(NotFound):
        scanner.story("0" * 16)
    with pytest.raises(NotFound):
        scanner.chapter("0" * 16)


# --- containment -----------------------------------------------------------


def test_a_path_out_of_the_repository_is_refused(scanner: Scanner) -> None:
    with pytest.raises(StorageError, match="outside"):
        scanner.path(PurePosixPath("../elsewhere"))


def test_a_path_inside_the_repository_resolves(root: Path, scanner: Scanner) -> None:
    assert scanner.path(PurePosixPath("stories")) == (root / "stories").resolve()


def test_a_symlink_leading_out_of_the_repository_is_refused(root: Path, scanner: Scanner) -> None:
    outside = root.parent / "outside.ink"
    outside.write_text("elsewhere", encoding="utf-8")
    (root / "stories" / "linked.ink").symlink_to(outside)

    with pytest.raises(StorageError, match="outside"):
        scanner.path(PurePosixPath("stories/linked.ink"))


# --- the held scan ---------------------------------------------------------


def test_a_second_scan_is_the_held_one(root: Path, scanner: Scanner) -> None:
    make_story(root, "example-story", chapters={"first.ink": ""})
    assert scanner.tree() is scanner.tree()


def test_a_new_chapter_on_disk_is_picked_up(root: Path, scanner: Scanner) -> None:
    story_dir = make_story(root, "example-story", chapters={"first.ink": ""})
    assert len(scanner.tree().chapters) == 1

    (story_dir / "chapters" / "second.ink").write_text("", encoding="utf-8")
    assert len(scanner.tree().chapters) == 2


def test_a_new_story_on_disk_is_picked_up(root: Path, scanner: Scanner) -> None:
    make_story(root, "one")
    assert len(scanner.tree().stories) == 1

    make_story(root, "two")
    assert len(scanner.tree().stories) == 2


def test_a_retitled_manifest_is_picked_up(root: Path, scanner: Scanner) -> None:
    story_dir = make_story(root, "example-story", title="Before")
    assert only_story(scanner).name == "Before"

    manifest = story_dir / "story.yaml"
    manifest.write_text("title: After\n", encoding="utf-8")
    # The mtime is what the held scan is checked against, and a test can write twice
    # inside one filesystem timestamp.
    os.utime(manifest, ns=(0, 1))
    assert only_story(scanner).name == "After"


def test_a_retitled_chapter_is_picked_up(root: Path, scanner: Scanner) -> None:
    story_dir = make_story(root, "example-story", chapters={"first.ink": "Once.\n"})
    assert [c.name for c in only_story(scanner).chapters] == ["first"]

    chapter = story_dir / "chapters" / "first.ink"
    chapter.write_text("---\ntitle: The Letter\n---\nOnce.\n", encoding="utf-8")
    os.utime(chapter, ns=(0, 1))
    assert [c.name for c in only_story(scanner).chapters] == ["The Letter"]


def test_writing_a_chapter_leaves_the_held_scan_alone(root: Path, scanner: Scanner) -> None:
    """Prose changes no name and no path, so nothing has to be rescanned."""
    make_story(root, "example-story", chapters={"first.ink": ""})
    before = scanner.tree()

    scanner.write_chapter(next(iter(before.chapters)), "Once.")
    assert scanner.tree() is before


def test_invalidate_forces_a_rescan(root: Path, scanner: Scanner) -> None:
    make_story(root, "example-story")
    before = scanner.tree()
    scanner.invalidate()
    assert scanner.tree() is not before


# --- naming ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "slug"),
    [
        ("First Chapter", "first-chapter"),
        ("first_chapter", "first-chapter"),
        ("  Spaced  Out  ", "spaced-out"),
        ("Ampersands & Quotes'", "ampersands-quotes"),
        ("Dashes -- everywhere", "dashes-everywhere"),
        ("-Trimmed-", "trimmed"),
        ("MiXeD CaSe", "mixed-case"),
        ("123", "123"),
    ],
)
def test_a_name_becomes_a_filename(name: str, slug: str) -> None:
    assert slugify(name, "fallback") == slug


def test_a_name_of_nothing_but_punctuation_falls_back() -> None:
    assert slugify("!?..", "chapter") == "chapter"


def test_a_free_path_is_numbered_from_two(tmp_path: Path) -> None:
    assert free_path(tmp_path, "first", CHAPTER_SUFFIX).name == "first.ink"

    (tmp_path / "first.ink").touch()
    assert free_path(tmp_path, "first", CHAPTER_SUFFIX).name == "first-2.ink"

    (tmp_path / "first-2.ink").touch()
    assert free_path(tmp_path, "first", CHAPTER_SUFFIX).name == "first-3.ink"


# --- changes ---------------------------------------------------------------


def test_creating_a_story_writes_a_manifest_and_a_chapters_directory(scanner: Scanner, root: Path) -> None:
    story = scanner.create_story("Example Story", "In one line.")

    story_dir = root / "stories" / "example-story"
    assert (story_dir / "chapters").is_dir()
    document = yaml.safe_load((story_dir / "story.yaml").read_text(encoding="utf-8"))
    assert document == {"title": "Example Story", "synopsis": "In one line.", "chapters": []}
    assert (story.name, story.summary) == ("Example Story", "In one line.")


def test_a_created_chapters_directory_is_kept_by_git(scanner: Scanner, root: Path) -> None:
    scanner.create_story("Example Story")
    assert (root / "stories" / "example-story" / "chapters" / ".gitkeep").is_file()


def test_two_stories_may_share_a_title(scanner: Scanner, root: Path) -> None:
    first = scanner.create_story("Example Story")
    second = scanner.create_story("Example Story")

    assert first.id != second.id
    assert {first.slug, second.slug} == {"example-story", "example-story-2"}
    assert first.name == second.name == "Example Story"


def test_creating_a_story_without_a_repository_says_so(tmp_path: Path) -> None:
    with pytest.raises(DataRootMissing, match="INKSPIRE_DATA_ROOT"):
        Scanner(tmp_path / "absent").create_story("Example Story")


def test_a_repository_with_no_stories_directory_gets_one(tmp_path: Path) -> None:
    scanner = Scanner(tmp_path)
    assert scanner.create_story("Example Story").slug == "example-story"


def test_retitling_a_story_keeps_its_id_and_its_slug(scanner: Scanner) -> None:
    story = scanner.create_story("Before", "Before.")
    updated = scanner.update_story(story.id, name="After", summary="After.")

    assert updated.id == story.id
    assert updated.slug == story.slug
    assert (updated.name, updated.summary) == ("After", "After.")


def test_retitling_a_story_leaves_its_chapter_ids_alone(scanner: Scanner) -> None:
    story = scanner.create_story("Before")
    chapter = scanner.create_chapter(story.id, "First Chapter")

    scanner.update_story(story.id, name="After")
    assert scanner.chapter(chapter.id).name == "First Chapter"


def test_retitling_a_story_keeps_the_rest_of_its_manifest(scanner: Scanner, root: Path) -> None:
    story = scanner.create_story("Before")
    manifest = root / "stories" / "before" / "story.yaml"
    manifest.write_text(
        yaml.safe_dump({"title": "Before", "chapters": [{"file": "a.ink", "status": "draft"}]}),
        encoding="utf-8",
    )
    scanner.invalidate()

    scanner.update_story(story.id, name="After")
    document = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    assert document == {"title": "After", "chapters": [{"file": "a.ink", "status": "draft"}]}


def test_a_created_chapter_is_a_header_and_no_prose(scanner: Scanner, root: Path) -> None:
    story = scanner.create_story("Example Story")
    chapter = scanner.create_chapter(story.id, "First Chapter")

    path = root / "stories" / "example-story" / "chapters" / "first-chapter.ink"
    assert path.read_text(encoding="utf-8") == "---\ntitle: First Chapter\n---\n"
    assert chapter.name == "First Chapter"
    assert chapter.story_id == story.id


def test_a_created_chapter_takes_a_place_in_the_order(
    scanner: Scanner, root: Path
) -> None:
    """The manifest says what order the chapters are read in, and nothing more."""
    story = scanner.create_story("Example Story")
    scanner.create_chapter(story.id, "First Chapter")
    scanner.create_chapter(story.id, "Second Chapter")

    document = yaml.safe_load(
        (root / "stories" / "example-story" / "story.yaml").read_text(encoding="utf-8")
    )
    assert document["chapters"] == [
        {"file": "first-chapter.ink"},
        {"file": "second-chapter.ink"},
    ]


def test_a_chapter_named_like_its_filename_gets_no_header(
    scanner: Scanner, root: Path
) -> None:
    """The filename already says the name, so a title would only repeat it."""
    story = scanner.create_story("Example Story")
    scanner.create_chapter(story.id, "first-chapter")

    path = root / "stories" / "example-story" / "chapters" / "first-chapter.ink"
    assert path.read_text(encoding="utf-8") == ""


def test_two_chapters_may_share_a_name(scanner: Scanner) -> None:
    story = scanner.create_story("Example Story")
    first = scanner.create_chapter(story.id, "First Chapter")
    second = scanner.create_chapter(story.id, "First Chapter")

    assert first.id != second.id
    assert {first.filename, second.filename} == {
        "first-chapter.ink",
        "first-chapter-2.ink",
    }


def test_renaming_a_chapter_renames_its_file_and_changes_its_id(scanner: Scanner, root: Path) -> None:
    story = scanner.create_story("Example Story")
    chapter = scanner.create_chapter(story.id, "Before")
    scanner.write_chapter(chapter.id, "Once.")

    renamed = scanner.update_chapter(chapter.id, name="After")

    chapters_dir = root / "stories" / "example-story" / "chapters"
    assert renamed.id != chapter.id
    assert renamed.filename == "after.ink"
    assert not (chapters_dir / "before.ink").exists()
    assert scanner.read_chapter(renamed.id) == "Once."


def test_renaming_a_chapter_keeps_its_place_in_the_order(scanner: Scanner, root: Path) -> None:
    story = scanner.create_story("Example Story")
    first = scanner.create_chapter(story.id, "One")
    scanner.create_chapter(story.id, "Two")

    manifest = root / "stories" / "example-story" / "story.yaml"
    document = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    document["chapters"] = [
        {"file": "one.ink", "status": "draft"},
        {"file": "two.ink"},
    ]
    manifest.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    scanner.invalidate()

    scanner.update_chapter(first.id, name="One, Revised")

    assert yaml.safe_load(manifest.read_text(encoding="utf-8"))["chapters"] == [
        {"file": "one-revised.ink", "status": "draft"},
        {"file": "two.ink"},
    ]


def test_a_rename_that_leaves_the_filename_alone_still_renames(scanner: Scanner, root: Path) -> None:
    """"first chapter" and "First Chapter" are one filename and two names."""
    story = scanner.create_story("Example Story")
    chapter = scanner.create_chapter(story.id, "first-chapter")

    renamed = scanner.update_chapter(chapter.id, name="First Chapter")

    assert renamed.id == chapter.id
    assert renamed.filename == "first-chapter.ink"
    assert renamed.name == "First Chapter"


def test_renaming_a_chapter_writes_the_name_into_its_header(
    scanner: Scanner, root: Path
) -> None:
    story = scanner.create_story("Example Story")
    chapter = scanner.create_chapter(story.id, "One")
    scanner.write_chapter(chapter.id, "Once.\n")

    renamed = scanner.update_chapter(chapter.id, name="One, Revised")

    path = root / "stories" / "example-story" / "chapters" / renamed.filename
    assert path.read_text(encoding="utf-8") == (
        "---\ntitle: One, Revised\n---\nOnce.\n"
    )


def test_renaming_a_chapter_to_its_own_filename_drops_the_title(
    scanner: Scanner, root: Path
) -> None:
    """Nothing is left to record, so the file is left without a header at all."""
    story = scanner.create_story("Example Story")
    chapter = scanner.create_chapter(story.id, "One, Revised")
    scanner.write_chapter(chapter.id, "Once.\n")

    renamed = scanner.update_chapter(chapter.id, name="one-revised")

    path = root / "stories" / "example-story" / "chapters" / renamed.filename
    assert path.read_text(encoding="utf-8") == "Once.\n"
    assert renamed.name == "one-revised"


def test_moving_a_chapter_leaves_its_header_alone(scanner: Scanner, root: Path) -> None:
    source = scanner.create_story("Source")
    target = scanner.create_story("Target")
    chapter = scanner.create_chapter(source.id, "First Chapter")

    moved = scanner.update_chapter(chapter.id, story_id=target.id)

    path = root / "stories" / "target" / "chapters" / moved.filename
    assert path.read_text(encoding="utf-8") == "---\ntitle: First Chapter\n---\n"
    assert moved.name == "First Chapter"


def test_moving_a_chapter_moves_its_file(scanner: Scanner, root: Path) -> None:
    source = scanner.create_story("Source")
    target = scanner.create_story("Target")
    chapter = scanner.create_chapter(source.id, "First Chapter")
    scanner.write_chapter(chapter.id, "Once.")

    moved = scanner.update_chapter(chapter.id, story_id=target.id)

    assert moved.story_id == target.id
    assert moved.name == "First Chapter"
    assert scanner.read_chapter(moved.id) == "Once."
    assert not (root / "stories" / "source" / "chapters" / "first-chapter.ink").exists()
    assert (root / "stories" / "target" / "chapters" / "first-chapter.ink").is_file()


def test_moving_a_chapter_takes_its_manifest_entry_with_it(scanner: Scanner, root: Path) -> None:
    source = scanner.create_story("Source")
    target = scanner.create_story("Target")
    chapter = scanner.create_chapter(source.id, "First Chapter")

    scanner.update_chapter(chapter.id, story_id=target.id)

    def listed(slug: str) -> list:
        path = root / "stories" / slug / "story.yaml"
        return yaml.safe_load(path.read_text(encoding="utf-8"))["chapters"]

    assert listed("source") == []
    assert listed("target") == [{"file": "first-chapter.ink"}]


def test_a_chapter_moved_onto_a_taken_filename_is_numbered(scanner: Scanner) -> None:
    source = scanner.create_story("Source")
    target = scanner.create_story("Target")
    scanner.create_chapter(target.id, "First Chapter")
    chapter = scanner.create_chapter(source.id, "First Chapter")

    moved = scanner.update_chapter(chapter.id, story_id=target.id)
    assert moved.filename == "first-chapter-2.ink"


def test_deleting_a_chapter_removes_its_file_and_its_entry(scanner: Scanner, root: Path) -> None:
    story = scanner.create_story("Example Story")
    chapter = scanner.create_chapter(story.id, "First Chapter")

    scanner.delete_chapter(chapter.id)

    manifest = root / "stories" / "example-story" / "story.yaml"
    assert not (root / "stories" / "example-story" / "chapters" / "first-chapter.ink").exists()
    assert yaml.safe_load(manifest.read_text(encoding="utf-8"))["chapters"] == []
    with pytest.raises(NotFound):
        scanner.chapter(chapter.id)


def test_deleting_a_story_takes_its_chapters(scanner: Scanner, root: Path) -> None:
    story = scanner.create_story("Example Story")
    scanner.create_chapter(story.id, "First Chapter")

    scanner.delete_story(story.id)

    assert not (root / "stories" / "example-story").exists()
    assert scanner.tree().stories == {}


def test_a_story_holding_a_lorebook_is_not_deleted(scanner: Scanner, root: Path) -> None:
    """A lorebook and a timeline are written by hand, not through this API."""
    story = scanner.create_story("Example Story")
    (root / "stories" / "example-story" / "lorebook").mkdir()
    (root / "stories" / "example-story" / "timeline.yaml").touch()
    scanner.invalidate()

    with pytest.raises(Conflict, match="lorebook, timeline.yaml"):
        scanner.delete_story(story.id)
    assert (root / "stories" / "example-story").is_dir()


# --- content ---------------------------------------------------------------


def test_a_chapter_reads_back_what_was_written(scanner: Scanner) -> None:
    story = scanner.create_story("Example Story")
    chapter = scanner.create_chapter(story.id, "First Chapter")

    scanner.write_chapter(chapter.id, "Once, on a cold morning.\n")
    assert scanner.read_chapter(chapter.id) == "Once, on a cold morning.\n"


def test_reading_a_chapter_leaves_out_its_header(root: Path, scanner: Scanner) -> None:
    make_story(
        root,
        "example-story",
        chapters={"first.ink": "---\ntitle: The Letter\n---\nOnce.\n"},
    )
    chapter = only_story(scanner).chapters[0]
    assert scanner.read_chapter(chapter.id) == "Once.\n"


def test_writing_a_chapter_keeps_the_header_on_disk(
    root: Path, scanner: Scanner
) -> None:
    """A client sends prose and nothing else, so the header has to survive the save."""
    story_dir = make_story(
        root,
        "example-story",
        chapters={
            "first.ink": "---\ntitle: The Letter\nstatus: draft\n---\nOnce.\n"
        },
    )
    chapter = only_story(scanner).chapters[0]

    scanner.write_chapter(chapter.id, "Twice.\n")

    assert (story_dir / "chapters" / "first.ink").read_text(encoding="utf-8") == (
        "---\ntitle: The Letter\nstatus: draft\n---\nTwice.\n"
    )


def test_writing_a_chapter_that_has_no_header_adds_none(
    root: Path, scanner: Scanner
) -> None:
    story_dir = make_story(root, "example-story", chapters={"first.ink": "Once.\n"})
    chapter = only_story(scanner).chapters[0]

    scanner.write_chapter(chapter.id, "Twice.\n")

    assert (story_dir / "chapters" / "first.ink").read_text(encoding="utf-8") == "Twice.\n"


def test_a_chapter_keeps_accents_and_quotes(scanner: Scanner) -> None:
    story = scanner.create_story("Example Story")
    chapter = scanner.create_chapter(story.id, "First Chapter")

    text = "Il rêva d'une « maison » — puis se réveilla.\n"
    scanner.write_chapter(chapter.id, text)
    assert scanner.read_chapter(chapter.id) == text


def test_a_write_leaves_no_temporary_file_behind(scanner: Scanner, root: Path) -> None:
    story = scanner.create_story("Example Story")
    chapter = scanner.create_chapter(story.id, "First Chapter")

    scanner.write_chapter(chapter.id, "Once.")
    chapters_dir = root / "stories" / "example-story" / "chapters"
    assert sorted(p.name for p in chapters_dir.iterdir()) == [
        ".gitkeep",
        "first-chapter.ink",
    ]


def test_a_chapter_deleted_under_the_client_is_not_found(scanner: Scanner, root: Path) -> None:
    story = scanner.create_story("Example Story")
    chapter = scanner.create_chapter(story.id, "First Chapter")
    (root / "stories" / "example-story" / "chapters" / "first-chapter.ink").unlink()

    with pytest.raises(NotFound):
        scanner.read_chapter(chapter.id)
    with pytest.raises(NotFound):
        scanner.write_chapter(chapter.id, "Once.")


def test_a_chapter_that_is_not_utf8_says_so(scanner: Scanner, root: Path) -> None:
    story = scanner.create_story("Example Story")
    chapter = scanner.create_chapter(story.id, "First Chapter")
    (root / "stories" / "example-story" / "chapters" / "first-chapter.ink").write_bytes(
        b"\xff\xfe not text"
    )

    with pytest.raises(StorageError, match="UTF-8"):
        scanner.read_chapter(chapter.id)


# --- the order the chapters are read in --------------------------------------


def test_the_manifest_decides_the_order(scanner: Scanner, root: Path) -> None:
    """Not the filenames: `story.yaml` is the one place the order lives."""
    make_story(
        root,
        "example-story",
        title="Example Story",
        chapters={"alpha.ink": "", "beta.ink": "", "gamma.ink": ""},
        listed=[{"file": "gamma.ink"}, {"file": "alpha.ink"}, {"file": "beta.ink"}],
    )

    story = next(iter(scanner.tree().stories.values()))
    assert [chapter.filename for chapter in story.chapters] == [
        "gamma.ink",
        "alpha.ink",
        "beta.ink",
    ]


def test_a_chapter_the_manifest_does_not_list_comes_last(
    scanner: Scanner, root: Path
) -> None:
    """A file arriving by `git pull` or from an editor shows up rather than disappearing."""
    make_story(
        root,
        "example-story",
        title="Example Story",
        chapters={"alpha.ink": "", "beta.ink": "", "zeta.ink": ""},
        listed=[{"file": "zeta.ink"}],
    )

    story = next(iter(scanner.tree().stories.values()))
    assert [chapter.filename for chapter in story.chapters] == [
        "zeta.ink",
        "alpha.ink",
        "beta.ink",
    ]


def test_a_manifest_naming_a_file_that_is_gone_still_scans(
    scanner: Scanner, root: Path
) -> None:
    """The filesystem decides what exists, so an entry pointing nowhere lists nothing."""
    make_story(
        root,
        "example-story",
        title="Example Story",
        chapters={"alpha.ink": ""},
        listed=[{"file": "deleted.ink"}, {"file": "alpha.ink"}],
    )

    story = next(iter(scanner.tree().stories.values()))
    assert [chapter.filename for chapter in story.chapters] == ["alpha.ink"]


def test_reordering_writes_the_manifest(scanner: Scanner, root: Path) -> None:
    story = scanner.create_story("Example Story")
    first = scanner.create_chapter(story.id, "One")
    second = scanner.create_chapter(story.id, "Two")

    reordered = scanner.reorder_chapters(story.id, [second.id, first.id])

    assert [chapter.filename for chapter in reordered.chapters] == ["two.ink", "one.ink"]
    manifest = root / "stories" / "example-story" / "story.yaml"
    assert yaml.safe_load(manifest.read_text(encoding="utf-8"))["chapters"] == [
        {"file": "two.ink"},
        {"file": "one.ink"},
    ]


def test_reordering_keeps_what_a_manifest_entry_carries(
    scanner: Scanner, root: Path
) -> None:
    """An entry is edited in place, so a key written by hand survives being moved."""
    story = scanner.create_story("Example Story")
    first = scanner.create_chapter(story.id, "One")
    second = scanner.create_chapter(story.id, "Two")

    manifest = root / "stories" / "example-story" / "story.yaml"
    document = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    document["chapters"] = [{"file": "one.ink", "note": "kept"}, {"file": "two.ink"}]
    manifest.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    scanner.invalidate()

    scanner.reorder_chapters(story.id, [second.id, first.id])

    assert yaml.safe_load(manifest.read_text(encoding="utf-8"))["chapters"] == [
        {"file": "two.ink"},
        {"file": "one.ink", "note": "kept"},
    ]


def test_a_chapter_left_out_of_an_order_keeps_a_place(
    scanner: Scanner, root: Path
) -> None:
    """A client working from a listing taken before a third chapter arrived must not
    drop it out of the manifest."""
    story = scanner.create_story("Example Story")
    first = scanner.create_chapter(story.id, "One")
    second = scanner.create_chapter(story.id, "Two")
    third = scanner.create_chapter(story.id, "Three")

    reordered = scanner.reorder_chapters(story.id, [second.id, first.id])

    assert [chapter.filename for chapter in reordered.chapters] == [
        "two.ink",
        "one.ink",
        "three.ink",
    ]
    assert third.id in {chapter.id for chapter in reordered.chapters}


def test_reordering_gives_an_unlisted_chapter_a_place(
    scanner: Scanner, root: Path
) -> None:
    """A chapter that arrived on disk is shown last; naming it in an order records it."""
    make_story(
        root,
        "example-story",
        title="Example Story",
        chapters={"alpha.ink": "", "beta.ink": ""},
        listed=[{"file": "alpha.ink"}],
    )
    story = next(iter(scanner.tree().stories.values()))
    beta = next(c for c in story.chapters if c.filename == "beta.ink")
    alpha = next(c for c in story.chapters if c.filename == "alpha.ink")

    scanner.reorder_chapters(story.id, [beta.id, alpha.id])

    manifest = root / "stories" / "example-story" / "story.yaml"
    assert yaml.safe_load(manifest.read_text(encoding="utf-8"))["chapters"] == [
        {"file": "beta.ink"},
        {"file": "alpha.ink"},
    ]


def test_reordering_refuses_a_chapter_of_another_story(scanner: Scanner) -> None:
    story = scanner.create_story("Example Story")
    other = scanner.create_story("Other Story")
    elsewhere = scanner.create_chapter(other.id, "One")

    with pytest.raises(Malformed, match=elsewhere.id):
        scanner.reorder_chapters(story.id, [elsewhere.id])


def test_reordering_refuses_an_unknown_chapter(scanner: Scanner) -> None:
    story = scanner.create_story("Example Story")

    with pytest.raises(Malformed):
        scanner.reorder_chapters(story.id, ["0" * 16])


def test_reordering_refuses_a_repeated_chapter(scanner: Scanner) -> None:
    story = scanner.create_story("Example Story")
    chapter = scanner.create_chapter(story.id, "One")

    with pytest.raises(Malformed, match="twice"):
        scanner.reorder_chapters(story.id, [chapter.id, chapter.id])


def test_reordering_an_unknown_story_is_not_found(scanner: Scanner) -> None:
    with pytest.raises(NotFound):
        scanner.reorder_chapters("0" * 16, [])


# --- what a header says, written ---------------------------------------------


def test_a_status_is_written_into_the_header(scanner: Scanner, root: Path) -> None:
    story = scanner.create_story("Example Story")
    chapter = scanner.create_chapter(story.id, "one")
    scanner.write_chapter(chapter.id, "Once.")

    updated = scanner.update_chapter(chapter.id, status="draft")

    assert updated.status == "draft"
    assert updated.id == chapter.id
    path = root / "stories" / "example-story" / "chapters" / "one.ink"
    assert path.read_text(encoding="utf-8") == "---\nstatus: draft\n---\nOnce."


def test_an_empty_status_removes_it(scanner: Scanner, root: Path) -> None:
    story = scanner.create_story("Example Story")
    chapter = scanner.create_chapter(story.id, "one")
    scanner.update_chapter(chapter.id, status="draft")

    cleared = scanner.update_chapter(chapter.id, status="")

    assert cleared.status == ""
    path = root / "stories" / "example-story" / "chapters" / "one.ink"
    assert path.read_text(encoding="utf-8") == ""


def test_a_rename_and_a_status_are_one_write(scanner: Scanner, root: Path) -> None:
    story = scanner.create_story("Example Story")
    chapter = scanner.create_chapter(story.id, "one")
    scanner.write_chapter(chapter.id, "Once.")

    updated = scanner.update_chapter(chapter.id, name="The Letter", status="revised")

    assert updated.name == "The Letter"
    assert updated.status == "revised"
    path = root / "stories" / "example-story" / "chapters" / "the-letter.ink"
    assert path.read_text(encoding="utf-8") == (
        "---\ntitle: The Letter\nstatus: revised\n---\nOnce."
    )


def test_a_status_written_by_hand_survives_a_rename(scanner: Scanner, root: Path) -> None:
    """The header is read from disk at the moment of the write, not sent by the client."""
    story = scanner.create_story("Example Story")
    chapter = scanner.create_chapter(story.id, "one")
    path = root / "stories" / "example-story" / "chapters" / "one.ink"
    path.write_text("---\nstatus: revised\n---\nOnce.", encoding="utf-8")
    scanner.invalidate()

    renamed = scanner.update_chapter(chapter.id, name="Two")

    assert renamed.status == "revised"
