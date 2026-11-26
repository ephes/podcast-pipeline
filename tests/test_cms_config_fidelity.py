"""CMS host, chapter and contributor configuration is refused or coalesced before any CMS request."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import typer
import yaml

from podcast_pipeline.cms_api import DjangoCastClient
from podcast_pipeline.cms_draft import CmsConfigError, build_cms_draft_plan, load_cms_config
from podcast_pipeline.entrypoints import cms_draft as cms_entry
from tests.test_cms_draft import FakeCast, _all_endpoints, _config, _run, _set_episode_yaml, _workspace


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeCast:
    server = FakeCast()

    def make_client(base_url: str, token: str) -> DjangoCastClient:
        return DjangoCastClient(base_url, token, transport=server.transport())

    monkeypatch.setattr(cms_entry, "DjangoCastClient", make_client)
    return server


def _load(workspace: Path) -> dict[str, Any]:
    data = yaml.safe_load((workspace / "episode.yaml").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _plan(workspace: Path) -> Any:
    episode_yaml = _load(workspace)
    return build_cms_draft_plan(episode_yaml=episode_yaml, workspace=workspace, config=load_cms_config(episode_yaml))


# Host names that collide after normalization


@pytest.mark.parametrize("where", ["global", "episode"])
def test_host_links_whose_keys_strip_to_one_name_are_refused_before_any_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeCast, where: str
) -> None:
    workspace = _workspace(tmp_path)
    # The YAML config is written with sorted keys, so " Speaker A " is read first.
    links = {"Speaker A": "https://one.example/@a", " Speaker A ": "https://two.example/@a"}
    if where == "global":
        _config(tmp_path, monkeypatch, host_links=links)
    else:
        _config(tmp_path, monkeypatch)
        _set_episode_yaml(workspace, cms={"host_links": links})

    with pytest.raises(
        typer.BadParameter,
        match=r"^cms\.host_links in .* has keys ' Speaker A ' and 'Speaker A' that both name host 'Speaker A'",
    ):
        _run(workspace)
    assert fake.requests == []


@pytest.mark.parametrize("where", ["global", "episode"])
def test_contributors_whose_keys_strip_to_one_name_are_refused_before_any_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeCast, where: str
) -> None:
    workspace = _workspace(tmp_path)
    contributors = {"Speaker A": "speaker-a", "Speaker A\t": {"slug": "other", "role": "guest"}}
    if where == "global":
        _config(tmp_path, monkeypatch, contributors=contributors)
    else:
        _config(tmp_path, monkeypatch)
        _set_episode_yaml(workspace, cms={"contributors": contributors})

    with pytest.raises(
        typer.BadParameter,
        match=r"^cms\.contributors in .* has keys 'Speaker A' and 'Speaker A\\t' that both name host 'Speaker A'",
    ):
        _run(workspace)
    assert fake.requests == []


def test_episode_entries_still_override_global_entries_of_the_same_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    _config(
        tmp_path,
        monkeypatch,
        host_links={"Speaker A": "https://global.example/@a"},
        contributors={"Speaker A": "global-a"},
    )
    _set_episode_yaml(
        workspace,
        hosts=["Speaker A"],
        cms={
            "host_links": {" Speaker A": "https://episode.example/@a"},
            "contributors": {"Speaker A ": {"slug": "episode-a", "role": "guest"}},
        },
    )
    _write_description(workspace, "Speaker A spricht.\n")

    plan = _plan(workspace)

    assert plan.overview_html == '<p><a href="https://episode.example/@a">Speaker A</a> spricht.</p>\n'
    assert plan.contributors_payload() == [{"slug": "episode-a", "role": "guest"}]


def _write_description(workspace: Path, text: str) -> None:
    (workspace / "copy" / "selected" / "description.md").write_text(text, encoding="utf-8")


# Chapter start aliases


def _chapters(workspace: Path, chapters: list[dict[str, Any]]) -> None:
    _set_episode_yaml(workspace, auphonic={"chapters": chapters})


@pytest.mark.parametrize(
    ("start", "start_sec", "expected"),
    [
        (0, 0, "00:00:00 Intro"),
        ("00:00:00", 0.0, "00:00:00 Intro"),
        ("01:02:03.5", 3723.5, "01:02:03.500000 Intro"),
        (90, "1:30", "00:01:30 Intro"),
        (1.0000004, 1.0000001, "00:00:01 Intro"),
    ],
)
def test_agreeing_chapter_start_aliases_emit_one_timestamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, start: object, start_sec: object, expected: str
) -> None:
    workspace = _workspace(tmp_path)
    _config(tmp_path, monkeypatch)
    _chapters(workspace, [{"start": start, "start_sec": start_sec, "title": "Intro"}])

    assert _plan(workspace).audio.chaptermarks == expected


@pytest.mark.parametrize(
    ("start", "start_sec"),
    [(0, 5), (5, 0), ("00:00:00", 0.000001), ("00:01:00", 61), (1.0000004, 1.0000006)],
)
def test_disagreeing_chapter_start_aliases_are_refused_while_planning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeCast, start: object, start_sec: object
) -> None:
    workspace = _workspace(tmp_path)
    _config(tmp_path, monkeypatch)
    _chapters(
        workspace,
        [{"start": 0, "title": "Intro"}, {"start": start, "start_sec": start_sec, "title": "Thema"}],
    )

    with pytest.raises(CmsConfigError, match=r"^auphonic\.chapters\[2\]\.start and \.start_sec disagree"):
        _plan(workspace)
    with pytest.raises(typer.BadParameter, match=r"auphonic\.chapters\[2\]\.start and \.start_sec disagree"):
        _run(workspace)
    assert fake.requests == []


@pytest.mark.parametrize(
    ("chapter", "key"),
    [
        ({"start": 3, "start_sec": "abc", "title": "Thema"}, "start_sec"),
        ({"start": None, "start_sec": 3, "title": "Thema"}, "start"),
        ({"start": 3, "start_sec": True, "title": "Thema"}, "start_sec"),
        ({"start_sec": -1, "title": "Thema"}, "start_sec"),
    ],
)
def test_invalid_chapter_start_alias_names_the_chapter_and_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, chapter: dict[str, Any], key: str
) -> None:
    workspace = _workspace(tmp_path)
    _config(tmp_path, monkeypatch)
    _chapters(workspace, [{"start": 0, "title": "Intro"}, chapter])

    with pytest.raises(CmsConfigError, match=rf"^auphonic\.chapters\[2\]\.{key} must be seconds or HH:MM:SS$"):
        _plan(workspace)


def test_start_sec_alone_still_sets_the_chapter_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = _workspace(tmp_path)
    _config(tmp_path, monkeypatch)
    _chapters(workspace, [{"start_sec": 0, "title": "Intro"}, {"start_sec": 12.5, "title": "Thema"}])

    assert _plan(workspace).audio.chaptermarks == "00:00:00 Intro\n00:00:12.500000 Thema"


# Contributor assignments deduplicated by slug and role


def test_host_aliases_of_one_contributor_and_role_are_sent_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: FakeCast, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = _workspace(tmp_path)
    _config(
        tmp_path,
        monkeypatch,
        host_links={},
        contributors={
            "Speaker A": "speaker-a",
            "Spk A": {"slug": "speaker-a", "role": "host"},
            "Speaker A (Gast)": {"slug": "speaker-a", "role": "guest"},
            "Speaker B": "speaker-b",
        },
    )
    _set_episode_yaml(workspace, hosts=["Speaker A", "Speaker B", "Spk A", "Speaker A (Gast)", "Speaker B"])
    _all_endpoints(fake)

    plan = _plan(workspace)
    assert plan.contributors_payload() == [
        {"slug": "speaker-a", "role": "host"},
        {"slug": "speaker-b", "role": "host"},
        {"slug": "speaker-a", "role": "guest"},
    ]
    assert plan.unmapped_hosts == ()

    _run(workspace)

    (patch,) = [
        json.loads(request.content)
        for request in fake.calls("PATCH", "/api/editor/episodes/141/")
        if "contributors" in json.loads(request.content)
    ]
    assert patch["contributors"] == plan.contributors_payload()
    assert fake.episodes[141]["contributors"] == plan.contributors_payload()
    assert len(fake.calls("POST", "/api/editor/episodes/141/transcript/")) == 1
    assert "No manual steps remain." in capsys.readouterr().out


def test_dry_run_lists_each_contributor_assignment_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = _workspace(tmp_path)
    _config(tmp_path, monkeypatch, contributors={"Speaker A": "speaker-a", "Spk A": "speaker-a"})
    _set_episode_yaml(workspace, hosts=["Spk A", "Speaker A"])

    cms_entry.run_cms_draft(workspace=workspace, dry_run=True)

    assert json.loads(capsys.readouterr().out)["contributors"] == [{"slug": "speaker-a", "role": "host"}]
